"""Quiet Studio palette and small local vector icons."""
from pathlib import Path
from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

ICONS = {
    "spark": '<path d="m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6Z"/><path d="m20 3 .6 1.4L22 5l-1.4.6L20 7l-.6-1.4L18 5l1.4-.6Z"/>',
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/><rect x="14" y="14" width="7" height="7" rx="1.4"/>',
    "film": '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M7 3v18M17 3v18M3 8h4M3 16h4M17 8h4M17 16h4"/><path d="m10 8 5 4-5 4Z"/>',
    "chart": '<path d="M4 4v16h17M8 15v-4M13 15V7M18 15V4"/>',
    "settings": '<path d="m10 3 4 0 1 3 3 1 2-1 2 4-2 2v3l-3 1-1 3h-4l-1-3-3-1-2 1-2-4 2-2V7l3-1Z"/><circle cx="12" cy="11.5" r="3"/>',
    "upload": '<path d="M12 16V3m-5 5 5-5 5 5M4 15v5h16v-5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "more": '<circle cx="5" cy="12" r="1" fill="currentColor"/><circle cx="12" cy="12" r="1" fill="currentColor"/><circle cx="19" cy="12" r="1" fill="currentColor"/>',
    "play": '<path d="m8 4 12 8-12 8Z"/>',
    "pause": '<path d="M8 4v16M16 4v16"/>',
    "download": '<path d="M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="2"/><path d="m3 17 6-6 4 4 3-3 5 5"/>',
    "folder": '<path d="M3 7V4h6l2 3h10v13H3Z"/>',
    "arrow": '<path d="M4 12h15m-6-6 6 6-6 6"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "chevron": '<path d="m6 9 6 6 6-6"/>',
    "close": '<path d="m6 6 12 12M18 6 6 18"/>',
    "sound": '<path d="m11 4-6 5H2v6h3l6 5Z"/><path d="M15 8c3 2 3 6 0 8M18 4c6 5 6 11 0 16"/>',
    "file": '<path d="M5 3h9l5 5v13H5Z"/><path d="M14 3v6h5M8 13h8M8 17h6"/>',
    "copy": '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    "eye": '<path d="M2 12c5-10 15-10 20 0-5 10-15 10-20 0Z"/><circle cx="12" cy="12" r="3"/>',
}


def icon(name, color="#7a8395", size=22):
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" color="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{ICONS.get(name, ICONS["file"])}</svg>'
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


TOOLTIP_STYLE = """
QToolTip { background-color: #ffffff; color: #293548; border: 1px solid #d8dce7; padding: 7px 10px; font-family: "Microsoft YaHei UI"; font-size: 12px; }
"""

COMBO_POPUP_STYLE = TOOLTIP_STYLE + """
QFrame#ComboPopup { background: white; border: 1px solid #e3e5ef; }
QListView { background: white; color: #293548; border: 0; padding: 4px; outline: none; font-family: "Microsoft YaHei UI"; font-size: 13px; }
QListView::item { padding: 7px 10px; }
QListView::item:selected { background: #efebfb; color: #6554a9; }
QScrollBar:vertical { width: 6px; background: white; margin: 2px; }
QScrollBar::handle:vertical { background: #d8dce7; min-height: 30px; border-radius: 3px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: white; }
"""

