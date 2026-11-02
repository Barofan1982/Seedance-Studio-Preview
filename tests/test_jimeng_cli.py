import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import jimeng_cli
from billing_cny import billing_details
from history_store import HistoryStore


class JimengHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.history_path = Path(self.temp.name) / 'history.sqlite3'
        self.store = HistoryStore(self.history_path)
        self.args = SimpleNamespace(
            mode='text', prompt='小猫在窗台伸懒腰', model='seedance-2.5',
            duration=5, resolution='720p', ratio='16:9',
            image=[], video=[], audio=[], first_frame=None, last_frame=None,
        )
        self.state = {'cny_rates': {'without_video': 70.0, 'with_video': 42.0},
                      'generation_profile': ''}
        self.payload = {
            'model': 'seedance-2.5',
            'content': [{'type': 'text', 'text': '小猫在窗台伸懒腰'}],
            'duration': 5, 'resolution': '720p', 'ratio': '16:9',
        }

    def tearDown(self):
        self.temp.cleanup()

    def task(self, status='succeeded'):
        return {
            'id': 'cgt-cli-1', 'status': status, 'model': 'seedance-2.5', 'resolution': '720p',
            'duration': 5, 'usage': {'completion_tokens': 100000, 'total_tokens': 120000},
            'content': {'video_url': 'https://cdn.example/video.mp4'},
        }

    def test_submission_and_result_feed_local_usage(self):
        jimeng_cli._record_submission(self.store, 'cgt-cli-1', self.payload, self.args, self.state)
        row = self.store.get('cgt-cli-1')
        self.assertEqual(row['status'], 'submitted')
        self.assertEqual(row['prompt'], '小猫在窗台伸懒腰')
        self.assertTrue(row['billing_rates_json'])

        jimeng_cli._record_result(self.store, self.task(), self.args, self.state)
        row = self.store.get('cgt-cli-1')
        self.assertEqual(row['status'], 'succeeded')
        self.assertEqual(row['completion_tokens'], 100000)
        self.assertEqual(row['total_tokens'], 120000)
        self.assertEqual(row['actual_duration'], 5)
        self.assertEqual(row['video_url'], 'https://cdn.example/video.mp4')

        detail = billing_details(row, {})
        self.assertEqual(detail['amount'], Decimal('7'))
        self.assertEqual(detail['usd'], Decimal('1'))
        self.assertEqual(detail['kind'], 'without_video')
        self.assertEqual(detail['resolution'], '720p')
        self.assertEqual(detail['source'], '官方参考价')

    def test_failed_task_stays_in_history_without_usage(self):
        jimeng_cli._record_submission(self.store, 'cgt-cli-1', self.payload, self.args, self.state)
        task = self.task('failed')
        task['error'] = {'code': 'InvalidParameter', 'message': 'bad request'}
        task.pop('content', None)
        task.pop('usage', None)
        jimeng_cli._record_result(self.store, task, self.args, self.state)
        row = self.store.get('cgt-cli-1')
        self.assertEqual(row['status'], 'failed')
        self.assertIn('InvalidParameter', row['error'])
        self.assertIsNone(row['completion_tokens'])
        self.assertIsNone(billing_details(row, {})['amount'])

    def test_resume_creates_missing_record_then_updates_it(self):
        jimeng_cli._record_result(self.store, self.task(), self.args, self.state)
        row = self.store.get('cgt-cli-1')
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'succeeded')
        self.assertEqual(row['prompt'], '小猫在窗台伸懒腰')
        self.assertEqual(json.loads(row['request_json'])['resolution'], '720p')

    def test_history_can_be_disabled(self):
        args = SimpleNamespace(no_history=True, data_dir=None)
        self.assertIsNone(jimeng_cli._history(args))

    def test_data_dir_selects_history_file(self):
        args = SimpleNamespace(no_history=False, data_dir=self.temp.name)
        store = jimeng_cli._history(args)
        self.assertEqual(store.path, self.history_path)
        self.assertIsNone(jimeng_cli._history(SimpleNamespace(no_history=True, data_dir=self.temp.name)))


if __name__ == '__main__':
    unittest.main()
