import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from lafamily.bot import FamilyBot
from lafamily.config import Config
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.models import Draft
from lafamily.modules.events.time import timestamp

G='111111111111111111';U='222222222222222222';V='333333333333333333';C='444444444444444444'


class ImprovementCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        self.bot=FamilyBot(Config('fake',int(V),int(G),Path(':memory:')),self.store)
        self.addAsyncCleanup(self.bot.close)

    def command(self,path):
        parts=path.split();command=self.bot.tree.get_command(parts[0],guild=discord.Object(id=int(G)))
        for name in parts[1:]:command=command.get_command(name)
        return command

    def interaction(self):
        return SimpleNamespace(id=123,guild_id=int(G),guild=SimpleNamespace(),user=SimpleNamespace(id=int(U),roles=[]),
            permissions=discord.Permissions.none(),response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())

    async def test_admin_and_organizer_commands_recheck_access(self):
        cases=[('баллы ведомости создать',(100,)),('баллы ведомости список',()),('баллы ведомости посмотреть',(1,)),
            ('баллы ведомости статус',(1,'paid')),('бот диагностика',()),('бот операции',()),
            ('бот восстановить-операцию',('report',1)),('мп запись создать',('Капт','2099-01-01 12:00')),
            ('мп запись закрыть',(1,)),('мп запись отчёт',(1,'1'))]
        for path,args in cases:
            with self.subTest(path=path),self.assertRaises(UserError):
                await self.command(path).callback(self.interaction(),*args)

    async def test_profile_hides_other_participants_subscriptions(self):
        self.store.set_price_subscription(G,V,'Медь',True)
        interaction=self.interaction()
        await self.command('участник профиль').callback(interaction,SimpleNamespace(id=int(V)))
        text=interaction.edit_original_response.call_args.kwargs['content']
        self.assertNotIn('Медь',text);self.assertNotIn('подписки',text)
        self.store.set_price_subscription(G,U,'Капуста',True)
        await self.command('участник профиль').callback(interaction)
        self.assertIn('Капуста',interaction.edit_original_response.call_args.kwargs['content'])

    async def test_history_handles_maximum_names_and_cancellation_reasons(self):
        kind=self.store.set_type(G,U,'*'*80,10)
        for index in range(5):
            saved=self.store.create_report(Draft(G,U,C,kind['id'],kind['name'],10,timestamp(),members=[U],request_id=str(index)))
            self.store.cancel_report(G,U,saved['id'],'*'*300)
        interaction=self.interaction()
        await self.command('баллы история').callback(interaction)
        payload=interaction.edit_original_response.call_args.kwargs
        self.assertLessEqual(len(payload['content']),2000)
        self.assertEqual(payload['allowed_mentions'].to_dict(),{'parse':[]})

    async def test_private_check_is_user_initiated_rate_limited_and_preserves_subscriptions(self):
        interaction=self.interaction();interaction.user.send=AsyncMock()
        self.store.set_price_subscription(G,U,'Медь',True)
        await self.command('скупщик проверить-лс').callback(interaction)
        interaction.user.send.assert_awaited_once()
        with self.assertRaises(UserError):await self.command('скупщик проверить-лс').callback(interaction)
        self.assertEqual(self.store.price_subscriptions(G,U),['Медь'])


if __name__=='__main__':unittest.main()
