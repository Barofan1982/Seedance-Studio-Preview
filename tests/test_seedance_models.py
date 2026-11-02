"""Model routing, capability boundaries, billing isolation and UI/history round trips."""
import json
import unittest
from decimal import Decimal
from copy import deepcopy
from unittest.mock import patch

from seedance_client import build_payload, SeedanceClient, SeedanceError
from seedance_models import DEFAULT_VIDEO_MODEL, MODEL_SPECS, RELAY_MODEL_PROFILES, model_spec, VISIBLE_VIDEO_MODELS
from studio_controller import collect_payload, generation_settings
from billing_cny import DEFAULT_CNY_RATES, estimate_cny
from model_settings_view import ModelPricesDialog
from tests import test_studio_workflow as workflow

V25 = 'doubao-seedance-2-5-260628'
V20 = 'doubao-seedance-2-0-260128'
FAST = 'doubao-seedance-2-0-fast-260128'
MINI = 'doubao-seedance-2-0-mini-260615'
V15 = 'doubao-seedance-1-5-pro-251215'
V10 = 'doubao-seedance-1-0-pro-250528'
V10FAST = 'doubao-seedance-1-0-pro-fast-251015'
VIDEO = 'https://example.com/ref.mp4'
IMAGE = 'https://example.com/ref.png'


def payload(**kwargs):
    return build_payload(**dict(dict(prompt='角色走入画面', duration=5, resolution='720p', ratio='16:9', mode='text'), **kwargs))


class ModelPayloadTests(unittest.TestCase):
    def test_new_relay_ids_preserve_exact_routing_and_family_constraints(self):
        for identity, profile in RELAY_MODEL_PROFILES.items():
            with self.subTest(identity=identity):
                spec = model_spec(identity)
                self.assertEqual(spec.id, profile)
                result = payload(model=identity, mode='reference', video_sources=[VIDEO])
                self.assertEqual(result['model'], identity)
                self.assertEqual('omni_reference_task_type' in result, identity == 'seedance-2.5')
                row = {'status':'succeeded', 'model':identity, 'completion_tokens':1_000_000,
                       'request_json':json.dumps(result)}
                expected = {'seedance-2.5':42, 'seedance-2.0-vip':28, 'seedance-2.0-fast-vip':22, 'seedance-2.0-mini':14}
                self.assertEqual(estimate_cny(row, DEFAULT_CNY_RATES), Decimal(expected[identity]))
        self.assertEqual(payload(model='seedance-2.0-vip', resolution='4k')['model'], 'seedance-2.0-vip')
        for identity in ('seedance-2.0-fast-vip', 'seedance-2.0-mini'):
            with self.assertRaises(ValueError): payload(model=identity, resolution='1080p')

    def test_every_builtin_has_distinct_routing_and_compatible_payload(self):
        for identity, spec in MODEL_SPECS.items():
            with self.subTest(identity=identity):
                p = payload(model=identity, generate_audio=spec.audio)
                expected = 'seedance-2.5-480p-720p-no-video' if identity == DEFAULT_VIDEO_MODEL else identity
                self.assertEqual(p['model'], expected)
                self.assertEqual('generate_audio' in p, spec.audio)
                self.assertEqual('output_format' in p, spec.task_type_field)
        p = payload(model=V20, resolution='4k', duration=15, mode='reference', video_sources=[VIDEO], image_sources=[IMAGE])
        self.assertEqual(p['model'], V20)
        self.assertNotIn('omni_reference_task_type', p)
        self.assertEqual([c.get('role') for c in p['content'][1:]], ['reference_image', 'reference_video'])

    def test_generation_boundaries_and_reference_counts(self):
        invalid = [dict(model=FAST, resolution='1080p'), dict(model=MINI, resolution='4k'),
                   dict(model=V25, resolution='4k'), dict(model=V20, duration=16), dict(model=V15, duration=13),
                   dict(model=V10, duration=-1, generate_audio=False), dict(model=V10, generate_audio=True),
                   dict(model=V20, output_format='mov'),
                   dict(model=V10FAST, mode='first_last_frame', first_frame=IMAGE, last_frame=IMAGE, generate_audio=False),
                   dict(model=V20, mode='reference', image_sources=[IMAGE]*10),
                   dict(model=V20, mode='reference', video_sources=[VIDEO]*4),
                   dict(model=V20, mode='reference', audio_sources=['https://example.com/a.mp3'])]
        for opts in invalid:
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                payload(**opts)
        self.assertEqual(payload(model=V10, duration=2, generate_audio=False, seed=11)['seed'], 11)
        self.assertEqual(payload(model=V15, duration=-1)['duration'], -1)
        self.assertEqual(payload(model=V20, mode='first_frame', first_frame=IMAGE)['ratio'], '16:9')

    def test_custom_model_requires_explicit_template_and_is_never_rewritten(self):
        with self.assertRaisesRegex(ValueError, '参数配置'):
            payload(model='my-seedance')
        result = payload(model='ep-private-video', model_profile=FAST, mode='reference', video_sources=[VIDEO])
        self.assertEqual(result['model'], 'ep-private-video')
        self.assertNotIn('omni_reference_task_type', result)
        with self.assertRaisesRegex(ValueError, '分辨率'):
            payload(model='ep-private-video', model_profile=FAST, resolution='1080p')
        with self.assertRaisesRegex(ValueError, '不匹配'):
            payload(model='seedance-2.5-1080p-with-video')

    def test_model_discovery_falls_back_only_on_404_without_changing_origin(self):
        client = SeedanceClient('https://example.com/api/v3', 'dummy')
        with patch.object(client, '_request', side_effect=[SeedanceError('missing', diagnostics={'http_status':404}),
                        {'data':[{'id':V20}, {'id':V20}, {'id':FAST}]}]) as request:
            self.assertEqual(client.list_models(), sorted([V20, FAST]))
            self.assertEqual(request.call_args_list[1].args, ('GET', '/v1/models'))
            self.assertTrue(request.call_args_list[1].kwargs['origin_path'])
        for status in (401, 429, 500):
            with patch.object(client, '_request', side_effect=SeedanceError('failed', diagnostics={'http_status':status})) as request:
                with self.assertRaises(SeedanceError): client.list_models()
                self.assertEqual(request.call_count, 1)

    def test_prices_are_isolated_by_actual_model_resolution_and_video_input(self):
        p = payload(model=V20, mode='reference', video_sources=[VIDEO])
        row = {'status':'succeeded', 'model':V20, 'completion_tokens':1_000_000, 'request_json':json.dumps(p)}
        self.assertEqual(estimate_cny(row, DEFAULT_CNY_RATES), Decimal(28))
        rates = {**DEFAULT_CNY_RATES, 'models':{V20:{'720p':{'with_video':28,'without_video':46}}}}
        self.assertEqual(estimate_cny(row, rates), Decimal(28))
        p['content'] = [{'type':'text','text':'走路'}]
        row['request_json'] = json.dumps(p)
        self.assertEqual(estimate_cny(row, rates), Decimal(46))
        p['resolution'] = '1080p'
        row['request_json'] = json.dumps(p)
        self.assertEqual(estimate_cny(row, rates), Decimal(51))
        p['model'] = FAST
        row.update(model=FAST, request_json=json.dumps(p))
        self.assertIsNone(estimate_cny(row, rates))
        row.update(model='seedance-2.5-1080p-with-video', request_json=json.dumps({'resolution':'1080p'}))
        rates['models'][DEFAULT_VIDEO_MODEL] = {'1080p':{}}
        self.assertIsNone(estimate_cny(row, rates))  # Explicit blank never falls back to 42.


