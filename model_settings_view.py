"""Video-model registration and separate actual/reference price settings."""
from copy import deepcopy

from PySide6.QtCore import QSignalBlocker
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QMessageBox

from billing_cny import parse_cny_rates, USD_CNY
from seedance_models import (MODEL_SPECS, DEFAULT_VIDEO_MODEL, OFFICIAL_PRICES, PRICE_SOURCE,
                             RELAY_ALIAS, RELAY_MODEL_PROFILES, model_name, model_spec)
from studio_widgets import StudioComboBox, button, label


class AddVideoModelDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle('添加视频模型')
        self.resize(540, 230)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.model_id = QLineEdit()
        self.model_id.setPlaceholderText('填写服务方提供的模型 ID 或接入点 ID')
        form.addRow('模型 ID', self.model_id)
        self.profile = StudioComboBox()
        for identity, spec in MODEL_SPECS.items():
            if identity != DEFAULT_VIDEO_MODEL:
                self.profile.addItem(spec.name, identity)
        form.addRow('参数模板', self.profile)
        layout.addLayout(form)
        layout.addWidget(label('模型 ID 原样提交。参数模板决定可选时长、分辨率和参考方式，请按服务方说明选择。', 'Small', True))
        layout.addWidget(button('添加并选用', 'Primary', callback=self.save))

    def save(self):
        try:
            self.controller.add_generation_model(self.model_id.text(), self.profile.currentData())
        except ValueError as error:
            QMessageBox.warning(self, '模型未添加', str(error))
            return
        self.accept()


class ModelPricesDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.rates = deepcopy(controller.state['cny_rates'].get('models', {}))
        self.current = None
        self.loaded_values = {}
        self.setWindowTitle('各模型单价 · 人民币 / 美元')
        self.resize(650, 460)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.model = StudioComboBox()
        self.model.fit_popup_to_contents = True
        selected = controller.state['generation_model']
        for identity in controller.generation_model_choices():
            price_identity = DEFAULT_VIDEO_MODEL if identity == 'seedance-2.5' and (
                selected == DEFAULT_VIDEO_MODEL or RELAY_ALIAS.fullmatch(selected)) else identity
            self.model.addItem(model_name(identity), price_identity)
        form.addRow('生成模型', self.model)
        self.resolution = StudioComboBox()
        form.addRow('分辨率', self.resolution)
        self.fields = {}
        for kind, title in (('without_video', '无视频输入'), ('with_video', '含视频输入')):
            field = QLineEdit()
            field.setPlaceholderText('待设置单价')
            self.fields[kind] = field
            form.addRow(title, field)
        layout.addLayout(form)
        layout.addWidget(label('填写中转自定义价（元 / 百万 tokens）。未配置的已知模型采用官方参考价；已有自定义价清空后暂停该项估算。', 'Small', True))
        self.reference = label('', 'Small', True)
        layout.addWidget(self.reference)
        self.converted = label('', 'Small', True)
        layout.addWidget(self.converted)
        for field in self.fields.values():
            field.textChanged.connect(self.update_conversion)
        layout.addWidget(label('固定汇率：1 美元 = 7 元人民币。官方价属于估算，中转实扣请在用量页同步；VIP 折扣以账单为准。', 'Small', True))
        actions = QHBoxLayout()
        actions.addWidget(button('官方价格文档', 'Quiet', callback=lambda: QDesktopServices.openUrl(QUrl(PRICE_SOURCE))))
        actions.addStretch()
        actions.addWidget(button('保存单价', 'Primary', callback=self.save))
        layout.addLayout(actions)
        self.model.currentIndexChanged.connect(self.change_model)
        self.resolution.currentIndexChanged.connect(self.change_resolution)
        chosen = self.model.findData(DEFAULT_VIDEO_MODEL if RELAY_ALIAS.fullmatch(selected) else selected)
        with QSignalBlocker(self.model):
            self.model.setCurrentIndex(max(0, chosen))
        self.change_model()

    def store_current(self):
        if self.current:
            values = {kind: field.text() for kind, field in self.fields.items()}
            if values == self.loaded_values:
                return  # Browsing prices must not create blank overrides.
            rates = parse_cny_rates(values)
            identity, resolution = self.current
            self.rates.setdefault(identity, {})[resolution] = rates

    def change_model(self):
        try:
            self.store_current()
        except ValueError as error:
            QMessageBox.warning(self, '单价无效', str(error))
            with QSignalBlocker(self.model):
                self.model.setCurrentIndex(self.model.findData(self.current[0]))
            return
        identity = self.model.currentData()
        spec = model_spec(identity, self.controller.state['custom_generation_models'].get(identity, ''))
        with QSignalBlocker(self.resolution):
            self.resolution.clear()
            for resolution in spec.resolutions:
                self.resolution.addItem(resolution.upper() if resolution == '4k' else resolution, resolution)
            target = self.resolution.findData(self.controller.state['resolution'])
            self.resolution.setCurrentIndex(max(0, target))
        self.current = None
        self.change_resolution()

    def change_resolution(self):
        try:
            self.store_current()
        except ValueError as error:
            QMessageBox.warning(self, '单价无效', str(error))
            with QSignalBlocker(self.resolution):
                self.resolution.setCurrentIndex(self.resolution.findData(self.current[1]))
            return
        identity, resolution = self.model.currentData(), self.resolution.currentData()
        self.current = (identity, resolution)
        rates = self.rates.get(identity, {}).get(resolution)
        if rates is None:
            rates = self.controller.state['cny_rates'] if identity == DEFAULT_VIDEO_MODEL and resolution in {'480p','720p'} else {}
        for kind, field in self.fields.items():
            price = rates.get(kind)
            field.setText('' if price is None else f'{price:.2f}')
        self.loaded_values = {kind: field.text() for kind, field in self.fields.items()}
        official_id = 'doubao-seedance-2-5-260628' if identity == DEFAULT_VIDEO_MODEL else identity
        official_id = RELAY_MODEL_PROFILES.get(official_id, official_id)
        official = OFFICIAL_PRICES.get(official_id, {}).get(resolution)
        def pair(value):
            return f'¥{value:.2f} / ${value / float(USD_CNY):.2f}' if value is not None else '不支持'
        self.reference.setText((f'官方参考价（2026-10-21，百万 tokens）：\n无视频 {pair(official[0])}；含视频 {pair(official[1])}')
                               if official else '官方参考价：此档位尚未录入，需设置中转单价。')
        for field in self.fields.values():
            field.setPlaceholderText('默认采用官方参考价' if official else '待设置单价')
        self.update_conversion()

    def update_conversion(self):
        try:
            rates = parse_cny_rates({kind: field.text() for kind, field in self.fields.items()})
        except ValueError:
            self.converted.setText('自定义价请输入有效数字。')
            return
        self.converted.setText('自定义价美元换算：' + '；'.join(
            f"{'含视频' if kind == 'with_video' else '无视频'} ${value / float(USD_CNY):.2f} / 百万 tokens"
            for kind, value in rates.items()) if rates else '尚无自定义单价。')

    def save(self):
        try:
            self.store_current()
        except ValueError as error:
            QMessageBox.warning(self, '单价未保存', str(error))
            return
        self.controller.state['cny_rates']['models'] = self.rates
        self.controller.save()
        self.controller.changed.emit('parameters')
        self.accept()
