from __future__ import annotations

import hashlib
import datetime
import json
import math
import re
from pathlib import Path
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QUrl, QStringListModel, Signal, QTimer
from PySide6.QtGui import QColor, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPixmap, QSyntaxHighlighter, QTextBlockFormat, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QCompleter, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QMenu,
    QPushButton, QScrollArea, QSlider, QTabWidget, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QSizePolicy,
    QComboBox, QListView, QProxyStyle, QStyle, QStyleFactory, QStyledItemDelegate,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from media_assets import thumbnail
from request_records import compact_record
from studio_controller import PURPOSE
from studio_theme import STYLE, COMBO_POPUP_STYLE, icon
from ui_language import FIELD_NAMES, error_text, parameter_value
from video_upload import needs_upload, source_expired, expiry_timestamp


def label(text="", name="", wrap=False):
    result = QLabel(text)
    result.setObjectName(name)
    result.setWordWrap(wrap)
    return result


def button(text="", name="Quiet", symbol="", callback=None):
    result = QPushButton(text)
    result.setObjectName(name)
    if symbol:
        result.setIcon(icon(symbol, "white" if name in {"Primary", "Generate"} else "#837792"))
        result.setIconSize(QSize(18, 18))
    if callback:
        result.clicked.connect(callback)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    return result


def card():
    result = QFrame()
    result.setObjectName("Card")
    return result


def scroll(content):
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(content)
    return area


