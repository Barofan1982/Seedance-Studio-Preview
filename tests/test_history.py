import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from history_store import HistoryStore
from seedance_client import build_payload
from request_records import compact_record


class HistoryTests(unittest.TestCase):
    def test_history_uses_latest_query_and_download_errors_without_changing_task_status(self):
        with tempfile.TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / 'history.sqlite3')
            payload = build_payload(prompt='小猫跑步', duration=4, resolution='480p', ratio='9:16', mode='text')
            history.add('task-1', payload)
            history.update({'id': 'task-1', 'status': 'running'})
            query = history.begin_call('query', {}, task_id='task-1')
            history.finish_call(query, error='HTTP 502')
            row = history.generation_records()[0]
            self.assertEqual(row['status'], 'running')
            self.assertEqual(row['query_error'], 'HTTP 502')
            query = history.begin_call('query', {}, task_id='task-1')
            history.finish_call(query, response={'id': 'task-1', 'status': 'succeeded'})
            history.update({'id': 'task-1', 'status': 'succeeded'})
            download = history.begin_call('download', {}, task_id='task-1')
            history.finish_call(download, error='磁盘空间不足')
            row = history.generation_records()[0]
            self.assertEqual(row['status'], 'succeeded')
            self.assertEqual(row['query_status'], 'succeeded')
            self.assertEqual(row['query_error'], '')
            self.assertEqual(row['download_error'], '磁盘空间不足')
            download = history.begin_call('download', {}, task_id='task-1')
            history.finish_call(download, response={'path': 'video.mp4'})
            row = history.generation_records()[0]
            self.assertEqual(row['download_status'], 'succeeded')
            self.assertEqual(row['download_error'], '')

    def test_exporting_folded_media_keeps_original_size_and_hash(self):
        folded = compact_record("data:image/png;base64,YQ==")
        self.assertEqual(compact_record(folded), folded)

    def test_non_json_diagnostic_objects_do_not_block_call_records_or_change_the_request(self):
        with tempfile.TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            payload = build_payload(prompt="拼块归位", duration=4, resolution="480p", ratio="9:16", mode="text")
            original = json.dumps(payload, ensure_ascii=False)
            secret = "sk-" + "x" * 32
            diagnostic = Mock(name=secret)
            call_id = history.begin_call("draft", {"generation_settings": payload, "diagnostic": diagnostic}, secrets=(secret,))
            history.finish_call(call_id, response={"prompt": "角色图绑定", "diagnostic": diagnostic})
            row = history.calls()[0]
            request = json.loads(row["request_json"])
            self.assertEqual(request["generation_settings"], payload)
            self.assertEqual(request["diagnostic"]["record_type"], "unittest.mock.Mock")
            self.assertEqual(json.loads(row["response_json"])["prompt"], "角色图绑定")
            self.assertEqual(json.dumps(payload, ensure_ascii=False), original)
            self.assertEqual(row["status"], "succeeded")
            self.assertNotIn(secret, str(row))

    def test_interruption_does_not_overwrite_an_already_received_result(self):
        with tempfile.TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            done = history.begin_call("draft", {})
            history.finish_call(done, response={"prompt": "已完成"})
            history.interrupt_call(done, "工具关闭")
            pending = history.begin_call("draft", {})
            history.interrupt_call(pending, "工具关闭")
            rows = history.calls()
            self.assertEqual(rows[0]["status"], "unknown")
            self.assertIsNone(rows[0]["duration_ms"])
            self.assertEqual(rows[1]["status"], "succeeded")

    def test_failed_and_uncertain_calls_preserve_parameters_without_secrets_or_media_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            secret = "sk-" + "x" * 32
            payload = {"duration": 8, "resolution": "1080p", "content": [{"image_url": {"url": "data:image/png;base64,AAAA"}}]}
            call_id = history.begin_call("create", {"payload": payload, "api_key": secret})
            history.finish_call(call_id, error=f"401 {secret}")
            uncertain = history.begin_call("create", {"payload": payload})
            history.finish_call(uncertain, error="请求可能已接单", uncertain=True)
            rows = history.calls()
            self.assertEqual([row["status"] for row in rows], ["unknown", "failed"])
            self.assertNotIn(secret, str(rows))
            self.assertNotIn("base64,AAAA", str(rows))
            request = json.loads(rows[1]["request_json"])
            self.assertEqual(request["payload"]["duration"], 8)
            self.assertEqual(request["payload"]["resolution"], "1080p")
            self.assertEqual(history.summary()["task_count"], 0)

    def test_request_and_iteration_lineage_are_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            payload = build_payload(prompt="@图片1 奔跑", duration=4, resolution="480p", ratio="9:16")
            assets = [{"path": "character.png", "handle": "@{character.png}"}]
            history.add("cgt-next", payload, original_prompt="@{character.png} 奔跑", assets=assets, parent_task_id="cgt-before")
            call_id = history.begin_call("create", {"payload": payload})
            history.finish_call(call_id, response={"id": "cgt-next"})
            row = history.rows()[0]
            self.assertEqual(json.loads(row["request_json"]), payload)
            self.assertEqual(json.loads(row["assets_json"]), assets)
            self.assertEqual(row["parent_task_id"], "cgt-before")
            self.assertEqual(row["original_prompt"], "@{character.png} 奔跑")
            self.assertEqual(history.calls()[0]["task_id"], "cgt-next")

    def test_counts_videos_seconds_tokens_and_saved_file(self):
        with tempfile.TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            payload = build_payload(prompt="测试视频", duration=5, resolution="720p", ratio="16:9", mode="text")
            history.add("cgt-1", payload, "text")
            history.update({
                "id": "cgt-1", "status": "succeeded", "duration": 6,
                "content": {"video_url": "https://example.com/out.mp4"},
                "usage": {"completion_tokens": 100000, "total_tokens": 120000},
            })
            history.set_local_path("cgt-1", Path(directory) / "out.mp4")
            total = history.summary()
            self.assertEqual((total["task_count"], total["video_count"], total["seconds"]), (1, 1, 6))
            self.assertEqual(total["completion_tokens"], 100000)
            self.assertEqual(total["total_tokens"], 120000)
            self.assertAlmostEqual(total["estimated_cost_usd"], 1.0)
            self.assertEqual(history.rows()[0]["local_path"], str(Path(directory) / "out.mp4"))

    def test_download_metadata_supplies_missing_actual_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            payload = build_payload(prompt="测试", duration=-1, resolution="720p", ratio="adaptive")
            history.add("cgt-adaptive", payload)
            history.update({"id": "cgt-adaptive", "status": "succeeded"})
            history.set_media_info("cgt-adaptive", {"width": 1280, "height": 720, "duration": 8.2, "has_audio": True})
            self.assertAlmostEqual(history.summary()["seconds"], 8.2)
            self.assertEqual(json.loads(history.rows()[0]["media_json"])["width"], 1280)


if __name__ == "__main__":
    unittest.main()
