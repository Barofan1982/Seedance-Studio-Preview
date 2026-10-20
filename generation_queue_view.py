"""A compact stack of queued creations, separate from prompt tools and history."""
from __future__ import annotations

import json
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QInputDialog, QMenu, QTextEdit, QVBoxLayout, QWidget
from generation_queue import ACTIVE, STATUS_NAMES
from studio_widgets import button, card, label, scroll, JsonDialog
from studio_controller import neutral_video_text
from ui_language import choice_text, error_text
from error_details_view import ErrorDetailsDialog


def settings_text(settings):
    seconds = settings.get('duration')
    return ' · '.join([settings.get('resolution',''), '自适应' if seconds == -1 else f'{seconds} 秒',
                      choice_text(settings.get('ratio','')), '有声' if settings.get('generate_audio') else '无声'])


class GenerationQueuePanel(QWidget):
    def __init__(self, controller, edit, show_dialog, show_error, parent=None):
        super().__init__(parent)
        self.controller, self.edit, self.show_dialog, self.show_error = controller, edit, show_dialog, show_error
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0,0,0,0); outer.setSpacing(12)
        top = QHBoxLayout()
        self.count = label('', 'Section'); top.addWidget(self.count,1)
        self.toggle = button('继续队列 · 付费','Primary',callback=self.toggle_queue)
        top.addWidget(self.toggle); outer.addLayout(top)
        self.notice = label('', 'Small',True); outer.addWidget(self.notice)
        self.show_past = QCheckBox('显示历史任务')
        self.show_past.toggled.connect(self.refresh)
        outer.addWidget(self.show_past)
        self.content = QWidget(); self.items = QVBoxLayout(self.content)
        self.items.setContentsMargins(0,0,3,0); self.items.setSpacing(10)
        self.area = scroll(self.content); outer.addWidget(self.area,1)
        self.refresh()

    def action(self, callback):
        try:
            callback()
        except Exception as error:
            self.show_error(str(error),call_id=False)

    def toggle_queue(self):
        self.action(self.controller.pause_queue if self.controller.queue_enabled else self.controller.start_queue)

    def refresh(self):
        position = self.area.verticalScrollBar().value()
        while self.items.count():
            item = self.items.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        rows = self.controller.queue.rows()
        waiting = [r for r in rows if r['status'] == 'waiting']
        active = [r for r in rows if r['status'] in ACTIVE]
        self.count.setText(f'待提交 {len(waiting)} · 进行中 {len(active)}')
        self.toggle.setText('暂停队列' if self.controller.queue_enabled else '继续队列 · 付费')
        self.toggle.setEnabled(bool(waiting or active))
        self.notice.setText(self.controller.queue_reason or
                           ('按顺序生成，保存当前成片后自动提交下一条。' if self.controller.queue_enabled else
                            '每条保留加入时的提示词、素材和参数。'))
        past = [r for r in rows if r['status'] in {'failed','succeeded','cancelled','unknown'}][::-1]
        uncertain = sum(r['status'] == 'unknown' for r in past)
        self.show_past.setText(f'显示历史任务（{len(past)} 条' + (f'，{uncertain} 条未收到回执）' if uncertain else '）'))
        self.show_past.setVisible(bool(past))
        display = active + waiting + (past if self.show_past.isChecked() else [])
        for item in display:
            identity = item['queue_id']
            panel = card(); layout = QVBoxLayout(panel)
            layout.setContentsMargins(14,12,14,12); layout.setSpacing(8)
            heading = QHBoxLayout()
            title = f'任务 #{identity}'
            if item['status'] == 'waiting': title += f' · 第 {waiting.index(item)+1} 条'
            heading.addWidget(label(title,'Section'),1)
            badge = label(STATUS_NAMES[item['status']],'HistoryStatus')
            badge.setProperty('tone', 'good' if item['status']=='succeeded' else 'warning' if item['status']=='unknown' else
                              'error' if item['status']=='failed' else 'info' if item['status'] in ACTIVE else 'neutral')
            heading.addWidget(badge)
            more = button('', 'Icon','more')
            more.clicked.connect(lambda _checked=False, row=item, anchor=more:self.menu(row,anchor))
            heading.addWidget(more); layout.addLayout(heading)
            prompt = label(neutral_video_text(item['summary']) or '无文字提示词','',True)
            prompt.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(prompt)
            layout.addWidget(label(settings_text(json.loads(item['settings_json'])),'Small',True))
            if item['error']:
                error = label(error_text(neutral_video_text(item['error']))[:220],'Small',True)
                error.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(error)
            self.items.addWidget(panel)
        if not display:
            empty = label('暂无待处理任务\n在创作页点击“生成视频”即可开始。','Muted',True)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter); empty.setMinimumHeight(180)
            self.items.addWidget(empty)
        self.items.addStretch()
        self.area.verticalScrollBar().setValue(position)

    def menu(self, item, anchor):
        identity = item['queue_id']
        menu = QMenu(self)
        menu.addAction('查看提示词与素材', lambda:self.action(lambda:self.details(identity)))
        menu.addAction('预览提交参数', lambda:self.action(lambda:self.show_dialog(
            JsonDialog('队列任务 · 提交参数', self.controller.queue.get(identity)['snapshot']['payload'],self))))
        if item['call_id']:
            menu.addAction('调用与错误详情',lambda:self.show_dialog(ErrorDetailsDialog(
                [r for r in self.controller.history.calls(limit=None) if r['call_id']==item['call_id'] or item['task_id'] and r['task_id']==item['task_id']],self)))
        if item['status'] == 'waiting':
            menu.addSeparator()
            menu.addAction('取消排队并编辑',lambda:self.action(lambda:self.edit(identity)))
            menu.addAction('取消这条',lambda:self.action(lambda:self.controller.remove_queue_item(identity)))
        elif item['status'] in {'failed','succeeded','cancelled','unknown'}:
            menu.addAction('载入再编辑',lambda:self.action(lambda:self.edit(identity)))
        if item['status'] == 'waiting':
            menu.addAction('上移',lambda:self.action(lambda:self.controller.move_queue_item(identity,-1)))
            menu.addAction('下移',lambda:self.action(lambda:self.controller.move_queue_item(identity,1)))
        if item['status'] == 'unknown':
            menu.addSeparator()
            menu.addAction('关联已接单的任务编号',lambda:self.associate(identity))
        if item['status'] in ACTIVE and item['task_id']:
            menu.addAction('继续查询与保存',lambda:self.action(self.controller.resume))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def details(self, identity):
        item = self.controller.queue.get(identity); snapshot = item['snapshot']
        dialog = QDialog(self); dialog.setWindowTitle(f'队列任务 #{identity}'); dialog.resize(850,700)
        layout = QVBoxLayout(dialog); layout.setContentsMargins(22,20,22,20)
        layout.addWidget(label(STATUS_NAMES[item['status']]+' · '+settings_text(snapshot['settings']),'Section',True))
        layout.addWidget(label('素材：'+('、'.join(a['handle'] for a in snapshot['assets'] if a.get('selected')) or '无'),'Small',True))
        layout.addWidget(label('自动保存到：'+snapshot['state']['download_dir'],'Small',True))
        text = QTextEdit(); text.setReadOnly(True); text.setPlainText(neutral_video_text(snapshot['original_prompt']))
        layout.addWidget(text,1)
        layout.addWidget(button('完成','Primary',callback=dialog.accept),alignment=Qt.AlignmentFlag.AlignRight)
        self.show_dialog(dialog)

    def associate(self, identity):
        value, accepted = QInputDialog.getText(self,'关联已接单的任务','填写中转站确认的任务编号：')
        if accepted:
            self.action(lambda:self.controller.attach_queue_task(identity,value))
