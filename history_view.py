"""A searchable history of videos and AI prompt versions, with visible template reuse."""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox,QDialog,QHBoxLayout,QLineEdit,QTextEdit,QVBoxLayout,QWidget
from history_outcomes import record_outcome
from studio_controller import neutral_video_text
from studio_widgets import Preview,StudioComboBox,button,cached_thumbnail,card,label,scroll
from ui_language import choice_text,error_text
from billing_cny import estimate_cny
from error_details_view import ErrorDetailsDialog, related_calls

TZ=datetime.timezone(datetime.timedelta(hours=8))

OUTCOME_FILTERS = [('全部状态','all'), ('有成片','saved'), ('失败','failed'),
                   ('待核对','uncertain'), ('进行中','progress'), ('成片未保存 / 缺失','unavailable'),
                   ('仅提示词','prompt'), ('已取消 / 过期','closed')]
SUMMARY_NAMES = {'saved':'有成片','failed':'失败','uncertain':'待核对','progress':'进行中',
                 'unavailable':'成片未保存 / 缺失','prompt':'仅提示词','closed':'已取消 / 过期'}


def outcome_badge(outcome):
    badge=label(outcome.title,'HistoryStatus')
    badge.setProperty('tone',outcome.tone)
    badge.setTextFormat(Qt.TextFormat.PlainText)
    return badge


def outcome_message(text,tone='neutral',*,short=False):
    full=neutral_video_text(text)
    visible=' '.join(full.split()) if short else full
    message=label(visible[:180]+('…' if len(visible)>180 else '') if short else visible,'HistoryReason',True)
    message.setProperty('tone',tone)
    message.setTextFormat(Qt.TextFormat.PlainText)
    message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if short and len(visible)>180:message.setToolTip(full)
    return message


def record_date(record):
    return datetime.datetime.fromtimestamp(record['created_at'],TZ).strftime('%m-%d %H:%M')


def record_settings(record):
    values=record.get('settings') or json.loads(record.get('request_json') or '{}')
    duration=values.get('duration',record.get('requested_duration'))
    return ' · '.join(str(v) for v in (values.get('resolution',record.get('resolution','')),
                     '自适应' if duration in {-1,'-1'} else f'{duration} 秒' if duration else '',
                     choice_text(values.get('ratio',record.get('ratio',''))),
                     '有声' if values.get('generate_audio') else '无声',values.get('output_format','mp4').upper()) if v)


class HistoryDetailDialog(QDialog):
    def __init__(self,record,controller,reuse,play,parent=None):
        super().__init__(parent)
        self.setWindowTitle('历史记录 · 提示词与模板')
        self.resize(900,800);self.setMinimumSize(700,560)
        outer=QVBoxLayout(self);outer.setContentsMargins(24,22,24,20);outer.setSpacing(14)
        body=QWidget();layout=QVBoxLayout(body);layout.setContentsMargins(0,0,0,0);layout.setSpacing(12)
        outer.addWidget(scroll(body),1)
        outcome=record_outcome(record)
        heading=QHBoxLayout()
        heading.addWidget(label(datetime.datetime.fromtimestamp(record['created_at'],TZ).strftime('%Y-%m-%d %H:%M:%S'),'Section'),1)
        heading.addWidget(outcome_badge(outcome));layout.addLayout(heading)
        if outcome.description:layout.addWidget(outcome_message(outcome.description,outcome.tone))
        if outcome.error:
            layout.addWidget(outcome_message('记录原因：'+error_text(outcome.error),outcome.tone))
            raw_error=QTextEdit();raw_error.setObjectName('OriginalError');raw_error.setReadOnly(True)
            raw_error.setPlainText(neutral_video_text(outcome.error));raw_error.setMaximumHeight(130);raw_error.hide()
            original=button('查看原始报错','Quiet',callback=lambda:raw_error.setVisible(raw_error.isHidden()))
            layout.addWidget(original);layout.addWidget(raw_error)
            self.error_dialogs = []
            def errors():
                dialog = ErrorDetailsDialog(related_calls(controller.history, record), self)
                self.error_dialogs.append(dialog)
                dialog.finished.connect(lambda _result: self.error_dialogs.remove(dialog) if dialog in self.error_dialogs else None)
                dialog.show()
            layout.addWidget(button('错误详情 · 接口返回与环节', 'Soft', callback=errors))
        layout.addWidget(label(record_settings(record),'Muted'))
        info=(f"提示词记录 #{record.get('call_id','')}" if record['record_kind']=='prompt' else
              record.get('task_id') or f"提交记录 #{record.get('call_id','')} · 未收到任务编号")
        if record.get('parent_task_id'):
            info+=' · 来源：'+record['parent_task_id']
        layout.addWidget(label(info,'Small',True))
        if record.get('actual_duration'):
            usage=f"产出 {record['actual_duration']:.2f} 秒"
            if record.get('total_tokens') is not None:usage+=f" · 计费单位 {record['total_tokens']:,} 个"
            layout.addWidget(label(usage,'Small'))
        amount=estimate_cny(record,controller.state['cny_rates'])
        if amount is not None:layout.addWidget(label(f'参考费用：约 {amount:.2f} 元人民币（按当前单价估算）','Small'))
        path=Path(record['local_path']) if record.get('local_path') else None
        if outcome.file_available:
            layout.addWidget(Preview(cached_thumbnail(path,controller.directory),150,callback=lambda:play(record)))
        if path:layout.addWidget(outcome_message('保存位置：'+str(path)))
        layout.addWidget(label('完整提示词','Small'))
        self.prompt=QTextEdit();self.prompt.setReadOnly(True)
        self.prompt.setMinimumHeight(220)
        self.prompt.setPlainText(neutral_video_text(record.get('original_prompt') or record.get('prompt','')))
        layout.addWidget(self.prompt,1)
        source=record.get('source_brief')
        if source:
            layout.addWidget(label('优化前原文','Small'))
            original=QTextEdit();original.setReadOnly(True);original.setPlainText(neutral_video_text(source));original.setMaximumHeight(110)
            layout.addWidget(original)
        assets=record.get('assets') if record['record_kind']=='prompt' else json.loads(record.get('assets_json') or '[]')
        layout.addWidget(label('素材：'+'、'.join(a.get('handle','') for a in assets) if assets else '素材：无','Small',True))
        actions=QHBoxLayout()
        copy=button('复制提示词','Quiet',callback=lambda:self.prompt.selectAll() or self.prompt.copy())
        actions.addWidget(copy);actions.addStretch()
        self.reuse_button=button('载入再编辑','Primary',callback=lambda:(reuse(record),self.accept()))
        actions.addWidget(self.reuse_button);actions.addWidget(button('完成','Save',callback=self.accept))
        outer.addLayout(actions)


