import json
import http.client
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, Mock, patch

from seedance_client import SeedanceClient, SeedanceError, SubmissionUncertain, build_payload, estimated_cost_usd, model_alias


class FakeHandler(BaseHTTPRequestHandler):
    requests = []
    force_no_account = False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((self.command, self.path, self.headers.get("Authorization"), body))
        if self.force_no_account:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"No eligible Seedance accounts"}}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"id":"cgt-demo"}')

    def do_GET(self):
        self.requests.append((self.command, self.path, self.headers.get("Authorization"), None))
        if self.path.endswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/api/v3/contents/generations/tasks/other")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"id":"cgt-demo","status":"succeeded","usage":{"completion_tokens":100000}}')

    def log_message(self, _format, *_args):
        pass


class ProxyTrapHandler(BaseHTTPRequestHandler):
    requests = 0

    def do_POST(self):
        type(self).requests += 1
        self.send_response(502)
        self.end_headers()

    def log_message(self, _format, *_args):
        pass


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        FakeHandler.requests.clear()
        FakeHandler.force_no_account = False

    def test_model_mapping_and_reference_payload(self):
        self.assertEqual(model_alias("720p", False), "seedance-2.5-480p-720p-no-video")
        self.assertEqual(model_alias("1080p", True), "seedance-2.5-1080p-with-video")
        payload = build_payload(
            prompt="  一只小猫散步  ",
            duration=5,
            resolution="1080p",
            ratio="16:9",
            image_url="https://example.com/cat.png",
            video_url="https://example.com/motion.mp4",
        )
        self.assertEqual(payload["model"], "seedance-2.5-1080p-with-video")
        self.assertEqual(payload["content"][1]["role"], "reference_image")
        self.assertEqual(payload["content"][2]["role"], "reference_video")
        self.assertEqual(payload["omni_reference_task_type"], "reference")

    def test_http_requires_explicit_opt_in(self):
        base = f"http://127.0.0.1:{self.server.server_port}/api/v3"
        with self.assertRaisesRegex(ValueError, "会明文传输"):
            SeedanceClient(base, "dummy")

    def test_create_network_failures_keep_cause_and_do_not_retry(self):
        payload = build_payload(prompt="猫", duration=4, resolution="480p", ratio="9:16")
        client = SeedanceClient("https://example.com/api/v3", "dummy-key")
        for transport_error in (urllib.error.URLError(ConnectionResetError(10054, "connection reset dummy-key")),
                                http.client.RemoteDisconnected("no response")):
            with self.subTest(error=type(transport_error).__name__):
                opener = Mock()
                opener.open.side_effect = transport_error
                with patch("seedance_client.urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(SubmissionUncertain) as failure:
                        client.create_task(payload)
                detail = failure.exception.diagnostics
                self.assertEqual(detail["phase"], "opening_connection_or_sending_request")
                self.assertIsNone(detail["http_status"])
                self.assertEqual(detail["timeout_seconds"], 300)
                self.assertEqual(detail["request_body_bytes"], len(json.dumps(payload,ensure_ascii=False).encode("utf-8")))
                self.assertNotIn("dummy-key", json.dumps(detail))
                self.assertEqual(opener.open.call_count, 1)

    def test_create_partial_response_keeps_received_status_and_request_id(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers = {"x-request-id": "trace-123"}
        response.read.side_effect = http.client.IncompleteRead(b'{"id":', 30)
        opener = Mock()
        opener.open.return_value = response
        client = SeedanceClient("https://example.com/api/v3", "dummy-key")
        payload = build_payload(prompt="猫", duration=4, resolution="480p", ratio="9:16")
        with patch("seedance_client.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(SubmissionUncertain) as failure:
                client.create_task(payload)
        detail = failure.exception.diagnostics
        self.assertEqual(detail["phase"], "reading_response")
        self.assertEqual(detail["http_status"], 200)
        self.assertEqual(detail["response_ids"]["x-request-id"], "trace-123")
        self.assertEqual(detail["error_type"], "IncompleteRead")
        self.assertEqual(opener.open.call_count, 1)

    def test_create_and_query_use_documented_paths_and_same_key(self):
        base = f"http://127.0.0.1:{self.server.server_port}/api/v3"
        client = SeedanceClient(base, "dummy", allow_http=True)
        payload = build_payload(prompt="花园里的猫", duration=5, resolution="720p", ratio="16:9")
        task_id = client.create_task(payload)
        task = client.get_task(task_id)
        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(
            [(method, path, auth) for method, path, auth, _ in FakeHandler.requests],
            [
                ("POST", "/api/v3/contents/generations/tasks", "Bearer dummy"),
                ("GET", "/api/v3/contents/generations/tasks/cgt-demo", "Bearer dummy"),
            ],
        )
        self.assertAlmostEqual(estimated_cost_usd(task, payload["model"]), 1.0)

    def test_gateway_post_ignores_system_http_proxy(self):
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyTrapHandler)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        ProxyTrapHandler.requests = 0
        try:
            base = f"http://127.0.0.1:{self.server.server_port}/api/v3"
            client = SeedanceClient(base, "dummy", allow_http=True)
            payload = build_payload(prompt="花园里的猫", duration=5, resolution="720p", ratio="16:9")
            with patch("urllib.request.getproxies", return_value={"http": f"http://127.0.0.1:{proxy.server_port}"}), \
                 patch("urllib.request.proxy_bypass", return_value=False):
                self.assertEqual(client.create_task(payload), "cgt-demo")
            self.assertEqual(ProxyTrapHandler.requests, 0)
            self.assertEqual(len(FakeHandler.requests), 1)
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=2)

    def test_no_eligible_account_error_names_server_configuration(self):
        FakeHandler.force_no_account = True
        base = f"http://127.0.0.1:{self.server.server_port}/api/v3"
        client = SeedanceClient(base, "dummy", allow_http=True)
        payload = build_payload(prompt="花园里的猫", duration=5, resolution="720p", ratio="16:9")
        with self.assertRaisesRegex(SeedanceError, r"Seedance \(Ark\)") as failure:
            client.create_task(payload)
        self.assertIn(payload["model"], str(failure.exception))
        self.assertIn("中转站管理员", str(failure.exception))

    def test_rejects_bad_reference_and_duration(self):
        with self.assertRaises(ValueError):
            build_payload(prompt="猫", duration=3, resolution="720p", ratio="16:9")
        with self.assertRaises(ValueError):
            build_payload(
                prompt="猫", duration=5, resolution="720p", ratio="16:9", video_url="http://example.com/test.mp4"
            )

    def test_seedance_25_modes_and_supported_options(self):
        text = build_payload(
            prompt="小猫对着镜头打哈欠", duration=5, resolution="720p", ratio="16:9",
            mode="text", watermark=True, output_format="mov", return_last_frame=True,
        )
        self.assertEqual(text["content"], [{"type": "text", "text": "小猫对着镜头打哈欠"}])
        self.assertNotIn("omni_reference_task_type", text)
        self.assertTrue(text["watermark"])
        self.assertEqual(text["output_format"], "mov")
        audio = build_payload(
            prompt="", duration=5, resolution="720p", ratio="16:9", mode="reference",
            audio_sources=["data:audio/mp3;base64,YQ=="],
        )
        self.assertEqual(audio["content"][0]["role"], "reference_audio")
        self.assertEqual(audio["omni_reference_task_type"], "reference")
        frames = build_payload(
            prompt="", duration=5, resolution="720p", ratio="adaptive", mode="first_last_frame",
            first_frame="https://example.com/start.png", last_frame="https://example.com/end.png",
        )
        self.assertEqual([item["role"] for item in frames["content"]], ["first_frame", "last_frame"])
        self.assertNotIn("omni_reference_task_type", frames)

    def test_seedance_25_rejects_incompatible_parameters(self):
        options = dict(prompt="猫", duration=5, resolution="720p", ratio="16:9")
        with self.assertRaisesRegex(ValueError, "随机种子和固定镜头"):
            build_payload(**options, seed=11)
        with self.assertRaisesRegex(ValueError, "自适应"):
            build_payload(**options, mode="first_frame", first_frame="https://example.com/a.png")
        with self.assertRaisesRegex(ValueError, "不能混入"):
            build_payload(**{**options, "ratio": "adaptive"}, mode="first_frame",
                          first_frame="https://example.com/a.png", image_url="https://example.com/b.png")
        with self.assertRaisesRegex(ValueError, "时长必须是 -1"):
            build_payload(**{**options, "ratio": "adaptive"}, task_type="edit",
                          video_url="https://example.com/video.mp4")

    def test_api_does_not_forward_key_on_redirect(self):
        base = f"http://127.0.0.1:{self.server.server_port}/api/v3"
        client = SeedanceClient(base, "dummy", allow_http=True)
        with self.assertRaisesRegex(SeedanceError, "HTTP 302"):
            client.get_task("redirect")
        self.assertEqual(len(FakeHandler.requests), 1)


if __name__ == "__main__":
    unittest.main()
