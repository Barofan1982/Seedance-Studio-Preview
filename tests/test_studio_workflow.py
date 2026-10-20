"""Regression checks for the Qt workflow, without real account or paid API calls."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import json
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QFontDatabase, QImage, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QMenu, QMessageBox, QPushButton, QTextEdit

from app import SettingsDialog, StudioWindow
from history_store import HistoryStore
from history_view import HistoryDetailDialog, record_settings
from media_assets import MediaAsset
from openai_prompt import PromptDraft
from seedance_client import SubmissionUncertain, build_payload
from studio_controller import DEFAULTS, StudioController, collect_payload, manifest, structured_prompt
from studio_widgets import AssetCard, KeyframeDialog
from studio_widgets import JsonDialog
from error_details_view import ErrorDetailsDialog
from error_diagnostics import response_snapshot
from seedance_client import SeedanceError
from video_analysis import analysis_assets, analysis_signature, needs_video_analysis


class StudioWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        for file in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/segoeui.ttf"):
            if Path(file).is_file():
                QFontDatabase.addApplicationFont(file)
        cls.application.setStyle("Fusion")
        cls.application.setFont(QFont("Microsoft YaHei UI", 10))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.auth = Mock()
        self.auth.active_account.return_value = {"email": "test@example.com"}
        self.auth.label.return_value = "test@example.com"
        self.auth.access_token.return_value = "dummy-plan-token"
        self.auth.models.return_value = [{"slug": "test-model", "display_name": "Test model"}]
        self.controller = self.new_controller()
        self.controller.api_key = "dummy-key"
        self.controller.state.update(base_url="https://example.com/api/v3", mode="纯文本", prompt="小猫跑步", duration="4", download_dir=str(self.directory / "videos"))
        self.errors = []
        self.controller.failed.connect(self.errors.append)
        self.window = None

    def new_controller(self):
        with patch("studio_controller.load_key", return_value=""), patch("studio_controller.load_openai_key", return_value=""):
            return StudioController(self.directory, auth=self.auth)

    def tearDown(self):
        if self.window:
            self.window.close()
        self.controller.close()
        self.application.processEvents()
        self.temporary.cleanup()

    def pump_until(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.application.processEvents()
            QTest.qWait(10)
        self.application.processEvents()
        self.assertTrue(condition(), self.errors or "background stage did not finish")

    def make_window(self):
        self.window = StudioWindow(self.controller, start_services=False)
        self.window.show()
        self.application.processEvents()
        return self.window

    def test_create_query_download_audit_and_restart_do_not_submit_twice(self):
        window = self.make_window()
        client = Mock()
        client.create_task.return_value = "cgt-test"
        signed_url = "https://example.com/video.mp4?Signature=valid-signature&Expires=99999"
        client.get_task.return_value = {
            "id": "cgt-test", "status": "succeeded", "duration": 4,
            "content": {"video_url": signed_url},
            "usage": {"completion_tokens": 111, "total_tokens": 123},
        }
        def download(url, path):
            self.assertEqual(url, signed_url)
            Path(path).write_bytes(b"dummy-output")
        with patch.object(self.controller, "seedance_client", return_value=client), \
             patch("studio_controller.download_video", side_effect=download) as downloader, \
             patch("studio_controller.video_metadata", return_value={"width": 1280, "height": 720, "duration": 4.1, "has_audio": True}):
            payload = collect_payload(self.controller.state, self.controller.assets)
            self.controller.generate(payload)
            self.pump_until(lambda: bool(self.controller.row() and self.controller.row()["local_path"]) and not self.controller.busy)
            self.assertEqual(client.create_task.call_count, 1)
            client.create_task.assert_called_once_with(payload)
            self.assertEqual(self.controller.row()["video_url"], signed_url)
            self.assertEqual(downloader.call_count, 1)
            self.assertEqual({r["operation"] for r in self.controller.history.calls()}, {"create", "query", "download"})
            self.assertTrue(all(r["status"] == "succeeded" for r in self.controller.history.calls()))
            self.assertEqual(self.controller.history.summary()["completion_tokens"], 111)
            self.assertEqual(self.controller.history.summary()["video_count"], 1)
            self.assertEqual(window.current_result["task_id"], "cgt-test")
            self.assertTrue(window.play_result.isEnabled())
            self.assertEqual(window.editor_pages.currentIndex(), 2)
            self.controller.close()
            restarted = self.new_controller()
            try:
                with patch.object(restarted, "seedance_client", return_value=client):
                    restarted.resume()
                self.assertFalse(restarted.busy)
                self.assertTrue(Path(restarted.row()["local_path"]).is_file())
                self.assertEqual(client.create_task.call_count, 1)
                self.assertEqual(client.get_task.call_count, 1)
            finally:
                restarted.close()

    def test_uncertain_submission_is_recorded_and_never_retried(self):
        client = Mock()
        client.create_task.side_effect = SubmissionUncertain("未确认结果 dummy-key", diagnostics={
            "phase": "opening_connection_or_sending_request", "reason": "reset dummy-key", "errno": 10054,
        })
        with patch.object(self.controller, "seedance_client", return_value=client):
            self.controller.generate()
            self.pump_until(lambda: not self.controller.busy)
        self.assertEqual(client.create_task.call_count, 1)
        self.assertFalse(self.controller._poll.isActive())
        self.assertEqual(self.controller.history.calls()[0]["status"], "unknown")
        self.assertNotIn("dummy-key", json.dumps(self.controller.history.calls()))
        diagnostics = json.loads(self.controller.history.calls()[0]["response_json"])["network_diagnostics"]
        self.assertEqual(diagnostics["errno"], 10054)
        self.assertIn("网络诊断", (self.directory / "logs" / "error.log").read_text(encoding="utf-8"))
        self.assertNotIn("dummy-key", (self.directory / "logs" / "error.log").read_text(encoding="utf-8"))
        self.assertTrue(self.errors)
        window = self.make_window()
        self.assertTrue(window.submission_notice.isVisible())
        self.assertIn("未收到任务编号", window.submission_title.text())
        self.assertIn("10054", window.submission_detail.text())
        self.assertIn("被对端或中间网络断开", window.result_info.text())
        self.assertIn("记录已保存，可继续生成", window.submission_detail.text())
        window.close()
        restarted = self.new_controller()
        self.window = StudioWindow(restarted, start_services=False)
        self.window.show()
        self.application.processEvents()
        with patch.object(restarted, "seedance_client", return_value=client):
            restarted.resume()
        self.assertTrue(self.window.submission_notice.isVisible())
        self.assertIn("提交未确认", self.window.submission_title.text())
        self.assertEqual(client.create_task.call_count, 1)

    def test_analysis_uses_membership_and_receives_local_competitor_and_output(self):
        competitor_path = self.directory / "competition.mp4"
        output_path = self.directory / "output.mp4"
        competitor_path.write_bytes(b"competitor")
        output_path.write_bytes(b"output")
        frame = self.directory / "frame.jpg"
        frame.write_bytes(b"mock-frame")
        self.controller.assets = [
            MediaAsset(competitor_path, "video", "competitor", "@{competition.mp4}", duration=27.8, selected=False),
            MediaAsset(None, "image", "character", "@{character.png}", "https://example.com/character.png"),
        ]
        self.controller.state.update(mode="全模态参考", openai_model="test-model", iteration_task_id="parent", review_output=True)
        self.controller.models = self.auth.models.return_value
        parent_payload = build_payload(prompt="原提示词", duration=4, resolution="720p", ratio="16:9")
        self.controller.history.add("parent", parent_payload, assets=manifest(self.controller.assets))
        self.controller.history.set_local_path("parent", output_path)
        draft = PromptDraft("拼图分析", [], "使用 @{character.png} 做拼图", [])
        with patch("studio_controller.OpenAIPromptClient") as factory, \
             patch("video_analysis.extract_reference_frames", return_value=(12, [(0, frame), (1, frame)])):
            factory.return_value.draft.return_value = draft
            self.controller.analyze()
            self.pump_until(lambda: not self.controller.busy)
            factory.assert_called_once_with("dummy-plan-token", "test-model", chatgpt_plan=True, transcription_api_key="", reasoning_effort=None)
            args, kwargs = factory.return_value.draft.call_args
            self.assertEqual(args[0].path, competitor_path)
            self.assertEqual(kwargs["generated_video"], output_path)
            self.assertEqual(kwargs["generation_settings"]["duration"], 4)
            self.assertFalse(kwargs["transcribe_audio"])
        self.assertTrue(self.controller.state["prompt"].startswith(draft.prompt))
        self.assertEqual(self.controller.state["prompt_draft"]["source_assets"], manifest(analysis_assets(self.controller.assets)))
        self.assertEqual(self.controller.state["prompt_draft"]["frame_count"], 2)
        self.assertEqual(self.controller.history.calls()[0]["operation"], "refine")

    def test_material_changes_keep_prompt_and_analysis_can_be_explicitly_required(self):
        competitor = MediaAsset(None, "video", "competitor", "@{competition.mp4}", "https://example.com/a.mp4", 10, False)
        character = MediaAsset(None, "image", "character", "@{c.png}", "https://example.com/c.png")
        assets = [competitor, character]
        frame = self.directory / "frame.jpg"
        frame.write_bytes(b"frame")
        state = {**DEFAULTS, "prompt": "使用 @{c.png}", "duration": "4", "frame_catalog": [{"path": str(frame)}],
                 "prompt_draft": {"source_assets": manifest(assets), "analysis_signature": analysis_signature(assets), "frame_count": 1}}
        self.assertIn("@图片1", collect_payload(state, assets)["content"][0]["text"])
        character.source_url = "https://example.com/changed.png"
        self.assertEqual(collect_payload(state, assets)["content"][1]["image_url"]["url"],character.source_url)
        state['auto_analyze'] = True
        with self.assertRaisesRegex(ValueError, "素材已变化"):
            collect_payload(state, assets)
        state['auto_analyze'] = False
        self.assertEqual(collect_payload(state, assets)["content"][1]["image_url"]["url"], character.source_url)
        frames = [MediaAsset(None, "image", "last", "@{last.png}", "https://example.com/end.png"),
                  MediaAsset(None, "image", "first", "@{first.png}", "https://example.com/start.png")]
        state.update(mode="首尾帧图片", ratio="adaptive", prompt="", duration="-1")
        payload = collect_payload(state, frames)
        self.assertEqual([r["role"] for r in payload["content"]], ["first_frame", "last_frame"])
        self.assertEqual(payload["duration"], -1)

    def test_direct_local_video_is_blocked_and_bad_durations_are_rejected(self):
        state = {**DEFAULTS, "prompt": "拼图", "duration": "4"}
        local = MediaAsset(self.directory / "motion.mp4", "video", "other", "@{motion.mp4}", duration=4)
        with self.assertRaisesRegex(ValueError, "本地视频需要上传"):
            collect_payload(state, [local])
        for duration in ("0", "1", "2", "3", "31"):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                collect_payload({**state, "duration": duration}, [])
        for duration in ("4", "30", "-1"):
            self.assertEqual(collect_payload({**state, "duration": duration}, [])["duration"], int(duration))

    def test_structure_preserves_text_and_repeated_click_does_not_duplicate(self):
        text, count = structured_prompt("现有的详细提示词", [], self.controller.state)
        self.assertEqual(count, 7)
        self.assertIn("现有的详细提示词", text)
        self.assertEqual(structured_prompt(text, [], self.controller.state), (text, 0))
        self.assertEqual(structured_prompt("【视频目标】已有目标", [], self.controller.state)[1], 6)

    def test_import_multiple_images_and_reuse_history_preserves_paths(self):
        def import_asset(path, role, existing):
            return MediaAsset(Path(path), "image", role, "@{" + Path(path).name + "}")
        with patch("studio_controller.add_local_asset", side_effect=import_asset):
            self.controller.import_files([str(self.directory / "a.png"), str(self.directory / "b.png")])
            self.pump_until(lambda: not self.controller.busy)
        self.assertEqual([a.purpose for a in self.controller.assets], ["character", "other"])
        payload = build_payload(prompt="@图片1角色", duration=4, resolution="1080p", ratio="9:16", output_format="mov", generate_audio=False)
        self.controller.history.add("reuse", payload, original_prompt="@{a.png}角色", assets=manifest(self.controller.assets))
        self.controller.reuse(self.controller.row("reuse"))
        self.assertEqual(self.controller.state["resolution"], "1080p")
        self.assertEqual(self.controller.state["output_format"], "mov")
        self.assertFalse(self.controller.state["generate_audio"])
        restarted = self.new_controller()
        try:
            self.assertEqual(manifest(restarted.assets), manifest(self.controller.assets))
            self.assertEqual(restarted.state["prompt"], "@{a.png}角色")
        finally:
            restarted.close()

    def test_ui_mentions_select_material_and_durations_match_request(self):
        self.controller.assets = [MediaAsset(None, "image", "character", "@{角色.png}", "https://example.com/role.png", selected=False)]
        window = self.make_window()
        picker = window.controls["duration"]
        self.assertEqual([picker.itemText(i) for i in range(picker.count())], ["自适应"] + [f"{n} 秒" for n in range(4, 31)])
        picker.setCurrentText("自适应")
        self.assertEqual(self.controller.state["duration"], "-1")
        window.insert_mention("@{角色.png}")
        self.assertTrue(self.controller.assets[0].selected)
        self.assertIn("@{角色.png}", window.prompt.toPlainText())

    def test_unified_editor_submits_legacy_requirements_and_can_clear_them(self):
        self.controller.assets = [
            MediaAsset(None, "video", "other", "@{washface.mp4}", "https://example.com/washface.mp4", 13.4),
            MediaAsset(None, "image", "character", "@{Sera.png}", "https://example.com/Sera.png"),
        ]
        brief = "完整参考 @{washface.mp4}，女性猫咪替换为 @{Sera.png}，保留动作与结尾。"
        self.controller.state.update(mode="全模态参考", prompt="", brief=brief, manual_prompt=True)
        before_ui = collect_payload(self.controller.state, self.controller.assets)
        self.assertEqual(before_ui["content"][0]["text"], "完整参考 @视频1，女性猫咪替换为 @图片1，保留动作与结尾。")
        window = self.make_window()
        self.assertEqual(window.prompt.toPlainText(), brief)
        self.assertEqual(collect_payload(self.controller.state, self.controller.assets), before_ui)
        window.prompt.clear()
        self.assertEqual(self.controller.state["brief"], "")
        self.assertFalse(any(item["type"] == "text" for item in collect_payload(self.controller.state, self.controller.assets)["content"]))

    def test_ai_optimizes_current_editor_and_original_can_be_restored(self):
        self.controller.models = self.auth.models.return_value
        self.controller.state["openai_model"] = "test-model"
        window = self.make_window()
        original = "【角色】女主替换为 Sera；保留原片动作和场景，不增加人物。"
        window.prompt.setPlainText(original)
        draft = PromptDraft("保留：原片动作与场景。\n角色：女主→Sera。\n需确认：无。", [], "【角色】女主用 Sera，沿用原片动作和场景。", [])
        with patch("studio_controller.OpenAIPromptClient") as factory:
            factory.return_value.draft.return_value = draft
            window.analyze()
            self.pump_until(lambda: not self.controller.busy)
            self.assertEqual(factory.return_value.draft.call_args.args[2], original)
        self.assertEqual(window.prompt.toPlainText(), draft.prompt)
        self.assertEqual(self.controller.state["ai_original_text"], original)
        self.assertIn("女主→Sera", window.analysis.toPlainText())
        self.assertEqual(collect_payload(self.controller.state, [])["content"][0]["text"], draft.prompt)
        window.restore_original_prompt()
        self.assertEqual(window.prompt.toPlainText(), original)
        self.assertIn("已过期", window.analysis.toPlainText())

    def test_at_popup_search_click_keyboard_and_duplicate_filenames(self):
        self.controller.state.update(mode="全模态参考", prompt="", brief="", manual_prompt=True)
        self.controller.assets = [
            MediaAsset(None, "image", "character", "@{Sera.png}", "https://example.com/a.png", selected=False),
            MediaAsset(None, "image", "character", "@{Sera.png (2)}", "https://example.com/b.png", selected=False),
            MediaAsset(None, "video", "other", "@{washface.mp4}", "https://example.com/v.mp4", 13.4, False),
        ]
        window = self.make_window()
        editor = window.prompt
        editor.setFocus()
        QTest.keyClicks(editor, "@")
        self.application.processEvents()
        self.assertTrue(editor.completer.popup().isVisible())
        self.assertEqual(editor.completer.completionCount(), 3)
        QTest.keyClick(editor, Qt.Key.Key_Down)
        QTest.keyClick(editor, Qt.Key.Key_Return)
        self.application.processEvents()
        self.assertEqual(editor.toPlainText(), "@{Sera.png (2)} ")
        self.assertTrue(self.controller.assets[1].selected)
        self.assertFalse(self.controller.assets[0].selected)
        self.assertFalse(editor.completer.popup().isVisible())
        self.assertEqual(collect_payload(self.controller.state, self.controller.assets)["content"][1]["image_url"]["url"], "https://example.com/b.png")

        # An input-method commit/paste and emoji before @ must use the correct cursor offsets.
        editor.setPlainText("🎬 完整参考 ")
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cursor)
        editor.insertPlainText("@face")
        self.application.processEvents()
        self.assertTrue(editor.completer.popup().isVisible())
        self.assertEqual(editor.completer.completionCount(), 1)
        popup = editor.completer.popup()
        QTest.mouseClick(popup.viewport(), Qt.MouseButton.LeftButton, pos=popup.visualRect(popup.currentIndex()).center())
        self.application.processEvents()
        self.assertEqual(editor.toPlainText(), "🎬 完整参考 @{washface.mp4} ")
        self.assertTrue(self.controller.assets[2].selected)
        payload = collect_payload(self.controller.state, self.controller.assets)
        self.assertEqual(payload["content"][0]["text"], "🎬 完整参考 @视频1")
        self.assertEqual(payload["content"][-1]["video_url"]["url"], "https://example.com/v.mp4")

    def test_prompt_menu_anchors_at_its_button(self):
        window = self.make_window()
        positions = []
        class CapturingMenu(QMenu):
            def exec(self, position):
                positions.append(position)
        with patch("app.QMenu", CapturingMenu):
            window.prompt_menu()
        position = positions[-1]
        self.assertEqual(position.y(), window.prompt_more.mapToGlobal(QPoint(0, window.prompt_more.height() + 4)).y())
        self.assertLess(position.x(), window.prompt_more.mapToGlobal(QPoint(0, 0)).x())

    def test_video_workflow_analyzes_before_submit_and_preserves_selected_frames(self):
        source = self.directory / "washface.mp4"
        source.write_bytes(b"local-video-version")
        frames = []
        for timestamp in (0, 1, 2, 2.92):
            path = self.directory / f"frame-{timestamp}.jpg"
            image = QImage(320, 480, QImage.Format.Format_RGB32)
            image.fill(Qt.GlobalColor.white)
            image.save(str(path))
            frames.append((timestamp, path))
        self.controller.assets = [
            MediaAsset(source, "video", "other", "@{washface.mp4}", "https://example.com/washface.mp4", 3),
            MediaAsset(None, "image", "character", "@{Sera.png}", "https://example.com/Sera.png"),
        ]
        self.controller.state.update(mode="全模态参考", prompt="女性角色换成 @{Sera.png}", brief="", openai_model="test-model", manual_prompt=False, auto_analyze=True)
        self.controller.models = self.auth.models.return_value
        window = self.make_window()
        self.assertEqual(window.generate_button.text(), "先优化提示词 →")
        self.assertNotIn("task_type", window.controls)
        with self.assertRaisesRegex(ValueError, "先优化提示词"):
            collect_payload(self.controller.state, self.controller.assets)
        def draft(_competitor, assets, brief, **kwargs):
            self.assertEqual(assets[0].path, source)
            self.assertEqual(len(kwargs["reference_frames"]), 4)
            chosen = [kwargs["reference_frames"][0]["handle"], kwargs["reference_frames"][-1]["handle"]]
            return PromptDraft("内容：女主多步护肤，男主简单洗脸。", [], "女性角色用 @{Sera.png}，保留 @{washface.mp4} 的动作对比。", [], keyframe_handles=chosen)
        with patch("video_analysis.extract_reference_frames", return_value=(3, frames)), \
             patch("studio_controller.OpenAIPromptClient") as factory, \
             patch.object(self.controller, "seedance_client") as seedance:
            factory.return_value.draft.side_effect = draft
            window.generate()
            self.pump_until(lambda: not self.controller.busy)
            seedance.assert_not_called()
        self.assertFalse(needs_video_analysis(self.controller.state, self.controller.assets))
        self.assertEqual(self.controller.state["prompt_draft"]["frame_count"], 4)
        self.assertEqual(window.generate_button.text(), "生成视频 · 付费")
        payload = collect_payload(self.controller.state, self.controller.assets)
        self.assertEqual(payload["omni_reference_task_type"], "reference")
        self.assertEqual(sum(v["type"] == "image_url" for v in payload["content"]), 1)
        self.assertEqual(payload["content"][-1]["video_url"]["url"], "https://example.com/washface.mp4")
        self.assertNotIn("【关键帧参考】", payload["content"][0]["text"])
        dialog = KeyframeDialog(self.controller, Mock(), window)
        dialog.show()
        dialog.resize(740, 540)
        self.application.processEvents()
        self.assertTrue(dialog.rect().contains(dialog.apply_button.mapTo(dialog, dialog.apply_button.rect().bottomRight())))
        self.assertFalse(dialog.attach_frames.isChecked())
        dialog.attach_frames.setChecked(True)
        chosen = list(dialog.choices)[:2]
        for handle in chosen:
            dialog.choices[handle].setChecked(True)
        dialog.choices[chosen[-1]].setChecked(False)
        dialog.apply()
        self.assertNotIn(chosen[-1], self.controller.state["prompt"])
        self.assertEqual(sum(a.purpose == "keyframe" for a in self.controller.assets), 1)
        restarted = self.new_controller()
        try:
            self.assertFalse(needs_video_analysis(restarted.state, restarted.assets))
            self.assertEqual(collect_payload(restarted.state, restarted.assets), collect_payload(self.controller.state, self.controller.assets))
            restarted.assets[0].path.write_bytes(b"changed-local-video")
            self.assertTrue(needs_video_analysis(restarted.state, restarted.assets))
        finally:
            restarted.close()

    def test_primary_controls_stay_inside_large_and_small_windows(self):
        self.controller.assets = [
            MediaAsset(None, "image", "character", "@{" + "很长的素材文件名" * 6 + ".png}", "https://example.com/image.png"),
            MediaAsset(self.directory / "reference.mp4", "video", "other", "@{reference.mp4}",
                       "https://example.com/reference.mp4", 13.466667),
        ]
        window = self.make_window()
        for width, height in ((1920, 1080), (1280, 800)):
            window.resize(width, height)
            window.advanced.show()
            self.application.processEvents()
            for widget in (window.generate_button, window.preview_button, window.ai_button, window.keyframe_button,
                           window.mention_button, window.import_button, window.account_button):
                with self.subTest(size=(width, height), widget=widget.text()):
                    origin = widget.mapTo(window, QPoint(0, 0))
                    self.assertTrue(window.rect().contains(origin))
                    self.assertTrue(window.rect().contains(origin + widget.rect().bottomRight()))
                    self.assertGreater(widget.height(), 20)
            self.assertEqual(window.asset_content.width(), window.asset_scroll.viewport().width())
            for widget in window.asset_content.findChildren(QPushButton) + window.asset_content.findChildren(QCheckBox):
                origin = widget.mapTo(window.asset_content, QPoint(0, 0))
                self.assertTrue(window.asset_content.rect().contains(origin + widget.rect().bottomRight()))
                self.assertLessEqual((origin + widget.rect().bottomRight()).x(), window.asset_scroll.viewport().width())
        settings = SettingsDialog(self.controller, window)
        try:
            settings.show()
            self.application.processEvents()
            complete = next(b for b in settings.findChildren(QPushButton) if b.text() == "保存并完成")
            self.assertTrue(settings.rect().contains(complete.mapTo(settings, complete.rect().bottomRight())))
        finally:
            settings.close()

    def test_generation_switches_to_result_immediately_and_tracks_real_stages(self):
        window=self.make_window()
        release=threading.Event()
        client=Mock()
        def create(_payload):
            release.wait(3)
            return 'progress-task'
        client.create_task.side_effect=create
        client.get_task.return_value={'id':'progress-task','status':'queued'}
        try:
            with patch.object(self.controller,'seedance_client',return_value=client):
                window.generate()
                self.application.processEvents()
                self.assertEqual(window.editor_pages.currentIndex(),2)
                self.assertEqual(window.generation_title.text(),'正在提交请求')
                self.assertTrue(window.generation_canvas.timer.isActive())
                tick=window.generation_canvas.tick
                QTest.qWait(90)
                self.assertGreater(window.generation_canvas.tick,tick)
                self.assertFalse(window.generate_button.isEnabled())
                release.set()
                self.pump_until(lambda:bool(self.controller.row()) and not self.controller.busy and not self.controller.query_busy)
                self.controller._poll.stop()
                self.assertEqual(window.generation_title.text(),'正在排队')
                self.assertEqual(window.generation_feedback['phase'],1)
                self.assertTrue(window.generation_timer.isActive())
                client.get_task.return_value={'id':'progress-task','status':'running'}
                self.controller.query()
                self.pump_until(lambda:not self.controller.query_busy)
                self.controller._poll.stop()
                self.assertEqual(window.generation_feedback['phase'],2)
            self.controller.pause_queries()
            window.refresh_generation()
            self.assertIn('查询暂停',window.generation_title.text())
            self.assertFalse(window.generation_canvas.timer.isActive())
            self.assertTrue(window.continue_result.isVisible())
            output=self.directory/'result.mp4';output.write_bytes(b'local-output')
            self.controller.history.update({'id':'progress-task','status':'succeeded','duration':4})
            self.controller.history.set_local_path('progress-task',output)
            self.controller.changed.emit('history')
            self.assertEqual(window.generation_title.text(),'视频已保存')
            self.assertFalse(window.generation_timer.isActive())
            self.assertTrue(window.reedit_result.isVisible())
        finally:
            release.set()

    def test_history_lists_ai_versions_and_uncertain_submissions_and_loads_without_submitting(self):
        self.controller.history.add('old-video',collect_payload(self.controller.state,[]),original_prompt='小猫追球')
        prompt_id=self.controller.history.begin_call('draft',{'creative_brief':'猫跑步',
            'generation_settings':{'mode':'text','resolution':'480p','ratio':'9:16','duration':4},'assets':[]})
        self.controller.history.finish_call(prompt_id,response={'draft':{'prompt':'小猫沿小路追球','analysis':'猫追球','shots':[],'uncertainties':[]},'frames':[]})
        uncertain_id=self.controller.history.begin_call('create',{'payload':collect_payload(self.controller.state,[]),'original_prompt':'小猫转头','assets':[]})
        self.controller.history.finish_call(uncertain_id,error='未确认',uncertain=True)
        window=self.make_window()
        self.assertEqual(window.editor_tabs.button(3).text(),'历史记录')
        self.assertEqual(len(window.workbench_history.records),3)
        window.workbench_history.kind.setCurrentIndex(2)
        self.assertEqual(window.workbench_history.count.text(),'1 条')
        record=self.controller.history.prompt_records()[0]
        with patch.object(self.controller,'seedance_client') as client:
            window.load_history_record(record)
            client.assert_not_called()
        self.assertEqual(window.prompt.toPlainText(),'小猫沿小路追球')
        self.assertEqual(self.controller.state['resolution'],'480p')
        self.assertEqual(self.controller.state['ratio'],'9:16')
        self.assertEqual(window.editor_pages.currentIndex(),0)
        self.assertEqual(len(self.controller.history.calls()),2)
        payload=collect_payload(self.controller.state,self.controller.assets)
        self.assertEqual(payload['content'][0]['text'],'小猫沿小路追球')
        dialog=HistoryDetailDialog(record,self.controller,Mock(),Mock(),window)
        try:
            dialog.show();dialog.resize(700,560);self.application.processEvents()
            self.assertTrue(dialog.rect().contains(dialog.reuse_button.mapTo(dialog,dialog.reuse_button.rect().bottomRight())))
        finally:
            dialog.close()

    def test_reusing_a_video_template_preserves_keyframes_and_analysis_proof(self):
        from copy import deepcopy
        source=self.directory/'reference.mp4';source.write_bytes(b'local-reference')
        frame=self.directory/'frame.jpg'
        image=QImage(320,480,QImage.Format.Format_RGB32);image.fill(Qt.GlobalColor.white);image.save(str(frame))
        self.controller.assets=[MediaAsset(source,'video','other','@{reference.mp4}','https://example.com/reference.mp4',5)]
        self.controller.state.update(mode='全模态参考',manual_prompt=False,prompt='保留 @{reference.mp4} 的动作',brief='')
        self.controller.state['frame_catalog']=[{'path':str(frame),'timestamp':0,'source_handle':'@{reference.mp4}','handle':'@{reference-00.00s.jpg}'}]
        self.controller.state['prompt_draft']={'frame_count':1,'analysis_signature':analysis_signature(self.controller.assets),'analysis':'角色动作'}
        self.controller.apply_keyframes(['@{reference-00.00s.jpg}'])
        payload=collect_payload(self.controller.state,self.controller.assets)
        template=deepcopy({key:self.controller.state[key] for key in ('frame_catalog','prompt_draft','generated_keyframe_binding','manual_prompt','ai_original_text','use_keyframes_for_generation')})
        self.controller.history.add('template-parent',payload,original_prompt=self.controller.state['prompt'],assets=manifest(self.controller.assets),template=template)
        self.controller.history.update({'id':'template-parent','status':'succeeded'})
        self.controller.state.update(prompt='其他内容',frame_catalog=[],prompt_draft={})
        self.controller.assets=[]
        self.controller.reuse(self.controller.row('template-parent'))
        self.assertFalse(needs_video_analysis(self.controller.state,self.controller.assets))
        self.assertEqual(collect_payload(self.controller.state,self.controller.assets),payload)
        self.assertEqual(self.controller.state['iteration_task_id'],'template-parent')
        client=Mock();client.create_task.return_value='template-child';client.get_task.return_value={'id':'template-child','status':'queued'}
        with patch.object(self.controller,'seedance_client',return_value=client):
            self.controller.generate()
            self.pump_until(lambda:bool(self.controller.row('template-child')) and not self.controller.busy and not self.controller.query_busy)
        self.controller._poll.stop()
        self.assertEqual(self.controller.row('template-child')['parent_task_id'],'template-parent')
        self.assertEqual(json.loads(self.controller.row('template-child')['template_json'])['frame_catalog'],template['frame_catalog'])

    def test_history_marks_outcomes_filters_problems_and_exposes_full_errors(self):
        payload=collect_payload(self.controller.state,self.controller.assets)
        self.controller.history.add('saved-task',payload)
        output=self.directory/'output.mp4';output.write_bytes(b'output')
        self.controller.history.update({'id':'saved-task','status':'succeeded'})
        self.controller.history.set_local_path('saved-task',output)
        error='HTTP 400: '+('invalid reference <video> '*15)
        failed=self.controller.history.begin_call('create',{'payload':payload})
        self.controller.history.finish_call(failed,error=error)
        uncertain=self.controller.history.begin_call('create',{'payload':payload})
        self.controller.history.finish_call(uncertain,error='等待结果超时',uncertain=True)
        prompt=self.controller.history.begin_call('draft',{'generation_settings':payload})
        self.controller.history.finish_call(prompt,response={'prompt':'小猫跑步'})
        window=self.make_window();panel=window.workbench_history
        self.assertEqual(panel.count.text(),'4 条')
        self.assertIn('1 有成片',panel.summary.text())
        self.assertIn('1 失败',panel.summary.text())
        for category,expected in [('saved','有成片'),('failed','提交失败'),('uncertain','提交未确认'),('prompt','仅提示词')]:
            panel.outcome_filter.setCurrentIndex(panel.outcome_filter.findData(category))
            self.application.processEvents()
            self.assertEqual(panel.count.text(),'1 条')
            card=panel.cards.itemAt(0).widget()
            badge=card.findChild(QLabel,'HistoryStatus')
            self.assertIn(expected,badge.text())
        panel.outcome_filter.setCurrentIndex(0)
        panel.search.setText('HTTP 400');self.application.processEvents()
        self.assertEqual(panel.count.text(),'1 条')
        card=panel.cards.itemAt(0).widget()
        reasons=card.findChildren(QLabel,'HistoryReason')
        self.assertTrue(any('状态码 400' in reason.text() for reason in reasons))
        self.assertTrue(all('invalid reference' not in reason.text() for reason in reasons))
        record=next(r for r in panel.records if r.get('call_id')==failed)
        dialog=HistoryDetailDialog(record,self.controller,Mock(),Mock(),window)
        try:
            dialog.show();dialog.resize(700,560);self.application.processEvents()
            original=dialog.findChild(QTextEdit,'OriginalError')
            self.assertEqual(original.toPlainText(),error)
            self.assertTrue(original.isHidden())
            toggle=next(b for b in dialog.findChildren(QPushButton) if b.text()=='查看原始报错')
            toggle.click();self.application.processEvents()
            self.assertTrue(original.isVisible())
            self.assertTrue(dialog.rect().contains(dialog.reuse_button.mapTo(dialog,dialog.reuse_button.rect().bottomRight())))
        finally:
            dialog.close()

    def test_chinese_choices_keep_raw_api_values_across_modes_history_and_restart(self):
        self.controller.state.update(ratio='adaptive',output_format='mov')
        window=self.make_window()
        ratio=window.controls['ratio'];output=window.controls['output_format']
        self.assertEqual(ratio.currentText(),'自适应')
        self.assertEqual(ratio.currentData(),'adaptive')
        self.assertEqual(output.currentText(),'MOV')
        self.assertEqual(output.currentData(),'mov')
        self.assertNotIn('adaptive',window.parameter_summary.text())
        ratio.setCurrentText('16:9');ratio.setCurrentText('自适应')
        output.setCurrentText('MP4')
        payload=collect_payload(self.controller.state,self.controller.assets)
        self.assertEqual(payload['ratio'],'adaptive')
        self.assertEqual(payload['output_format'],'mp4')
        self.controller.set_value('mode','首帧图片')
        self.assertEqual(ratio.currentText(),'自适应')
        self.controller.set_value('mode','纯文本')
        self.controller.history.add('chinese-template',payload)
        record=self.controller.history.rows()[0]
        self.assertIn('自适应',record_settings(record))
        self.assertNotIn('adaptive',record_settings(record))
        self.controller.reuse(record);self.controller.save()
        restarted=self.new_controller()
        try:
            self.assertEqual(restarted.state['ratio'],'adaptive')
            self.assertEqual(collect_payload(restarted.state,restarted.assets)['ratio'],'adaptive')
        finally:
            restarted.close()
        box=QMessageBox(QMessageBox.Icon.Information,'提示','已完成',QMessageBox.StandardButton.Ok,window)
        self.assertIn('确定',box.button(QMessageBox.StandardButton.Ok).text())
        menu=window.prompt.createStandardContextMenu()
        try:
            actions=[a.text() for a in menu.actions() if not a.isSeparator()]
            self.assertTrue(any('复制' in text for text in actions))
            self.assertFalse(any('Copy' in text or 'Paste' in text for text in actions))
        finally:
            menu.deleteLater()

    def test_chinese_parameter_preview_exports_exact_original_request(self):
        self.controller.state['ratio']='adaptive'
        payload=collect_payload(self.controller.state,self.controller.assets)
        dialog=JsonDialog('提交预览',{'request':payload},full_value=payload)
        try:
            self.assertEqual(dialog.tabs.tabText(0),'中文说明')
            self.assertEqual(dialog.tabs.tabText(1),'原始参数')
            self.assertEqual(dialog.tabs.currentIndex(),0)
            self.assertEqual(json.loads(dialog.view.toPlainText())['request']['ratio'],'adaptive')
            root=dialog.overview.topLevelItem(0)
            items={root.child(i).text(0):root.child(i).text(1) for i in range(root.childCount())}
            self.assertEqual(items['画面比例'],'自适应')
            dialog.copy()
            self.assertEqual(json.loads(self.application.clipboard().text())['request']['ratio'],'adaptive')
            output=self.directory/'request.json'
            with patch('studio_widgets.QFileDialog.getSaveFileName',return_value=(str(output),'')):
                dialog.export()
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')),payload)
        finally:
            dialog.close()

    def test_cny_prices_are_saved_recalculate_history_and_export_chinese_columns(self):
        import csv
        payload=collect_payload(self.controller.state,self.controller.assets)
        self.controller.history.add('image-input',payload)
        self.controller.history.update({'id':'image-input','status':'succeeded','usage':{'completion_tokens':200000}})
        video_payload=build_payload(prompt='男主洗脸',duration=4,resolution='480p',ratio='9:16',video_sources=['https://example.com/video.mp4'])
        self.controller.history.add('video-input',video_payload)
        self.controller.history.update({'id':'video-input','status':'succeeded','usage':{'completion_tokens':250000}})
        window=self.make_window()
        self.assertEqual(window.metrics['estimated_cost_cny'].text(),'24.50')
        dialog=SettingsDialog(self.controller,window)
        try:
            dialog.cny_rates['without_video'].setText('80')
            dialog.cny_rates['with_video'].setText('40')
            dialog.save()
            self.assertEqual(window.metrics['estimated_cost_cny'].text(),'26.00')
            restarted=self.new_controller()
            try:self.assertEqual(restarted.state['cny_rates'],{'without_video':80.0,'with_video':40.0})
            finally:restarted.close()
            output=self.directory/'usage.csv';self.controller.export_csv(output)
            with output.open(encoding='utf-8-sig',newline='') as stream:rows=list(csv.DictReader(stream))
            amounts={r['任务编号']:r['参考费用（人民币元）'] for r in rows}
            self.assertEqual(amounts,{'image-input':'16.00','video-input':'10.00'})
            self.assertTrue(all(r['状态']=='已完成' for r in rows))
        finally:
            dialog.close()

    def test_generate_uploads_local_video_first_and_reuses_full_url_after_restart(self):
        source=self.directory/'local.mp4';source.write_bytes(b'video-file')
        asset=MediaAsset(source,'video','other','@{local.mp4}',duration=5)
        self.controller.assets=[asset]
        self.controller.state.update(mode='全模态参考',manual_prompt=True,prompt='参考 @{local.mp4}，替换角色')
        upload=Mock();upload.signed_in.return_value=True
        signed='https://example.com/local.mp4?X-Tos-Signature=a%2Fb&X-Tos-Credential=x%2Fy'
        upload.upload.return_value={'url':signed,'id':'media-id','expires_at':'2099-01-01T00:00:00Z','duration':5}
        self.controller.upload_auth=upload
        client=Mock();client.create_task.return_value='uploaded-task'
        client.get_task.return_value={'id':'uploaded-task','status':'running'}
        window=self.make_window()
        with patch.object(self.controller,'seedance_client',return_value=client):
            window.generate()
            self.pump_until(lambda: self.controller.row() is not None and self.controller.row()['status']=='running' and not self.controller.busy)
        self.controller._poll.stop()
        upload.upload.assert_called_once()
        payload=client.create_task.call_args.args[0]
        self.assertEqual(payload['content'][-1]['video_url']['url'],signed)
        self.assertTrue(payload['model'].endswith('-with-video'))
        self.assertIn('@视频1',payload['content'][0]['text'])
        restarted=self.new_controller()
        try:
            self.assertEqual(restarted.assets[0].source_url,signed)
            self.assertEqual(restarted.assets[0].upload_id,'media-id')
            self.assertEqual(restarted.pending_video_uploads(),[])
            restarted.reuse(self.controller.row())
            self.assertEqual(restarted.assets[0].source_expires_at,'2099-01-01T00:00:00Z')
        finally:restarted.close()
        uploaded_call=next(call for call in self.controller.history.calls() if call['operation']=='upload_video')
        self.assertEqual(uploaded_call['status'],'succeeded')
        self.assertNotIn('a%2Fb',uploaded_call['response_json'])

    def test_partial_upload_failure_keeps_successes_and_never_creates_video(self):
        from video_upload import UploadError
        files=[self.directory/'one.mp4',self.directory/'two.mp4']
        for file in files:file.write_bytes(b'video')
        self.controller.assets=[MediaAsset(file,'video','other',f'@{{{file.name}}}',duration=5) for file in files]
        upload=Mock();upload.signed_in.return_value=True
        upload.upload.side_effect=[{'url':'https://example.com/one.mp4','id':'one','expires_at':'2099-01-01T00:00:00Z','duration':5},
                                   UploadError('中转返回状态码 413：单个视频不得超过 200 MB。',413)]
        self.controller.upload_auth=upload
        continuation=Mock()
        self.controller.upload_videos([0,1],success=continuation)
        self.pump_until(lambda:not self.controller.busy)
        self.assertEqual(self.controller.assets[0].source_url,'https://example.com/one.mp4')
        self.assertFalse(self.controller.assets[1].source_url)
        continuation.assert_not_called()
        self.assertIn('413',self.errors[-1])
        self.assertEqual(self.controller.history.calls()[0]['status'],'failed')
        self.assertEqual(self.controller.pending_video_uploads(),[1])

    def test_upload_url_rotation_keeps_valid_frame_analysis_but_stale_analysis_stays_stale(self):
        source=self.directory/'local.mp4';source.write_bytes(b'video')
        frame=self.directory/'frame.jpg';frame.write_bytes(b'frame')
        self.controller.assets=[MediaAsset(source,'video','other','@{local.mp4}','https://example.com/old.mp4',5)]
        self.controller.state.update(mode='全模态参考',manual_prompt=False,auto_analyze=True,prompt='参考 @{local.mp4}',
            frame_catalog=[{'path':str(frame)}],prompt_draft={'source_assets':manifest(self.controller.assets),
            'analysis_signature':analysis_signature(self.controller.assets),'frame_count':1})
        upload=Mock();upload.signed_in.return_value=True
        upload.upload.return_value={'url':'https://example.com/new.mp4','id':'new','expires_at':'2099-01-01T00:00:00Z','duration':5}
        self.controller.upload_auth=upload
        self.controller.upload_videos([0]);self.pump_until(lambda:not self.controller.busy)
        self.assertFalse(needs_video_analysis(self.controller.state,self.controller.assets))
        self.assertEqual(collect_payload(self.controller.state,self.controller.assets)['content'][-1]['video_url']['url'],
                         'https://example.com/new.mp4')
        self.controller.state['prompt_draft']['analysis_signature']='stale-analysis'
        self.controller.state['auto_analyze']=False
        self.controller.upload_videos([0]);self.pump_until(lambda:not self.controller.busy)
        self.controller.state['auto_analyze']=True
        self.assertTrue(needs_video_analysis(self.controller.state,self.controller.assets))

    def test_cancelled_upload_login_cannot_continue_generation_or_record_password(self):
        from app import UploadLoginDialog
        gate=threading.Event()
        upload=Mock();upload.account.return_value={}
        def login(_username,_password):
            gate.wait(2);return {'username':'test-user'}
        upload.login.side_effect=login
        self.controller.upload_auth=upload
        continuation=Mock();dialog=UploadLoginDialog(self.controller,success=continuation)
        dialog.show();dialog.username.setText('test-user');dialog.password.setText('never-log-this-password')
        dialog.login();dialog.reject();gate.set()
        self.pump_until(lambda:not self.controller.busy)
        continuation.assert_not_called()
        calls=json.dumps(self.controller.history.calls())
        self.assertNotIn('never-log-this-password',calls)

    def test_upload_keeps_material_scrolling_enabled_and_records_waiting_phase(self):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QWheelEvent
        self.controller.assets=[MediaAsset(None,'image','other',f'@{{image-{index}.png}}',f'https://example.com/{index}.png') for index in range(16)]
        window=self.make_window();window.resize(1280,800);self.application.processEvents()
        gate=threading.Event()
        self.controller.upload_feedback={'active':True,'phase':'validating','started_at':time.time()-40,'phase_started_at':time.time(),
                                          'index':1,'count':1,'filename':'local.mp4','percent':100}
        self.controller.start_job('upload_video',{'files':['local.mp4']},lambda:gate.wait(2),lambda _value:None)
        self.controller._receive_upload_event({'phase':'waiting_response'})
        self.controller.upload_feedback['phase_started_at']=time.time()-40
        self.controller.refresh_upload_feedback()
        self.application.processEvents()
        self.assertTrue(window.material_panel.isEnabled())
        self.assertTrue(window.asset_scroll.isEnabled())
        self.assertFalse(window.import_button.isEnabled())
        self.assertFalse(any(choice.isEnabled() for choice in window.asset_content.findChildren(QCheckBox)))
        bar=window.asset_scroll.verticalScrollBar();self.assertGreater(bar.maximum(),0)
        event=QWheelEvent(QPointF(30,30),QPointF(window.asset_scroll.mapToGlobal(QPoint(30,30))),QPoint(0,0),QPoint(0,-120),
                         Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.NoScrollPhase,False)
        QApplication.sendEvent(window.asset_scroll.viewport(),event)
        self.assertGreater(bar.value(),0)
        self.assertIn('等待中转响应',window.upload_title.text())
        self.assertIn('重复上传',window.upload_detail.text())
        self.assertEqual(window.upload_bar.maximum(),0)
        live=json.loads(self.controller.history.calls()[0]['response_json'])
        self.assertEqual(live['upload_progress']['phase'],'waiting_response')
        gate.set();self.pump_until(lambda:not self.controller.busy)
        self.assertTrue(window.import_button.isEnabled())
        self.assertFalse(window.upload_notice.isVisible())

    def test_removed_uploaded_video_can_be_restored_or_reimported_without_uploading(self):
        source=self.directory/'uploaded.mp4';source.write_bytes(b'video')
        stat=source.stat();signed='https://example.com/video.mp4?X-Tos-Signature=original-signature'
        asset=MediaAsset(source,'video','other','@{uploaded.mp4}',signed,5,True,'2099-01-01T00:00:00Z','media',stat.st_size,stat.st_mtime_ns)
        self.controller.assets=[asset]
        self.controller.upload_cache[self.controller._video_cache_key(source)]=manifest([asset])[0]
        self.controller.update_asset(0,remove=True)
        self.assertEqual(self.controller.assets,[])
        self.assertTrue(source.is_file())
        self.controller.restore_removed_asset()
        self.assertEqual(self.controller.assets[0].source_url,signed)
        self.assertEqual(self.controller.pending_video_uploads(),[])
        self.controller.update_asset(0,remove=True)
        with patch('studio_controller.add_local_asset',return_value=MediaAsset(source,'video','competitor','@{uploaded.mp4}',duration=5,selected=False)):
            self.controller.import_files([str(source)])
            self.pump_until(lambda:not self.controller.busy)
        self.assertEqual(self.controller.assets[0].source_url,signed)
        self.assertEqual(self.controller.pending_video_uploads(),[])
        self.assertFalse(any(call['operation']=='upload_video' for call in self.controller.history.calls()))

    def test_material_groups_keep_original_selection_and_mention_bindings(self):
        assets = [
            MediaAsset(None, 'image', 'character', '@{角色.png}', 'https://example.com/character.png'),
            MediaAsset(None, 'audio', 'audio', '@{声音.mp3}', 'https://example.com/sound.mp3', 4),
            MediaAsset(None, 'video', 'other', '@{参考.mp4}', 'https://example.com/reference.mp4', 5),
            MediaAsset(None, 'image', 'scene', '@{场景.png}', 'https://example.com/scene.png', selected=False),
        ]
        self.controller.assets = assets
        window = self.make_window()
        groups = [window.asset_layout.itemAt(index).widget() for index in range(window.asset_layout.count() - 1)]
        self.assertEqual([group.property('kind') for group in groups], ['video', 'image', 'audio'])
        self.assertEqual([len(group.findChildren(AssetCard)) for group in groups], [1, 2, 1])
        self.assertEqual([group.findChild(QLabel, 'AssetGroupTitle').text() for group in groups], ['视频', '图片', '音频'])
        self.assertEqual(window.material_hint.text(), '图片 1 · 视频 1 · 音频 1')
        self.assertEqual([asset.handle for asset in self.controller.assets], [asset.handle for asset in assets])
        with patch.object(self.controller, 'update_asset') as update:
            groups[0].findChild(QCheckBox).setChecked(False)
            update.assert_called_once_with(2, selected=False)
            update.reset_mock()
            groups[1].findChildren(QCheckBox)[1].setChecked(True)
            update.assert_called_once_with(3, selected=True)
        video_card = groups[0].findChild(AssetCard)
        video_menu = video_card.findChild(QMenu)
        next(action for action in video_menu.actions() if action.text() == '插入 @ 引用').trigger()
        self.assertIn('@{参考.mp4}', window.prompt.toPlainText())
        self.assertTrue(self.controller.assets[2].selected)
        self.controller.assets = [assets[0]]
        window.refresh_assets()
        self.application.processEvents()
        self.assertEqual(len(window.asset_content.findChildren(AssetCard)), 1)
        self.assertEqual(window.asset_content.findChild(QLabel, 'AssetGroupTitle').text(), '图片')

    def test_uploaded_library_survives_removal_and_restart_and_keeps_expired_entries_disabled(self):
        from upload_library import record_status
        source=self.directory/'uploaded.mp4';source.write_bytes(b'video')
        stat=source.stat()
        signed='https://example.com/video.mp4?X-Tos-Signature=keep-complete'
        asset=MediaAsset(source,'video','other','@{uploaded.mp4}',signed,5,True,'2099-01-01T00:00:00Z','stored',stat.st_size,stat.st_mtime_ns)
        self.controller.state.update(mode='全模态参考',manual_prompt=True,prompt='参考 @{uploaded.mp4}')
        self.controller.assets=[asset]
        self.controller.upload_library.remember(manifest([asset])[0]);self.controller.upload_library.save()
        self.controller.update_asset(0,remove=True)
        restarted=self.new_controller()
        try:
            self.assertEqual(len(restarted.upload_library.rows()),1)
            restarted.add_uploaded_asset('stored')
            self.assertEqual(restarted.assets[0].source_url,signed)
            self.assertEqual(restarted.pending_video_uploads(),[])
            self.assertFalse(restarted.history.calls())
        finally:restarted.close()
        window=self.make_window();window.navigate(3)
        panel=window.uploaded_library
        self.assertTrue(panel.add_button.isEnabled())
        self.controller.upload_library.records['stored']['source_expires_at']='2000-01-01T00:00:00Z'
        self.controller.refresh_upload_expiry()
        self.assertEqual(panel.table.rowCount(),1)
        self.assertEqual(panel.table.item(0,5).text(),'已过期')
        self.assertEqual(panel.table.item(0,0).foreground().color().name(),'#a5aab5')
        self.assertFalse(panel.add_button.isEnabled())
        self.assertTrue(panel.renew_button.isEnabled())
        with self.assertRaisesRegex(ValueError,'不能调用'):self.controller.add_uploaded_asset('stored')
        self.assertEqual(self.controller.assets,[])
        from studio_controller import load_assets
        self.controller.assets=load_assets([self.controller.upload_library.get('stored')])
        with patch.object(self.controller,'seedance_client') as client, self.assertRaisesRegex(ValueError,'不能提交'):
            self.controller.generate({'model':'cached-preview'})
        client.assert_not_called()
        self.controller.assets=[]
        self.assertEqual(record_status({'source_url':signed,'source_expires_at':''})[0],'unknown')
        panel.filter.setCurrentIndex(1);self.assertEqual(panel.table.rowCount(),0)
        self.assertFalse(panel.add_button.isEnabled())
        panel.filter.setCurrentIndex(2);self.assertEqual(panel.table.rowCount(),1)
        self.controller.upload_library.save()
        self.assertNotIn(b'keep-complete',self.controller.upload_library.path.read_bytes())

    def test_uploaded_library_renewal_preserves_old_record_and_never_generates_a_video(self):
        source=self.directory/'uploaded.mp4';source.write_bytes(b'video')
        stat=source.stat()
        asset=MediaAsset(source,'video','other','@{uploaded.mp4}','https://example.com/expired.mp4',5,True,
                         '2000-01-01T00:00:00Z','expired',stat.st_size,stat.st_mtime_ns)
        self.controller.state.update(mode='全模态参考',manual_prompt=True,prompt='参考 @{uploaded.mp4}')
        self.controller.upload_library.remember(manifest([asset])[0]);self.controller.upload_library.save()
        upload=Mock();upload.signed_in.return_value=True
        upload.upload.return_value={'url':'https://example.com/new.mp4?X-Tos-Signature=new','id':'renewed',
                                    'expires_at':'2099-01-01T00:00:00Z','duration':5}
        self.controller.upload_auth=upload
        index=self.controller.add_uploaded_asset('expired',renew=True)
        with self.assertRaisesRegex(ValueError,'需要上传'):collect_payload(self.controller.state,self.controller.assets)
        self.controller.upload_videos([index]);self.pump_until(lambda:not self.controller.busy)
        self.assertEqual(len(self.controller.upload_library.rows()),2)
        self.assertEqual(self.controller.assets[0].upload_id,'renewed')
        self.assertEqual(self.controller.pending_video_uploads(),[])
        self.assertEqual([row['operation'] for row in self.controller.history.calls()],['upload_video'])
        self.assertFalse(self.controller.history.rows())

    def test_library_cloud_copy_can_be_reused_after_local_file_changes_without_analysing_wrong_file(self):
        source=self.directory/'uploaded.mp4';source.write_bytes(b'video')
        stat=source.stat()
        asset=MediaAsset(source,'video','other','@{uploaded.mp4}','https://example.com/original.mp4',5,True,
                         '2099-01-01T00:00:00Z','original',stat.st_size,stat.st_mtime_ns)
        self.controller.upload_library.remember(manifest([asset])[0]);self.controller.upload_library.save()
        self.controller.state.update(mode='全模态参考',manual_prompt=True,prompt='参考 @{uploaded.mp4}')
        source.write_bytes(b'new-and-different-video')
        self.controller.add_uploaded_asset('original')
        self.assertIsNone(self.controller.assets[0].path)
        self.assertEqual(self.controller.pending_video_uploads(),[])
        self.assertEqual(collect_payload(self.controller.state,self.controller.assets)['content'][-1]['video_url']['url'],
                         'https://example.com/original.mp4')

    def test_image_library_remembers_more_than_twenty_removed_images_and_restores_all_after_restart(self):
        from image_library import image_copy
        images=[]
        for index in range(22):
            path=self.directory/f'image-{index}.png'
            picture=QImage(300,300,QImage.Format.Format_RGB32);picture.fill(0xff000000+index*7000);picture.save(str(path))
            images.append(MediaAsset(path,'image','character' if index==0 else 'other',f'@{{image-{index}.png}}'))
        self.controller.assets=list(images)
        self.controller.remember_images(images)
        while self.controller.assets:self.controller.update_asset(0,remove=True)
        self.assertEqual(len(self.controller.state['removed_assets']),20)
        self.assertEqual(len(self.controller.image_library.rows()),22)
        for image in images:
            image.path.unlink()
        restarted=self.new_controller()
        try:
            self.assertEqual(len(restarted.image_library.rows()),22)
            restarted.state.update(mode='全模态参考',manual_prompt=True,prompt='图片参考')
            restarted.add_library_images([row['library_id'] for row in restarted.image_library.rows()])
            self.pump_until(lambda:not restarted.busy,timeout=10)
            self.assertEqual(len(restarted.assets),22)
            self.assertTrue(all(image.path.is_file() for image in restarted.assets))
            self.assertEqual({image.handle for image in restarted.assets},{image.handle for image in images})
            self.assertTrue(all(image_copy(row) for row in restarted.image_library.rows()))
            self.assertEqual([call['operation'] for call in restarted.history.calls()],['restore_images'])
            self.assertFalse(restarted.history.rows())
        finally:restarted.close()

    def test_image_import_creates_local_copy_and_undo_survives_deleting_the_original(self):
        path=self.directory/'角色.png'
        picture=QImage(300,300,QImage.Format.Format_RGB32);picture.fill(0xff4499aa);picture.save(str(path))
        self.controller.import_files([str(path)],'character');self.pump_until(lambda:not self.controller.busy)
        self.assertEqual(len(self.controller.image_library.rows()),1)
        row=self.controller.image_library.rows()[0];copy=Path(row['snapshot_path']);self.assertEqual(copy.read_bytes(),path.read_bytes())
        self.controller.update_asset(0,remove=True);path.unlink();self.controller.restore_removed_asset()
        self.assertEqual(self.controller.assets[0].path,copy)
        self.assertEqual(self.controller.assets[0].handle,'@{角色.png}')

    def test_image_library_recovers_old_history_and_does_not_duplicate_or_overwrite_same_named_images(self):
        paths=[]
        for index in range(2):
            folder=self.directory/str(index);folder.mkdir();path=folder/'角色.png'
            picture=QImage(300,300,QImage.Format.Format_RGB32);picture.fill(0xff113355+index);picture.save(str(path));paths.append(path)
        old=MediaAsset(paths[0],'image','character','@{角色.png}')
        call=self.controller.history.begin_call('import',{'files':[str(paths[0])]})
        self.controller.history.finish_call(call,response={'assets':manifest([old])})
        # Recreate a pre-library environment: only history knows about this image.
        self.controller.image_library.path.unlink()
        restarted=self.new_controller()
        try:
            self.assertEqual(len(restarted.image_library.rows()),1)
            restarted.assets=[old];restarted.remember_images([old])
            other=MediaAsset(paths[1],'image','scene','@{角色.png}')
            restarted.remember_images([other]);self.assertEqual(len(restarted.image_library.rows()),2)
            restarted.state.update(mode='全模态参考',manual_prompt=True)
            restarted.add_library_images([row['library_id'] for row in restarted.image_library.rows()])
            self.pump_until(lambda:not restarted.busy)
            self.assertEqual(len(restarted.assets),2)
            self.assertEqual(len({a.handle for a in restarted.assets}),2)
            self.assertEqual(restarted.assets[0].path,paths[0])
            self.assertNotEqual(restarted.assets[0].path.read_bytes(),restarted.assets[1].path.read_bytes())
        finally:restarted.close()

    def test_image_library_panel_multiselect_all_and_missing_files_preserve_generation_settings(self):
        from image_library import image_copy
        for index,role in enumerate(('character','scene')):
            path=self.directory/f'image-{index}.png'
            picture=QImage(300,300,QImage.Format.Format_RGB32);picture.fill(0xff553311+index);picture.save(str(path))
            self.controller.remember_images([MediaAsset(path,'image',role,f'@{{image-{index}.png}}')])
        self.controller.state.update(mode='纯文本',prompt='保留我的提示词',brief='保留我的提示词')
        window=self.make_window();window.navigate(3);panel=window.image_library
        self.assertEqual(panel.table.rowCount(),2);self.assertTrue(panel.all_button.isEnabled())
        for row in range(2):
            for column in range(5):panel.table.item(row,column).setSelected(True)
        panel.selection_changed();self.assertIn('2',panel.add_button.text())
        window.add_from_image_library(panel.selected_ids());self.pump_until(lambda:not self.controller.busy)
        self.assertEqual(len(self.controller.assets),2);self.assertFalse(any(a.selected for a in self.controller.assets))
        self.assertEqual(self.controller.state['mode'],'纯文本')
        self.assertEqual(self.controller.state['prompt'],'保留我的提示词')
        window.navigate(3)
        record=self.controller.image_library.rows()[0]
        image_copy(record).unlink();Path(record['path']).unlink()
        panel.refresh();self.assertEqual(panel.table.item(0,4).text(),'文件缺失')
        panel.table.clearSelection();panel.table.selectRow(0);self.assertFalse(panel.add_button.isEnabled())
        panel.filter.setCurrentIndex(2);self.assertEqual(panel.table.rowCount(),1);self.assertFalse(panel.all_button.isEnabled())

    def test_image_library_keeps_the_saved_version_when_original_changes(self):
        path=self.directory/'角色.png'
        picture=QImage(300,300,QImage.Format.Format_RGB32);picture.fill(0xff553311);picture.save(str(path))
        self.controller.assets=[MediaAsset(path,'image','character','@{角色.png}')]
        self.controller.remember_images(self.controller.assets)
        record=self.controller.image_library.rows()[0]
        original_bytes=path.read_bytes()
        picture.fill(0xff991133);picture.save(str(path))
        self.controller.state['mode']='全模态参考'
        self.controller.add_library_images([record['library_id']]);self.pump_until(lambda:not self.controller.busy)
        self.assertEqual(len(self.controller.assets),2)
        self.assertEqual(self.controller.assets[1].path.read_bytes(),original_bytes)
        self.assertNotEqual(self.controller.assets[0].handle,self.controller.assets[1].handle)


    def test_full_api_error_is_audited_shown_and_exportable_after_failure(self):
        window = self.make_window()
        raw = json.dumps({'error': {'code': 'InvalidParameter.TaskTypeConstraint',
                                   'message': 'invalid duration ' + ('details ' * 800) + 'LAST-DETAIL'}}).encode()
        trace = {**response_snapshot(raw, {'x-request-id': 'trace-ui'}, 400),
                 'phase': 'http_response', 'endpoint': 'https://example.com/api/v3/contents/generations/tasks',
                 'method': 'POST', 'timeout_seconds': 180}
        client = Mock()
        client.create_task.side_effect = SeedanceError('接口返回 HTTP 400：参数不符合要求 dummy-key', diagnostics=trace)
        with patch.object(self.controller, 'seedance_client', return_value=client):
            window.generate()
            self.pump_until(lambda: not self.controller.busy)
        call = self.controller.history.calls()[0]
        self.assertEqual(call['status'], 'failed')
        self.assertEqual(json.loads(call['request_json'])['timeout_seconds'], 180)
        self.assertIn('LAST-DETAIL', call['response_json'])
        self.assertTrue(window.error_result.isVisible())
        self.assertFalse(window.dialogs)
        window.show_current_error()
        dialog = window.dialogs[-1]
        self.assertIsInstance(dialog, ErrorDetailsDialog)
        self.assertIn('InvalidParameter.TaskTypeConstraint', dialog.summary.toPlainText())
        self.assertIn('trace-ui', dialog.summary.toPlainText())
        self.assertIn('LAST-DETAIL', dialog.response.toPlainText())
        dialog.copy()
        self.assertIn('LAST-DETAIL', self.application.clipboard().text())
        self.assertNotIn('dummy-key', self.application.clipboard().text())
        destination = self.directory / 'diagnosis.txt'
        with patch('error_details_view.QFileDialog.getSaveFileName', return_value=(str(destination), '')):
            dialog.export()
        self.assertIn('LAST-DETAIL', destination.read_text(encoding='utf-8'))
        client.create_task.assert_called_once()

    def test_cloud_failed_task_preserves_full_error_and_query_remains_successful(self):
        self.controller.history.add('failed-cloud-task', collect_payload(self.controller.state, []))
        self.controller.state['task_id'] = 'failed-cloud-task'
        window = self.make_window()
        task = {'id': 'failed-cloud-task', 'status': 'failed',
                'error': {'code': 'InputVideoSensitiveContentDetected', 'message': ('detail '*300)+'LAST-CAUSE'}}
        client = Mock()
        client.get_task.return_value = task
        client.last_response_diagnostics = response_snapshot(json.dumps(task).encode(), {'x-request-id':'cloud-trace'}, 200)
        with patch.object(self.controller, 'seedance_client', return_value=client):
            self.controller.query()
            self.pump_until(lambda: not self.controller.query_busy)
        self.assertIn('LAST-CAUSE', self.controller.row()['error'])
        self.assertEqual(self.controller.row()['status'], 'failed')
        call = self.controller.history.calls()[0]
        self.assertEqual(call['status'], 'succeeded')
        self.assertFalse(window.dialogs)
        window.show_current_error()
        dialog = window.dialogs[-1]
        self.assertIn('云端生成视频', dialog.summary.toPlainText())
        self.assertIn('查询已完成，云端生成失败', dialog.summary.toPlainText())
        self.assertIn('HTTP 状态码：200', dialog.summary.toPlainText())
        self.assertIn('LAST-CAUSE', dialog.response.toPlainText())
        self.assertFalse(window.generation_bar.isVisible())

    def test_prompt_tools_only_appear_on_creation_tab(self):
        window = self.make_window()
        for index in range(5):
            window.editor_pages.setCurrentIndex(index)
            self.application.processEvents()
            for item in (window.ai_button, window.mention_button, window.keyframe_button,
                         window.prompt_count, window.prompt_more):
                self.assertEqual(item.isVisible(), index == 0, (index, item.objectName()))
        window.editor_pages.setCurrentIndex(0)
        window.navigate(1)
        self.assertFalse(window.ai_button.isVisible())

    def test_changed_reference_image_and_prompt_submit_without_automatic_analysis(self):
        self.controller.assets = [
            MediaAsset(None,'video','other','@{reference.mp4}','https://example.com/reference.mp4',10),
            MediaAsset(None,'image','character','@{character.png}','https://example.com/old.png'),
        ]
        self.controller.state.update(mode='全模态参考',manual_prompt=False,auto_analyze=False,
                                     prompt='使用 @{character.png} 替换 @{reference.mp4} 的角色')
        self.controller.state['prompt_draft']={'analysis_signature':'old-analysis','frame_count':0}
        self.controller.assets[1].source_url='https://example.com/replacement.png'
        window=self.make_window()
        window.prompt.setPlainText('使用 @{character.png}，保留 @{reference.mp4} 的动作，镜头固定')
        current_prompt=window.prompt.toPlainText()
        client=Mock();client.create_task.return_value='direct-reference'
        client.get_task.return_value={'id':'direct-reference','status':'running'}
        self.assertFalse(window.controls['auto_analyze'].isChecked())
        self.assertEqual(window.generate_button.text(),'生成视频 · 付费')
        with patch('studio_controller.OpenAIPromptClient') as gpt, \
             patch('video_analysis.extract_reference_frames') as extraction, \
             patch.object(self.controller,'seedance_client',return_value=client):
            window.generate()
            self.pump_until(lambda:self.controller.row('direct-reference') and
                            not self.controller.busy and not self.controller.query_busy)
            gpt.assert_not_called();extraction.assert_not_called()
        self.controller._poll.stop()
        payload=client.create_task.call_args[0][0]
        self.assertEqual(payload['content'][1]['image_url']['url'],'https://example.com/replacement.png')
        self.assertEqual(payload['content'][-1]['video_url']['url'],'https://example.com/reference.mp4')
        self.assertEqual(self.controller.state['prompt'],current_prompt)
        self.assertEqual({r['operation'] for r in self.controller.history.calls()},{'create','query'})

    def test_optional_analysis_does_not_bypass_missing_mentions_or_parameter_validation(self):
        self.controller.state.update(mode='全模态参考',auto_analyze=False,prompt='使用 @{removed.png}')
        with self.assertRaises(ValueError):collect_payload(self.controller.state,[])
        self.controller.state.update(prompt='镜头固定',duration='3')
        with self.assertRaises(ValueError):collect_payload(self.controller.state,[])
        self.controller.state.update(duration='4')
        self.controller.assets=[MediaAsset(None,'video','competitor','@{analysis-only.mp4}',
                                          'https://example.com/source.mp4',8,False)]
        self.controller.state['prompt']='使用 @{analysis-only.mp4}'
        with self.assertRaisesRegex(ValueError,'仅用于分析'):collect_payload(self.controller.state,self.controller.assets)

    def test_background_query_keeps_editor_stable_and_cannot_end_optimization(self):
        self.controller.history.add('active-video', collect_payload(self.controller.state, []))
        self.controller.state['task_id'] = 'active-video'
        window = self.make_window()
        query_gate, optimize_gate = threading.Event(), threading.Event()
        client = Mock()
        def query(_task):
            query_gate.wait(3)
            return {'id': 'active-video', 'status': 'running'}
        client.get_task.side_effect = query
        changes = []
        self.controller.busy_changed.connect(changes.append)
        try:
            with patch.object(self.controller, 'seedance_client', return_value=client):
                self.controller.query()
                self.assertTrue(self.controller.query_busy)
                self.assertFalse(self.controller.busy)
                self.assertTrue(window.ai_button.isEnabled())
                self.assertTrue(window.prompt.isEnabled())
                self.assertTrue(window.parameter_panel.isEnabled())
                self.assertFalse(window.job_progress.isVisible())
                self.assertTrue(window.generate_button.isEnabled())
                self.assertEqual(window.generate_button.text(), '生成视频 · 付费')
                window.prompt.setPlainText('准备下一条提示词')
                self.controller.query()
                self.assertEqual(client.get_task.call_count, 1)
                self.assertTrue(self.controller.start_job('draft', {}, lambda: optimize_gate.wait(3), lambda _value: None))
                foreground_call = self.controller.active_call
                self.assertFalse(window.ai_button.isEnabled())
                query_gate.set()
                self.pump_until(lambda: not self.controller.query_busy)
                self.controller._poll.stop()
                self.assertTrue(self.controller.busy)
                self.assertEqual(self.controller.active_call, foreground_call)
                self.assertFalse(window.ai_button.isEnabled())
                self.assertEqual(changes, [True])
                optimize_gate.set()
                self.pump_until(lambda: not self.controller.busy)
                self.assertEqual(changes, [True, False])
                self.assertTrue(window.ai_button.isEnabled())
                self.assertEqual(window.prompt.toPlainText(), '准备下一条提示词')
        finally:
            query_gate.set(); optimize_gate.set()

    def test_next_generation_is_rejected_without_replacing_active_task(self):
        self.controller.history.add('active-video', collect_payload(self.controller.state, []))
        self.controller.state['task_id'] = 'active-video'
        window = self.make_window()
        self.controller._poll.start(5000)
        with patch.object(self.controller, 'seedance_client') as factory:
            with self.assertRaisesRegex(ValueError, '不会自动排队'):
                self.controller.generate()
            factory.assert_not_called()
        self.assertEqual(self.controller.state['task_id'], 'active-video')
        self.assertTrue(self.controller._poll.isActive())
        self.assertEqual(self.controller.history.calls(), [])
        self.assertTrue(window.generate_button.isEnabled())
        self.assertEqual(window.generate_button.text(), '生成视频 · 付费')
        self.assertTrue(window.ai_button.isEnabled())
        self.controller.history.update({'id': 'active-video', 'status': 'failed'})
        window.refresh_generation()
        self.assertTrue(window.generate_button.isEnabled())

    def test_video_finished_during_optimization_is_still_saved_automatically(self):
        self.controller.history.add('completed-during-draft', collect_payload(self.controller.state, []))
        self.controller.state['task_id'] = 'completed-during-draft'
        self.make_window()
        query_gate, optimize_gate = threading.Event(), threading.Event()
        client = Mock()
        def query(_task):
            query_gate.wait(3)
            return {'id': 'completed-during-draft', 'status': 'succeeded',
                    'content': {'video_url': 'https://example.com/result.mp4'}}
        client.get_task.side_effect = query
        def download(_url, path):
            Path(path).write_bytes(b'saved-video')
        try:
            with patch.object(self.controller, 'seedance_client', return_value=client), \
                 patch('studio_controller.download_video', side_effect=download) as downloader, \
                 patch('studio_controller.video_metadata', return_value={'duration':4}):
                self.controller.query()
                self.controller.start_job('draft', {}, lambda: optimize_gate.wait(3), lambda _value: None)
                foreground_call = self.controller.active_call
                query_gate.set()
                self.pump_until(lambda: not self.controller.query_busy)
                self.assertEqual(self.controller.row()['status'], 'succeeded')
                self.assertTrue(self.controller.busy)
                self.assertEqual(self.controller.active_call, foreground_call)
                self.assertTrue(self.controller._poll.isActive())
                downloader.assert_not_called()
                optimize_gate.set()
                self.pump_until(lambda: not self.controller.busy)
                self.controller._poll.stop()
                self.controller.resume()
                self.pump_until(lambda: bool(self.controller.row()['local_path']) and not self.controller.busy)
                downloader.assert_called_once()
                self.assertTrue(Path(self.controller.row()['local_path']).is_file())
                client.create_task.assert_not_called()
        finally:
            query_gate.set(); optimize_gate.set()


if __name__ == "__main__":
    unittest.main()
