"""Native voice bar interactions and event-driven audio rendering."""

import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QTimer, Qt
    from PySide6.QtGui import QContextMenuEvent, QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMenu, QPushButton
    from jarvis.desktop_ui import VoiceTile

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


if QT_AVAILABLE:

    class PaintProbe(QObject):
        def __init__(self):
            super().__init__()
            self.count = 0

        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Paint:
                self.count += 1
            return False


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is an optional desktop dependency")
class VoiceBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.controller = Mock()
        self.controller.user_paused = False
        self.controller.speech_enabled = True
        self.controller.speaking = False
        self.tile = VoiceTile(self.controller)
        self.probe = PaintProbe()
        self.tile.installEventFilter(self.probe)
        self.tile.show()
        QTest.qWait(20)

    def tearDown(self):
        self.tile.hide()
        self.tile.removeEventFilter(self.probe)
        self.tile.deleteLater()
        self.probe.deleteLater()
        self.app.processEvents()

    def test_compact_bar_has_logo_and_real_expand_button(self):
        self.assertGreaterEqual(self.tile.width(), 280)
        self.assertLessEqual(self.tile.width(), 360)
        self.assertLessEqual(self.tile.height(), 84)
        self.assertGreater(self.tile.width(), self.tile.height() * 3)
        self.assertFalse(self.tile.logo.isNull())
        self.assertIsInstance(self.tile.expand_button, QPushButton)
        self.assertTrue(self.tile.expand_button.isVisible())
        self.assertTrue(self.tile.expand_button.accessibleName())
        QTest.mouseClick(self.tile, Qt.MouseButton.LeftButton, pos=QPoint(110, 36))
        self.controller.show_results.assert_called_once_with()
        QTest.mouseClick(self.tile.expand_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.controller.show_results.call_count, 2)

    def test_drag_moves_and_persists_position_without_opening_results(self):
        local = QPoint(110, 36)
        origin = self.tile.pos()
        start = self.tile.mapToGlobal(local)
        delta = QPoint(QApplication.startDragDistance() + 25, 18)
        events = (
            (
                QEvent.Type.MouseButtonPress,
                local,
                start,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
            ),
            (
                QEvent.Type.MouseMove,
                local + delta,
                start + delta,
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
            ),
            (
                QEvent.Type.MouseButtonRelease,
                local,
                start + delta,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
            ),
        )
        for kind, position, global_position, button, buttons in events:
            self.app.sendEvent(
                self.tile,
                QMouseEvent(
                    kind,
                    QPointF(position),
                    QPointF(global_position),
                    button,
                    buttons,
                    Qt.KeyboardModifier.NoModifier,
                ),
            )
        self.assertEqual(self.tile.pos(), origin + delta)
        self.controller.save_tile_position.assert_called_once_with(
            self.tile.x(), self.tile.y()
        )
        self.controller.show_results.assert_not_called()

    def test_dragging_expand_button_does_not_trigger_a_click(self):
        button = self.tile.expand_button
        local = button.rect().center()
        origin = self.tile.pos()
        start = button.mapToGlobal(local)
        delta = QPoint(QApplication.startDragDistance() + 25, 18)
        events = (
            (
                QEvent.Type.MouseButtonPress,
                local,
                start,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
            ),
            (
                QEvent.Type.MouseMove,
                local + delta,
                start + delta,
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
            ),
            (
                QEvent.Type.MouseButtonRelease,
                local,
                start + delta,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
            ),
        )
        for kind, position, global_position, mouse_button, buttons in events:
            self.app.sendEvent(
                button,
                QMouseEvent(
                    kind,
                    QPointF(position),
                    QPointF(global_position),
                    mouse_button,
                    buttons,
                    Qt.KeyboardModifier.NoModifier,
                ),
            )
        self.assertEqual(self.tile.pos(), origin + delta)
        self.controller.save_tile_position.assert_called_once_with(
            self.tile.x(), self.tile.y()
        )
        self.controller.show_results.assert_not_called()
        self.assertFalse(button.isDown())
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.controller.show_results.assert_called_once_with()

    def test_context_menu_preserves_pause_speech_settings_models_and_quit(self):
        point = QPoint(110, 36)
        executed = []

        class NonBlockingMenu(QMenu):
            def exec(self, position):
                executed.append(self)
                return None

        with patch("jarvis.desktop_ui.QMenu", NonBlockingMenu):
            self.app.sendEvent(
                self.tile,
                QContextMenuEvent(
                    QContextMenuEvent.Reason.Mouse,
                    point,
                    self.tile.mapToGlobal(point),
                ),
            )
        self.assertEqual(len(executed), 1)
        menu = executed[0]
        actions = {action.text(): action for action in menu.actions()}
        for label in (
            "暂停聆听",
            "朗读任务结果",
            "停止播报",
            "查看任务与结果",
            "设置",
            "语音模型管理",
            "下载语音模型",
            "退出 JARVIS",
        ):
            self.assertIn(label, actions)
        self.assertTrue(actions["朗读任务结果"].isChecked())
        self.assertFalse(actions["停止播报"].isEnabled())
        actions["暂停聆听"].trigger()
        self.controller.toggle_pause.assert_called_once_with()
        actions["查看任务与结果"].trigger()
        self.controller.show_results.assert_called_once_with()
        actions["设置"].trigger()
        self.controller.show_settings.assert_called_once_with()
        actions["语音模型管理"].trigger()
        self.controller.show_models.assert_called_once_with()
        models = actions["下载语音模型"].menu()
        model_actions = models.actions()
        self.assertEqual(len(model_actions), 2)
        model_actions[0].trigger()
        self.controller.download_model.assert_called_with("stt")
        model_actions[1].trigger()
        self.controller.download_model.assert_called_with("tts")
        actions["退出 JARVIS"].trigger()
        self.controller.request_quit.assert_called_once_with()

    def test_audio_is_real_bounded_input_and_silence_resets_it(self):
        self.assertFalse(self.tile.listening)
        self.tile.set_audio_level(0.8)
        self.assertEqual(self.tile.audio_level, 0)
        self.tile.set_listening(True)
        self.tile.set_audio_level(0.5)
        self.assertGreater(self.tile.audio_level, 0)
        self.assertLessEqual(self.tile.audio_level, 1)
        for index in range(100):
            self.tile.set_audio_level((index % 20 + 1) / 20)
        self.assertLessEqual(len(self.tile._levels), 24)
        self.assertTrue(all(0 <= level <= 1 for level in self.tile._levels))
        self.tile.set_audio_level(0.8, speech=False)
        self.assertEqual(self.tile.audio_level, 0)
        self.assertFalse(any(self.tile._levels))
        self.tile.set_audio_level(0.7)
        self.tile.set_audio_level(0)
        self.assertEqual(self.tile.audio_level, 0)
        self.assertFalse(any(self.tile._levels))

    def test_paused_error_and_explicit_reset_clear_active_wave(self):
        self.tile.set_listening(True)
        self.tile.set_audio_level(0.7)
        self.tile.set_listening(False)
        self.assertFalse(self.tile.listening)
        self.assertEqual(self.tile.audio_level, 0)
        self.assertFalse(any(self.tile._levels))
        self.tile.set_audio_level(1)
        self.assertEqual(self.tile.audio_level, 0)
        self.tile.set_listening(True)
        self.tile.set_audio_level(0.7)
        self.tile.set_state("麦克风不可用", "测试设备错误", error=True)
        self.assertEqual(self.tile.audio_level, 0)
        self.tile.set_audio_level(1)
        self.assertEqual(self.tile.audio_level, 0)
        self.tile.set_state("正在聆听", "测试麦克风输入", error=False)
        self.tile.set_listening(True)
        self.tile.set_audio_level(0.7)
        self.tile.reset_audio_level()
        self.assertEqual(self.tile.audio_level, 0)
        self.assertFalse(any(self.tile._levels))

    def test_invalid_audio_values_never_poison_paint_geometry(self):
        self.tile.set_listening(True)
        for level in (float("nan"), float("inf"), -float("inf"), None, "invalid"):
            with self.subTest(level=level):
                self.tile.set_audio_level(0.7)
                self.tile.set_audio_level(level)
                self.assertEqual(self.tile.audio_level, 0)
                self.assertFalse(any(self.tile._levels))
        self.tile.set_audio_level(10)
        self.assertLessEqual(self.tile.audio_level, 1)
        self.tile.set_audio_level(-10)
        self.assertEqual(self.tile.audio_level, 0)

    def test_idle_silence_and_hidden_updates_do_not_schedule_animation(self):
        self.assertFalse(self.tile.findChildren(QTimer))
        self.tile.set_listening(True)
        QTest.qWait(20)
        initial = self.probe.count
        for _ in range(50):
            self.tile.set_audio_level(0, speech=False)
        QTest.qWait(60)
        self.assertEqual(self.probe.count, initial)
        self.tile.set_audio_level(0.75)
        QTest.qWait(20)
        active = self.probe.count
        self.assertGreater(active, initial)
        QTest.qWait(60)
        self.assertEqual(self.probe.count, active)
        self.tile.hide()
        self.app.processEvents()
        hidden = self.probe.count
        for index in range(20):
            self.tile.set_audio_level(index / 20)
        self.tile.reset_audio_level()
        QTest.qWait(40)
        self.assertEqual(self.probe.count, hidden)
        self.assertEqual(self.tile.audio_level, 0)
        self.assertFalse(any(self.tile._levels))


if __name__ == "__main__":
    unittest.main()
