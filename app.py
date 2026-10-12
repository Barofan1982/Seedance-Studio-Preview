"""Seedance Studio: a separate, calmer desktop interface."""
from __future__ import annotations

import datetime
import faulthandler
import json
import os
import sys
import traceback
from pathlib import Path
from PySide6.QtCore import QPoint, Qt, QSignalBlocker, QTimer, QUrl
from PySide6.QtGui import QCursor, QDesktopServices, QFont, QKeySequence, QShortcut, QTextCursor, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QProgressBar, QPushButton, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget, QAbstractItemView, QSizePolicy, QTabWidget,
)
from media_assets import IMAGE_EXTENSIONS
from request_records import compact_record
from secure_store import save_key, save_openai_key
from seedance_client import RATIOS, RESOLUTIONS, CREATE_TIMEOUT_SECONDS
from studio_controller import APP_DIR, MODES, TASK_TYPES, PURPOSE, StudioController, collect_payload, generation_settings, manifest, structured_prompt, neutral_video_text, prompt_for_submission, review_summary
from studio_theme import STYLE, TOOLTIP_STYLE, icon
from studio_widgets import AssetCard, ElideLabel, JsonDialog, KeyframeDialog, Preview, PromptEditor, StudioComboBox, VideoDialog, button, cached_thumbnail, card, label, scroll
from video_analysis import needs_video_analysis, reference_videos
from generation_status import PHASES, generation_feedback
from history_view import HistoryDetailDialog, HistoryPanel
from ui_language import choice_text, error_text, install_chinese_translation
from billing_cny import cny_summary, parse_cny_rates
from video_upload import needs_upload, source_expired
from upload_library_view import UploadLibraryPanel
from image_library_view import ImageLibraryPanel
from image_library import image_copy
from error_details_view import ErrorDetailsDialog, related_calls
from generation_queue_view import GenerationQueuePanel
from agent_platforms import PLATFORMS
from platform_login_view import PlatformLoginDialog
import time


def combo(values):
    widget = StudioComboBox()
    for value in values:
        widget.addItem(choice_text(value), value)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return widget


class UploadLoginDialog(QDialog):
    def __init__(self, controller, parent=None, success=None):
        super().__init__(parent)
        self.controller, self.success = controller, success
        self._cancelled = False
        self._login_pending = False
        self.finished.connect(lambda _result: setattr(self,'_cancelled',True))
        self.setWindowTitle('登录视频上传账号')
        self.resize(470, 290)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28,24,28,24)
        layout.addWidget(label('连接视频上传', 'Title'))
        layout.addWidget(label('使用中转账号上传本地视频，自动获取参考地址。','Muted',True))
        self.username = QLineEdit(controller.upload_auth.account().get('username',''))
        self.username.setPlaceholderText('中转账号 / 手机号码')
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText('中转密码')
        form = QFormLayout()
        form.addRow('账号',self.username); form.addRow('密码',self.password)
        layout.addLayout(form)
        self.notice = label('', 'Muted', True)
        layout.addWidget(self.notice)
        tools = QHBoxLayout(); tools.addStretch()
        tools.addWidget(button('取消',callback=self.reject))
        self.login_button = button('登录并保存','Primary',callback=self.login)
        tools.addWidget(self.login_button); layout.addLayout(tools)
        controller.busy_changed.connect(self.update_busy)
        controller.failed.connect(self.login_failed)
        self.password.returnPressed.connect(self.login)

    def update_busy(self, busy):
        self.login_button.setEnabled(not busy)
        self.username.setEnabled(not busy); self.password.setEnabled(not busy)

    def login(self):
        if self.controller.busy: return
        username, password = self.username.text().strip(), self.password.text()
        if not username or not password:
            self.notice.setText('请填写中转账号和密码。'); return
        def connected():
            if self._cancelled: return
            self._login_pending = False
            self.password.clear()
            self.accept()
            if self.success: self.success()
        self.notice.setText('正在安全连接中转…')
        self._login_pending = True
        self.controller.sign_in_upload(username,password,connected)

    def login_failed(self, detail):
        if self._login_pending and not self._cancelled:
            self._login_pending = False
            self.notice.setText(error_text(detail))


class SettingsDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("工作室设置")
        self.resize(760, 700)
        self.setMinimumSize(640, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 22)
        layout.setSpacing(20)
        layout.addWidget(label("连接与保存", "Title"))
        content = card()
        form = QFormLayout(content)
        form.setContentsMargins(24, 24, 24, 24)
        form.setHorizontalSpacing(30)
        form.setVerticalSpacing(18)
        self.address = QLineEdit(controller.state["base_url"])
        form.addRow("Seedance 地址", self.address)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("已加密保存；留空保留当前密钥" if controller.api_key else "填写 Seedance 接口密钥")
        form.addRow("Seedance 密钥", self.key)
        platform_row = QWidget()
        platform_layout = QHBoxLayout(platform_row)
        platform_layout.setContentsMargins(0,0,0,0)
        self.platform_label = label(PLATFORMS[controller.state['ai_provider']], 'Muted')
        platform_layout.addWidget(self.platform_label, 1)
        platform_layout.addWidget(button('登录 / 切换', callback=self.platform_login))
        form.addRow('分析平台', platform_row)
        model_row = QWidget()
        model_layout = QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        self.model = StudioComboBox()
        model_layout.addWidget(self.model, 1)
        refresh = button("", "Icon", "spark", controller.refresh_models)
        refresh.setToolTip("刷新当前分析账号的模型")
        model_layout.addWidget(refresh)
        form.addRow("分析模型", model_row)
        self.populate_models()
        controller.changed.connect(self.updated)
        self.openai_key = QLineEdit()
        self.openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_key.setPlaceholderText("选填，仅语音转写使用，按接口用量单独计费")
        form.addRow("语音转写密钥", self.openai_key)
        self.transcribe = QCheckBox("启用语音转写 · 接口另计费")
        self.transcribe.setChecked(controller.state["transcribe_audio"])
        form.addRow("", self.transcribe)
        destination_row = QWidget()
        destination = QHBoxLayout(destination_row)
        destination.setContentsMargins(0, 0, 0, 0)
        self.download_dir = QLineEdit(controller.state["download_dir"])
        destination.addWidget(self.download_dir, 1)
        choose = button("", "Icon", "folder", self.choose_directory)
        choose.setToolTip("选择自动下载目录")
        destination.addWidget(choose)
        form.addRow("自动保存到", destination_row)
        upload_row = QWidget()
        upload_layout = QHBoxLayout(upload_row)
        upload_layout.setContentsMargins(0,0,0,0)
        self.upload_account = label(controller.upload_auth.label(),'Muted')
        upload_layout.addWidget(self.upload_account,1)
        upload_layout.addWidget(button('登录',callback=self.upload_login))
        self.upload_logout = button('退出',callback=controller.sign_out_upload)
        self.upload_logout.setEnabled(controller.upload_auth.signed_in())
        upload_layout.addWidget(self.upload_logout)
        form.addRow('视频上传账号',upload_row)
        self.cny_rates = {}
        for kind, title in (('without_video','无视频输入单价'),('with_video','有视频输入单价')):
            field = QLineEdit(str(controller.state['cny_rates'].get(kind,'')))
            field.setPlaceholderText('元 / 百万计费单位；留空表示未设置')
            field.setToolTip('用于人民币参考费用估算；以中转站账单为准。')
            self.cny_rates[kind] = field
            price_row = QWidget()
            price_layout = QHBoxLayout(price_row)
            price_layout.setContentsMargins(0, 0, 0, 0)
            price_layout.addWidget(field, 1)
            price_layout.addWidget(label('元 / 百万计费单位', 'Muted'))
            form.addRow(title, price_row)
        self.http = QCheckBox("允许当前明文中转连接")
        self.http.setChecked(controller.http_allowed() and controller.state["base_url"].startswith("http://"))
        self.http.setToolTip("该地址会以明文传输接口密钥和请求；确认仅对当前地址生效。")
        form.addRow("", self.http)
        layout.addWidget(scroll(content), 1)
        tools = QHBoxLayout()
        tools.addWidget(label("工作室试用版 · 配置独立保存", "Muted"))
        tools.addStretch()
        tools.addWidget(button("取消", callback=self.reject))
        tools.addWidget(button("保存并完成", "Save", "check", self.save))
        layout.addLayout(tools)

    def populate_models(self):
        self.model.clear()
        for item in self.controller.models:
            self.model.addItem(item.get("display_name") or item["slug"], item["slug"])
        selected = self.model.findData(self.controller.analysis_model())
        if selected >= 0:
            self.model.setCurrentIndex(selected)
        self.model.setEnabled(bool(self.controller.models))
        if not self.controller.models:
            self.model.addItem("登录分析平台后获取模型")

    def updated(self, category):
        if category in {"models", "account"}:
            self.platform_label.setText(PLATFORMS[self.controller.state['ai_provider']])
            self.populate_models()
        if category == 'upload_account':
            self.upload_account.setText(self.controller.upload_auth.label())
            self.upload_logout.setEnabled(self.controller.upload_auth.signed_in())

    def upload_login(self):
        if self.controller.busy: return
        dialog = UploadLoginDialog(self.controller,self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()

    def platform_login(self):
        if not self.controller.busy:
            dialog = PlatformLoginDialog(self.controller, self)
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            dialog.open()

    def choose_directory(self):
        value = QFileDialog.getExistingDirectory(self, "选择自动下载目录", self.download_dir.text())
        if value:
            self.download_dir.setText(value)

    def save(self):
        controller = self.controller
        try:
            rates = parse_cny_rates({kind:field.text() for kind,field in self.cny_rates.items()})
        except ValueError as error:
            QMessageBox.warning(self,'单价未保存',str(error))
            return
        address = self.address.text().strip().rstrip("/")
        if self.key.text().strip():
            controller.api_key = self.key.text().strip()
            save_key(controller.api_key)
        if self.openai_key.text().strip():
            controller.openai_key = self.openai_key.text().strip()
            save_openai_key(controller.openai_key)
        controller.state.update(base_url=address, download_dir=self.download_dir.text().strip(),
                                cny_rates=rates,
                                transcribe_audio=self.transcribe.isChecked(),
                                allow_http_for=address if self.http.isChecked() else "")
        if self.model.currentData():
            controller.set_analysis_model(self.model.currentData())
        controller.save()
        controller.changed.emit("parameters")
        self.accept()


class StudioWindow(QMainWindow):
    def __init__(self, controller=None, *, start_services=True):
        super().__init__()
        install_chinese_translation(QApplication.instance())
        self.controller = controller or StudioController()
        self.setWindowTitle("Seedance Studio · 界面试用版")
        self.resize(1280, 800)
        self.setMinimumSize(1280, 800)
        self.setStyleSheet(STYLE)
        self.dialogs = []
        self.controls = {}
        self._build()
        self.submission_timer = QTimer(self)
        self.submission_timer.setInterval(1000)
        self.submission_timer.timeout.connect(self.refresh_submission)
        self.generation_timer = QTimer(self)
        self.generation_timer.setInterval(1000)
        self.generation_timer.timeout.connect(self.refresh_generation)
        self.waiting_samples = []
        self._connect()
        self.refresh("all")
        if start_services and self.generation_feedback['phase'] in {0,1,2,3}:
            self.editor_pages.setCurrentIndex(2)
        self.setAcceptDrops(True)
        shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        shortcut.activated.connect(self.controller.save)
        if start_services:
            QTimer.singleShot(200, self.controller.refresh_models)
            QTimer.singleShot(900, self.controller.resume)

    def fit_to_screen(self):
        """Open centered at 80% of the available screen, respecting Windows scaling."""
        application = QApplication.instance()
        screen = application.screenAt(QCursor.pos()) or self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        # Include the native title bar and border when sizing and centering.
        border_width = max(0, self.frameGeometry().width() - self.width())
        border_height = max(0, self.frameGeometry().height() - self.height())
        max_width = max(1, available.width() - border_width - 32)
        max_height = max(1, available.height() - border_height - 32)
        self.setMinimumSize(min(1280, max_width), min(800, max_height))
        width = min(max_width, max(self.minimumWidth(), round(available.width() * .8) - border_width))
        height = min(max_height, max(self.minimumHeight(), round(available.height() * .8) - border_height))
        self.resize(width, height)
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    def _build(self):
        root = QWidget()
        root.setObjectName("AppBody")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._rail())
        body = QWidget()
        outer.addWidget(body, 1)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 12)
        layout.setSpacing(22)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(5)
        self.title = label("创作工作台", "Title")
        titles.addWidget(self.title)
        titles.addWidget(label("视频创作工作室", "Subtitle"))
        header.addLayout(titles, 1)
        self.saved_chip = label("已保存", "Good")
        self.saved_chip.setToolTip("当前草稿和设置保存在工作室试用版目录。")
        header.addWidget(self.saved_chip)
        self.account_button = button("ChatGPT", "Soft", "spark", self.account_menu)
        header.addWidget(self.account_button)
        self.header_more = button("", "Icon", "more", self.header_menu)
        self.header_more.setToolTip("工作台选项")
        header.addWidget(self.header_more)
        layout.addLayout(header)
        self.pages = QStackedWidget()
        self.workbench = self._workbench()
        self.library = self._library()
        self.usage = self._usage()
        self.pages.addWidget(self.workbench)
        self.pages.addWidget(self.library)
        self.pages.addWidget(self.usage)
        self.pages.addWidget(self._uploaded_library())
        layout.addWidget(self.pages, 1)
        footer = QHBoxLayout()
        self.status_indicator = label("●", "Muted")
        self.status = ElideLabel("准备就绪", "Small")
        footer.addWidget(self.status_indicator)
        footer.addWidget(self.status, 1)
        footer.addWidget(label("界面试用版", "Small"))
        layout.addLayout(footer)

    def _rail(self):
        rail = QWidget()
        rail.setObjectName("Rail")
        rail.setFixedWidth(88)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(13, 25, 13, 18)
        layout.setSpacing(15)
        logo = label("创", "Logo")
        logo.setFixedSize(48, 48)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(label("工作室", "RailCaption"), alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(24)
        self.nav_group = QButtonGroup(self)
        for index, (title, symbol) in enumerate((("创作", "spark"), ("历史", "film"), ("用量", "chart"), ("素材库", "folder"))):
            item = button(title, "Nav", symbol)
            item.setCheckable(True)
            item.setIcon(icon(symbol, "#aca4d8", 20))
            item.setMinimumHeight(66)
            item.clicked.connect(lambda _checked=False, i=index: self.navigate(i))
            self.nav_group.addButton(item, index)
            layout.addWidget(item)
        self.nav_group.button(0).setChecked(True)
        layout.addStretch()
        settings = button("设置", "Nav", "settings", self.open_settings)
        settings.setIcon(icon("settings", "#8792a6", 21))
        settings.setMinimumHeight(58)
        layout.addWidget(settings)
        return rail

    def _workbench(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        self.material_panel = card()
        self.material_panel.setFixedWidth(298)
        materials = QVBoxLayout(self.material_panel)
        materials.setContentsMargins(18, 20, 18, 18)
        materials.setSpacing(16)
        title = QHBoxLayout()
        title.addWidget(label("参考素材", "Section"), 1)
        self.material_count = label("0", "Chip")
        title.addWidget(self.material_count)
        materials.addLayout(title)
        self.import_button = button("导入素材", "Soft", "plus", self.import_files)
        self.import_button.setToolTip("选择本地图片、参考视频或音频；也可以直接拖入窗口。")
        import_row = QHBoxLayout()
        import_row.setSpacing(5)
        import_row.addWidget(self.import_button, 1)
        self.import_more = button("", "Icon", "chevron", self.import_menu)
        self.import_more.setToolTip("按用途添加 / 文件夹 / 网址")
        import_row.addWidget(self.import_more)
        materials.addLayout(import_row)
        self.upload_notice = QFrame()
        self.upload_notice.setObjectName('SubmissionNotice')
        upload_layout = QVBoxLayout(self.upload_notice)
        upload_layout.setContentsMargins(12,10,12,10)
        upload_layout.setSpacing(6)
        self.upload_title = label('正在上传视频','SubmissionTitle')
        self.upload_filename = ElideLabel('','Small')
        self.upload_bar = QProgressBar()
        self.upload_bar.setTextVisible(False)
        self.upload_bar.setFixedHeight(5)
        self.upload_detail = label('','Small',True)
        self.upload_detail.setMinimumHeight(42)
        for widget in (self.upload_title,self.upload_filename,self.upload_bar,self.upload_detail):
            upload_layout.addWidget(widget)
        materials.addWidget(self.upload_notice)
        self.upload_notice.hide()
        self.asset_content = QWidget()
        self.asset_layout = QVBoxLayout(self.asset_content)
        self.asset_layout.setContentsMargins(0, 0, 0, 0)
        self.asset_layout.setSpacing(12)
        self.asset_scroll = scroll(self.asset_content)
        materials.addWidget(self.asset_scroll, 1)
        self.material_hint = label("图片 0 · 视频 0 · 音频 0", "Small")
        self.material_hint.setToolTip("本次送入 Seedance 的参考数量；选中的本地视频会先上传获取参考地址。")
        materials.addWidget(self.material_hint)
        layout.addWidget(self.material_panel)
        layout.addWidget(self._composer(), 1)
        self.parameter_panel = self._parameters()
        self.parameter_panel.setFixedWidth(268)
        layout.addWidget(self.parameter_panel)
        return page

    def _composer(self):
        container = card()
        container.setMinimumWidth(420)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        self.editor_tabs = QButtonGroup(self)
        for index, text in enumerate(("创作", "核对", "成片", "历史记录", "待提交")):
            item = button(text, "Tab")
            item.setCheckable(True)
            item.clicked.connect(lambda _checked=False, i=index: self.editor_pages.setCurrentIndex(i))
            self.editor_tabs.addButton(item, index)
            heading.addWidget(item)
        self.editor_tabs.button(0).setChecked(True)
        heading.addStretch()
        self.prompt_more = button("", "Icon", "more", self.prompt_menu)
        self.prompt_more.setToolTip("插入结构 / 恢复原文 / 创作方案")
        heading.addWidget(self.prompt_more)
        layout.addLayout(heading)
        self.submission_notice = QFrame()
        self.submission_notice.setObjectName("SubmissionNotice")
        notice_layout = QVBoxLayout(self.submission_notice)
        notice_layout.setContentsMargins(14, 10, 14, 10)
        notice_layout.setSpacing(4)
        self.submission_title = label("", "SubmissionTitle")
        self.submission_detail = label("", "Small", True)
        self.submission_title.setTextFormat(Qt.TextFormat.PlainText)
        self.submission_detail.setTextFormat(Qt.TextFormat.PlainText)
        notice_layout.addWidget(self.submission_title)
        notice_layout.addWidget(self.submission_detail)
        self.submission_notice.hide()
        layout.addWidget(self.submission_notice)
        self.editor_pages = QStackedWidget()
        edit = QWidget()
        edit_layout = QVBoxLayout(edit)
        edit_layout.setContentsMargins(0, 0, 0, 0)
        edit_layout.setSpacing(12)
        prompt_title = QHBoxLayout()
        prompt_title.addWidget(label("创作提示词", "Small"), 1)
        self.iteration = ElideLabel("", "Small")
        prompt_title.addWidget(self.iteration)
        edit_layout.addLayout(prompt_title)
        self.prompt = PromptEditor()
        edit_layout.addWidget(self.prompt, 1)
        self.editor_pages.addWidget(edit)
        self.analysis = QTextEdit()
        self.analysis.setReadOnly(True)
        self.analysis.setPlaceholderText("只显示保留内容、角色对应关系和需要确认的问题。")
        self.editor_pages.addWidget(self.analysis)
        self.result_page = QWidget()
        self.result_layout = QVBoxLayout(self.result_page)
        self.result_layout.setContentsMargins(0, 0, 0, 0)
        self.result_layout.setSpacing(10)
        self.result_visual = QStackedWidget()
        self.result_visual.setMinimumHeight(160)
        self.result_preview = Preview(height=350, placeholder="成片将在这里显示")
        self.result_preview.setMinimumHeight(150)
        self.result_preview.setMaximumHeight(16777215)
        self.result_preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.result_visual.addWidget(self.result_preview)
        from studio_widgets import GenerationCanvas
        self.generation_canvas = GenerationCanvas()
        self.result_visual.addWidget(self.generation_canvas)
        self.result_layout.addWidget(self.result_visual, 1)
        self.generation_title = label("", "Section")
        self.generation_elapsed = label("", "Small")
        title_row = QHBoxLayout()
        title_row.addWidget(self.generation_title, 1)
        title_row.addWidget(self.generation_elapsed)
        self.result_layout.addLayout(title_row)
        steps = QHBoxLayout()
        self.generation_steps = []
        for text in PHASES:
            step = label(text, "ProgressStep")
            step.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.generation_steps.append(step)
            steps.addWidget(step, 1)
        self.result_layout.addLayout(steps)
        self.generation_bar = QProgressBar()
        self.generation_bar.setTextVisible(False)
        self.generation_bar.setFixedHeight(4)
        self.result_layout.addWidget(self.generation_bar)
        self.generation_estimate = label("", "Small", True)
        # A full-width row lets Qt reserve the height of every wrapped line.
        # Layout AlignRight previously shrank this label and clipped its second line.
        self.generation_estimate.setMinimumWidth(0)
        self.generation_estimate.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.result_layout.addWidget(self.generation_estimate)
        self.result_info = label("生成完成后，成片会自动保存到本机。", "Muted", True)
        self.result_layout.addWidget(self.result_info)
        result_actions = QHBoxLayout()
        self.play_result = button("播放成片", "Soft", "play", self.show_current_video)
        self.open_result = button("打开文件夹", "Quiet", "folder", self.open_output_folder)
        self.refine_result = button("对比优化", "Primary", "spark", self.refine_current)
        self.reedit_result = button("再编辑生成", "Soft", callback=self.reedit_current)
        self.continue_result = button("继续查询", "Quiet", callback=self.continue_generation)
        self.error_result = button("错误详情", "Soft", callback=self.show_current_error)
        self.result_layout.addWidget(self.error_result, alignment=Qt.AlignmentFlag.AlignLeft)
        for action in (self.play_result, self.open_result, self.refine_result):
            action.setEnabled(False)
        result_actions.addWidget(self.play_result)
        result_actions.addWidget(self.open_result)
        result_actions.addWidget(self.reedit_result)
        result_actions.addWidget(self.continue_result)
        result_actions.addStretch(1)
        result_actions.addWidget(self.refine_result)
        self.result_layout.addLayout(result_actions)
        self.editor_pages.addWidget(self.result_page)
        self.workbench_history = HistoryPanel(self.controller, self.load_history_record, self.show_history_record, self.play_history_record)
        self.editor_pages.addWidget(self.workbench_history)
        self.queue_panel = GenerationQueuePanel(self.controller,self.edit_queued,self.show_dialog,self.show_error)
        self.editor_pages.addWidget(self.queue_panel)
        layout.addWidget(self.editor_pages, 1)
        self.job_progress = QProgressBar()
        self.job_progress.setRange(0, 0)
        self.job_progress.setTextVisible(False)
        self.job_progress.setFixedHeight(3)
        self.job_progress.hide()
        layout.addWidget(self.job_progress)
        bottom = QHBoxLayout()
        self.ai_button = button("智能优化提示词", "Primary", "spark", self.analyze)
        self.ai_button.setToolTip("使用当前登录的分析平台理解参考画面并优化提示词；额度来源可在平台登录窗口查看。")
        bottom.addWidget(self.ai_button)
        self.mention_button = button("@ 素材", "Quiet", callback=self.mention_menu)
        bottom.addWidget(self.mention_button)
        self.keyframe_button = button("关键帧", "Soft", callback=self.choose_keyframes)
        self.keyframe_button.setEnabled(False)
        bottom.addWidget(self.keyframe_button)
        bottom.addStretch()
        self.prompt_count = label("0 字", "Muted")
        bottom.addWidget(self.prompt_count)
        edit_layout.addLayout(bottom)
        return container

    def _parameters(self):
        container = card()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(19, 22, 19, 18)
        layout.setSpacing(20)
        layout.addWidget(label("生成设置", "Section"))
        model = QHBoxLayout()
        model.addWidget(label("Seedance 2.5", "Chip"), 1)
        self.task_chip = label("参考生视频", "Small")
        model.addWidget(self.task_chip)
        layout.addLayout(model)
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(13)
        for row, (name, text, values) in enumerate((
            ("resolution", "分辨率", RESOLUTIONS), ("duration", "时长", ["自适应"] + [f"{n} 秒" for n in range(4, 31)]),
            ("ratio", "比例", RATIOS), ("mode", "生成方式", MODES),
        )):
            grid.addWidget(label(text, "Small"), row * 2, 0, 1, 2)
            field = combo(values)
            self.controls[name] = field
            if name == "duration":
                field.currentTextChanged.connect(lambda text: self.controller.set_value("duration", "-1" if text == "自适应" else text.split()[0]))
            else:
                field.currentIndexChanged.connect(lambda _index, key=name, picker=field: self.controller.set_value(key, picker.currentData()))
            grid.addWidget(field, row * 2 + 1, 0, 1, 2)
        self.voice = QCheckBox("生成声音")
        self.voice.setToolTip("Seedance 生成视频时附带声音。")
        self.voice.toggled.connect(lambda value: self.controller.set_value("generate_audio", value))
        grid.addWidget(self.voice, 8, 0, 1, 2)
        more = button("更多参数", "Quiet", "chevron", self.toggle_advanced)
        grid.addWidget(more, 9, 0, 1, 2)
        self.advanced = QWidget()
        advanced = QVBoxLayout(self.advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.setSpacing(13)
        for key, text, values in (("output_format", "视频格式", ["mp4", "mov"]),):
            advanced.addWidget(label(text, "Small"))
            field = combo(values)
            self.controls[key] = field
            field.currentIndexChanged.connect(lambda _index, name=key, picker=field: self.controller.set_value(name, picker.currentData()))
            advanced.addWidget(field)
        for key, text in (("watermark", "添加水印"), ("return_last_frame", "返回尾帧"), ("auto_analyze", "生成前自动分析")):
            field = QCheckBox(text)
            self.controls[key] = field
            if key == "auto_analyze":
                field.setToolTip("默认关闭，可直接使用当前提示词。开启后，参考素材未分析或变化时先抽帧优化。")
            field.toggled.connect(lambda value, name=key: self.controller.set_value(name, value))
            advanced.addWidget(field)
        self.advanced.hide()
        grid.addWidget(self.advanced, 10, 0, 1, 2)
        grid.setRowStretch(11, 1)
        layout.addWidget(scroll(body), 1)
        self.parameter_summary = label("", "Small", True)
        layout.addWidget(self.parameter_summary)
        self.generate_button = button("生成视频 · 付费", "Generate", "arrow", self.generate)
        layout.addWidget(self.generate_button)
        self.preview_button = button("预览请求", "Quiet", "eye", self.preview_request)
        layout.addWidget(self.preview_button)
        self.download_chip = label("自动保存到本机", "Small")
        self.download_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.download_chip)
        return container

    def _library(self):
        page = card()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 25, 28, 22)
        layout.setSpacing(18)
        top = QHBoxLayout()
        top.addWidget(label("历史记录 · 成片与提示词", "Section"), 1)
        refresh = button("刷新", "Quiet", callback=self.refresh_history)
        top.addWidget(refresh)
        self.library_more = button("", "Icon", "more", self.library_menu)
        top.addWidget(self.library_more)
        layout.addLayout(top)
        self.library_history = HistoryPanel(self.controller, self.load_history_record, self.show_history_record, self.play_history_record)
        layout.addWidget(self.library_history, 1)
        return page

    def _usage(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(22)
        metrics = QHBoxLayout()
        metrics.setSpacing(18)
        self.metrics = {}
        for key, title, unit in (("video_count", "已生成视频", "条"), ("seconds", "产出时长", "秒"),
                                 ("completion_tokens", "生成计费单位", "个"), ("estimated_cost_cny", "参考费用", "元（人民币）")):
            section = card()
            item = QVBoxLayout(section)
            item.setContentsMargins(24, 22, 24, 22)
            item.setSpacing(17)
            item.addWidget(label(title, "Muted"))
            value = label("0", "Metric")
            self.metrics[key] = value
            item.addWidget(value)
            item.addWidget(label(unit, "Small"))
            metrics.addWidget(section, 1)
        layout.addLayout(metrics)
        detail = card()
        content = QVBoxLayout(detail)
        content.setContentsMargins(28, 24, 28, 24)
        content.setSpacing(18)
        top = QHBoxLayout()
        top.addWidget(label("用量与调用", "Section"), 1)
        top.addWidget(button("导出用量", "Save", "download", self.export_usage))
        top.addWidget(button("调用记录", "Quiet", "file", self.show_calls))
        content.addLayout(top)
        self.total_tokens = label("", "Muted")
        content.addWidget(self.total_tokens)
        self.fee_note = label("", "Small", True)
        content.addWidget(self.fee_note)
        content.addStretch()
        layout.addWidget(detail, 1)
        return page

    def _uploaded_library(self):
        page = card()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28,25,28,22)
        layout.setSpacing(18)
        layout.addWidget(label('素材库', 'Section'))
        self.material_library_tabs = QTabWidget()
        self.image_library = ImageLibraryPanel(self.controller, self.add_from_image_library, self.preview_library_image)
        self.material_library_tabs.addTab(self.image_library,'本地图片')
        self.uploaded_library = UploadLibraryPanel(self.controller, self.add_from_upload_library, self.renew_from_upload_library)
        self.material_library_tabs.addTab(self.uploaded_library,'已上传视频')
        layout.addWidget(self.material_library_tabs, 1)
        return page

    def add_from_image_library(self, identities):
        def finished():
            self.navigate(0)
        self.guarded(lambda:self.controller.add_library_images(identities,success=finished))

    def preview_library_image(self, identity):
        def action():
            record=self.controller.image_library.get(identity)
            from studio_controller import load_assets
            asset=load_assets([record])[0]
            asset.path=image_copy(record)
            self.preview_asset(asset)
        self.guarded(action)

    def add_from_upload_library(self, identity):
        def action():
            self.controller.add_uploaded_asset(identity)
            self.navigate(0)
        self.guarded(action)

    def renew_from_upload_library(self, identity):
        def action():
            index = self.controller.add_uploaded_asset(identity, renew=True)
            self.navigate(0)
            self.upload_assets([index])
        self.guarded(action)

    def _connect(self):
        controller = self.controller
        controller.changed.connect(self.refresh)
        controller.status_changed.connect(self.status.setText)
        controller.status_changed.connect(lambda _status: self.refresh_submission())
        controller.busy_changed.connect(self.busy)
        controller.failed.connect(self.show_error)
        controller.result_ready.connect(self.result_ready)
        self.prompt.textChanged.connect(self.prompt_changed)
        self.prompt.asset_chosen.connect(self.activate_mentioned_asset)
        self.editor_pages.currentChanged.connect(lambda i: self.editor_tabs.button(i).setChecked(True))
        self.editor_pages.currentChanged.connect(self.refresh_job_progress)
        self.editor_pages.currentChanged.connect(lambda i: self.prompt_more.setVisible(i == 0))
        self.pages.currentChanged.connect(self.refresh_job_progress)

    def refresh(self, category):
        state = self.controller.state
        if category in {'all','queue'}:
            self.queue_panel.refresh()
            waiting = sum(r['status']=='waiting' for r in self.controller.queue.rows())
            self.editor_tabs.button(4).setText(f'待提交 · {waiting}' if waiting else '待提交')
            self.refresh_generation()
        if category in {'all','upload_progress'}:
            self.refresh_upload()
        if category in {'all','uploaded_library'}:
            self.uploaded_library.refresh()
        if category in {'all','image_library'}:
            self.image_library.refresh()
        if category in {"all", "draft"}:
            with QSignalBlocker(self.prompt):
                self.prompt.setPlainText(prompt_for_submission(state))
            self.prompt_changed()
            self.iteration.setText("成片对比" if state["review_output"] else "")
            self.iteration.setToolTip(f"迭代来源：{state['iteration_task_id']}" if state["iteration_task_id"] else "")
            self.ai_button.setText("智能对比优化" if state["review_output"] else "智能优化提示词")
            if category == "draft":
                self.editor_pages.setCurrentIndex(0)
        if category in {"all", "draft", "text", "assets"}:
            self.analysis.setPlainText(review_summary(state, self.controller.assets))
        if category in {"all", "assets", "draft"}:
            self.refresh_assets()
        if category in {"all", "parameters", "draft"}:
            self.refresh_usage()
            self.task_chip.setText({"全模态参考": "参考生视频", "纯文本": "文生视频", "首帧图片": "首帧生视频", "首尾帧图片": "首尾帧生视频"}[state["mode"]])
            for key, widget in self.controls.items():
                with QSignalBlocker(widget):
                    if isinstance(widget, QCheckBox):
                        widget.setChecked(bool(state[key]))
                    else:
                        text = "自适应" if key == "duration" and state[key] == "-1" else f"{state[key]} 秒" if key == "duration" else state[key]
                        widget.setCurrentIndex(widget.findData(text))
            with QSignalBlocker(self.voice):
                self.voice.setChecked(state["generate_audio"])
            duration = "自适应" if state["duration"] == "-1" else f"{state['duration']} 秒"
            self.parameter_summary.setText(f"{state['resolution']} · {duration} · {choice_text(state['ratio'])}\n{'有声' if state['generate_audio'] else '无声'} · {choice_text(state['output_format'])}")
            self.download_chip.setToolTip(state["download_dir"])
        if category in {"all", "account", "models"}:
            active = self.controller.analysis_account()
            platform = PLATFORMS[state['ai_provider']]
            self.account_button.setText(f"{platform} 已连接" if active else "选择分析平台 / 登录")
            self.account_button.setToolTip(self.controller.analysis_label())
        if category in {"all", "history"}:
            self.refresh_history()
            self.refresh_usage()
            self.waiting_samples = self.controller.history.completion_times()
            self.refresh_submission()
        if category in {"all", "history"}:
            row = self.controller.row()
            if row:
                self.update_result(row)
            self.refresh_generation()
        if category == "saved":
            self.saved_chip.setText("已保存")
        elif category in {"text", "parameters", "assets"}:
            self.saved_chip.setText("草稿")
        if category in {"all", "draft", "assets", "parameters"}:
            has_video = bool(reference_videos(self.controller.assets))
            self.ai_button.setText("抽帧并优化" if has_video else "智能对比优化" if state["review_output"] else "智能优化提示词")
            count = sum(a.selected and a.purpose == "keyframe" for a in self.controller.assets)
            self.keyframe_button.setText(f"关键帧 · {count}" if state["frame_catalog"] else "关键帧")
            self.keyframe_button.setEnabled(bool(state["frame_catalog"]) and not self.controller.busy)
            self.refresh_generation()

    def refresh_submission(self):
        call = self.controller.history.latest_submission()
        self.submission_timer.stop()
        if not call:
            self.submission_notice.hide()
            return
        created = datetime.datetime.fromtimestamp(
            call["created_at"], datetime.timezone(datetime.timedelta(hours=8))
        ).strftime("%H:%M:%S")
        tone = "busy"
        if call["status"] == "running":
            elapsed = max(0, int(datetime.datetime.now().timestamp() - call["created_at"]))
            title = "正在提交生成请求"
            detail = f"已等待 {elapsed} 秒 · 等待服务器返回任务编号，响应等待上限约 {CREATE_TIMEOUT_SECONDS // 60} 分钟。"
            self.submission_timer.start()
        elif call["status"] == "unknown":
            tone = "warning"
            title = "提交未确认 · 未收到任务编号"
            seconds = round((call["duration_ms"] or 0) / 1000)
            detail = (f"{created} 提交，约 {seconds} 秒后连接结束（记录 #{call['call_id']}）。"
                      "服务器是否接单需由中转后台核对；请勿重复提交。")
        elif call["status"] == "failed":
            tone = "warning"
            title = "提交失败"
            detail = f"{created} · " + str(call["error"] or "请求未完成，请查看调用记录。")[:350]
        else:
            row = self.controller.row(call["task_id"] or self.controller.state["task_id"])
            if not row:
                self.submission_notice.hide()
                return
            status = row["status"]
            title = {"queued": "任务已提交 · 排队中", "running": "正在生成视频",
                     "succeeded": "视频生成完成", "failed": "视频生成失败",
                     "cancelled": "任务已取消", "expired": "任务已过期"}.get(status, status)
            detail = f"任务编号：{row['task_id']}"
            if status == "succeeded":
                tone = "good"
                if row["local_path"] and Path(row["local_path"]).is_file():
                    title = "视频已自动保存"
                    detail += " · 可在成片或作品库中播放。"
                else:
                    detail += " · 等待自动保存到本机。"
            elif status in {"failed", "cancelled", "expired"}:
                tone = "warning"
                detail += " · " + str(row["error"] or "请查看调用记录。")[:250]
        self.submission_title.setText(title)
        self.submission_detail.setText(neutral_video_text(compact_record(
            detail, (self.controller.api_key, self.controller.openai_key)
        )))
        if self.submission_notice.property("tone") != tone:
            self.submission_notice.setProperty("tone", tone)
            self.submission_notice.style().unpolish(self.submission_notice)
            self.submission_notice.style().polish(self.submission_notice)
        self.submission_notice.show()

    def refresh_assets(self):
        scroll_position = self.asset_scroll.verticalScrollBar().value()
        while self.asset_layout.count():
            item = self.asset_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        for kind, title in (("video", "视频"), ("image", "图片"), ("audio", "音频")):
            # Only group the display: original indexes still own selection and @ bindings.
            entries = [(index, asset) for index, asset in enumerate(self.controller.assets) if asset.kind == kind]
            if not entries:
                continue
            group = QWidget()
            group.setObjectName("AssetGroup")
            group.setProperty("kind", kind)
            content = QVBoxLayout(group)
            content.setContentsMargins(0, 0, 0, 4)
            content.setSpacing(10)
            heading = QHBoxLayout()
            heading.addWidget(label(title, "AssetGroupTitle"))
            heading.addStretch()
            heading.addWidget(label(f"{len(entries)} 项", "Small"))
            content.addLayout(heading)
            for index, asset in entries:
                content.addWidget(AssetCard(asset, index, self.controller, self.insert_mention, self.preview_asset))
            self.asset_layout.addWidget(group)
        if not self.controller.assets:
            empty = label("把图片、视频拖到这里\n或点击导入素材", "Muted", True)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setMinimumHeight(160)
            self.asset_layout.addWidget(empty)
        self.asset_layout.addStretch()
        self.material_count.setText(str(len(self.controller.assets)))
        selected = [a for a in self.controller.assets if a.selected and a.purpose != "competitor"]
        self.material_hint.setText(" · ".join(f"{text} {sum(a.kind == kind for a in selected)}" for kind, text in (("image", "图片"), ("video", "视频"), ("audio", "音频"))))
        self.prompt.set_assets(self.controller.assets)
        self.lock_material_controls(self.controller.busy)
        QTimer.singleShot(0, lambda: self.asset_scroll.verticalScrollBar().setValue(scroll_position))

    def lock_material_controls(self, busy):
        # Keep the scroll area and previews interactive while protecting the job's inputs.
        for widget in self.material_panel.findChildren(QPushButton) + self.material_panel.findChildren(QCheckBox):
            widget.setEnabled(not busy)

    def refresh_upload(self):
        feedback = self.controller.upload_feedback
        active = feedback.get('active',False)
        self.upload_notice.setVisible(active)
        self.refresh_job_progress()
        if not active: return
        phase = feedback.get('phase')
        waiting = feedback.get('phase_seconds',0)
        self.upload_filename.setText(f'{feedback.get("index",1)}/{feedback.get("count",1)} · {feedback.get("filename","")}')
        if phase == 'waiting_response':
            self.upload_title.setText(f'等待中转响应 · {waiting} 秒')
            self.upload_bar.setRange(0,0)
            self.upload_detail.setText('文件已发送，等待返回视频地址。' if waiting < 30 else
                                      '等待较久，可能有网络或中转处理延迟。请勿重复上传；接口等待上限约 5 分钟。')
        elif phase == 'connecting_or_sending':
            self.upload_title.setText(f'发送视频 · {feedback.get("percent",0)}%')
            self.upload_bar.setRange(0,100)
            self.upload_bar.setValue(feedback.get('percent',0))
            sent,total=feedback.get('sent_bytes',0),feedback.get('total_bytes',0)
            self.upload_detail.setText(f'已发送 {sent/1024/1024:.1f} / {total/1024/1024:.1f} MB' if total else '正在连接视频上传服务…')
        else:
            self.upload_title.setText({'validating':'校验本地视频','refreshing_account':'续期上传账号',
                                      'reading_response':'读取上传结果'}.get(phase,'准备上传'))
            self.upload_bar.setRange(0,0)
            self.upload_detail.setText(f'已用 {feedback.get("elapsed_seconds",0)} 秒')

    def refresh_history(self):
        self.workbench_history.refresh()
        self.library_history.refresh()

    def refresh_usage(self):
        summary = self.controller.history.summary()
        for key, widget in self.metrics.items():
            if key == 'estimated_cost_cny':
                continue
            value = summary.get(key) or 0
            widget.setText(f"{value:,.2f}".rstrip("0").rstrip(".") if key == "seconds" else f"{value:,}")
        fees = cny_summary(self.controller.history.rows(), self.controller.state['cny_rates'])
        self.metrics['estimated_cost_cny'].setText(f"{fees['amount']:,.2f}" if fees['amount'] is not None else '待设置')
        note = f"已按当前人民币单价估算 {fees['priced']} 条成片；以中转站实际账单为准。"
        if fees['unpriced']:note += f"另有 {fees['unpriced']} 条缺少单价或用量，未计入金额。"
        self.fee_note.setText(note)
        self.total_tokens.setText(f"总计费单位  {summary['total_tokens'] or 0:,}    ·    任务  {summary['task_count'] or 0} 个")

    def prompt_changed(self):
        text = self.prompt.toPlainText()
        self.prompt_count.setText(f"{len(text):,} 字")
        self.controller.set_value("brief", text)
        self.controller.set_value("prompt", text)

    def busy(self, value):
        self.refresh_submission()
        for widget in (self.ai_button, self.generate_button, self.mention_button, self.keyframe_button, self.prompt, self.parameter_panel, self.refine_result):
            widget.setEnabled(not value)
        self.lock_material_controls(value)
        self.saved_chip.setText("处理中" if value else "已保存")
        self.status_indicator.setText("◌" if value else "●")
        if not value:
            self.keyframe_button.setEnabled(bool(self.controller.state["frame_catalog"]))
            row = getattr(self, "current_result", None)
            self.refine_result.setEnabled(bool(row and row["local_path"] and Path(row["local_path"]).is_file()))
        operation = self.controller.history.call_summary(self.controller.active_call) if self.controller.active_call else None
        if value and operation and operation['operation'] == 'create' and not self.controller.queue_auto_submission:
            self.navigate(0)
            self.editor_pages.setCurrentIndex(2)
        self.refresh_generation()

    def refresh_job_progress(self, *_args):
        # Uploads and the result page have their own contextual progress display.
        self.job_progress.setVisible(self.controller.busy and self.pages.currentIndex() == 0
                                     and self.editor_pages.currentIndex() in {0, 1}
                                     and not self.controller.upload_feedback.get('active', False))

    def navigate(self, index):
        self.pages.setCurrentIndex(index)
        self.nav_group.button(index).setChecked(True)
        self.title.setText(("创作工作台", "历史记录", "用量", "素材库")[index])
        if index == 3:
            self.uploaded_library.refresh()
            self.image_library.refresh()
        if index:
            self.refresh_history()
            self.refresh_usage()

    def toggle_advanced(self):
        self.advanced.setVisible(self.advanced.isHidden())

    def guarded(self, action):
        if self.controller.busy:
            return
        try:
            action()
        except Exception as error:
            self.show_error(str(error), call_id=False)

    def show_error(self, detail, *, call_id=None):
        detail = neutral_video_text(compact_record(detail, (self.controller.api_key, self.controller.openai_key)))
        identity = getattr(self.controller, 'last_error_call_id', None) if call_id is None else call_id
        calls = [call for call in self.controller.history.calls(limit=None) if identity and call['call_id'] == identity]
        if calls:
            response = json.loads(calls[0].get('response_json') or '{}')
            evidence = calls[0].get('error', '') + ' ' + str(response.get('error', '') if isinstance(response, dict) else '')
            if detail not in neutral_video_text(evidence): calls = []
        if not calls:
            calls = [{'created_at': time.time(), 'operation': 'validation', 'status': 'failed', 'error': detail,
                      'response_json': {'error_diagnostics': {'phase': 'validating', 'endpoint': '本地校验（请求未发送）'}}}]
        self.show_dialog(ErrorDetailsDialog(calls, self))

    def analyze(self):
        self.guarded(self.controller.analyze)

    def generate(self):
        def action():
            if needs_video_analysis(self.controller.state, self.controller.assets):
                self.controller.analyze()
                return
            def submit():
                payload = collect_payload(self.controller.state, self.controller.assets)
                if self.ensure_http():
                    identity = self.controller.enqueue_generation(payload)
                    if identity and not self.controller.busy:
                        self.editor_pages.setCurrentIndex(4)
            pending = self.controller.pending_video_uploads()
            if pending:
                self.upload_assets(pending,success=lambda: self.guarded(submit))
                return
            submit()
        self.guarded(action)

    def upload_assets(self, indices, success=None):
        if self.controller.busy: return
        def upload():
            self.guarded(lambda: self.controller.upload_videos(indices,success=success))
        if not self.controller.upload_auth.signed_in():
            self.show_dialog(UploadLoginDialog(self.controller,self,success=upload))
        else:
            upload()

    def choose_keyframes(self):
        if self.controller.busy or not self.controller.state["frame_catalog"]:
            return
        self.show_dialog(KeyframeDialog(self.controller, self.preview_asset, self))

    def ensure_http(self):
        if self.controller.http_allowed():
            return True
        answer = QMessageBox.question(self, "确认中转连接",
            f"当前中转地址使用明文连接：\n{self.controller.state['base_url']}\n\n连接会明文传输密钥和请求。继续并记住此地址？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.state["allow_http_for"] = self.controller.state["base_url"].strip().rstrip("/")
            self.controller.save()
            return True
        return False

    def preview_request(self):
        def action():
            payload = collect_payload(self.controller.state, self.controller.assets)
            self.controller.save()
            self.show_dialog(JsonDialog("提交预览 · 尚未生成", {"request": payload, "素材映射": manifest(self.controller.assets)}, self, payload))
        self.guarded(action)

    def insert_structure(self):
        text, count = structured_prompt(self.prompt.toPlainText(), self.controller.assets, self.controller.state)
        self.prompt.setPlainText(text)
        cursor = self.prompt.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start if not count else QTextCursor.MoveOperation.End)
        self.prompt.setTextCursor(cursor)
        self.prompt.ensureCursorVisible()
        self.prompt.setFocus()
        self.editor_pages.setCurrentIndex(0)
        self.controller.set_status(f"已补齐 {count} 段结构" if count else "已有完整结构")

    def activate_mentioned_asset(self, handle):
        if self.controller.busy:
            return
        for index, asset in enumerate(self.controller.assets):
            if asset.handle == handle and not asset.selected:
                self.controller.update_asset(index, selected=True)
                break

    def insert_mention(self, handle):
        if self.controller.busy:
            return
        self.activate_mentioned_asset(handle)
        self.editor_pages.setCurrentIndex(0)
        self.prompt.insertPlainText(handle + " ")
        self.prompt.setFocus()

    def mention_menu(self):
        menu = QMenu(self)
        for asset in self.controller.assets:
            if asset.purpose != "competitor" and not (asset.upload_id and source_expired(asset)):
                menu.addAction(asset.filename, lambda _checked=False, a=asset: self.insert_mention(a.handle))
        if menu.isEmpty():
            menu.addAction("先导入素材").setEnabled(False)
        menu.exec(self.mention_button.mapToGlobal(self.mention_button.rect().topLeft()))

    def prompt_menu(self):
        menu = QMenu(self)
        menu.addAction("插入提示词结构", self.insert_structure)
        original = self.controller.state.get("ai_original_text", "")
        restore = menu.addAction("恢复优化前原文", self.restore_original_prompt)
        restore.setEnabled(bool(original.strip()) and original != self.prompt.toPlainText())
        menu.addAction("查看创作方案参数", lambda: self.show_dialog(JsonDialog("创作方案", {
            "generation_settings": generation_settings(self.controller.state, self.controller.assets),
            "assets": manifest(self.controller.assets), "creative_brief": original or self.controller.state["brief"],
            "current_prompt": self.controller.state["prompt"], "analysis": self.controller.state["prompt_draft"],
        }, self)))
        menu.addSeparator()
        menu.addAction("清除成片迭代来源", self.clear_iteration)
        menu.ensurePolished()
        position = self.prompt_more.mapToGlobal(QPoint(
            self.prompt_more.width() - menu.sizeHint().width(), self.prompt_more.height() + 4
        ))
        menu.exec(position)

    def restore_original_prompt(self):
        original = self.controller.state.get("ai_original_text", "")
        if not original.strip():
            return
        self.prompt.setPlainText(original)
        self.editor_pages.setCurrentIndex(0)
        self.prompt.setFocus()
        self.controller.save()
        self.controller.set_status("已恢复优化前原文")

    def clear_iteration(self):
        self.controller.state.update(iteration_task_id="", review_output=False)
        self.controller.save()
        self.refresh("draft")

    def import_files(self, purpose=None):
        if self.controller.busy:
            return
        filters = "素材 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif *.mp4 *.mov *.m4v *.webm *.mp3 *.wav)"
        if isinstance(purpose, bool):
            purpose = None
        if purpose == "competitor":
            filters = "视频 (*.mp4 *.mov *.m4v *.webm)"
        files, _ = QFileDialog.getOpenFileNames(self, "导入本地素材", str(Path.home() / "Downloads"), filters)
        if files:
            self.controller.import_files(files, purpose)

    def import_menu(self):
        menu = QMenu(self)
        menu.addAction('从素材库恢复图片 / 视频',lambda: self.navigate(3))
        restore = menu.addAction('恢复上次移除的素材',lambda: self.guarded(self.controller.restore_removed_asset))
        restore.setEnabled(bool(self.controller.state.get('removed_assets')))
        menu.addSeparator()
        for purpose in ("competitor", "character", "scene", "first", "last", "audio", "other"):
            menu.addAction("添加" + PURPOSE[purpose], lambda _checked=False, p=purpose: self.import_files(p))
        menu.addSeparator()
        menu.addAction("导入图片文件夹", self.import_folder)
        urls = menu.addMenu("添加网址")
        for kind, text in (("image", "图片"), ("video", "视频"), ("audio", "音频")):
            urls.addAction(text, lambda _checked=False, k=kind: self.add_url(k))
        menu.exec(self.import_more.mapToGlobal(self.import_more.rect().bottomLeft()))

    def import_folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择图片文件夹")
        if path:
            files = [str(p) for p in Path(path).iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
            if files:
                self.controller.import_files(files)

    def add_url(self, kind):
        url, accepted = QInputDialog.getText(self, "添加素材网址", "可公开访问的安全网址（以 https:// 开头）")
        if not accepted or not url.strip():
            return
        duration = None
        if kind in {"video", "audio"}:
            duration, accepted = QInputDialog.getDouble(self, "参考时长", "素材时长（秒）", 5, 2, 30, 1)
            if not accepted:
                return
        self.guarded(lambda: self.controller.add_url(url, kind, duration))

    def preview_asset(self, asset):
        if asset.path and asset.path.is_file():
            if asset.kind in {"video", "audio"}:
                self.show_dialog(VideoDialog(asset.path, self))
            else:
                dialog = QDialog(self)
                dialog.setWindowTitle(asset.filename)
                dialog.resize(800, 740)
                layout = QVBoxLayout(dialog)
                view = Preview(QPixmap(str(asset.path)), 600, placeholder="图片无法预览")
                layout.addWidget(view, 1)
                layout.addWidget(button("完成", "Save", callback=dialog.accept), alignment=Qt.AlignmentFlag.AlignRight)
                self.show_dialog(dialog)
        elif asset.source_url:
            QDesktopServices.openUrl(QUrl(asset.source_url))
        else:
            self.show_error("素材文件已移动或删除，请重新导入。")

    def update_result(self, row):
        path = Path(row["local_path"]) if row["local_path"] else None
        available = bool(path and path.is_file())
        self.result_preview.pixmap = cached_thumbnail(path, self.controller.directory) if available else QPixmap()
        self.result_preview.callback = self.show_current_video if available else None
        self.result_preview.update()
        self.current_result = row
        for item in (self.play_result, self.open_result, self.refine_result):
            item.setEnabled(available)
        if available:
            media = json.loads(row["media_json"] or "{}")
            dimensions = f"{media['width']} × {media['height']}" if media.get("width") and media.get("height") else row["resolution"]
            seconds = media.get("duration") or row["actual_duration"]
            self.result_info.setText(f"{dimensions}    ·    {seconds:g} 秒    ·    已保存" if seconds else f"{dimensions}    ·    已保存")
            self.result_info.setToolTip(str(path))
        else:
            self.result_info.setText("成片尚未保存，可从作品库继续查询或下载。")

    def result_ready(self, row):
        if row and row["task_id"] == self.controller.state["task_id"]:
            self.update_result(row)
            self.refresh_generation()

    def show_current_video(self):
        row = getattr(self, "current_result", None)
        if row and row["local_path"] and Path(row["local_path"]).is_file():
            self.show_dialog(VideoDialog(Path(row["local_path"]), self))

    def open_output_folder(self):
        row = getattr(self, "current_result", None)
        path = Path(row["local_path"]).parent if row and row["local_path"] else Path(self.controller.state["download_dir"])
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def refine_current(self):
        row = getattr(self, "current_result", None)
        if row:
            self.reuse_history(row, True)

    def reedit_current(self):
        row = getattr(self, 'current_result', None)
        if row:
            self.reuse_history(row)

    def refresh_generation(self):
        call = self.controller.history.latest_submission()
        row = self.controller.row()
        if call and call['status'] == 'succeeded' and row and row['task_id'] != call['task_id']:
            call = self.controller.history.submission_for_task(row['task_id'])
        if call and call['status'] in {'running', 'unknown', 'failed'}:
            row = None
        timing = self.controller.history.task_timing(row['task_id']) if row else {}
        active = self.controller.history.call_summary(self.controller.active_call) if self.controller.active_call else None
        operation = active['operation'] if active and active['task_id'] == (row or {}).get('task_id') else ''
        if row:
            row = {**row, 'local_available':bool(row['local_path'] and Path(row['local_path']).is_file())}
            settings = {'model':row['model'], 'resolution':row['resolution'], 'duration':row['requested_duration']}
        else:
            settings = generation_settings(self.controller.state,self.controller.assets)
        value = generation_feedback(call,row,timing,self.waiting_samples,settings,operation=operation,
                                    paused=self.controller._query_failures > 3)
        self.generation_feedback = value
        self.generation_title.setText(value['title'])
        self.generation_elapsed.setText(value['elapsed'])
        self.generation_estimate.setText(value['estimate'])
        self.generation_estimate.setVisible(bool(value['estimate']))
        self.generation_estimate.setToolTip(value['estimate_source'])
        self.generation_canvas.set_active(value['active'],value['tone'])
        available = bool(row and row['local_available'] and value['phase'] == 4)
        self.result_visual.setCurrentIndex(0 if available or value['tone'] == 'idle' else 1)
        if not available:
            self.result_info.setText(value['detail'])
        for index, step in enumerate(self.generation_steps):
            tone = 'done' if value['phase'] > index else 'current' if value['phase'] == index else 'waiting'
            step.setProperty('stage',tone)
            step.style().unpolish(step);step.style().polish(step)
        self.generation_bar.setRange(0,0 if value['active'] else 100)
        if not value['active']:
            self.generation_bar.setValue(100 if available else 0)
        self.generation_bar.setVisible(value['active'])
        self.refresh_job_progress()
        self.error_result.setVisible(value['tone'] == 'warning' or bool(row and row.get('error')) or
                                     bool(row and any(c.get('status') in {'failed','unknown'} for c in
                                                      self.controller.history.calls(limit=1, task_id=row['task_id']))))
        self.play_result.setVisible(available)
        self.open_result.setVisible(available)
        self.refine_result.setVisible(available)
        self.reedit_result.setVisible(available)
        self.reedit_result.setEnabled(available and not self.controller.busy)
        self.continue_result.setVisible(bool(value['task_id']) and value['phase'] in {1,2,3})
        self.continue_result.setText('保存到本机' if value['phase'] == 3 else '继续查询')
        self.continue_result.setEnabled(not self.controller.busy)
        pending = value['phase'] == 0 or bool(self.controller.pending_generation())
        waiting = any(r['status'] in {'waiting','unknown'} for r in self.controller.queue.rows())
        self.generate_button.setEnabled(not self.controller.busy)
        self.generate_button.setText('先分析视频 →' if needs_video_analysis(self.controller.state,self.controller.assets) else
                                     '加入队列 · 付费' if pending or waiting else '生成视频 · 付费')
        self.generate_button.setToolTip('保存本条提示词、素材和参数，轮到时自动提交。' if pending or waiting else '')
        self.refine_result.setEnabled(available and not self.controller.busy)
        if value['active'] or value['phase'] in {1,2}:
            if not self.generation_timer.isActive():self.generation_timer.start()
        else:
            self.generation_timer.stop()


    def continue_generation(self):
        value = self.generation_feedback
        self.guarded(lambda:self.controller.download(value['task_id']) if value['phase'] == 3
                     else self.controller.query(value['task_id']) if self.ensure_http() else None)

    def edit_queued(self, identity):
        self.controller.edit_queue_item(identity)
        self.navigate(0)
        self.editor_pages.setCurrentIndex(0)
        self.prompt.setFocus()

    def show_current_error(self):
        call = self.controller.history.latest_submission()
        record = call if call and call['status'] in {'running','unknown','failed'} else self.controller.row() or call or {}
        self.show_dialog(ErrorDetailsDialog(related_calls(self.controller.history, record), self))

    def load_history_record(self, record):
        if record['record_kind'] == 'prompt':
            self.guarded(lambda:self.controller.reuse_prompt(record))
            if self.controller.busy:return
            self.navigate(0);self.editor_pages.setCurrentIndex(0);self.prompt.setFocus()
        else:
            self.reuse_history(record)

    def show_history_record(self, record):
        self.show_dialog(HistoryDetailDialog(record,self.controller,self.load_history_record,self.play_history_record,self))

    def play_history_record(self, record):
        path=Path(record['local_path']) if record.get('local_path') else None
        if path and path.is_file():
            self.show_dialog(VideoDialog(path,self))

    def history_menu(self, row, anchor):
        menu = QMenu(anchor)
        local = bool(row["local_path"] and Path(row["local_path"]).is_file())
        play = menu.addAction("播放成片", lambda: self.show_dialog(VideoDialog(Path(row["local_path"]), self)))
        play.setEnabled(local)
        menu.addAction("复用素材与提示词", lambda: self.reuse_history(row))
        refine = menu.addAction("对比优化提示词", lambda: self.reuse_history(row, True))
        refine.setEnabled(local)
        menu.addSeparator()
        menu.addAction("继续查询", lambda: self.guarded(lambda: self.controller.query(row["task_id"]) if self.ensure_http() else None))
        menu.addAction("下载到本机", lambda: self.guarded(lambda: self.controller.download(row["task_id"])))
        menu.addAction("查看详情", lambda: self.show_dialog(JsonDialog("作品详情", row, self)))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def reuse_history(self, row, refine=False):
        if self.controller.busy:
            self.controller.set_status("当前操作完成后可复用作品")
            return
        self.controller.reuse(row, refine)
        self.navigate(0)
        self.editor_pages.setCurrentIndex(0)
        self.prompt.setFocus()

    def library_menu(self):
        menu = QMenu(self)
        menu.addAction("导出生成历史", self.export_usage)
        menu.addAction("查看调用记录", self.show_calls)
        menu.exec(self.library_more.mapToGlobal(self.library_more.rect().bottomLeft()))

    def header_menu(self):
        menu = QMenu(self)
        menu.addAction("保存草稿", self.controller.save)
        menu.addAction("打开下载目录", self.open_output_folder)
        menu.addSeparator()
        menu.addAction("调用记录", self.show_calls)
        menu.addAction("设置", self.open_settings)
        menu.exec(self.header_more.mapToGlobal(self.header_more.rect().bottomLeft()))

    def account_menu(self):
        if self.controller.busy:
            self.controller.set_status('当前操作完成后可切换分析平台')
            return
        self.show_dialog(PlatformLoginDialog(self.controller, self))

    def open_settings(self):
        if self.controller.busy:
            self.controller.set_status("当前操作完成后可更改连接设置")
            return
        SettingsDialog(self.controller, self).exec()

    def show_calls(self):
        self.show_dialog(ErrorDetailsDialog(self.controller.history.calls(limit=None), self, only_errors=True))

    def export_usage(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出用量", "视频生成用量.csv", "用量表格 (*.csv)")
        if path:
            self.controller.export_csv(path)
            self.controller.set_status("用量已导出")

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)
        dialog.finished.connect(lambda _result: self.dialogs.remove(dialog) if dialog in self.dialogs else None)
        dialog.show()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and not self.controller.busy:
            event.acceptProposedAction()

    def dropEvent(self, event):
        files = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile() and Path(url.toLocalFile()).is_file()]
        if files and not self.controller.busy:
            self.controller.import_files(files)
            event.acceptProposedAction()

    def closeEvent(self, event):
        self.submission_timer.stop()
        self.generation_timer.stop()
        for dialog in list(self.dialogs):
            dialog.close()
        self.controller.close()
        super().closeEvent(event)


def main(*, login_platform=None):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    error_log = (APP_DIR / "error.log").open("a", encoding="utf-8")
    faulthandler.enable(error_log)
    application = QApplication(sys.argv)
    install_chinese_translation(application)
    application.setApplicationName("Seedance Studio")
    application.setStyle("Fusion")
    # Tooltips are separate windows and must also receive the application style.
    application.setStyleSheet(TOOLTIP_STYLE)
    application.setFont(QFont("Microsoft YaHei UI", 10))
    try:
        controller = StudioController() if login_platform in PLATFORMS else None
        if controller:
            controller.state['ai_provider'] = login_platform
        window = StudioWindow(controller)
        window.show()
        window.fit_to_screen()
        if controller:
            dialog = PlatformLoginDialog(controller, window)
            window.show_dialog(dialog)
            dialog.login()
        result = application.exec()
    except Exception:
        error_log.write(compact_record(traceback.format_exc()) + "\n")
        error_log.flush()
        QMessageBox.critical(None, "工作室启动失败", f"详细记录：{APP_DIR / 'error.log'}")
        raise
    finally:
        error_log.close()
    return result


if __name__ == "__main__":
    main()
