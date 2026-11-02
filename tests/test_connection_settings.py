"""Gateway v1 routing and safe migration of only the old built-in address."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from seedance_client import SeedanceClient, DEFAULT_BASE_URL, DEFAULT_MODEL_CATALOG_URL, LEGACY_BASE_URL
from studio_controller import StudioController
from tests import test_client
from tests.test_client import FakeHandler

class V1RoutingTests(unittest.TestCase):
    setUpClass=classmethod(test_client.ClientTests.setUpClass.__func__)
    tearDownClass=classmethod(test_client.ClientTests.tearDownClass.__func__)
    def setUp(self):
        FakeHandler.requests.clear()
        FakeHandler.force_no_account=False
    def test_v1_submission_and_query_paths(self):
        client=SeedanceClient(f"http://127.0.0.1:{self.server.server_port}/v1",'dummy',allow_http=True)
        self.assertEqual(client.create_task({'model':'seedance-2.5','content':[]}), 'cgt-demo')
        client.get_task('cgt-demo')
        self.assertEqual([r[1] for r in FakeHandler.requests],['/v1/contents/generations/tasks','/v1/contents/generations/tasks/cgt-demo'])

class ConnectionSettingsTests(unittest.TestCase):
    def test_settings_ui_saves_both_addresses_and_preserves_chinese_prompt(self):
        from PySide6.QtWidgets import QApplication
        from app import SettingsDialog
        application=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            with patch('studio_controller.load_key',return_value=''),patch('studio_controller.load_openai_key',return_value=''):
                c=StudioController(Path(folder))
            c.state['prompt']='保留原有中文提示词'
            dialog=SettingsDialog(c)
            self.assertEqual(dialog.address.text(),DEFAULT_BASE_URL)
            self.assertEqual(dialog.catalog_address.text(),DEFAULT_MODEL_CATALOG_URL)
            dialog.save()
            stored=json.loads(c.state_file.read_bytes().decode('utf-8'))
            self.assertEqual(stored['base_url'],DEFAULT_BASE_URL)
            self.assertEqual(stored['model_catalog_url'],DEFAULT_MODEL_CATALOG_URL)
            self.assertEqual(stored['prompt'],'保留原有中文提示词')
            dialog.deleteLater();c.close();application.processEvents()

    def test_catalog_uses_exact_configured_route(self):
        c=SeedanceClient(DEFAULT_BASE_URL,'dummy',model_catalog_url=DEFAULT_MODEL_CATALOG_URL)
        with patch.object(c,'_request',return_value={'data':[{'id':'seedance-2.5'}]}) as request:
            self.assertEqual(c.list_models(),['seedance-2.5'])
        request.assert_called_once_with('GET','/v1/models',origin_path=True)

    def test_catalog_cannot_send_key_to_another_site(self):
        with self.assertRaises(ValueError):
            SeedanceClient(DEFAULT_BASE_URL,'dummy',model_catalog_url='https://another.invalid/v1/models')

    def test_legacy_migration_preserves_draft_and_other_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder)
            original={'base_url':LEGACY_BASE_URL,'allow_http_for':LEGACY_BASE_URL,'prompt':'保留角色与中文提示词','duration':'15','ratio':'16:9'}
            (directory/'state.json').write_text(json.dumps(original,ensure_ascii=False),encoding='utf-8')
            with patch('studio_controller.load_key',return_value=''),patch('studio_controller.load_openai_key',return_value=''):
                c=StudioController(directory)
            self.assertEqual(c.state['base_url'],DEFAULT_BASE_URL)
            self.assertEqual(c.state['model_catalog_url'],DEFAULT_MODEL_CATALOG_URL)
            self.assertEqual(c.state['allow_http_for'],'')
            self.assertEqual({k:c.state[k] for k in ('prompt','duration','ratio')},{k:original[k] for k in ('prompt','duration','ratio')})
            c.close()

    def test_custom_gateway_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder)
            (directory/'state.json').write_text(json.dumps({'base_url':'https://custom.invalid/api/v3'}),encoding='utf-8')
            with patch('studio_controller.load_key',return_value=''),patch('studio_controller.load_openai_key',return_value=''):
                c=StudioController(directory)
            self.assertEqual(c.state['base_url'],'https://custom.invalid/api/v3')
            self.assertEqual(c.state['model_catalog_url'],'')
            c.close()
