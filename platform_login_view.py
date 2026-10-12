"""A user-facing account picker: no developer registration or credential fields."""
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QDialog, QFormLayout, QHBoxLayout, QVBoxLayout
from agent_platforms import PLATFORMS
from studio_widgets import StudioComboBox, button, card, label

FEES = {
    'chatgpt': '使用 ChatGPT 计划额度。',
    'deepseek': '使用 DeepSeek 账号余额或赠送额度，按平台规则计费。',
}


class PlatformLoginDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.login_pending = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle('分析平台 · 登录授权')
        self.resize(650, 460)
        self.setMinimumSize(550, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(18)
        layout.addWidget(label('选择分析平台', 'Title'))
        layout.addWidget(label('用于理解素材、分析画面和优化提示词。', 'Muted', True))
        panel = card()
        form = QFormLayout(panel)
        form.setContentsMargins(22, 22, 22, 22)
        form.setVerticalSpacing(18)
        self.provider = StudioComboBox()
        for key, name in PLATFORMS.items():
            self.provider.addItem(name, key)
        self.provider.setCurrentIndex(self.provider.findData(controller.state['ai_provider']))
        form.addRow('平台', self.provider)
        self.account = label('', 'Body', True)
        form.addRow('账号状态', self.account)
        self.model = StudioComboBox()
        form.addRow('分析模型', self.model)
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
        self.cancel_login = button('取消登录', callback=controller.cancel_ai_login)
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
        controller.changed.connect(self.updated)
        controller.busy_changed.connect(self.busy_changed)
        controller.status_changed.connect(self.status_changed)
        self.refresh()

    def select_provider(self):
        if not self.controller.busy:
            self.controller.select_ai_provider(self.provider.currentData())
            self.refresh()

    def select_model(self):
        model = self.model.currentData()
        if model and not self.controller.busy:
            self.controller.set_analysis_model(model)
            self.controller.save()

    def updated(self, category):
        if category in {'models', 'account'}:
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
        active = bool(controller.analysis_account())
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
                self.model.addItem('登录后显示可用模型')
        self.account.setText(('已连接 · ' if active else '') + controller.analysis_label())
        self.fees.setText(FEES[provider])
        self.notice.setText('请在浏览器中完成授权，完成后自动返回连接状态。' if self.login_pending else
                            'DeepSeek 模型来自官方预置目录，实际可用性在调用时验证。' if provider == 'deepseek' and active else '')
        self.provider.setEnabled(not controller.busy)
        self.model.setEnabled(bool(controller.models) and not controller.busy)
        self.refresh_button.setEnabled(active and not controller.busy)
        self.logout.setVisible(provider != 'chatgpt')
        self.logout.setEnabled(active and not controller.busy)
        self.cancel_login.setVisible(self.login_pending and provider != 'chatgpt')
        self.login_button.setText('等待浏览器授权…' if self.login_pending else '重新登录' if active else '登录授权')
        self.login_button.setEnabled(not controller.busy)
        self.done_button.setEnabled(not self.login_pending)

    def login(self):
        if self.controller.busy:
            return
        self.login_pending = True
        self.controller.sign_in()
        if not self.controller.busy:
            self.login_pending = False
        self.refresh()

    def closeEvent(self, event):
        if self.login_pending:
            self.controller.cancel_ai_login()
        super().closeEvent(event)