class HistoryPanel(QWidget):
    def __init__(self,controller,reuse,detail,play,parent=None,*,snapshots=None):
        super().__init__(parent)
        self.controller=controller;self.reuse=reuse;self.detail=detail;self.play=play
        self.records=[]
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(12)
        header=QHBoxLayout()
        self.kind=StudioComboBox()
        for text,value in [('全部记录','all'),('视频生成','task'),('提示词版本','prompt')]:
            self.kind.addItem(text,value)
        header.addWidget(self.kind)
        self.outcome_filter=StudioComboBox()
        for text,value in OUTCOME_FILTERS:self.outcome_filter.addItem(text,value)
        header.addWidget(self.outcome_filter)
        self.count=label('','Small');header.addWidget(self.count,1)
        header.addWidget(button('刷新','Quiet',callback=self.refresh))
        layout.addLayout(header)
        self.search=QLineEdit();self.search.setPlaceholderText('搜索提示词、日期或任务编号')
        search_row=QHBoxLayout();search_row.addWidget(self.search,1)
        if snapshots:
            self.snapshot_button=button('创作快照','Soft',callback=snapshots)
            search_row.addWidget(self.snapshot_button)
        layout.addLayout(search_row)
        self.summary=label('','Small',True);layout.addWidget(self.summary)
        self.body=QWidget();self.cards=QVBoxLayout(self.body);self.cards.setContentsMargins(0,0,0,0);self.cards.setSpacing(12)
        layout.addWidget(scroll(self.body),1)
        self.kind.currentIndexChanged.connect(self.render);self.search.textChanged.connect(self.render)
        self.outcome_filter.currentIndexChanged.connect(self.render)

    def refresh(self):
        self.records=sorted(self.controller.history.generation_records()+self.controller.history.prompt_records(),key=lambda r:r['created_at'],reverse=True)
        self.render()

    def render(self):
        while self.cards.count():
            item=self.cards.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        term=self.search.text().strip().casefold();kind=self.kind.currentData();category=self.outcome_filter.currentData()
        classified=[(r,record_outcome(r)) for r in self.records]
        totals={key:sum(outcome.category==key for _,outcome in classified) for key in SUMMARY_NAMES}
        self.summary.setText(' · '.join(f'{count} {SUMMARY_NAMES[key]}' for key,count in totals.items() if count))
        rows=[(r,outcome) for r,outcome in classified if (kind=='all' or (r['record_kind']=='prompt')==(kind=='prompt'))
              and (category=='all' or outcome.category==category)
              and (not term or term in (r.get('original_prompt','')+' '+r.get('task_id','')+' '+record_date(r)
                                       +' '+outcome.title+' '+outcome.description+' '+outcome.error).casefold())]
        self.count.setText(f'{len(rows)} 条')
        for record,outcome in rows:
            panel=card();content=QVBoxLayout(panel);content.setContentsMargins(14,12,14,12);content.setSpacing(8)
            identity=(f" · #{record.get('call_id','')}" if record['record_kind'] in {'prompt','submission'} else '')
            heading=QHBoxLayout();heading.addWidget(label(record_date(record)+identity,'Small'),1)
            heading.addWidget(outcome_badge(outcome))
            content.addLayout(heading)
            path=Path(record['local_path']) if record.get('local_path') else None
            if outcome.file_available:
                content.addWidget(Preview(cached_thumbnail(path,self.controller.directory),100,callback=lambda r=record:self.play(r)))
            if outcome.description:content.addWidget(outcome_message(outcome.description,outcome.tone,short=True))
            if outcome.error:content.addWidget(outcome_message('原因：'+error_text(outcome.error),outcome.tone,short=True))
            content.addWidget(label(record_settings(record),'Small'))
            text=record.get('original_prompt') or record.get('prompt','')
            short=' '.join(neutral_video_text(text).split())
            snippet=label(short[:135]+('…' if len(short)>135 else ''),'Muted',True)
            snippet.setTextFormat(Qt.TextFormat.PlainText);content.addWidget(snippet)
            actions=QHBoxLayout()
            actions.addWidget(button('完整记录','Quiet',callback=lambda _checked=False,r=record:self.detail(r)))
            actions.addStretch()
            load=button('载入再编辑','Soft',callback=lambda _checked=False,r=record:self.reuse(r))
            load.setEnabled(not self.controller.busy);actions.addWidget(load)
            content.addLayout(actions);self.cards.addWidget(panel)
        if not rows:
            self.cards.addWidget(label('没有符合筛选条件的记录。' if self.records else '暂无记录，生成和智能优化后的版本会保存在这里。','Muted',True))
        self.cards.addStretch()
