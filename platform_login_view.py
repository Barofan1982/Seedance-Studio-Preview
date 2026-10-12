"""A user-facing account picker: no developer registration or credential fields."""
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QDialog, QFormLayout, QHBoxLayout, QVBoxLayout, QLineEdit
from agent_platforms import PLATFORMS
from studio_widgets import StudioComboBox, button, card, label
from reasoning_settings import EFFORT_LABELS

FEES = {
    'chatgpt': '使用 ChatGPT 计划额度。',
    'deepseek': 'DeepSeek API 按用量单独计费，不使用 ChatGPT 会员额度。',
}


class PlatformLoginDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.login_pending = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle('分析平台 · 连接设置')
        self.resize(650, 560)
        self.setMinimumSize(550, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(18)
        layout.addWidget(label('选择分析平台', 'Title'))
        layout.addWidget(label('用于理解素材、分析画面和优化提示词。', 'Muted', True))
        panel = card()
        form = self.form = QFormLayout(panel)
        form.setContentsMargins(22, 22, 22, 22)
        form.setVerticalSpacing(18)
        self.provider = StudioComboBox()
        for key, name in PLATFORMS.items():
            self.provider.addItem(name, key)
        self.provider.setCurrentIndex(self.provider.findData(controller.state['ai_provider']))
        form.addRow('平台', self.provider)
        self.account = label('', 'Body', True)
        form.addRow('连接状态', self.account)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText('填写 DeepSeek API Key')
        self.api_key.setMinimumWidth(240)
        form.addRow('接口密钥', self.api_key)
        self.model = StudioComboBox()
        form.addRow('分析模型', self.model)
        self.reasoning = StudioComboBox()
        form.addRow('思考强度', self.reasoning)
        self.fees = label('', 'Muted', True)
        form.addRow('额度来源', self.fees)
        layout.addWidget(panel)
        self.notice = label('', 'Muted', True)
        self.notice.setMinimumHeight(42)
        layout.addWidget(self.notice)
        layout.addStretch()
        actions = QHBoxLayout()
        self.logout = button('退出登录', callback=controller.sign_out_ai)
        self.refresh_button = button('刷新模型', callback=controller.refresh_models)
        self.cancel_login = button('取消校验', callback=controller.cancel_ai_login)
        self.login_button = button('登录授权', 'Primary', 'spark', self.login)
        self.done_button = button('完成', 'Save', 'check', self.accept)
        for widget in (self.logout, self.refresh_button, self.cancel_login):
            actions.addWidget(widget)
        actions.addStretch()
        actions.addWidget(self.login_button)
        actions.addWidget(self.done_button)
        layout.addLayout(actions)
        self.provider.currentIndexChanged.connect(self.select_provider)
        self.model.currentIndexChanged.connect(self.select_model)
        self.reasoning.currentIndexChanged.connect(self.select_reasoning)
        controller.changed.connect(self.updated)
        controller.busy_changed.connect(self.busy_changed)
        controller.status_changed.connect(self.status_changed)
        self.refresh()

    def select_provider(self):
        if not self.controller.busy:
            self.api_key.clear()
            self.controller.select_ai_provider(self.provider.currentData())
            self.refresh()

    def select_model(self):
        model = self.model.currentData()
        if model and not self.controller.busy:
            self.controller.set_analysis_model(model)
            self.controller.save()
            self.refresh_reasoning()

    def select_reasoning(self):
        value = self.reasoning.currentData()
        if value is not None and not self.controller.busy:
            self.controller.set_analysis_reasoning_effort(value)

    def refresh_reasoning(self):
        options = self.controller.reasoning_options()
        with QSignalBlocker(self.reasoning):
            self.reasoning.clear()
            for effort in options:
                self.reasoning.addItem(EFFORT_LABELS[effort], effort)
            if options:
                self.reasoning.setCurrentIndex(self.reasoning.findData(self.controller.analysis_reasoning_effort()))
            else:
                self.reasoning.addItem('高 · 等待模型信息' if not self.controller.models else '模型默认（未提供可调档位）')
        self.reasoning.setEnabled(bool(options) and not self.controller.busy)

    def updated(self, category):
        if category == 'ai_preferences':
            self.refresh_reasoning()
            return
        if category in {'models', 'account'}:
            if category == 'models' and self.controller.state['ai_provider'] == 'deepseek' and self.controller.analysis_account():
                self.api_key.clear()
            self.refresh()

    def status_changed(self, value):
        if self.login_pending or self.controller.busy:
            self.notice.setText(value)

    def busy_changed(self, busy):
        if not busy:
            self.login_pending = False
        self.refresh()

    def refresh(self):
        controller = self.controller
        provider = controller.state['ai_provider']
        is_api = provider == 'deepseek'
        active = bool(controller.analysis_account())
        self.form.setRowVisible(self.api_key, is_api)
        self.api_key.setEnabled(not controller.busy)
        self.api_key.setPlaceholderText('已保存密钥；填写新密钥可替换' if active else '填写 DeepSeek API Key')
        with QSignalBlocker(self.provider):
            self.provider.setCurrentIndex(self.provider.findData(provider))
        with QSignalBlocker(self.model):
            self.model.clear()
            for item in controller.models:
                self.model.addItem(item.get('display_name') or item['slug'], item['slug'])
            index = self.model.findData(controller.analysis_model())
            if index >= 0:
                self.model.setCurrentIndex(index)
            if not controller.models:
                self.model.addItem('保存并校验密钥后获取模型' if is_api else '登录后显示可用模型')
        self.account.setText(('已连接 · ' if active else '') + controller.analysis_label())
        self.fees.setText(FEES[provider])
        self.notice.setText(('正在读取模型列表，不调用付费推理。' if is_api else '请在浏览器中完成 ChatGPT 授权。') if self.login_pending else
                            '密钥在本机加密保存；模型校验不调用推理。只有分析或优化提示词才产生 API 用量。' if is_api else '')
        self.provider.setEnabled(not controller.busy)
        self.model.setEnabled(bool(controller.models) and not controller.busy)
        self.refresh_reasoning()
        self.refresh_button.setEnabled(active and not controller.busy)
        self.logout.setVisible(is_api); self.logout.setText('清除密钥')
        self.logout.setEnabled(active and not controller.busy)
        self.cancel_login.setVisible(self.login_pending and is_api)
        self.login_button.setText(('正在校验…' if self.login_pending else '保存并校验') if is_api else
                                 '等待浏览器授权…' if self.login_pending else '重新登录' if active else '登录授权')
        self.login_button.setEnabled(not controller.busy)
        self.done_button.setEnabled(not self.login_pending)

    def login(self):
        if self.controller.busy:
            return
        if self.controller.state['ai_provider'] == 'deepseek':
            key = self.api_key.text().strip()
            if not key:
                if self.controller.analysis_account():
                    self.controller.refresh_models()
                else:
                    self.notice.setText('请先填写 DeepSeek API Key。')
                    self.api_key.setFocus()
                return
            self.login_pending = True
            self.controller.configure_deepseek_api(key)
        else:
            self.login_pending = True
            self.controller.sign_in()
        if not self.controller.busy:
            self.login_pending = False
        self.refresh()

    def closeEvent(self, event):
        if self.login_pending:
            self.controller.cancel_ai_login()
        super().closeEvent(event)
