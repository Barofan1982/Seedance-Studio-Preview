import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch, Mock

from billing_cny import DEFAULT_CNY_RATES, billing_details, estimate_cny, pricing_snapshot, relay_usage_rows
from history_store import HistoryStore
from model_settings_view import ModelPricesDialog
from seedance_client import SeedanceClient, SeedanceError
from tests import test_studio_workflow as workflow


def row(model='seedance-2.0-vip', resolution='720p', video=True, tokens=240587):
    return {'task_id':'test-price', 'status':'succeeded', 'model':model, 'resolution':resolution,
            'actual_duration':13, 'completion_tokens':tokens, 'total_tokens':tokens * 2,
            'request_json':json.dumps({'model':model, 'resolution':resolution,
                                       'content':[{'type':'video_url'}] if video else []})}


class ModelBillingTests(unittest.TestCase):
    def test_model_tiers_conversion_and_actual_tokens_without_double_counting(self):
        cases = [('seedance-2.5', '1080p', True, 46), ('seedance-2.5', '1080p', False, 77),
                 ('seedance-2.0-vip', '4k', True, 16), ('seedance-2.0-fast-vip', '720p', False, 37),
                 ('seedance-2.0-mini', '720p', True, 14)]
        for model, resolution, video, rate in cases:
            with self.subTest(model=model, resolution=resolution, video=video):
                d = billing_details(row(model, resolution, video), DEFAULT_CNY_RATES)
                self.assertEqual(d['amount'], Decimal(240587) * rate / 1_000_000)
                self.assertEqual(d['usd'], d['amount'] / 7)
                self.assertEqual(d['per_second'], d['amount'] / 13)
                self.assertEqual(d['source'], '官方参考价')

    def test_1080_does_not_inherit_480_contract_and_explicit_blank_stays_unpriced(self):
        r = row('seedance-2.5-1080p-with-video', '1080p', tokens=1_000_000)
        self.assertEqual(estimate_cny(r, DEFAULT_CNY_RATES), Decimal(46))
        custom = {**DEFAULT_CNY_RATES, 'models':{'seedance-2.5-auto':{'1080p':{'with_video':33}}}}
        self.assertEqual(estimate_cny(r, custom), Decimal(33))
        custom['models']['seedance-2.5-auto']['1080p'] = {}
        self.assertIsNone(estimate_cny(r, custom))

    def test_unknown_input_model_and_missing_tokens_never_guessed(self):
        for r in [row('private-unknown'), {**row(), 'request_json':'{}'}, {**row(), 'completion_tokens':None},
                  {**row(), 'completion_tokens':True}, {**row(), 'status':'failed'}]:
            self.assertIsNone(estimate_cny(r, DEFAULT_CNY_RATES))
        zero = {**DEFAULT_CNY_RATES, 'models':{'seedance-2.0-vip':{'720p':{'with_video':0}}}}
        self.assertEqual(estimate_cny(row(), zero), Decimal(0))

    def test_history_keeps_selected_and_returned_model_and_submission_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / 'history.sqlite3')
            payload = {'model':'seedance-2.0-vip', 'resolution':'720p', 'ratio':'9:16',
                       'content':[{'type':'video_url'}], 'duration':13}
            history.add('test-price', payload, billing_rates=pricing_snapshot(DEFAULT_CNY_RATES))
            history.update({'id':'test-price','status':'succeeded','model':'doubao-seedance-2-0-260128',
                            'resolution':'1080p','duration':13,'usage':{'completion_tokens':240587,'total_tokens':481174}})
            saved = history.rows()[0]
            self.assertEqual(saved['model'], 'seedance-2.0-vip')
            self.assertEqual(saved['actual_model'], 'doubao-seedance-2-0-260128')
            self.assertEqual(saved['resolution'], '720p')
            self.assertEqual(saved['actual_resolution'], '1080p')
            changed = {'models': {'seedance-2.0-vip':{'1080p':{'with_video':99}}}}
            with patch('billing_cny.OFFICIAL_PRICES', {}):
                d = billing_details(saved, changed)
            self.assertEqual(d['amount'], Decimal('7.458197'))  # 240587 * 31 / 1M, not 99.
            self.assertTrue(d['snapshot'])
            history.update({'id':'test-price','status':'succeeded','usage':{'completion_tokens':True,'total_tokens':-1}})
            self.assertEqual(history.rows()[0]['completion_tokens'], 240587)
            self.assertEqual(history.rows()[0]['total_tokens'], 481174)

    def test_aggregate_receipts_use_actual_charge_not_nominal_cost_or_forecast(self):
        rows = relay_usage_rows({'model_stats':[
            {'model':'seedance-2.0-vip','requests':3,'output_tokens':100000,'cost':8,'actual_cost':0.4},
            {'model':'empty','requests':1,'output_tokens':0,'actual_cost':0},
            {'model':'missing','requests':1,'output_tokens':100000,'cost':9},
            {'model':'invalid','actual_cost':True}]})
        self.assertEqual(rows[0]['cny'], Decimal('2.8'))
        self.assertEqual(rows[0]['effective_rate_usd'], Decimal(4))
        self.assertEqual(rows[1]['usd'], Decimal(0))
        self.assertIsNone(rows[1]['effective_rate_usd'])
        self.assertIsNone(rows[2]['usd'])
        self.assertIsNone(rows[3]['usd'])

    def test_usage_is_read_only_same_origin_and_requires_model_receipts(self):
        client = SeedanceClient('https://relay.example/api/v3', 'dummy')
        with patch.object(client, '_request', return_value={'model_stats':[]}) as request:
            client.get_usage()
            request.assert_called_once_with('GET', '/v1/usage?days=90&timezone=UTC', origin_path=True)
        with patch.object(client, '_request', return_value={}):
            with self.assertRaises(SeedanceError):
                client.get_usage()


