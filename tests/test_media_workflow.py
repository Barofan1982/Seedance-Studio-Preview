import base64
import json
import tempfile
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from media_assets import MediaAsset, add_local_asset, compiled_mentions, image_data_url, sample_video_frames, video_metadata, _run_media_tool
from openai_prompt import OpenAIPromptClient, OpenAIError, _read_response_stream, _response_text
from seedance_client import build_payload


class MediaWorkflowTests(unittest.TestCase):
    @staticmethod
    def _stream(events, final_newline=True):
        value = "\n\n".join("data: " + json.dumps(event, ensure_ascii=False) for event in events)
        if final_newline:
            value += "\n\n"
        return value.encode("utf-8").splitlines(keepends=True)

    def test_streamed_text_is_read_when_completion_does_not_repeat_it(self):
        text = '{"seedance_prompt":"小猫奔跑"}'
        events = [
            {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": text[:10]},
            {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": text[10:]},
            {"type": "response.output_text.done", "output_index": 1, "content_index": 0, "text": text},
            {"type": "response.completed", "response": {"status": "completed", "output": [], "usage": {"total_tokens": 42}}},
        ]
        result = _read_response_stream(self._stream(events, final_newline=False))
        self.assertEqual(_response_text(result), text)
        self.assertEqual(result["usage"]["total_tokens"], 42)

    def test_stream_does_not_accept_partial_text_without_successful_completion(self):
        events = [{"type": "response.output_text.delta", "delta": "partial"}]
        with self.assertRaisesRegex(OpenAIError, "没有完成响应"):
            _read_response_stream(self._stream(events))
        events.append({"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}})
        with self.assertRaisesRegex(OpenAIError, "会员可共享额度"):
            _read_response_stream(self._stream(events))

    def test_stream_reads_finished_message_and_does_not_duplicate_completed_text(self):
        message = {"type": "message", "content": [{"type": "output_text", "text": "完整文字"}]}
        events = [
            {"type": "response.output_text.delta", "delta": "完整文字"},
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {"type": "response.completed", "response": {"status": "completed", "output": []}},
        ]
        self.assertEqual(_response_text(_read_response_stream(self._stream(events))), "完整文字")
        events[-1]["response"]["output"] = [message]
        self.assertEqual(_response_text(_read_response_stream(self._stream(events))), "完整文字")

    @unittest.skipUnless(os.name == "nt", "Windows console behavior")
    def test_all_media_commands_use_no_console_window_and_no_interactive_stdin(self):
        with patch("media_assets.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            _run_media_tool(["ffprobe", "-version"])
        self.assertEqual(run.call_args.kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW, subprocess.CREATE_NO_WINDOW)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_thirty_second_references_and_outputs_can_be_sampled_but_longer_references_are_rejected(self):
        with patch("media_assets.video_duration", return_value=30), patch("media_assets._run_media_tool"):
            import shutil
            for generated in (False, True):
                frames = sample_video_frames(Path("video.mp4"), generated=generated, max_frames=2)
                self.assertEqual(len(frames), 2)
                target = frames[0][1].parent.resolve()
                self.assertTrue(target.is_relative_to(Path(tempfile.gettempdir()).resolve()))
                shutil.rmtree(target)
        with patch("media_assets.video_duration", return_value=30.1):
            with self.assertRaisesRegex(ValueError, "不能超过 30"):
                sample_video_frames(Path("competitor.mp4"))

    def test_local_output_metadata_reports_actual_pixels_duration_and_audio_track(self):
        from types import SimpleNamespace
        result = SimpleNamespace(stdout=json.dumps({"format": {"duration": "8.2"}, "streams": [
            {"codec_type": "video", "width": 720, "height": 1280}, {"codec_type": "audio"}
        ]}))
        with patch("media_assets._run_media_tool", return_value=result):
            info = video_metadata(Path("output.mp4"))
        self.assertEqual(info, {"width": 720, "height": 1280, "duration": 8.2, "has_audio": True})

    def test_refinement_labels_competitor_and_generated_frames_separately_and_includes_target_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference_frames = root / "reference_frames"
            output_frames = root / "output_frames"
            reference_frames.mkdir()
            output_frames.mkdir()
            ref = reference_frames / "frame.jpg"
            out = output_frames / "frame.jpg"
            ref.write_bytes(b"reference-jpeg")
            out.write_bytes(b"output-jpeg")
            competitor = MediaAsset(root / "competitor.mp4", "video", "competitor", "@{competitor.mp4}", duration=12, selected=False)
            response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
                "analysis": "成片角色与素材不一致", "shots": [], "seedance_prompt": "改写提示词", "uncertainties": []
            })}]}]}
            with patch("openai_prompt.sample_video_frames", side_effect=[[(0, ref)], [(1, out)]]), \
                 patch.object(OpenAIPromptClient, "_post", return_value=response) as post:
                OpenAIPromptClient("fake-key").draft(competitor, [], "保持角色一致", transcribe_audio=False,
                    generation_settings={"duration": 8, "generate_audio": False},
                    generated_video=root / "output.mp4", previous_prompt="旧提示词")
            body = json.loads(post.call_args.args[1])
            text = "\n".join(item.get("text", "") for item in body["input"][0]["content"])
            self.assertIn('"duration": 8', text)
            self.assertIn("上一版提示词：旧提示词", text)
            self.assertIn("参考视频 0.0 秒画面", text)
            self.assertIn("上一版生成成片 1.0 秒画面", text)

    def test_local_images_and_filename_mentions_reach_seedance(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "角色.png"
            image.write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/nXcAAAAASUVORK5CYII="
            ))
            with patch("media_assets.image_dimensions", return_value=(300, 300)):
                character = add_local_asset(image, "character", [])
            scene = MediaAsset(None, "image", "scene", "@{背景.jpg}", "https://example.com/scene.jpg")
            compiled, images, videos, audios = compiled_mentions(
                "以 @{角色.png} 为主角，在 @{背景.jpg} 中奔跑", [character, scene]
            )
            self.assertEqual(compiled, "以 @图片1 为主角，在 @图片2 中奔跑")
            self.assertEqual(videos, [])
            self.assertEqual(audios, [])
            payload = build_payload(
                prompt=compiled, duration=5, resolution="720p", ratio="16:9",
                image_sources=[image_data_url(images[0].path), images[1].source_url],
            )
            self.assertEqual(payload["model"], "seedance-2.5-480p-720p-no-video")
            self.assertTrue(payload["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
            self.assertEqual(payload["content"][2]["image_url"]["url"], "https://example.com/scene.jpg")

    def test_unselected_mention_is_rejected(self):
        image = MediaAsset(Path("character.png"), "image", "character", "@{character.png}", selected=False)
        with self.assertRaisesRegex(ValueError, "未选中"):
            compiled_mentions("使用 @{character.png}", [image])
        with self.assertRaisesRegex(ValueError, "只勾选了 0"):
            compiled_mentions("使用 @图片1", [])

    def test_openai_draft_uses_local_frame_as_image_and_no_direct_competitor_mention(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(b"mock-jpeg")
            competitor = MediaAsset(Path(directory) / "参考视频.mp4", "video", "competitor", "@{参考视频.mp4}", duration=12.0, selected=False)
            response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
                "analysis": "动作与节奏", "shots": [], "seedance_prompt": "一个角色奔跑", "uncertainties": []
            })}]}]}
            requests = []

            def fake_post(_self, _url, data, _content_type, timeout=180):
                requests.append(json.loads(data))
                return response

            with patch("openai_prompt.sample_video_frames", return_value=[(0.0, frame)]), \
                 patch.object(OpenAIPromptClient, "_post", fake_post):
                draft = OpenAIPromptClient("dummy-key").draft(competitor, [], "替换角色", transcribe_audio=False)
            self.assertEqual(draft.prompt, "一个角色奔跑")
            content = requests[0]["input"][0]["content"]
            self.assertTrue(any(item.get("type") == "input_image" for item in content))
            self.assertIn("不能在 seedance_prompt 里写它的 @文件名", content[0]["text"])
            self.assertFalse(requests[0]["store"])

    def test_chatgpt_plan_draft_streams_without_api_only_fields(self):
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "analysis": "简要分析", "shots": [], "seedance_prompt": "一只猫奔跑", "uncertainties": []
        })}]}]}
        requests = []

        def fake_stream(_self, body, timeout=300):
            requests.append(body)
            return response

        with patch.object(OpenAIPromptClient, "_stream_response", fake_stream):
            draft = OpenAIPromptClient("oauth-token", chatgpt_plan=True).draft(None, [], "一只猫奔跑", transcribe_audio=False)
        self.assertEqual(draft.prompt, "一只猫奔跑")
        self.assertTrue(requests[0]["stream"])
        self.assertFalse(requests[0]["store"])
        self.assertNotIn("max_output_tokens", requests[0])

    def test_direct_video_reference_preserves_user_mapping_without_inventing_frames(self):
        video = MediaAsset(None, "video", "other", "@{washface.mp4}", "https://example.com/washface.mp4", 13.4)
        brief = "完整参考 @{washface.mp4}，女性猫咪改为 Sera，最后男性改为 Jace。"
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "analysis": "保留原片，女主→Sera，结尾男性→Jace", "shots": [], "seedance_prompt": brief, "uncertainties": []
        })}]}]}
        with patch.object(OpenAIPromptClient, "_stream_response", return_value=response) as stream:
            draft = OpenAIPromptClient("fake-token", chatgpt_plan=True).draft(None, [video], brief, transcribe_audio=False)
        body = stream.call_args.args[0]
        content = body["input"][0]["content"]
        self.assertEqual(len(content), 1)
        text = content[0]["text"]
        self.assertIn(brief, text)
        self.assertIn("reference_video", text)
        self.assertIn("AI 本次没有该视频的抽帧", text)
        self.assertNotIn("不能在 seedance_prompt 里写它的 @文件名", text)
        self.assertEqual(draft.prompt, brief)

    def test_prepared_frames_are_sent_to_chatgpt_and_kept_for_seedance(self):
        with tempfile.TemporaryDirectory() as folder:
            frame = Path(folder) / "frame.jpg"
            frame.write_bytes(b"vision-image")
            catalog = [{"path": str(frame), "timestamp": float(second), "source_handle": "@{washface.mp4}",
                        "handle": f"@{{washface-{second:02}.00s.jpg}}"} for second in range(15)]
            characters = [MediaAsset(frame, "image", "character", f"@{{{name}.png}}") for name in ("Jace", "Sera")]
            response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
                "analysis": "男主洗脸", "shots": [], "seedance_prompt": "男主使用 Jace", "uncertainties": [],
                "keyframe_handles": [catalog[0]["handle"]]
            })}]}]}
            with (patch.object(OpenAIPromptClient, "_stream_response", return_value=response) as stream,
                  patch("openai_prompt.sample_video_frames") as sampler,
                  patch("openai_prompt._scaled_jpeg_data_url", return_value="data:image/jpeg;base64,cm9sZQ==")):
                draft = OpenAIPromptClient("fake-token", chatgpt_plan=True).draft(None,
                    [MediaAsset(None, "video", "other", "@{washface.mp4}", "https://example.com/washface.mp4", 15.0), *characters],
                    "替换角色", reference_frames=catalog)
            sampler.assert_not_called()
            content = stream.call_args.args[0]["input"][0]["content"]
            self.assertEqual(sum(item["type"] == "input_image" for item in content), 17)
            self.assertIn("0.00 秒", content[1]["text"])
            self.assertIn(catalog[0]["handle"], content[1]["text"])
            self.assertIn("14.00 秒", content[29]["text"])
            self.assertIn("Sera.png", content[-2]["text"])
            self.assertIn("全部均已提供，包括结尾", content[0]["text"])
            self.assertNotIn("最多直接查看前 12 张", content[0]["text"])
            self.assertNotIn("未分析其内容", content[0]["text"])
            self.assertNotIn("AI 本次没有该视频的抽帧", content[0]["text"])
            self.assertEqual(draft.keyframe_handles, [catalog[0]["handle"]])
            self.assertTrue(frame.is_file())


if __name__ == "__main__":
    unittest.main()
