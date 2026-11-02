"""Offline subprocess fixture: never use the real Studio profile or network."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget
from single_instance import SingleInstance, activate_window

parser = argparse.ArgumentParser()
parser.add_argument('--profile', type=Path, required=True)
parser.add_argument('--report', type=Path, required=True)
parser.add_argument('--hold-ms', type=int, default=15000)
parser.add_argument('--main', action='store_true')
parser.add_argument('--maximized', action='store_true')
parser.add_argument('--startup-delay-ms', type=int, default=0)
args = parser.parse_args()
report = {'pid':os.getpid(),'activations':0,'controller_created':False}

def save(**updates):
    report.update(updates)
    temporary = args.report.with_suffix('.pending')
    temporary.write_text(json.dumps(report), encoding='utf-8')
    temporary.replace(args.report)

if args.main:
    os.environ['APPDATA'] = str(args.profile.parent)
    import app
    assert app.APP_DIR == args.profile
    original_controller = app.StudioController
    original_show = app.StudioWindow.show
    def controller(*a, **kw):
        save(controller_created=True)
        return original_controller(*a, **kw)
    def show(window):
        original_show(window)
        timer=QTimer(window)
        timer.timeout.connect(lambda: window.close() if args.report.with_suffix('.stop').exists() else None)
        timer.start(50)
        window._probe_stop_timer=timer
        save(owned=True,ready=True)
    app.StudioController = controller
    app.StudioWindow.show = show
    result = app.main(start_services=False, quit_after_ms=args.hold_ms)
    save(owned=report['controller_created'],result=result,finished=True)
    raise SystemExit(result)

application = QApplication([])
guard = SingleInstance(args.profile)
owned = guard.acquire_or_activate()
if not owned:
    save(owned=False,finished=True)
    raise SystemExit(0)
window = QWidget()
if args.maximized:
    window.showMaximized()
window.showMinimized()
def activated():
    activate_window(window)
    save(activations=report['activations']+1,minimized=window.isMinimized(),maximized=window.isMaximized())
guard.activation_requested.connect(activated)
save(owned=True,ready=True,minimized=window.isMinimized(),server=guard.server_name)
QTimer.singleShot(args.hold_ms, application.quit)
stop_timer=QTimer()
stop_timer.timeout.connect(lambda: application.quit() if args.report.with_suffix('.stop').exists() else None)
stop_timer.start(50)
try:
    time.sleep(args.startup_delay_ms / 1000)
    application.exec()
finally:
    guard.close()
