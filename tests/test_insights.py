import unittest
from dataclasses import replace
from pathlib import Path

from lafamily.database import Store
from lafamily.errors import UserError
from lafamily.modules.events.models import Draft
from lafamily.modules.events.time import week_range, timestamp
from lafamily.modules.prices.parser import parse_prices

G='111111111111111111'; U='222222222222222222'; V='333333333333333333'


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:'); self.addCleanup(self.store.close)
        kind=self.store.set_type(G,U,'Капт',10,win_bonus=5,mvp_bonus=3)
        self.draft=Draft(G,U,'444444444444444444',kind['id'],'Капт',10,timestamp(),members=[U,V],
                         outcome='win',win_bonus=5,mvp_id=U,mvp_bonus=3)

    def test_history_profile_and_cancelled_points(self):
        saved=self.store.create_report(self.draft)
        history=self.store.points_history(G,U)
        self.assertEqual((history[0]['base_points'],history[0]['win_bonus'],history[0]['mvp_points']),(10,5,3))
        self.assertEqual(self.store.participant_profile(G,U,week_range())['total'],18)
        self.store.cancel_report(G,V,saved['id'],'Тест')
        self.assertEqual(self.store.participant_profile(G,U,week_range())['total'],0)
        self.assertEqual(self.store.points_history(G,U)[0]['cancelled_by'],V)
        self.assertEqual(self.store.points_history('other',U),[])

    def test_duplicate_requires_ack_and_changes_invalidate_it(self):
        self.store.create_report(self.draft)
        other=replace(self.draft,request_id='different',enforce_duplicates=True)
        with self.assertRaises(UserError):self.store.create_report(other)
        key,matches=self.store.duplicate_report_key(other)
        self.assertEqual(len(matches),1)
        other.duplicate_ack=key
        other.members=[V]
        with self.assertRaises(UserError):self.store.create_report(other)
        other.duplicate_ack=self.store.duplicate_report_key(other)[0]
        # MVP must still belong to the new roster.
        other.mvp_id=None
        other.duplicate_ack=self.store.duplicate_report_key(other)[0]
        self.assertNotEqual(self.store.create_report(other)['id'],1)

    def test_payout_snapshot_survives_cancellation_and_state_is_guarded(self):
        report=self.store.create_report(self.draft)
        sheet=self.store.create_payout_sheet(G,U,'request',week_range(),101)
        self.assertEqual(sum(r['amount'] for r in sheet['rows']),101)
        self.assertEqual(self.store.create_payout_sheet(G,U,'request',week_range(),500)['id'],sheet['id'])
        with self.assertRaises(UserError):self.store.transition_payout(G,U,sheet['id'],'paid')
        self.store.transition_payout(G,U,sheet['id'],'approved')
        self.store.cancel_report(G,U,report['id'],'Тест')
        self.assertEqual(self.store.payout_sheet(G,sheet['id'])['rows'],sheet['rows'])
        self.store.transition_payout(G,U,sheet['id'],'paid')
        with self.assertRaises(UserError):self.store.transition_payout(G,U,sheet['id'],'approved')
        with self.assertRaises(UserError):self.store.payout_sheet('other',sheet['id'])

    def test_second_approved_sheet_for_same_week_is_rejected(self):
        self.store.create_report(self.draft)
        a=self.store.create_payout_sheet(G,U,'1',week_range(),100)
        b=self.store.create_payout_sheet(G,U,'2',week_range(),100)
        self.store.transition_payout(G,U,a['id'],'approved')
        with self.assertRaises(UserError):self.store.transition_payout(G,U,b['id'],'approved')

    def test_price_snapshots_are_idempotent_and_isolated(self):
        report=parse_prices(Path(__file__).with_name('fixtures').joinpath('prices_example.txt').read_text(encoding='utf-8-sig'))
        self.store.save_price_history(G,'1','rev',report)
        self.store.save_price_history(G,'1','rev',report)
        rows=self.store.price_history_data(G,'Медь','2000','2100')
        self.assertEqual(rows[0]['reports'],1)
        self.assertEqual(self.store.price_history_data('other','Медь','2000','2100'),[])

    def test_baseline_detects_removal_but_not_cancellation(self):
        report=self.store.create_report(self.draft)
        self.assertEqual(self.store.health_snapshot(G)['warnings'],[])
        self.store.cancel_report(G,U,report['id'],'Тест')
        self.assertEqual(self.store.health_snapshot(G)['warnings'],[])
        self.store.db.execute('DELETE FROM report_members')
        self.assertTrue(self.store.health_snapshot(G)['warnings'])

    def test_price_edit_and_revert_select_current_revision_without_duplicate_points(self):
        text=Path(__file__).with_name('fixtures').joinpath('prices_example.txt').read_text(encoding='utf-8-sig')
        original=parse_prices(text)
        changed=parse_prices(text.replace('- Медь: $36 ($43)','- Медь: $60 ($72)'))
        self.store.save_price_history(G,'100','a',original)
        self.store.save_price_history(G,'100','b',changed)
        self.assertEqual(self.store.price_history_data(G,'Медь','2000','2100')[0]['maximum'],60)
        self.store.save_price_history(G,'100','a',original)
        row=self.store.price_history_data(G,'Медь','2000','2100')[0]
        self.assertEqual(row['maximum'],36);self.assertEqual(row['reports'],1)


if __name__=='__main__':unittest.main()
