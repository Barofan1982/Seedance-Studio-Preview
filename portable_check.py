"""Offline executable checks; never load the creator's profile or call paid APIs."""
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path


def main():
    index = sys.argv.index('--self-test')
    output = Path(sys.argv[index + 1]).resolve()
    report = {'frozen': bool(getattr(sys, 'frozen', False)), 'checks': {}}
    try:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            os.environ['APPDATA'] = str(home / 'profile')
            os.environ['QT_QPA_PLATFORM'] = 'offscreen'
            from PySide6.QtCore import QLibraryInfo, QUrl
            from PySide6.QtGui import QFont, QImage
            from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
            from PySide6.QtWidgets import QApplication
            from app import StudioWindow
            from media_assets import _run_media_tool, video_metadata, sample_video_frames
            from secure_store import load_protected_json, save_protected_json
            from studio_controller import StudioController
            from ui_language import install_chinese_translation
            application = QApplication.instance() or QApplication([])
            application.setStyle('Fusion'); application.setFont(QFont('Microsoft YaHei UI', 10))
            install_chinese_translation(application)
            bundle = Path(sys._MEIPASS) if report['frozen'] else Path(__file__).parent
            report['checks']['resources'] = all((bundle / 'resources' / name).is_file() for name in ('check.svg', 'chevron.svg'))
            report['checks']['chinese_translation'] = bool(application._studio_chinese_translators)
            report['checks']['media_tools'] = all(_run_media_tool([name, '-version']).returncode == 0 for name in ('ffmpeg', 'ffprobe'))
            movie = home / '视频测试.mp4'
            _run_media_tool(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=purple:s=480x854:r=30',
                             '-t', '4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(movie)])
            info = video_metadata(movie)
            report['checks']['video_metadata'] = info['width'] == 480 and info['height'] == 854 and abs(info['duration'] - 4) < .1
            frames = sample_video_frames(movie, max_frames=3)
            report['checks']['extract_frames'] = len(frames) == 3 and all(not QImage(str(path)).isNull() for _, path in frames)
            frame_root = frames[0][1].parent
            player = QMediaPlayer(); sink = QVideoSink(); player.setVideoSink(sink)
            seen = []
            sink.videoFrameChanged.connect(lambda frame: seen.append(frame.isValid()))
            player.setSource(QUrl.fromLocalFile(str(movie))); player.play()
            deadline = time.monotonic() + 12
            while not any(seen) and player.error() == QMediaPlayer.Error.NoError and time.monotonic() < deadline:
                application.processEvents(); time.sleep(.02)
            report['checks']['video_playback'] = any(seen)
            report['playback_error'] = player.errorString()
            player.stop(); player.setSource(QUrl()); application.processEvents()
            encrypted = home / 'encrypted.bin'
            save_protected_json(encrypted, {'test': 'offline-check'})
            report['checks']['windows_encryption'] = load_protected_json(encrypted) == {'test': 'offline-check'} and b'offline-check' not in encrypted.read_bytes()
            controller = StudioController(home / 'studio')
            window = StudioWindow(controller, start_services=False)
            window.show(); window.fit_to_screen(); window.resize(1280, 800); application.processEvents()
            report['checks']['window'] = not window.isMaximized() and window.rect().contains(window.generate_button.mapTo(window, window.generate_button.rect().bottomRight()))
            controller.state['prompt'] = '独立便携版保存测试'; controller.save()
            report['checks']['save_draft'] = json.loads(controller.state_file.read_text(encoding='utf-8'))['prompt'] == '独立便携版保存测试'
            report['checks']['clean_profile'] = not controller.api_key and not controller.openai_key and not controller.analysis_account() and not controller.history.rows()
            window.close(); application.processEvents()
            if frame_root.is_relative_to(Path(tempfile.gettempdir())):
                shutil.rmtree(frame_root)
            report['success'] = report['frozen'] and all(report['checks'].values())
    except Exception:
        report['success'] = False
        report['error'] = traceback.format_exc()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['success'] else 1
