"""Reference editing and dropdown rendering without network or account access."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import unittest
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from media_assets import MediaAsset
from studio_theme import STYLE
from studio_widgets import PromptEditor, StudioComboBox


class PromptEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setStyle("Fusion")
        if Path("C:/Windows/Fonts/msyh.ttc").is_file():
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
        cls.application.setFont(QFont("Microsoft YaHei UI", 10))

    def setUp(self):
        self.editor = PromptEditor()
        self.editor.setStyleSheet(STYLE)
        self.editor.set_assets([MediaAsset(None, "image", "character", "@{角色🎬.png}", "https://example.com/a.png")])
        self.editor.resize(640, 300)
        self.editor.show()
        self.editor.setFocus()
        self.application.processEvents()

    def tearDown(self):
        self.editor.completer.popup().hide()
        self.editor.close()
        self.editor.deleteLater()
        self.application.processEvents()

    def cursor(self, text, start, end=None):
        self.editor.setPlainText(text)
        cursor = self.editor.textCursor()
        cursor.setPosition(len(text[:start].encode("utf-16-le")) // 2)
        if end is not None:
            cursor.setPosition(len(text[:end].encode("utf-16-le")) // 2, QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.application.processEvents()

    def test_backspace_and_delete_remove_whole_reference_and_undo_restores_it(self):
        text = "前🎥 @{角色🎬.png} 后"
        start, end = text.index("@"), text.index("}") + 1
        for key, position, modifier in (
            (Qt.Key.Key_Backspace, end, Qt.KeyboardModifier.NoModifier),
            (Qt.Key.Key_Backspace, start + 4, Qt.KeyboardModifier.NoModifier),
            (Qt.Key.Key_Delete, start, Qt.KeyboardModifier.NoModifier),
            (Qt.Key.Key_Delete, start + 4, Qt.KeyboardModifier.NoModifier),
            (Qt.Key.Key_Backspace, end, Qt.KeyboardModifier.ControlModifier),
        ):
            with self.subTest(key=key, position=position, modifier=modifier):
                self.cursor(text, position)
                self.assertFalse(self.editor.completer.popup().isVisible())
                QTest.keyClick(self.editor, key, modifier)
                self.assertEqual(self.editor.toPlainText(), "前🎥  后")
                self.assertFalse(self.editor.completer.popup().isVisible())
                self.editor.undo()
                self.assertEqual(self.editor.toPlainText(), text)
                self.assertFalse(self.editor.completer.popup().isVisible())
                self.editor.redo()
                self.assertEqual(self.editor.toPlainText(), "前🎥  后")

    def test_partial_selection_expands_to_all_overlapping_references(self):
        text = "前 @{角色🎬.png} 中 @{另一角色.png} 后"
        self.cursor(text, text.index("角色"), text.index("另一") + 2)
        QTest.keyClick(self.editor, Qt.Key.Key_Delete)
        self.assertEqual(self.editor.toPlainText(), "前  后")
        self.editor.undo()
        self.assertEqual(self.editor.toPlainText(), text)

    def test_cut_and_context_menu_delete_preserve_complete_reference(self):
        text = "前 @{角色🎬.png} 后"
        for operation in ("shortcut-cut", "edit-cut", "edit-delete"):
            with self.subTest(operation=operation):
                self.cursor(text, text.index("角色"), text.index(".png"))
                if operation == "shortcut-cut":
                    QTest.keyClick(self.editor, Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier)
                else:
                    menu = self.editor.createStandardContextMenu()
                    next(action for action in menu.actions() if action.objectName() == operation).trigger()
                    menu.deleteLater()
                self.assertEqual(self.editor.toPlainText(), "前  后")
                if "cut" in operation:
                    self.assertEqual(self.application.clipboard().text(), "@{角色🎬.png}")
                self.assertFalse(self.editor.completer.popup().isVisible())
                self.editor.undo()
                self.assertEqual(self.editor.toPlainText(), text)

    def test_deleting_an_incomplete_mention_does_not_reopen_picker(self):
        self.cursor("", 0)
        QTest.keyClicks(self.editor, "@")
        self.assertTrue(self.editor.completer.popup().isVisible())
        QTest.keyClick(self.editor, Qt.Key.Key_Backspace)
        self.assertEqual(self.editor.toPlainText(), "")
        self.assertFalse(self.editor.completer.popup().isVisible())
        QTest.keyClicks(self.editor, "@")
        self.assertTrue(self.editor.completer.popup().isVisible())
        self.cursor("前 @{角色🎬.png} 后", 6)
        self.assertFalse(self.editor.completer.popup().isVisible())

    def test_dropdown_stays_light_and_scrollable_under_dark_system_palette(self):
        previous = self.application.palette()
        dark = QPalette(previous)
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Button, QPalette.ColorRole.Base):
            dark.setColor(role, QColor("#202020"))
        window = QWidget()
        try:
            self.application.setPalette(dark)
            window.setStyleSheet(STYLE)
            layout = QVBoxLayout(window)
            combo = StudioComboBox()
            for value in ["自适应"] + [f"{i} 秒" for i in range(4, 31)]:
                combo.addItem(value)
            layout.addWidget(combo)
            window.resize(320, 400)
            window.show()
            self.application.processEvents()
            combo.showPopup()
            self.application.processEvents()
            popup = combo.view().window()
            pixels = popup.grab().toImage()
            self.assertLess(popup.height(), 400)
            for y in (1, pixels.height() - 2):
                self.assertGreater(min(pixels.pixelColor(pixels.width() // 2, y).getRgb()[:3]), 200)
            self.assertGreater(combo.view().verticalScrollBar().maximum(), 0)
            first = combo.view().visualRect(combo.model().index(0, 0))
            second = combo.view().visualRect(combo.model().index(1, 0))
            self.assertGreaterEqual(second.top(), first.bottom())
            ninth = combo.view().visualRect(combo.model().index(8, 0))
            self.assertGreaterEqual(ninth.top(), combo.view().viewport().height() - 2)
            combo.hidePopup()
            combo.setCurrentIndex(combo.count() - 1)
            combo.showPopup()
            self.application.processEvents()
            index = combo.model().index(3, 0)
            combo.view().scrollTo(index)
            self.application.processEvents()
            QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=combo.view().visualRect(index).center())
            self.assertEqual(combo.currentText(), "6 秒")
            self.assertFalse(popup.isVisible())
        finally:
            window.close()
            window.deleteLater()
            self.application.setPalette(previous)
            self.application.processEvents()


if __name__ == "__main__":
    unittest.main()
