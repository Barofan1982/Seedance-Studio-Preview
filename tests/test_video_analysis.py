import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_assets import MediaAsset, _run_media_tool, image_dimensions
from video_analysis import extract_reference_frames


class ReferenceFrameTests(unittest.TestCase):
    def test_every_second_and_ending_are_persistent_and_valid_image_references(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "scene.mp4"
            _run_media_tool(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                             "testsrc=size=320x480:rate=30", "-t", "2.4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)])
            asset = MediaAsset(video, "video", "other", "@{scene.mp4}", "https://example.com/scene.mp4")
            duration, frames = extract_reference_frames(asset, root / "frames")
            self.assertAlmostEqual(duration, 2.4, places=1)
            self.assertEqual([t for t, _ in frames[:3]], [0, 1, 2])
            self.assertGreater(frames[-1][0], 2.2)
            self.assertEqual(image_dimensions(frames[0][1]), (320, 480))
            with patch("video_analysis._run_media_tool") as process:
                self.assertEqual(extract_reference_frames(asset, root / "frames"), (duration, frames))
                process.assert_not_called()
            with patch("video_analysis._run_media_tool", side_effect=ValueError("source unreadable")):
                video.write_bytes(b"changed-source")
                with self.assertRaisesRegex(ValueError, "source unreadable"):
                    extract_reference_frames(asset, root / "frames")

    def test_too_long_video_is_rejected_before_extracting_images(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "long.mp4"
            video.write_bytes(b"probe-fixture")
            from types import SimpleNamespace
            probe = SimpleNamespace(stdout=json.dumps({"format": {"duration": "31"}, "streams": [
                {"codec_type": "video", "width": 480, "height": 854}]}))
            with patch("video_analysis._run_media_tool", return_value=probe) as run:
                with self.assertRaisesRegex(ValueError, "30 秒"):
                    extract_reference_frames(MediaAsset(video, "video", "competitor", "@{long.mp4}"), root / "frames")
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
