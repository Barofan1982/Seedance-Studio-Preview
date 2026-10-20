import tempfile
import unittest
from pathlib import Path

from history_outcomes import record_outcome


class HistoryOutcomeTests(unittest.TestCase):
    def test_prompt_versions_do_not_claim_a_video_even_with_a_source_task(self):
        outcome = record_outcome({'record_kind': 'prompt', 'status': 'draft', 'task_id': 'source-task'})
        self.assertEqual(outcome.category, 'prompt')
        self.assertFalse(outcome.file_available)
        self.assertIn('非成片', outcome.title)

    def test_unconfirmed_and_rejected_submissions_remain_distinct(self):
        for status, category, title in [('unknown', 'uncertain', '提交未确认'),
                                         ('failed', 'failed', '提交失败')]:
            with self.subTest(status=status):
                outcome = record_outcome({'record_kind': 'submission', 'status': status, 'error': 'HTTP 503: no accounts'})
                self.assertEqual(outcome.category, category)
                self.assertIn(title, outcome.title)
                self.assertIn('HTTP 503', outcome.error)
        outcome = record_outcome({'record_kind': 'submission', 'status': 'succeeded', 'task_id': ''})
        self.assertEqual(outcome.category, 'uncertain')
        self.assertIn('不影响后续生成', outcome.description)
        self.assertIn('可能额外计费', outcome.description)

    def test_completed_tasks_require_an_existing_nonempty_file_to_show_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            record = {'record_kind': 'task', 'status': 'succeeded', 'local_path': str(path)}
            self.assertEqual(record_outcome(record).category, 'unavailable')
            path.touch()
            self.assertIn('空文件', record_outcome(record).description)
            path.write_bytes(b'output')
            outcome = record_outcome(record)
            self.assertTrue(outcome.file_available)
            self.assertEqual(outcome.category, 'saved')
            path.unlink()
            self.assertFalse(record_outcome(record).file_available)

    def test_generation_success_and_download_failure_are_separate(self):
        record = {'status': 'succeeded', 'video_url': 'https://example.com/video.mp4'}
        self.assertIn('待保存', record_outcome(record).title)
        record.update(download_status='running')
        self.assertEqual(record_outcome(record).category, 'progress')
        record.update(download_status='failed', download_error='磁盘空间不足')
        outcome = record_outcome(record)
        self.assertEqual(outcome.category, 'unavailable')
        self.assertIn('生成成功', outcome.title)
        self.assertIn('保存失败', outcome.title)
        self.assertEqual(outcome.error, '磁盘空间不足')
        self.assertIn('未返回文件', record_outcome({'status': 'succeeded'}).title)

    def test_query_errors_do_not_convert_running_tasks_into_generation_failures(self):
        record = {'status': 'running', 'task_id': 'task-1', 'query_status': 'failed', 'query_error': 'HTTP 502'}
        outcome = record_outcome(record)
        self.assertEqual(outcome.category, 'progress')
        self.assertIn('查询异常', outcome.title)
        self.assertIn('最后确认状态', outcome.description)
        self.assertEqual(outcome.error, 'HTTP 502')
        record.update(query_status='succeeded', query_error='')
        self.assertNotIn('查询异常', record_outcome(record).title)
        for status in ('queued', 'submitting'):
            self.assertEqual(record_outcome({'status': status}).category, 'progress')

    def test_terminal_and_unrecognized_states_show_their_actual_result(self):
        self.assertEqual(record_outcome({'status': 'failed', 'error': 'ContentRejected'}).category, 'failed')
        for status in ('cancelled', 'expired'):
            self.assertEqual(record_outcome({'status': status}).category, 'closed')
        self.assertEqual(record_outcome({'status': 'new-server-status'}).category, 'uncertain')


if __name__ == '__main__':
    unittest.main()