class BillingWorkflowTests(unittest.TestCase):
    setUpClass = classmethod(workflow.StudioWorkflowTests.setUpClass.__func__)
    setUp = workflow.StudioWorkflowTests.setUp
    tearDown = workflow.StudioWorkflowTests.tearDown
    new_controller = workflow.StudioWorkflowTests.new_controller
    pump_until = workflow.StudioWorkflowTests.pump_until
    make_window = workflow.StudioWorkflowTests.make_window

    def test_browsing_prices_does_not_disable_reference_rates(self):
        c = self.controller
        c.set_value('generation_model', 'seedance-2.0-vip')
        dialog = ModelPricesDialog(c)
        dialog.resolution.setCurrentIndex(dialog.resolution.findData('1080p'))
        dialog.save()
        self.assertNotIn('seedance-2.0-vip', c.state['cny_rates']['models'])
        self.assertIn('$7.29', dialog.reference.text())
        self.assertEqual(estimate_cny(row(resolution='1080p'), c.state['cny_rates']), Decimal('7.458197'))
        dialog.deleteLater()

    def test_usage_ui_receipts_account_scope_and_queue_snapshot(self):
        c = self.controller
        window = self.make_window()
        self.assertIn('¥70.00 / $10.00', window.price_hint.text())
        client = Mock(base_url=c.state['base_url'])
        client.get_usage.return_value = {'model_stats':[{'model':'seedance-2.0-vip','requests':1,
                                                       'output_tokens':100000,'actual_cost':0.4}]}
        with patch.object(c, 'seedance_client', return_value=client):
            c.refresh_billing_usage()
            self.pump_until(lambda: not c.query_busy)
        self.assertEqual(window.billing_panel.cloud.item(0,4).text(), '¥2.80')
        snap = c.generation_snapshot()
        c.state['cny_rates']['without_video'] = 99
        self.assertEqual(snap['billing_rates']['without_video'], 70)
        c.api_key = 'changed-key'
        window.refresh_usage()
        self.assertEqual(window.billing_panel.cloud.rowCount(), 0)
