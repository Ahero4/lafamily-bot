import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import discord
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.onboarding import validate_identity, EntryView, ReviewView, OnboardingService, IdentityModal

class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    def test_identity_validation(self):
        self.assertEqual(validate_identity(' Sin   Bertoni ', '15096')[2], 'Sin Bertoni | 15096')
        for name, static in [('Sin', '1'), ('@everyone Test', '1'), ('Sin Bertoni', '0'), ('Sin Bertoni', 'abc')]:
            with self.assertRaises(UserError): validate_identity(name, static)

    async def test_persistent_buttons_and_saved_request(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        service = OnboardingService(store)
        for view in (EntryView(service), ReviewView(service)):
            self.addCleanup(view.stop)
            self.assertTrue(view.is_persistent())
        store.configure_onboarding('1','2','3','4')
        self.assertEqual(store.onboarding_settings('1')['role_id'], '4')
        store.save_onboarding_request('5','1','6','4')
        store.approve_onboarding('5','7')
        self.assertEqual(store.onboarding_request('5')['approved'], 1)

    async def test_approved_request_cannot_be_approved_again(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        store.save_onboarding_request('5','1','6','4')
        store.approve_onboarding('5','7')
        service = OnboardingService(store)
        message = SimpleNamespace(id=5)
        modal_message = SimpleNamespace(embeds=[service.embed('Sin Bertoni','15096',6)], id=5)
        modal = IdentityModal(service, store.onboarding_request('5'), modal_message)
        self.addCleanup(modal.stop)
        modal.name._value='Sin Bertoni'
        modal.static._value='15096'
        interaction = SimpleNamespace(guild_id=1,user=SimpleNamespace(id=7),permissions=discord.Permissions(administrator=True),
            guild=SimpleNamespace(fetch_member=AsyncMock()),response=SimpleNamespace(defer=AsyncMock()))
        with self.assertRaises(UserError): await modal.on_submit(interaction)
        interaction.guild.fetch_member.assert_not_awaited()


    async def test_rejection_is_final_and_notification_has_no_mentions(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        store.save_onboarding_request('5','1','6','4')
        store.reject_onboarding('5','7','Неверный Static ID')
        self.assertEqual(store.onboarding_request('5')['approved'], -1)
        with self.assertRaises(UserError): store.reject_onboarding('5','7','Повтор')
        member = SimpleNamespace(send=AsyncMock())
        self.assertTrue(await OnboardingService.notify(member, 'Отклонено'))
        self.assertEqual(member.send.call_args.kwargs['allowed_mentions'].to_dict(), {'parse': []})
        view = ReviewView(OnboardingService(store))
        self.addCleanup(view.stop)
        self.assertTrue(any(item.label == 'Отклонить заявку' for item in view.children))

    async def test_closed_direct_messages_are_reported(self):
        response = SimpleNamespace(status=403, reason='Forbidden')
        member = SimpleNamespace(send=AsyncMock(side_effect=discord.Forbidden(response, 'DM closed')))
        self.assertFalse(await OnboardingService.notify(member, 'Отклонено'))
