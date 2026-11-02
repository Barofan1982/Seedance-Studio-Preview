"""Compact tables: local per-task estimates and independent relay aggregate receipts."""
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget
from datetime import datetime, timezone
from math import isfinite
from billing_cny import billing_details, relay_usage_rows
from seedance_models import model_name
from studio_widgets import label


def money(value, symbol='¥'):
    return f'{symbol}{value:.2f}' if value is not None else '未返回'


class BillingUsagePanel(QTabWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.local = self.table(['模型 / 任务', '分辨率', '成片时长', '计费 tokens', '单价 ¥ / 百万', '费用 ¥ / $', '¥ / 秒', '依据'])
        self.addTab(self.local, '每条成片 · 费用估算')
        cloud = QWidget()
        layout = QVBoxLayout(cloud)
        self.cloud_note = label('点击“同步中转用量”查看当前密钥的扣费记录。', 'Small', True)
        layout.addWidget(self.cloud_note)
        self.cloud = self.table(['模型', '请求数', '输出 tokens', '实扣美元', '折合人民币', '平均 $ / 百万'])
        layout.addWidget(self.cloud, 1)
        self.addTab(cloud, '中转实扣 · 按模型')

    @staticmethod
    def table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(56)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        return table

    @staticmethod
    def populate(table, rows):
        table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                table.setItem(r, c, item)

    def refresh(self, records):
        rows = []
        for record in records:
            if record.get('status') != 'succeeded':
                continue
            d = billing_details(record, self.controller.state['cny_rates'])
            duration = record.get('actual_duration')
            duration_text = (f'{duration:.1f} 秒' if isinstance(duration, (int, float))
                             and not isinstance(duration, bool) and isfinite(duration) and duration >= 0
                             else '未记录')
            rows.append([model_name(record['model']) + '\n' + record['task_id'],
                         record.get('actual_resolution') or record.get('resolution', ''),
                         duration_text,
                         f"{d['tokens']:,}" if isinstance(d['tokens'], (int, float)) and not isinstance(d['tokens'], bool) else '未返回',
                         money(d['rate']), money(d['amount']) + ' / ' + money(d['usd'], '$'),
                         money(d['per_second']), d['reason']])
        self.populate(self.local, rows)
        cache = self.controller.state.get('relay_billing_usage', {})
        if cache.get('scope') != self.controller.billing_scope():
            self.populate(self.cloud, [])
            self.cloud_note.setText('点击“同步中转用量”查看当前密钥的扣费记录。')
            return
        response = cache.get('response', {})
        cloud_rows = relay_usage_rows(response)
        self.populate(self.cloud, [[model_name(r['model']), r['requests'] if r['requests'] is not None else '未返回',
                                   f"{r['tokens']:,}" if r['tokens'] is not None else '未返回', money(r['usd'], '$'),
                                   money(r['cny']), money(r['effective_rate_usd'], '$')] for r in cloud_rows])
        stamp = datetime.fromtimestamp(cache['checked_at'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        self.cloud_note.setText(f'同步于 {stamp} · 查询最近 90 天 · 实扣金额按 1:7 换算。\n'
                                '中转仅返回按模型汇总。平均单价为历史均值，后续以实际账单为准。')
