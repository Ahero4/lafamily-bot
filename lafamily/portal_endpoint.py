"""HTTPS-запросы бота к отдельным интеграциям портала."""

import os
from urllib.parse import urlsplit

import aiohttp

from lafamily.errors import UserError


class PortalEndpoint:
    def __init__(self, guild_id, endpoint, configuration_error):
        self.guild_id = guild_id
        self.endpoint = endpoint
        self.configuration_error = configuration_error

    def credentials(self):
        url = urlsplit(os.getenv('FAMILY_BOT_SYNC_URL', '').strip())
        token = os.getenv('FAMILY_BOT_SYNC_TOKEN', '').strip()
        if (url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment
                or not token or os.getenv('FAMILY_BOT_GUILD_ID') != str(self.guild_id)):
            raise UserError(self.configuration_error)
        return f'{url.scheme}://{url.netloc}', token

    async def request(self, action, **fields):
        origin, token = self.credentials()
        headers = {'Origin': origin, 'Authorization': 'Bearer ' + token}
        payload = dict(action=action, guildId=str(self.guild_id), **fields)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
                async with session.post(f'{origin}/api/integrations/family-bot/{self.endpoint}', headers=headers,
                                        json=payload, allow_redirects=False) as response:
                    if response.status in (403, 404, 409):
                        data = await response.json()
                        reason = data.get('error') if isinstance(data, dict) else None
                        raise UserError(reason[:300] if isinstance(reason, str) and reason else 'Запись недоступна.')
                    if response.status != 200:
                        raise UserError('Портал временно недоступен. Попробуйте позже.')
                    data = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            raise UserError('Не удалось связаться с порталом. Попробуйте ещё раз.') from error
        if not isinstance(data, dict):
            raise UserError('Не удалось прочитать ответ портала.')
        return data