STYLE = TOOLTIP_STYLE + """
* { font-family: "Microsoft YaHei UI"; font-size: 13px; color: #252b3b; }
QMainWindow, QDialog, QWidget#AppBody { background: #f5f6fa; }
QWidget#Rail { background: #171d2b; }
QLabel#Logo { background: #7364ed; color: white; font-size: 25px; font-weight: 700; border-radius: 13px; }
QLabel#RailCaption { color: #778197; font-size: 10px; }
QPushButton#Nav { background: transparent; border: 0; color: #858fa4; border-radius: 12px; padding: 10px 0; font-size: 12px; }
QPushButton#Nav:checked { background: #2b3149; color: #dcd8ff; }
QPushButton#Nav:hover { background: #252d3e; color: white; }
QLabel#Title { font-size: 23px; font-weight: 700; color: #22283a; }
QLabel#Subtitle { color: #949bac; font-size: 12px; }
QFrame#Card { background: white; border: 1px solid #e8ebf2; border-radius: 15px; }
QFrame#Asset { background: white; border: 1px solid #e8ebf2; border-radius: 12px; }
QFrame#Asset:hover { border-color: #c5beee; background: #fdfcff; }
QFrame#Asset[unavailable="true"] { background: #f7f8fa; border-color: #e4e6eb; }
QFrame#Asset[unavailable="true"] QLabel { color: #a0a6b2; }
QFrame#SubmissionNotice { background: #f7f8fc; border: 1px solid #e1e5ee; border-radius: 10px; }
QFrame#SubmissionNotice[tone="warning"] { background: #fff7e8; border-color: #ecd19d; }
QFrame#SubmissionNotice[tone="good"] { background: #edf7f2; border-color: #c4dfcf; }
QLabel#SubmissionTitle { color: #343a4d; font-size: 14px; font-weight: 600; }
QFrame#SubmissionNotice[tone="warning"] QLabel#SubmissionTitle { color: #855117; }
QLabel#Section { font-size: 15px; font-weight: 700; color: #343a4d; }
QLabel#AssetGroupTitle { font-size: 12px; font-weight: 600; color: #697386; }
QLabel#ProgressStep { color: #a1a7b5; background: #f4f5f8; border-radius: 7px; padding: 6px 2px; font-size: 11px; }
QLabel#ProgressStep[stage="current"] { color: #7662c5; background: #eee9fa; font-weight: 600; }
QLabel#ProgressStep[stage="done"] { color: #408969; background: #edf7f2; }
QLabel#Muted { color: #697386; font-size: 12px; }
QLabel#Small { color: #626d80; font-size: 11px; }
QLabel#Chip { color: #796cbf; background: #f0edfd; padding: 5px 9px; border-radius: 7px; font-size: 11px; }
QLabel#Good { color: #408969; background: #edf7f2; padding: 5px 10px; border-radius: 8px; font-size: 11px; }
QLabel#HistoryStatus { padding: 5px 9px; border-radius: 7px; font-size: 11px; font-weight: 600; }
QLabel#HistoryStatus[tone="good"] { color: #267453; background: #e9f6ef; }
QLabel#HistoryStatus[tone="error"] { color: #ab3346; background: #fdeef0; }
QLabel#HistoryStatus[tone="warning"] { color: #906019; background: #fff3dd; }
QLabel#HistoryStatus[tone="info"] { color: #6555ac; background: #f0edfd; }
QLabel#HistoryStatus[tone="neutral"] { color: #626d80; background: #f0f2f6; }
QLabel#HistoryReason { color: #626d80; font-size: 12px; }
QLabel#HistoryReason[tone="error"] { color: #ab3346; }
QLabel#HistoryReason[tone="warning"] { color: #906019; }
QLabel#Metric { font-size: 27px; font-weight: 700; color: #343a52; }
QLabel#Preview { background: #edf0f6; border-radius: 10px; color: #98a2b6; }
QPushButton { border: 1px solid #e5e7ef; border-radius: 9px; background: white; padding: 9px 14px; color: #636c80; }
QPushButton:hover { background: #f4f3fb; border-color: #cdc6ef; color: #6a59c7; }
QPushButton:pressed { background: #e9e5f8; }
QPushButton:disabled { color: #aeb4c0; background: #f0f2f6; border-color: #e9ecf2; }
QPushButton#Primary { background: #7762d8; border: 1px solid #7762d8; color: white; font-weight: 600; }
QPushButton#Primary:hover { background: #6651c5; border-color: #6651c5; }
QPushButton#Generate { background: #df9464; border: 1px solid #df9464; color: white; padding: 13px; font-size: 14px; font-weight: 600; }
QPushButton#Generate:hover { background: #cf8050; border-color: #cf8050; }
QPushButton#Primary:disabled, QPushButton#Generate:disabled { background: #c5c9d3; border-color: #c5c9d3; color: #f8f9fb; }
QPushButton#Quiet { border: 0; background: transparent; color: #858da0; padding: 6px 8px; }
QPushButton#Quiet:hover { background: #f0edfa; color: #7361c4; }
QPushButton#Soft { background: #f1effc; border: 0; color: #7765bd; }
QPushButton#Save { background: #ecf6f0; border: 0; color: #3f8868; }
QPushButton#Icon { border: 0; background: transparent; padding: 5px; border-radius: 7px; }
QPushButton#Icon:hover { background: #f0edf8; }
QPushButton#Tab { background: transparent; border: 0; color: #9299a9; padding: 8px 13px; border-radius: 7px; }
QPushButton#Tab:checked { background: #f0edfb; color: #7662c5; font-weight: 600; }
QComboBox, QLineEdit { background: #f8f9fc; border: 1px solid #edf0f5; border-radius: 8px; padding: 9px 10px; min-height: 15px; }
QComboBox:focus, QLineEdit:focus { border-color: #b7ade5; background: white; }
QComboBox::drop-down { border: 0; width: 24px; }
QComboBox QAbstractItemView { background: white; selection-background-color: #efebfb; selection-color: #6554a9; border: 1px solid #e3e5ef; padding: 4px; }
QTextEdit, QPlainTextEdit { border: 0; background: white; padding: 8px; font-size: 14px; placeholder-text-color: #667085; selection-background-color: #e4ddfa; selection-color: #453775; }
QTextEdit#Brief { background: #f8f9fc; border: 1px solid #edf0f5; border-radius: 10px; font-size: 13px; padding: 8px; }
QTextEdit#Prompt { color: #293548; background: #fbfcff; border: 1px solid #dce2ed; border-radius: 10px; padding: 12px; font-size: 15px; }
QTextEdit#Prompt:focus { border-color: #a596db; background: white; }
QTextEdit#JSON { font-family: "Consolas"; font-size: 12px; }
QCheckBox { color: #768094; spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #d9deea; background: white; border-radius: 4px; }
QCheckBox::indicator:checked { border-color: #8170d0; background: #8170d0; image: none; }
QCheckBox:disabled { color: #aeb4c0; }
QScrollArea { border: 0; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { width: 6px; background: transparent; margin: 2px; }
QScrollBar::handle:vertical { background: #d8dce7; min-height: 35px; border-radius: 3px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { height: 6px; background: transparent; }
QScrollBar::handle:horizontal { background: #d8dce7; min-width: 35px; border-radius: 3px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QMenu { background: white; border: 1px solid #e7e8f0; padding: 7px; border-radius: 10px; }
QMenu::item { padding: 9px 23px; border-radius: 6px; color: #606a80; }
QMenu::item:selected { background: #f0edf9; color: #715db8; }
QMenu::separator { background: #eef0f5; height: 1px; margin: 5px 9px; }
QListView#MentionPicker { background: white; color: #293548; border: 1px solid #d8d0ee; border-radius: 8px; padding: 5px; font-size: 14px; outline: none; }
QListView#MentionPicker::item { padding: 9px 12px; border-radius: 5px; }
QListView#MentionPicker::item:selected { background: #eee9fb; color: #6246b3; }
QTableWidget { border: 0; background: white; gridline-color: #f1f3f8; selection-background-color: #f0edfa; selection-color: #6755ac; }
QHeaderView::section { background: #fafbfe; border: 0; padding: 12px; color: #9ba2b0; font-size: 12px; }
QTreeWidget { border: 1px solid #e8ebf2; border-radius: 9px; background: white; selection-background-color: #f0edfa; selection-color: #6755ac; }
QTreeWidget::item { padding: 6px 2px; }
QTabWidget::pane { border: 0; margin-top: 7px; }
QTabBar::tab { background: transparent; color: #9299a9; padding: 8px 13px; border-radius: 7px; }
QTabBar::tab:selected { background: #f0edfb; color: #7662c5; font-weight: 600; }
QTableWidget::item { padding: 9px; border-bottom: 1px solid #f1f3f8; }
QProgressBar { border: 0; background: #eceaf4; border-radius: 2px; max-height: 4px; }
QProgressBar::chunk { background: #9786dd; border-radius: 2px; }
QSplitter::handle { background: transparent; }
QSlider::groove:horizontal { background: #e9e8f1; height: 4px; border-radius: 2px; }
QSlider::handle:horizontal { background: #8370cf; width: 12px; margin: -4px 0; border-radius: 6px; }
"""

_resources = (Path(__file__).parent / "resources").as_posix()
STYLE += f"""
QComboBox::down-arrow {{ image: url("{_resources}/chevron.svg"); width: 10px; height: 10px; }}
QCheckBox::indicator:checked {{ image: url("{_resources}/check.svg"); }}
"""
