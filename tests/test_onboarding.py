import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import discord
from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.onboarding import validate_identity, EntryView, ReviewView, OnboardingService, IdentityModal, RejectModal, RecoveryView, register_onboarding

class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    def reviewer_case(self, roles=(), admin=False):
        store = Store(':memory:')
        self.addCleanup(store.close)
        store.configure('1', '7', '8', ['10', '11'], ['12'])
        store.save_onboarding_request('5', '1', '6', '4', 'Sin Bertoni', '15096', '3')
        service = OnboardingService(store)
        role = MagicMock(spec=discord.Role)
        role.is_default.return_value = False
        role.managed = False
        role.permissions = discord.Permissions.none()
        role.__ge__.return_value = False
        member = SimpleNamespace(id=6, top_role=1, edit=AsyncMock(), add_roles=AsyncMock(), send=AsyncMock())
        message = SimpleNamespace(id=5, embeds=[service.embed('Sin Bertoni', '15096', 6)], edit=AsyncMock())
        interaction = SimpleNamespace(guild_id=1, message=message,
            user=SimpleNamespace(id=7, display_name='Reviewer', roles=[SimpleNamespace(id=r) for r in roles]),
            permissions=discord.Permissions(administrator=admin),
            guild=SimpleNamespace(owner_id=99, name='Family', fetch_member=AsyncMock(return_value=member),
                get_role=lambda rid: role, me=SimpleNamespace(top_role=10,
                    guild_permissions=discord.Permissions(manage_roles=True, manage_nicknames=True))),
            response=SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock()), edit_original_response=AsyncMock())
        return store, service, member, interaction

    async def test_configured_reviewers_can_approve_and_repair_without_reissuing_role(self):
        for roles, admin in [([10], False), ([11], False), ([12], False), ([], True)]:
            with self.subTest(roles=roles, admin=admin):
                store, service, member, interaction = self.reviewer_case(roles, admin)
                view = ReviewView(service)
                self.addCleanup(view.stop)
                await view.review.callback(interaction)
                modal = interaction.response.send_modal.call_args.args[0]
                self.addCleanup(modal.stop)
                modal.name._value, modal.static._value = 'Sin Bertoni', '15096'
                await modal.on_submit(interaction)
                self.assertEqual(store.onboarding_request('5')['actor_id'], '7')
                self.assertEqual(store.onboarding_request('5')['approved'], 1)
                member.edit.assert_awaited_once()
                member.add_roles.assert_awaited_once()
                self.assertEqual(interaction.message.edit.call_args.kwargs['embed'].footer.text, 'Утвердил: Reviewer')
                repair = RecoveryView(service)
                self.addCleanup(repair.stop)
                await repair.repair.callback(interaction)
                member.add_roles.assert_awaited_once()
                member.send.assert_awaited_once()

    async def test_organizer_can_reject_with_reason(self):
        store, service, member, interaction = self.reviewer_case([10])
        view = ReviewView(service)
        self.addCleanup(view.stop)
        await view.reject.callback(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        self.addCleanup(modal.stop)
        modal.reason._value = 'Неверный Static ID'
        await modal.on_submit(interaction)
        self.assertEqual(store.onboarding_request('5')['approved'], -1)
        self.assertEqual(store.onboarding_request('5')['reason'], 'Неверный Static ID')
        member.add_roles.assert_not_awaited()
        member.send.assert_awaited_once()

    async def test_unconfigured_members_and_revoked_roles_cannot_review(self):
        for revoke in ('member', 'user_role', 'configured_role'):
            with self.subTest(revoke=revoke):
                store, service, member, interaction = self.reviewer_case([10])
                view, repair = ReviewView(service), RecoveryView(service)
                self.addCleanup(view.stop)
                self.addCleanup(repair.stop)
                await view.review.callback(interaction)
                approve = interaction.response.send_modal.call_args.args[0]
                self.addCleanup(approve.stop)
                approve.name._value, approve.static._value = 'Sin Bertoni', '15096'
                await view.reject.callback(interaction)
                reject = interaction.response.send_modal.call_args.args[0]
                self.addCleanup(reject.stop)
                reject.reason._value = 'Отказ'
                if revoke == 'configured_role':
                    store.configure('1', '7', '8', ['11'], ['12'])
                else:
                    interaction.user.roles = [] if revoke == 'user_role' else [SimpleNamespace(id=999)]
                for action in (view.review.callback, view.reject.callback, repair.repair.callback,
                               approve.on_submit, reject.on_submit):
                    with self.assertRaises(UserError):
                        await action(interaction)
                self.assertEqual(store.onboarding_request('5')['approved'], 0)
                member.add_roles.assert_not_awaited()
                member.edit.assert_not_awaited()
                member.send.assert_not_awaited()

    async def test_organizer_cannot_change_onboarding_settings(self):
        store, service, member, interaction = self.reviewer_case([10])
        client = discord.Client(intents=discord.Intents.none())
        tree = discord.app_commands.CommandTree(client)
        register_onboarding(tree, service, 1)
        command = tree.get_command('заявки', guild=discord.Object(id=1)).get_command('настройки')
        with self.assertRaises(UserError):
            await command.callback(interaction, None, None, None)
        self.assertIsNone(store.onboarding_settings('1'))

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
