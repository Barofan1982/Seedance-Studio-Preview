import http.client
import io
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from seedance_client import SeedanceError, download_video


URL = 'https://example.com/result.mp4'
BUILD_OPENER = urllib.request.build_opener


class VideoResponse(io.BytesIO):
    status = 200

    def __init__(self, data=b'complete-video', length=None, url=URL):
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data) if length is None else length)}
        self.url = url

    def geturl(self):
        return self.url


class VideoDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'result.mp4'
        self.opener = Mock()
        self.build = self.enterContext(patch('seedance_client.urllib.request.build_opener', return_value=self.opener))
        self.sleep = self.enterContext(patch('seedance_client.time.sleep'))

    def test_timeout_retries_get_directly_without_generation_request(self):
        self.opener.open.side_effect = [urllib.error.URLError(TimeoutError()), VideoResponse()]
        download_video(URL, self.path)
        self.assertEqual(self.path.read_bytes(), b'complete-video')
        self.assertEqual(self.opener.open.call_count, 2)
        for call in self.build.call_args_list:
            self.assertEqual(call.args[0].proxies, {})
        for call in self.opener.open.call_args_list:
            self.assertEqual(call.kwargs['timeout'], 300)
            request = call.args[0]
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(request.full_url, URL)
            self.assertIsNone(request.data)
            self.assertNotIn('Authorization', request.headers)
        self.sleep.assert_called_once_with(2)

    def test_connection_also_waits_300_seconds_instead_of_api_connect_limit(self):
        self.build.side_effect = BUILD_OPENER
        observed = []
        def timeout(connection):
            observed.append(connection.timeout)
            raise TimeoutError('connection timed out')
        with patch.object(http.client.HTTPSConnection, 'connect', autospec=True, side_effect=timeout):
            with self.assertRaises(SeedanceError) as raised:
                download_video(URL, self.path)
        self.assertEqual(observed, [300, 300])
        self.assertEqual(raised.exception.diagnostics['connect_timeout_seconds'], 300)

    def test_partial_read_disconnect_restarts_file_without_appending(self):
        response = VideoResponse()
        response.read = Mock(side_effect=[b'partial', http.client.RemoteDisconnected('closed')])
        self.opener.open.side_effect = [response, VideoResponse()]
        download_video(URL, self.path)
        self.assertEqual(self.path.read_bytes(), b'complete-video')
        self.assertFalse(self.path.with_name('result.mp4.part').exists())
        self.assertTrue(response.closed)

    def test_truncated_and_empty_responses_are_not_saved_as_success(self):
        for response in (VideoResponse(b'short', length=20), VideoResponse(b'')):
            with self.subTest(response=response):
                self.opener.open.side_effect = [response, VideoResponse()]
                download_video(URL, self.path)
                self.assertEqual(self.path.read_bytes(), b'complete-video')

    def test_retry_limit_preserves_existing_output_and_diagnoses_route(self):
        self.path.write_bytes(b'existing-video')
        self.opener.open.side_effect = urllib.error.URLError(TimeoutError())
        with self.assertRaises(SeedanceError) as raised:
            download_video(URL, self.path)
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(self.path.read_bytes(), b'existing-video')
        self.assertFalse(self.path.with_name('result.mp4.part').exists())
        trace = raised.exception.diagnostics
        self.assertEqual(len(trace['attempts']), 2)
        self.assertEqual(trace['timeout_seconds'], 300)
        self.assertIn('直连', trace['connection_route'])
        self.assertIsNone(trace['http_status'])

    def test_expired_url_is_reported_without_retry(self):
        self.opener.open.side_effect = urllib.error.HTTPError(URL, 403, 'Expired', {}, io.BytesIO(b'expired'))
        with self.assertRaises(SeedanceError) as raised:
            download_video(URL, self.path)
        self.assertEqual(raised.exception.diagnostics['http_status'], 403)
        self.assertEqual(self.opener.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_local_write_failure_does_not_redownload(self):
        self.opener.open.return_value = VideoResponse()
        with patch.object(Path, 'open', side_effect=PermissionError('disk denied')):
            with self.assertRaises(SeedanceError) as raised:
                download_video(URL, self.path)
        self.assertEqual(raised.exception.diagnostics['phase'], 'writing_output')
        self.assertEqual(self.opener.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_unsafe_redirect_is_rejected_without_saving(self):
        self.opener.open.return_value = VideoResponse(url='http://example.com/insecure.mp4')
        with self.assertRaises(ValueError):
            download_video(URL, self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.opener.open.call_count, 1)


if __name__ == '__main__':
    unittest.main()