class ElideLabel(QLabel):
    def __init__(self, text="", name=""):
        super().__init__()
        self.full_text = text
        self.setObjectName(name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setToolTip(text)
        super().setText(text)

    def setText(self, text):
        self.full_text = str(text)
        self.setToolTip(self.full_text)
        self._fit()

    def _fit(self):
        super().setText(self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideMiddle, max(20, self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def sizeHint(self):
        return QSize(110, super().sizeHint().height())

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())


def cached_thumbnail(path: Path | None, directory: Path) -> QPixmap:
    if not path or not path.is_file():
        return QPixmap()
    digest = hashlib.sha256((str(path) + str(path.stat().st_mtime_ns)).encode()).hexdigest()[:18]
    destination = directory / "thumbnails" / f"{digest}.png"
    try:
        if not destination.exists():
            thumbnail(path, destination)
        return QPixmap(str(destination))
    except Exception:
        return QPixmap()


class Preview(QWidget):
    clicked = None

    def __init__(self, pixmap=None, height=144, callback=None, *, placeholder="导入参考视频", play_overlay=True):
        super().__init__()
        self.pixmap = pixmap or QPixmap()
        self.callback = callback
        self.placeholder = placeholder
        self.play_overlay = play_overlay
        self.setMinimumHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(height)
        if callback:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 10, 10)
        painter.setClipPath(clip)
        painter.fillRect(self.rect(), QColor("#eef0f6"))
        if not self.pixmap.isNull():
            size = self.pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            x, y = (self.width() - size.width()) // 2, (self.height() - size.height()) // 2
            painter.drawPixmap(x, y, self.pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            painter.setPen(QColor("#adb5c7"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
        if self.callback and self.play_overlay:
            painter.setBrush(QColor(35, 38, 57, 180))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(self.rect().center(), 18, 18)
            icon("play", "white", 17).paint(painter, self.width() // 2 - 8, self.height() // 2 - 9, 18, 18)

    def mousePressEvent(self, event):
        if self.callback and event.button() == Qt.MouseButton.LeftButton:
            self.callback()


class GenerationCanvas(QWidget):
    """An animated waiting frame; its animation deliberately does not imply a completion percentage."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Expanding)
        self.active=False
        self.tick=0
        self.tone='busy'
        self.timer=QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.animate)

    def animate(self):
        self.tick+=1
        self.update()

    def set_active(self,active,tone='busy'):
        self.active=active
        self.tone=tone
        if active and self.isVisible():
            self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def showEvent(self,event):
        if self.active:
            self.timer.start()
        super().showEvent(event)

    def hideEvent(self,event):
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self,event):
        painter=QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect=QRectF(self.rect()).adjusted(1,1,-1,-1)
        shape=QPainterPath();shape.addRoundedRect(rect,16,16)
        painter.setClipPath(shape)
        painter.fillRect(rect,QColor('#f3f0fc' if self.tone!='warning' else '#fff6e9'))
        shift=(self.tick%160)/160*self.width()*2-self.width()
        gradient=QLinearGradient(shift,0,shift+self.width(),self.height())
        gradient.setColorAt(0,QColor(255,255,255,0));gradient.setColorAt(.5,QColor(255,255,255,170));gradient.setColorAt(1,QColor(255,255,255,0))
        painter.fillRect(rect,gradient)
        center=QPointF(self.width()/2,self.height()/2-8)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#e6def8' if self.tone!='warning' else '#f7e3c4'))
        painter.drawEllipse(center,43,43)
        for i in range(8):
            angle=2*math.pi*i/8+self.tick*.025
            color=QColor('#a794d5' if self.tone!='warning' else '#dca466');color.setAlpha(65+24*i)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(center.x()+math.cos(angle)*60,center.y()+math.sin(angle)*60),3.5,3.5)
        icon('spark' if self.tone!='warning' else 'file','#8771c5',36).paint(painter,int(center.x()-18),int(center.y()-18),36,36)
        painter.setPen(QColor('#8b80a6'))
        painter.drawText(QRectF(0,center.y()+75,self.width(),24),Qt.AlignmentFlag.AlignCenter,'视频生成')


class MentionHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text):
        import re
        style = QTextCharFormat()
        style.setForeground(QColor("#7b63bd"))
        style.setBackground(QColor("#f4f0fd"))
        for match in re.finditer(r"@\{[^}]+\}|@(?:图片|视频|音频)\d+", text):
            self.setFormat(match.start(), match.end() - match.start(), style)


class ComboListStyle(QProxyStyle):
    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_ComboBox_Popup:
            return 0
        return super().styleHint(hint, option, widget, returnData)


class StudioComboBox(QComboBox):
    """Use a bounded list with an explicit light popup, including its outer frame."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._list_style = ComboListStyle(QStyleFactory.create("Fusion"))
        self._list_style.setParent(self)
        self.setStyle(self._list_style)
        self.setView(QListView(self))
        self.view().setItemDelegate(QStyledItemDelegate(self.view()))
        self.view().setStyleSheet(COMBO_POPUP_STYLE)
        self.view().setUniformItemSizes(True)
        self.setMaxVisibleItems(8)
        self.view().setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view().setTextElideMode(Qt.TextElideMode.ElideMiddle)
        popup = self.view().window()
        popup.setObjectName("ComboPopup")
        popup.setStyleSheet(COMBO_POPUP_STYLE)
        popup.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)


class PromptEditor(QTextEdit):
    asset_chosen = Signal(str)

    def __init__(self):
        super().__init__()
        self._updating_mentions = False
        self.setObjectName("Prompt")
        self.setAcceptRichText(False)
        # QTextEdit clips multiline placeholders to the empty paragraph height.
        self.setPlaceholderText("写下创作要求，直接生成或先用智能优化。")
        self.highlighter = MentionHighlighter(self.document())
        self.completer = QCompleter(self)
        self.completer.setWidget(self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setMaxVisibleItems(8)
        self.completer.popup().setObjectName("MentionPicker")
        # QCompleter owns a top-level popup, outside the main window's stylesheet.
        self.completer.popup().setStyleSheet(STYLE)
        self.completer.popup().setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.mention_model = QStringListModel(self.completer)
        self.completer.setModel(self.mention_model)
        self.completer.activated[str].connect(self.insert_completion)
        self.handles = {}
        self.mention_start = None
        self.textChanged.connect(self.update_mentions)
        self.cursorPositionChanged.connect(self.update_mentions)

    def setPlainText(self, text):
        self._updating_mentions = True
        try:
            self.completer.popup().hide()
            self.mention_start = None
            super().setPlainText(text)
            cursor = self.textCursor()
            cursor.select(QTextCursor.SelectionType.Document)
            paragraph = QTextBlockFormat()
            paragraph.setLineHeight(150.0, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            cursor.mergeBlockFormat(paragraph)
            cursor.clearSelection()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.setTextCursor(cursor)
        finally:
            self._updating_mentions = False

    def set_assets(self, assets):
        # Handles include a suffix for duplicate filenames, so each entry stays distinct.
        self.handles = {a.handle.removeprefix("@{").removesuffix("}"): a.handle
                        for a in assets if a.purpose != "competitor" and not (a.upload_id and source_expired(a))}
        self.mention_model.setStringList(list(self.handles))
        self.update_mentions()

    def insert_completion(self, name):
        handle = self.handles.get(name)
        if not handle or self.mention_start is None:
            return
        cursor = self.textCursor()
        self._updating_mentions = True
        try:
            cursor.setPosition(self.mention_start)
            cursor.setPosition(self.textCursor().position(), QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(handle + " ")
            self.setTextCursor(cursor)
            self.mention_start = None
            self.asset_chosen.emit(handle)
        finally:
            self._updating_mentions = False
        self.completer.popup().hide()

    def update_mentions(self):
        if self._updating_mentions:
            return
        popup = self.completer.popup()
        if not self.isEnabled() or self.isReadOnly() or not (self.hasFocus() or popup.isVisible()):
            popup.hide()
            return
        cursor = self.textCursor()
        if cursor.hasSelection():
            popup.hide()
            return
        position = cursor.position()
        if any(start <= position < end for start, end in self.reference_spans()):
            self.mention_start = None
            popup.hide()
            return
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        before = cursor.selectedText()
        at = before.rfind("@")
        prefix = before[at + 1:].removeprefix("{") if at >= 0 else ""
        if at < 0 or "}" in prefix or len(prefix) > 150 or not self.handles:
            self.mention_start = None
            popup.hide()
            return
        # QTextCursor positions use UTF-16 units, including two units for emoji.
        self.mention_start = position - len(before[at:].encode("utf-16-le")) // 2
        self.completer.setCompletionPrefix(prefix)
        if not self.completer.completionCount():
            popup.hide()
            return
        self.completer.setCurrentRow(0)
        popup.setCurrentIndex(self.completer.currentIndex())
        rect = self.cursorRect()
        rect.setWidth(min(self.viewport().width(), max(280, min(460, popup.sizeHintForColumn(0) + 36))))
        self.completer.complete(rect)

    def reference_spans(self):
        text = self.toPlainText()
        for match in re.finditer(r"@\{[^{}\r\n]+\}|@(?:图片|视频|音频)\d+", text):
            start = len(text[:match.start()].encode("utf-16-le")) // 2
            end = start + len(match.group().encode("utf-16-le")) // 2
            yield start, end

    def expanded_reference_selection(self, cursor):
        selected = QTextCursor(cursor)
        start, end = selected.selectionStart(), selected.selectionEnd()
        overlaps = [(left, right) for left, right in self.reference_spans() if left < end and right > start]
        if overlaps:
            selected.setPosition(min(start, overlaps[0][0]))
            selected.setPosition(max(end, overlaps[-1][1]), QTextCursor.MoveMode.KeepAnchor)
        return selected, bool(overlaps)

    def cut(self):
        if self.isReadOnly():
            return
        updating = self._updating_mentions
        self._updating_mentions = True
        self.completer.popup().hide()
        self.mention_start = None
        try:
            cursor, _ = self.expanded_reference_selection(self.textCursor())
            self.setTextCursor(cursor)
            super().cut()
        finally:
            self._updating_mentions = updating

    def delete_selection(self):
        if self.isReadOnly() or not self.textCursor().hasSelection():
            return
        updating = self._updating_mentions
        self._updating_mentions = True
        self.completer.popup().hide()
        self.mention_start = None
        try:
            cursor, _ = self.expanded_reference_selection(self.textCursor())
            cursor.beginEditBlock()
            cursor.removeSelectedText()
            cursor.endEditBlock()
            self.setTextCursor(cursor)
        finally:
            self._updating_mentions = updating

    def createStandardContextMenu(self, *args):
        menu = super().createStandardContextMenu(*args)
        callbacks = {"edit-cut": self.cut, "edit-delete": self.delete_selection}
        for action in menu.actions():
            callback = callbacks.get(action.objectName())
            if callback:
                action.triggered.disconnect()
                action.triggered.connect(callback)
        return menu

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        menu.exec(event.globalPos())
        menu.deleteLater()

    def keyPressEvent(self, event):
        popup = self.completer.popup()
        if event.matches(QKeySequence.StandardKey.Cut):
            self.cut()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and not event.matches(QKeySequence.StandardKey.Undo):
            updating = self._updating_mentions
            self._updating_mentions = True
            popup.hide()
            self.mention_start = None
            try:
                cursor = self.textCursor()
                if not cursor.hasSelection():
                    word = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                    if event.key() == Qt.Key.Key_Backspace:
                        operation = QTextCursor.MoveOperation.PreviousWord if word else QTextCursor.MoveOperation.PreviousCharacter
                    else:
                        operation = QTextCursor.MoveOperation.NextWord if word else QTextCursor.MoveOperation.NextCharacter
                    cursor.movePosition(operation, QTextCursor.MoveMode.KeepAnchor)
                expanded, overlaps = self.expanded_reference_selection(cursor)
                if overlaps and not self.isReadOnly():
                    expanded.beginEditBlock()
                    expanded.removeSelectedText()
                    expanded.endEditBlock()
                    self.setTextCursor(expanded)
                else:
                    super().keyPressEvent(event)
            finally:
                self._updating_mentions = updating
            event.accept()
            return
        if popup.isVisible():
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                self.insert_completion(popup.currentIndex().data())
                event.accept()
                return
            if event.key() == Qt.Key.Key_Escape:
                popup.hide()
                event.accept()
                return
            if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                model = self.completer.completionModel()
                offset = 1 if event.key() == Qt.Key.Key_Down else -1
                row = (popup.currentIndex().row() + offset) % model.rowCount()
                popup.setCurrentIndex(model.index(row, 0))
                popup.scrollTo(popup.currentIndex())
                event.accept()
                return
        super().keyPressEvent(event)


class VideoDialog(QDialog):
    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(path.name)
        self.resize(1000, 720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        self.video = QVideoWidget()
        self.video.setStyleSheet("background: #141a27; border-radius: 12px;")
        layout.addWidget(self.video, 1)
        controls = QHBoxLayout()
        self.play = button("播放", "Primary", "play", self.toggle)
        controls.addWidget(self.play)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        controls.addWidget(self.slider, 1)
        self.time = label("00:00", "Muted")
        controls.addWidget(self.time)
        layout.addLayout(controls)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.75)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.durationChanged.connect(self.slider.setMaximum)
        self.player.positionChanged.connect(self.position)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.player.errorOccurred.connect(lambda _error, detail: self.time.setText(error_text(detail)))
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()
        self.play.setText("暂停")

    def toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play.setText("播放")
        else:
            self.player.play()
            self.play.setText("暂停")

    def position(self, position):
        self.slider.setValue(position)
        seconds = position // 1000
        self.time.setText(f"{seconds // 60:02}:{seconds % 60:02}")

    def closeEvent(self, event):
        self.player.stop()
        super().closeEvent(event)


class JsonDialog(QDialog):
    def __init__(self, title, value, parent=None, full_value=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(940, 700)
        self.full_value = full_value if full_value is not None else value
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.addWidget(label(title, "Section"))
        self.view = QTextEdit()
        self.view.setObjectName("JSON")
        self.view.setReadOnly(True)
        compact=compact_record(value)
        self.view.setPlainText(json.dumps(compact, ensure_ascii=False, indent=2))
        self.overview=QTreeWidget();self.overview.setHeaderLabels(['项目','内容'])
        self.overview.setColumnWidth(0,220)
        self.overview.setWordWrap(True)
        def append(parent,data):
            items=data.items() if isinstance(data,dict) else ((f'第 {index+1} 项',item) for index,item in enumerate(data))
            for key,item in items:
                title=FIELD_NAMES.get(key,key if any('\u4e00'<=c<='\u9fff' for c in key) else '其他参数')
                group=isinstance(item,(dict,list))
                text=f'{len(item)} 项' if group else parameter_value(item,key)
                node=QTreeWidgetItem(parent,[title,text[:240]+('…' if len(text)>240 else '')])
                if len(text)>240:node.setToolTip(1,text)
                if group:append(node,item)
        if isinstance(compact,(dict,list)):append(self.overview,compact)
        else:QTreeWidgetItem(self.overview,['内容',parameter_value(compact)])
        self.overview.expandToDepth(1)
        self.tabs=QTabWidget();self.tabs.addTab(self.overview,'中文说明');self.tabs.addTab(self.view,'原始参数')
        layout.addWidget(self.tabs, 1)
        tools = QHBoxLayout()
        tools.addWidget(button("复制原始参数", "Quiet", "copy", self.copy))
        tools.addWidget(button("导出完整参数", "Save", "download", self.export))
        tools.addStretch()
        tools.addWidget(button("完成", "Primary", callback=self.accept))
        layout.addLayout(tools)

    def copy(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.view.toPlainText())

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出参数", "生成请求.json", "参数文件 (*.json)")
        if path:
            Path(path).write_text(json.dumps(self.full_value, ensure_ascii=False, indent=2), encoding="utf-8")


class AssetCard(QFrame):
    def __init__(self, asset, index, controller, mention, preview, parent=None):
        super().__init__(parent)
        self.setObjectName("Asset")
        unavailable = bool(asset.upload_id and source_expired(asset))
        self.setProperty('unavailable', unavailable)
        self.setMinimumWidth(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        pixmap = cached_thumbnail(asset.path, controller.directory) if asset.kind != "audio" else QPixmap()
        if asset.purpose == "competitor":
            layout.addWidget(Preview(pixmap, 136, callback=lambda: preview(asset)))
        row = QHBoxLayout()
        row.setSpacing(10)
        if asset.purpose != "competitor":
            image = Preview(pixmap, 58, callback=lambda: preview(asset), placeholder="音频" if asset.kind == "audio" else "图片", play_overlay=asset.kind in {"video", "audio"})
            image.setFixedWidth(58)
            row.addWidget(image)
        text = QVBoxLayout()
        text.setSpacing(6)
        title = ElideLabel(asset.filename)
        title.setStyleSheet("font-weight: 600; font-size: 12px;")
        text.addWidget(title)
        detail = QHBoxLayout()
        detail.setSpacing(5)
        if asset.kind == 'video' and asset.purpose != 'competitor':
            status = '地址已过期' if source_expired(asset) else '待上传' if needs_upload(asset) else '已上传' if asset.upload_id else '网址参考'
            badge = label(status,'Chip')
            if asset.source_expires_at:
                expires = datetime.datetime.fromtimestamp(expiry_timestamp(asset.source_expires_at),
                           datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y年%m月%d日 %H:%M')
                badge.setToolTip('参考地址到期：' + expires + '（北京时间）；过期地址不能调用，需重新上传。')
            detail.addWidget(badge)
        else:
            detail.addWidget(label(PURPOSE.get(asset.purpose, "参考"), "Chip"))
        if asset.duration:
            duration_text = f"{asset.duration:.1f} 秒"
            detail.addWidget(ElideLabel(duration_text, "Small"), 1)
        if asset.purpose == "competitor":
            badge = label("仅分析", "Small")
            badge.setToolTip("本地视频会逐秒抽帧交给智能分析；在素材菜单选择上传并用于生成后，可直接参考视频。")
            detail.addWidget(badge)
        detail.addStretch()
        text.addLayout(detail)
        row.addLayout(text, 1)
        right = QVBoxLayout()
        if asset.purpose != "competitor":
            checkbox = QCheckBox()
            checkbox.setToolTip("用于本次生成")
            checkbox.setChecked(asset.selected)
            checkbox.toggled.connect(lambda selected: controller.update_asset(index, selected=selected))
            right.addWidget(checkbox, alignment=Qt.AlignmentFlag.AlignHCenter)
        more = button("", "Icon", "more")
        more.setFixedSize(24, 24)
        more.setToolTip("素材操作")
        menu = QMenu(more)
        if asset.kind == "video":
            upload = menu.addAction('重新上传视频' if asset.upload_id else '上传并用于生成',
                                    lambda: self.window().upload_assets([index]))
            upload.setEnabled(bool(asset.path and asset.path.is_file()))
            def link_file():
                path, _ = QFileDialog.getOpenFileName(self, "关联本地视频", str(asset.path.parent) if asset.path else "", "视频 (*.mp4 *.mov *.webm *.m4v)")
                if path:
                    try:
                        controller.link_local_video(index, path)
                    except Exception as error:
                        controller.failed.emit(str(error))
            def link_url():
                url, accepted = QInputDialog.getText(self, "关联视频网址", "可公开访问的视频网址（以 https:// 开头）", text=asset.source_url)
                if accepted and url.strip():
                    try:
                        controller.link_video_url(index, url)
                    except Exception as error:
                        controller.failed.emit(str(error))
            menu.addAction("关联本地视频", link_file)
            menu.addAction("关联视频网址", link_url)
            menu.addSeparator()
        if asset.purpose != "competitor":
            menu.addAction("插入 @ 引用", lambda: mention(asset.handle)).setEnabled(not unavailable)
            if asset.kind == "image":
                roles = menu.addMenu("素材用途")
                for purpose in ("character", "scene", "first", "last", "other"):
                    roles.addAction(PURPOSE[purpose], lambda _checked=False, p=purpose: controller.update_asset(index, purpose=p))
            menu.addSeparator()
        menu.addAction("预览", lambda: preview(asset))
        menu.addAction("移除", lambda: controller.update_asset(index, remove=True))
        more.clicked.connect(lambda: menu.exec(more.mapToGlobal(more.rect().bottomLeft())))
        right.addWidget(more)
        row.addLayout(right)
        layout.addLayout(row)
        if asset.kind == 'video' and asset.path and (asset.purpose == 'competitor' or needs_upload(asset)):
            layout.addWidget(button('上传视频','Save','upload',lambda: self.window().upload_assets([index])))


class KeyframeDialog(QDialog):
    def __init__(self, controller, preview, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("选择关键帧")
        self.resize(900, 760)
        self.setMinimumSize(740, 540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        layout.addWidget(label(f"每秒抽帧 · {len(controller.state['frame_catalog'])} 张", "Section"))
        body = QWidget()
        grid = QGridLayout(body)
        grid.setSpacing(12)
        selected = {a.handle for a in controller.assets if a.purpose == "keyframe" and a.selected}
        self.capacity = 30 - sum(a.selected and a.kind == "image" and a.purpose != "keyframe" for a in controller.assets)
        self.choices = {}
        from media_assets import MediaAsset
        for index, frame in enumerate(controller.state["frame_catalog"]):
            tile = card()
            content = QVBoxLayout(tile)
            content.setContentsMargins(10, 10, 10, 10)
            asset = MediaAsset(Path(frame["path"]), "image", "keyframe", frame["handle"])
            content.addWidget(Preview(QPixmap(str(asset.path)), 160, callback=lambda a=asset: preview(a), play_overlay=False))
            content.addWidget(ElideLabel(frame["source_handle"].removeprefix("@{").removesuffix("}"), "Small"))
            choice = QCheckBox(f"{frame['timestamp']:.2f} 秒")
            choice.setChecked(frame["handle"] in selected)
            choice.toggled.connect(self.update_count)
            self.choices[frame["handle"]] = choice
            content.addWidget(choice)
            grid.addWidget(tile, index // 4, index % 4)
        layout.addWidget(scroll(body), 1)
        footer = QHBoxLayout()
        self.summary = label("", "Muted")
        footer.addWidget(self.summary, 1)
        footer.addWidget(button("取消", "Quiet", callback=self.reject))
        self.apply_button = button("应用关键帧", "Save", callback=self.apply)
        footer.addWidget(self.apply_button)
        layout.addLayout(footer)
        self.update_count()

    def update_count(self):
        count = sum(c.isChecked() for c in self.choices.values())
        self.summary.setText(f"已选 {count} 张 · 当前最多 {self.capacity} 张")
        self.apply_button.setEnabled(count <= self.capacity)

    def apply(self):
        try:
            self.controller.apply_keyframes([h for h, c in self.choices.items() if c.isChecked()])
        except ValueError as error:
            self.summary.setText(str(error))
            return
        self.accept()