class ModelWorkflowTests(unittest.TestCase):
    setUpClass = classmethod(workflow.StudioWorkflowTests.setUpClass.__func__)
    setUp = workflow.StudioWorkflowTests.setUp
    tearDown = workflow.StudioWorkflowTests.tearDown
    new_controller = workflow.StudioWorkflowTests.new_controller
    pump_until = workflow.StudioWorkflowTests.pump_until
    make_window = workflow.StudioWorkflowTests.make_window

    def test_refreshed_relay_catalog_is_selectable_persisted_and_priced_independently(self):
        c = self.controller
        window = self.make_window()
        client = unittest.mock.Mock()
        client.base_url = c.state['base_url']
        discovered = list(RELAY_MODEL_PROFILES) + ['seedance-2.5-480p-720p-no-video']
        client.list_models.return_value = discovered
        with patch.object(c, 'seedance_client', return_value=client):
            c.refresh_generation_models()
            self.pump_until(lambda: not c.busy)
        self.assertEqual(c.generation_model_choices(), list(VISIBLE_VIDEO_MODELS))
        self.assertEqual(window.video_model.count(), 4)
        self.assertEqual([window.video_model.itemText(i) for i in range(4)],
                         ['Seedance 2.5', 'Seedance 2.0 Mini', 'Seedance 2.0 Fast VIP', 'Seedance 2.0 VIP'])
        for identity in RELAY_MODEL_PROFILES:
            index = list(VISIBLE_VIDEO_MODELS).index(identity)
            window.video_model.setCurrentIndex(index)
            actual = collect_payload(c.state, [])['model']
            self.assertEqual(actual, 'seedance-2.5-480p-720p-no-video' if identity == 'seedance-2.5' else identity)
            self.assertIn('已列出', c.generation_model_availability())
        c.set_value('generation_model', 'seedance-2.0-vip')
        dialog = ModelPricesDialog(c, window)
        self.assertEqual(dialog.model.currentData(), 'seedance-2.0-vip')
        self.assertEqual(dialog.fields['with_video'].text(), '')
        self.assertIn('28.00', dialog.reference.text())
        dialog.fields['with_video'].setText('12.34')
        dialog.save()
        self.assertEqual(c.state['cny_rates']['models']['seedance-2.0-vip']['720p']['with_video'], 12.34)
        restarted = self.new_controller()
        try:
            self.assertEqual(restarted.generation_model_choices(), list(VISIBLE_VIDEO_MODELS))
            self.assertEqual(collect_payload(restarted.state, [])['model'], 'seedance-2.0-vip')
        finally:
            restarted.close()
        dialog.deleteLater()

    def test_picker_updates_limits_actual_preview_and_persists(self):
        window = self.make_window()
        c = self.controller
        c.state.update(duration='30', resolution='1080p', output_format='mov')
        window.video_model.setCurrentIndex(window.video_model.findData('seedance-2.0-fast-vip'))
        self.assertEqual(c.state['generation_model'], 'seedance-2.0-fast-vip')
        self.assertEqual(c.state['duration'], '5')
        self.assertEqual(c.state['resolution'], '720p')
        self.assertEqual(window.controls['resolution'].findData('1080p'), -1)
        self.assertEqual(window.controls['duration'].findData('16 秒'), -1)
        self.assertEqual(collect_payload(c.state, [])['model'], 'seedance-2.0-fast-vip')
        c.set_value('generation_model', V10)
        self.assertEqual(window.controls['duration'].findData('自适应'), -1)
        self.assertFalse(window.voice.isEnabled())
        window.busy(False)
        self.assertFalse(window.voice.isEnabled())
        c.save()
        restarted = self.new_controller()
        try:
            self.assertEqual(collect_payload(restarted.state, [])['model'], V10)
        finally:
            restarted.close()

    def test_history_queue_and_draft_restore_exact_model_and_custom_profile(self):
        c = self.controller
        c.add_generation_model('ep-custom', FAST)
        snap = c.generation_snapshot()
        c.history.add('demo-model', snap['payload'], mode='text', assets=[], template=snap['template'])
        queue_id = c.queue.add(snap)
        c.set_value('generation_model', DEFAULT_VIDEO_MODEL)
        c.reuse(c.row('demo-model'))
        self.assertEqual(collect_payload(c.state, [])['model'], 'ep-custom')
        self.assertEqual(c.state['generation_profile'], FAST)
        c.set_value('generation_model', V20)
        c.edit_queue_item(queue_id)
        self.assertEqual(collect_payload(c.state, [])['model'], 'ep-custom')
        c.reuse_prompt({'settings':snap['settings'], 'assets':[], 'original_prompt':'猫', 'frames':[],
                        'draft':{'prompt':'猫'}})
        self.assertEqual(c.state['generation_model'], 'ep-custom')
        c._replace_workspace(snap['state'], [])
        self.assertEqual(c.state['generation_profile'], FAST)
        c._replace_workspace({'prompt':'旧快照', 'mode':'纯文本'}, [])
        self.assertEqual(c.state['generation_model'], DEFAULT_VIDEO_MODEL)

    def test_refresh_catalog_does_not_auto_choose_or_claim_unlisted_model_works(self):
        c = self.controller
        c.set_value('generation_model', V20)
        client = unittest.mock.Mock()
        client.base_url = c.state['base_url']
        client.list_models.return_value = ['seedance-2.5-480p-720p-no-video']
        with patch.object(c, 'seedance_client', return_value=client):
            c.refresh_generation_models()
            self.pump_until(lambda: not c.busy)
        self.assertEqual(c.state['generation_model'], V20)
        self.assertIn('未列出', c.generation_model_availability())
        c.set_value('generation_model', DEFAULT_VIDEO_MODEL)
        self.assertIn('已列出', c.generation_model_availability())
        c.state['base_url'] = 'https://another.example/api/v3'
        self.assertIn('待确认', c.generation_model_availability())

    def test_price_dialog_keeps_official_reference_separate_and_saves_per_model(self):
        c = self.controller
        c.set_value('generation_model', 'seedance-2.0-vip')
        dialog = ModelPricesDialog(c)
        self.assertEqual(dialog.fields['without_video'].text(), '')
        self.assertIn('46.00', dialog.reference.text())
        dialog.fields['without_video'].setText('55')
        dialog.fields['with_video'].setText('33')
        dialog.save()
        self.assertEqual(c.state['cny_rates']['models']['seedance-2.0-vip']['720p'], {'without_video':55.0, 'with_video':33.0})
        self.assertEqual(c.state['cny_rates']['without_video'], 70)
        dialog.deleteLater()
