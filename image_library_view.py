"""Local images can be restored individually, in groups, or as a whole collection."""
from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLineEdit,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from image_library import image_copy, image_status
from studio_widgets import StudioComboBox, button, label
from upload_library_view import date_text

ROLES = {'character':'角色','scene':'场景','first':'首帧','last':'尾帧','keyframe':'关键帧','other':'参考'}


class ImageLibraryPanel(QWidget):
    def __init__(self, controller, add, preview, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.records = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(16)
        tools = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText('搜索图片文件名')
        self.filter = StudioComboBox()
        for text, value in (('全部图片','all'),('可使用','available'),('缺失文件','missing')):self.filter.addItem(text,value)
        tools.addWidget(self.search,1);tools.addWidget(self.filter)
        tools.addWidget(button('刷新','Quiet',callback=self.refresh))
        layout.addLayout(tools)
        self.summary = label('','Small');layout.addWidget(self.summary)
        self.table = QTableWidget(0,5)
        self.table.setHorizontalHeaderLabels(['图片','文件名','用途','保存时间','状态'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False);self.table.verticalHeader().hide();self.table.setIconSize(QSize(48,48))
        header=self.table.horizontalHeader();header.setSectionResizeMode(0,QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0,78);header.setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        for column in (2,3,4):header.setSectionResizeMode(column,QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table,1)
        layout.addWidget(label('图片保存在本机，可长期复用。按住 Ctrl 或 Shift 可多选。','Small',True))
        actions=QHBoxLayout()
        self.preview_button=button('预览','Soft','eye',lambda:preview(self.selected_ids()[0]))
        self.all_button=button('全部添加','Quiet',callback=lambda:add(self.available_ids()))
        self.add_button=button('添加所选','Primary','plus',lambda:add(self.selected_ids(available_only=True)))
        actions.addWidget(self.preview_button);actions.addWidget(self.all_button);actions.addStretch();actions.addWidget(self.add_button)
        layout.addLayout(actions)
        self.search.textChanged.connect(self.refresh);self.filter.currentIndexChanged.connect(self.refresh)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.cellDoubleClicked.connect(lambda _row,_column:add(self.selected_ids(available_only=True)) if self.add_button.isEnabled() else None)
        controller.busy_changed.connect(lambda _busy:self.selection_changed())
        self.refresh()

    def selected_ids(self, available_only=False):
        rows=sorted({item.row() for item in self.table.selectedItems()})
        return [self.records[row]['library_id'] for row in rows
                if row<len(self.records) and (not available_only or image_status(self.records[row])[0]=='available')]

    def available_ids(self):
        return [record['library_id'] for record in self.records if image_status(record)[0]=='available']

    def refresh(self, *_args):
        selected=set(self.selected_ids())
        all_records=self.controller.image_library.rows()
        usable=sum(image_status(record)[0]=='available' for record in all_records)
        self.summary.setText(f'{len(all_records)} 张图片 · {usable} 张可使用')
        text,filter_value=self.search.text().strip().lower(),self.filter.currentData()
        self.records=[record for record in all_records if (not text or text in record['filename'].lower())
                      and (filter_value=='all' or image_status(record)[0]==filter_value)]
        self.table.blockSignals(True);self.table.setRowCount(len(self.records));self.table.clearSelection()
        for row,record in enumerate(self.records):
            state,status=image_status(record);saved=image_copy(record)
            values=('',record['filename'],ROLES.get(record.get('purpose'),'参考'),date_text(record['added_at']),status)
            for column,value in enumerate(values):
                item=QTableWidgetItem(value)
                if column==0 and saved:
                    pixmap=QPixmap(str(saved)).scaled(48,48,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
                    item.setIcon(QIcon(pixmap))
                if column==1:
                    item.setData(Qt.ItemDataRole.UserRole,record['library_id']);item.setToolTip(record['filename'])
                if state!='available':item.setForeground(QColor('#a5aab5'));item.setBackground(QColor('#f7f8fa'))
                elif column==4:item.setForeground(QColor('#23956e'))
                self.table.setItem(row,column,item)
            self.table.setRowHeight(row,64)
            if record['library_id'] in selected:
                for column in range(5):self.table.item(row,column).setSelected(True)
        self.table.blockSignals(False);self.selection_changed()

    def selection_changed(self):
        selected=self.selected_ids(available_only=True)
        self.add_button.setEnabled(bool(selected) and not self.controller.busy)
        self.add_button.setText(f'添加所选（{len(selected)}）' if selected else '添加所选')
        self.all_button.setEnabled(bool(self.available_ids()) and not self.controller.busy)
        self.preview_button.setEnabled(len(self.selected_ids())==1 and bool(selected))
