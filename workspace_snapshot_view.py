"""Browse and restore saved workspaces without submitting a generation task."""
import datetime
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QHeaderView, QAbstractItemView
from studio_widgets import button, label

TZ = datetime.timezone(datetime.timedelta(hours=8))


class WorkspaceSnapshotDialog(QDialog):
    def __init__(self, controller, restore, parent=None):
        super().__init__(parent)
        self.controller, self.restore = controller, restore
        self.setWindowTitle('创作快照 · 恢复素材与提示词')
        self.resize(920, 760)
        self.setMinimumSize(700, 540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addWidget(label('创作快照', 'Section'))
        layout.addWidget(label('清空前自动保存；恢复前也会保存当前内容。恢复后由你决定何时生成。', 'Muted', True))
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索提示词或素材文件名')
        row.addWidget(self.search, 1)
        row.addWidget(button('刷新', 'Quiet', callback=self.refresh))
        layout.addLayout(row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['保存时间', '内容', '素材', '保存原因'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(150)
        layout.addWidget(self.detail, 1)
        actions = QHBoxLayout()
        self.count = label('', 'Small')
        actions.addWidget(self.count, 1)
        self.restore_button = button('恢复这份创作', 'Primary', callback=self.restore_selected)
        actions.addWidget(self.restore_button)
        actions.addWidget(button('关闭', 'Quiet', callback=self.reject))
        layout.addLayout(actions)
        self.search.textChanged.connect(self.render)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        controller.changed.connect(self.controller_changed)
        controller.busy_changed.connect(self.update_busy)
        self.refresh()

    def controller_changed(self, category):
        if category == 'workspace_snapshots':
            self.refresh()

    def update_busy(self, *_args):
        row = self.table.currentRow()
        self.restore_button.setEnabled(not self.controller.busy and 0 <= row < len(self.visible)
                                       and not self.visible[row].get('error'))

    def refresh(self):
        self.records = self.controller.workspace_snapshots.rows()
        self.render()

    def render(self):
        term = self.search.text().strip().casefold()
        self.visible = [record for record in self.records if not term or term in
                        (record['state'].get('prompt', '') + ' ' + record['state'].get('brief', '') + ' ' +
                         ' '.join(a.get('handle', '') for a in record['assets'])).casefold()]
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.visible))
        for index, record in enumerate(self.visible):
            date = datetime.datetime.fromtimestamp(record['created_at'], TZ).strftime('%m-%d %H:%M:%S') if record.get('created_at') else '无法读取'
            prompt = record['state'].get('prompt', '').strip() or record['state'].get('brief', '').strip()
            title = record.get('error') or (' '.join(prompt.split())[:65] if prompt else '仅素材')
            counts = ' · '.join(f'{sum(a["kind"] == kind for a in record["assets"])} {name}'
                                for kind, name in (('video', '视频'), ('image', '图片'), ('audio', '音频')))
            for column, value in enumerate((date, title, counts, record.get('reason', ''))):
                self.table.setItem(index, column, QTableWidgetItem(value))
        self.count.setText(f'{len(self.visible)} 份快照 · 本地保留，重复快照共用素材副本')
        if self.visible:
            self.table.selectRow(0)
        else:
            self.detail.setPlainText('暂无匹配的创作快照。清空当前创作前会自动保存。')
        self.update_busy()

    def selection_changed(self):
        index = self.table.currentRow()
        if not 0 <= index < len(self.visible):
            self.update_busy()
            return
        record = self.visible[index]
        state = record['state']
        prompt = state.get('prompt', '').strip() or state.get('brief', '').strip()
        text = record.get('error') or '提示词\n' + (prompt or '（空）') + '\n\n素材\n' + ('\n'.join(
            ('已勾选 ' if a.get('selected', True) else '未勾选 ') + a.get('handle', '') for a in record['assets']) or '（无）')
        warnings = self.controller.workspace_snapshots.warnings(record) if not record.get('error') else []
        if warnings:
            text += '\n\n恢复提醒\n' + '\n'.join(warnings)
        self.detail.setPlainText(text)
        self.update_busy()

    def restore_selected(self):
        index = self.table.currentRow()
        if 0 <= index < len(self.visible) and not self.controller.busy:
            self.restore(self.visible[index]['snapshot_id'], self)
