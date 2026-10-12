"""Readable call evidence, with per-call original responses and report export."""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit,
    QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QAbstractItemView,
)
from error_diagnostics import TZ, call_details, call_issue, call_report, json_value
from request_records import compact_record
from studio_widgets import StudioComboBox, button, label


def related_calls(history, record):
    task_id = record.get('task_id')
    if task_id:
        return history.calls(limit=None, task_id=task_id)
    identity = record.get('call_id')
    return [call for call in history.calls(limit=None) if call['call_id'] == identity] if identity else []


class ErrorDetailsDialog(QDialog):
    def __init__(self, calls, parent=None, *, only_errors=False):
        super().__init__(parent)
        self.setWindowTitle('错误详情 · 调用环节与接口返回')
        self.resize(1080, 800)
        self.setMinimumSize(860, 640)
        self.calls = compact_record(calls)
        self.rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        heading.addWidget(label('调用与错误清单', 'Section'), 1)
        self.filter = StudioComboBox()
        self.filter.addItem('全部环节', 'all')
        self.filter.addItem('仅异常记录', 'errors')
        self.filter.setCurrentIndex(1 if only_errors else 0)
        heading.addWidget(self.filter)
        layout.addLayout(heading)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索调用编号、任务编号、错误码或报错内容')
        layout.addWidget(self.search)
        self.count = label('', 'Small')
        layout.addWidget(self.count)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(['编号', '时间', '环节', '结果', 'HTTP 状态', '业务错误码'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(160)
        self.table.setMaximumHeight(225)
        for column, width in enumerate((70, 150, 145, 125, 120)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        self.tabs = QTabWidget()
        self.summary = QTextEdit()
        self.summary.setObjectName('ErrorSummary')
        self.response = QTextEdit()
        self.response.setObjectName('ErrorResponse')
        self.request = QTextEdit()
        self.request.setObjectName('ErrorRequest')
        self.evidence = QTextEdit()
        self.evidence.setObjectName('ErrorEvidence')
        for widget in (self.summary, self.response, self.request, self.evidence):
            widget.setReadOnly(True)
        self.tabs.addTab(self.summary, '环节与原因')
        self.tabs.addTab(self.response, '接口完整返回')
        self.tabs.addTab(self.request, '提交参数')
        self.tabs.addTab(self.evidence, '完整诊断记录')
        layout.addWidget(self.tabs, 1)
        actions = QHBoxLayout()
        self.copy_button = button('复制本条报告', 'Quiet', 'copy', self.copy)
        self.export_button = button('导出当前清单', 'Save', 'download', self.export)
        actions.addWidget(self.copy_button)
        actions.addWidget(self.export_button)
        actions.addStretch()
        actions.addWidget(button('完成', 'Primary', callback=self.accept))
        layout.addLayout(actions)
        self.filter.currentIndexChanged.connect(self.render)
        self.search.textChanged.connect(self.render)
        self.table.itemSelectionChanged.connect(self.select)
        self.render()

    def render(self):
        term = self.search.text().strip().casefold()
        self.rows = [call for call in self.calls
                     if (self.filter.currentData() != 'errors' or call_issue(call))
                     and (not term or term in (call_report(call) + json.dumps(call, ensure_ascii=False)).casefold())]
        self.count.setText(f'{len(self.rows)} 条调用 · {sum(call_issue(c) for c in self.rows)} 条异常')
        self.table.setRowCount(len(self.rows))
        for index, call in enumerate(self.rows):
            details = call_details(call)
            status = ('云端生成失败' if call.get('operation') == 'query' and details['response'].get('status') == 'failed' else
                      {'running':'进行中', 'succeeded':'已完成', 'failed':'失败', 'unknown':'提交未确认'}.get(call.get('status'), '待核对'))
            values = [str(call.get('call_id') or '本地'),
                      datetime.datetime.fromtimestamp(call.get('created_at') or 0, TZ).strftime('%m-%d %H:%M:%S'),
                      details['stage'], status, str(details['http_status']) if details['http_status'] is not None else '未收到 / 未记录',
                      details['code'] or '未提供 / 未记录']
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(index, column, item)
            self.table.setRowHeight(index, 40)
        self.table.setFixedHeight(max(82, min(225, self.table.horizontalHeader().height() + 40 * len(self.rows) + 2)))
        self.export_button.setEnabled(bool(self.rows))
        if self.rows:
            self.table.selectRow(next((i for i,c in enumerate(self.rows) if call_issue(c)), 0))
        self.select()

    def current(self):
        index = self.table.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def select(self):
        call = self.current()
        self.copy_button.setEnabled(bool(call))
        if not call:
            for widget in (self.summary, self.response, self.request, self.evidence): widget.clear()
            return
        details = call_details(call)
        self.summary.setPlainText(call_report(call))
        body = details['diagnostics'].get('response_body')
        if body is None:
            server = {k:v for k,v in details['response'].items()
                      if k not in {'error_diagnostics', 'network_diagnostics', 'api_diagnostics', 'diagnostics', 'upload_progress'}}
            body = json.dumps(server, ensure_ascii=False, indent=2) if server else '未收到响应正文，或旧版本未保存；现有信息请看“完整诊断记录”。'
        note = details['diagnostics'].get('response_body_note')
        self.response.setPlainText((note + '\n\n' if note else '') + (body or '接口返回正文为空。'))
        self.request.setPlainText(json.dumps(json_value(call.get('request_json')), ensure_ascii=False, indent=2))
        self.evidence.setPlainText(json.dumps(call, ensure_ascii=False, indent=2))

    def report(self, call):
        details = call_details(call)
        body = details['diagnostics'].get('response_body')
        if body is None:
            body = json.dumps(details['response'], ensure_ascii=False, indent=2)
        return call_report(call) + '\n\n接口返回与诊断：\n' + (body or '接口返回正文为空。') + \
               '\n\n提交参数（密钥与媒体编码已隐藏）：\n' + json.dumps(json_value(call.get('request_json')), ensure_ascii=False, indent=2) + \
               '\n\n完整记录：\n' + json.dumps(call, ensure_ascii=False, indent=2)

    def copy(self):
        if self.current(): QApplication.clipboard().setText(self.report(self.current()))

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, '导出错误诊断', '接口错误诊断.txt', '诊断报告 (*.txt)')
        if path:
            Path(path).write_text('\n\n' + ('\n\n' + '='*60 + '\n\n').join(self.report(c) for c in self.rows), encoding='utf-8')
