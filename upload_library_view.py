"""Reusable uploaded videos, including visibly unavailable expired records."""
import datetime
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHeaderView, QHBoxLayout, QLineEdit,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from studio_widgets import StudioComboBox, button, label
from upload_library import record_status
from video_upload import expiry_timestamp


def date_text(timestamp):
    return datetime.datetime.fromtimestamp(timestamp).strftime('%m-%d %H:%M') if timestamp else '未知'


class UploadLibraryPanel(QWidget):
    def __init__(self, controller, add, renew, parent=None):
        super().__init__(parent)
        self.controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索上传的视频文件名')
        self.filter = StudioComboBox()
        for text, value in (('全部记录','all'), ('可使用','available'), ('已失效','unavailable')):
            self.filter.addItem(text, value)
        tools.addWidget(self.search, 1)
        tools.addWidget(self.filter)
        tools.addWidget(button('刷新', 'Quiet', callback=self.refresh))
        layout.addLayout(tools)
        self.summary = label('', 'Small')
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(['文件名','时长','大小','上传时间','到期时间','状态'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for index in range(1, 6):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        self.notice = label('', 'Small', True)
        layout.addWidget(self.notice)
        actions = QHBoxLayout()
        self.renew_button = button('重新上传', 'Soft', 'upload', lambda: renew(self.selected_id()))
        self.add_button = button('添加到当前创作', 'Primary', 'plus', lambda: add(self.selected_id()))
        actions.addWidget(self.renew_button)
        actions.addStretch()
        actions.addWidget(self.add_button)
        layout.addLayout(actions)
        self.search.textChanged.connect(self.refresh)
        self.filter.currentIndexChanged.connect(self.refresh)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.cellDoubleClicked.connect(lambda _row, _column: add(self.selected_id()) if self.add_button.isEnabled() else None)
        controller.busy_changed.connect(lambda _busy: self.selection_changed())
        self.refresh()

    def selected_id(self):
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else ''

    def refresh(self, *_args):
        identity = self.selected_id()
        records = self.controller.upload_library.rows()
        now = time.time()
        available = sum(record_status(row, now)[0] == 'available' for row in records)
        self.summary.setText(f'{len(records)} 条上传记录 · {available} 条可使用 · 有效期按中转返回时间更新')
        text, filter_value = self.search.text().strip().lower(), self.filter.currentData()
        rows = []
        for record in records:
            name = record.get('handle', '').removeprefix('@{').removesuffix('}') or Path(record.get('path', '')).name
            status, status_text = record_status(record, now)
            if text and text not in name.lower(): continue
            if filter_value == 'available' and status != 'available': continue
            if filter_value == 'unavailable' and status == 'available': continue
            rows.append((record, name, status, status_text))
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        selected = -1
        for row_index, (record, name, status, status_text) in enumerate(rows):
            duration = record.get('duration')
            values = (name, f'{duration:g} 秒' if duration else '未知',
                      f'{record.get("upload_file_size", 0)/1024/1024:.1f} MB',
                      date_text(record.get('uploaded_at', 0)), date_text(expiry_timestamp(record.get('source_expires_at'))), status_text)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record['upload_id'])
                    item.setToolTip(name)
                if column in {3,4}: item.setToolTip(value + '（本地时间）')
                if status != 'available':
                    item.setForeground(QColor('#a5aab5'))
                    item.setBackground(QColor('#f7f8fa'))
                elif column == 5:
                    item.setForeground(QColor('#23956e'))
                self.table.setItem(row_index, column, item)
            self.table.setRowHeight(row_index, 62)
            if record['upload_id'] == identity: selected = row_index
        self.table.blockSignals(False)
        if selected >= 0: self.table.selectRow(selected)
        elif rows: self.table.selectRow(0)
        self.selection_changed()

    def selection_changed(self):
        identity = self.selected_id()
        record = self.controller.upload_library.get(identity) if identity else None
        active = bool(record and record_status(record)[0] == 'available')
        self.add_button.setEnabled(active and not self.controller.busy)
        path = Path(record['path']) if record and record.get('path') else None
        self.renew_button.setEnabled(bool(path and path.is_file()) and not self.controller.busy)
        if not record:
            self.notice.setText('上传后自动入库；从附件移除也可在有效期内再次添加。')
        elif active:
            self.notice.setText('直接复用已上传地址，无需再次上传。')
        else:
            self.notice.setText('地址已失效或即将到期，不能调用；重新上传后可继续使用。')
