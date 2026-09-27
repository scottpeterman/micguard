"""Mic Guard GUI: floating mic indicator + tray icon, device-level mute enforcement.

  Red   = every input muted     Green = something is live     Grey = audio backend error
  Click the dot to toggle. Drag to move. Right-click (or the tray icon) for Settings / Quit.

In muted mode the app enforces: if anything unmutes an input it alarms and (optionally) re-mutes.
Quitting leaves devices in whatever state they're in.
"""
import ctypes
import sys
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRectF, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLineEdit, QMenu, QPushButton, QSpinBox, QSystemTrayIcon, QWidget,
)

from .backends import BackendError, load_backend
from .sounds import TONES, tone_path

GREEN = "#22c55e"
RED = "#ef4444"
GREY = "#6b7280"
IS_MAC = sys.platform == "darwin"
BACKEND_RETRY_S = 5.0

DEFAULTS = {
    "auto_remute": True,
    "alarm_on_unmute": True,
    "alarm_on_mute": False,
    "alarm_sound": "Double beep",
    "alarm_file": "",
    "alarm_volume": 80,
    "alarm_repeat_s": 5,
    "poll_ms": 250,
    "always_on_top": True,
    "show_window": True,
    "show_tray": True,
    "hide_dock": True,
    "mute_on_launch": False,
    "size": 56,
}


def set_dock_hidden(hidden):
    """macOS only: NSApp.setActivationPolicy_(1 = accessory, no Dock icon / 0 = regular)."""
    if not IS_MAC:
        return
    try:
        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        send0 = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
        send1 = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(("objc_msgSend", objc))
        nsapp = send0(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
        send1(nsapp, objc.sel_registerName(b"setActivationPolicy:"), 1 if hidden else 0)
    except (OSError, AttributeError):
        pass


# ---------------------------------------------------------------- drawing

def paint_mic(p, s, color, muted):
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(1, 1, s - 2, s - 2))

    white = QColor("white")
    cx = s / 2
    bw, bh = s * 0.22, s * 0.36
    p.setBrush(white)
    p.drawRoundedRect(QRectF(cx - bw / 2, s * 0.18, bw, bh), bw / 2, bw / 2)

    pen = QPen(white, max(1.5, s * 0.055))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    aw = s * 0.40
    p.drawArc(QRectF(cx - aw / 2, s * 0.26, aw, s * 0.38), 180 * 16, 180 * 16)
    p.drawLine(QPointF(cx, s * 0.64), QPointF(cx, s * 0.76))
    p.drawLine(QPointF(cx - s * 0.12, s * 0.76), QPointF(cx + s * 0.12, s * 0.76))
    if muted:
        p.drawLine(QPointF(s * 0.26, s * 0.22), QPointF(s * 0.74, s * 0.82))


def render_mic(size, color, muted):
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    paint_mic(p, size, color, muted)
    p.end()
    return pm


# ---------------------------------------------------------------- widgets

class Indicator(QWidget):
    def __init__(self, guard):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.guard = guard
        self.color, self.muted = GREY, True
        self._press = None
        self._origin = QPoint()
        self._drag = False
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        if IS_MAC:
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)

    def apply_flags(self, on_top, size):
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setFixedSize(size, size)

    def set_state(self, color, muted, tip):
        if (color, muted) != (self.color, self.muted):
            self.color, self.muted = color, muted
            self.update()
        self.setToolTip(tip)

    def paintEvent(self, _event):
        p = QPainter(self)
        paint_mic(p, self.width(), self.color, self.muted)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.globalPosition().toPoint()
            self._origin = self.pos()
            self._drag = False

    def mouseMoveEvent(self, e):
        if self._press is None:
            return
        delta = e.globalPosition().toPoint() - self._press
        if self._drag:
            self.move(self._origin + delta)
        elif delta.manhattanLength() > 4:
            self._drag = True
            # Wayland forbids client-side positioning; hand the drag to the compositor.
            if QGuiApplication.platformName() == "wayland" and self.windowHandle().startSystemMove():
                self._press = None
                return
            self.move(self._origin + delta)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        if self._drag:
            self.guard.cfg.setValue("pos", self.pos())
        else:
            self.guard.toggle()
        self._press = None

    def contextMenuEvent(self, e):
        self.guard.menu.exec(e.globalPos())


class SettingsDialog(QDialog):
    def __init__(self, guard):
        super().__init__()
        self.guard = guard
        self.setWindowTitle("Mic Guard Settings")
        g = guard.get
        form = QFormLayout(self)

        self.auto_remute = QCheckBox("Re-mute automatically if something unmutes")
        self.auto_remute.setChecked(g("auto_remute"))
        self.alarm_on_unmute = QCheckBox("Alarm when an input goes live in muted mode")
        self.alarm_on_unmute.setChecked(g("alarm_on_unmute"))
        self.alarm_on_mute = QCheckBox("Alarm when inputs get muted in open mode")
        self.alarm_on_mute.setChecked(g("alarm_on_mute"))
        form.addRow("Enforcement", self.auto_remute)
        form.addRow("", self.alarm_on_unmute)
        form.addRow("", self.alarm_on_mute)

        self.sound = QComboBox()
        self.sound.addItems(list(TONES) + ["Custom WAV…"])
        self.sound.setCurrentText("Custom WAV…" if g("alarm_file") else g("alarm_sound"))
        test = QPushButton("Test")
        test.clicked.connect(self._test)
        row = QHBoxLayout()
        row.addWidget(self.sound, 1)
        row.addWidget(test)
        form.addRow("Alarm sound", row)

        self.file = QLineEdit(g("alarm_file"))
        self.file.setPlaceholderText("path to a .wav file")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        frow = QHBoxLayout()
        frow.addWidget(self.file, 1)
        frow.addWidget(browse)
        form.addRow("Custom file", frow)
        self.sound.currentTextChanged.connect(self._sync_file_row)
        self._file_widgets = (self.file, browse)
        self._sync_file_row(self.sound.currentText())

        self.volume = QSpinBox()
        self.volume.setRange(0, 100)
        self.volume.setSuffix(" %")
        self.volume.setValue(g("alarm_volume"))
        form.addRow("Alarm volume", self.volume)

        self.repeat = QSpinBox()
        self.repeat.setRange(0, 300)
        self.repeat.setSuffix(" s")
        self.repeat.setSpecialValueText("once")
        self.repeat.setValue(g("alarm_repeat_s"))
        form.addRow("Repeat alarm while live", self.repeat)

        self.poll = QSpinBox()
        self.poll.setRange(100, 5000)
        self.poll.setSingleStep(50)
        self.poll.setSuffix(" ms")
        self.poll.setValue(g("poll_ms"))
        form.addRow("Poll interval", self.poll)

        self.size = QSpinBox()
        self.size.setRange(24, 200)
        self.size.setSuffix(" px")
        self.size.setValue(g("size"))
        form.addRow("Indicator size", self.size)

        self.on_top = QCheckBox("Always on top")
        self.on_top.setChecked(g("always_on_top"))
        self.show_window = QCheckBox("Show floating indicator")
        self.show_window.setChecked(g("show_window"))
        self.show_tray = QCheckBox("Show tray / menu bar icon")
        self.show_tray.setChecked(g("show_tray"))
        self.show_tray.setEnabled(QSystemTrayIcon.isSystemTrayAvailable())
        self.hide_dock = QCheckBox("Hide Dock icon")
        self.hide_dock.setChecked(g("hide_dock"))
        self.mute_on_launch = QCheckBox("Mute all inputs on launch")
        self.mute_on_launch.setChecked(g("mute_on_launch"))
        form.addRow("Display", self.on_top)
        extras = [self.show_window, self.show_tray] + ([self.hide_dock] if IS_MAC else []) + [self.mute_on_launch]
        for w in extras:
            form.addRow("", w)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _sync_file_row(self, text):
        for w in self._file_widgets:
            w.setEnabled(text == "Custom WAV…")

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Alarm sound", str(Path.home()), "WAV audio (*.wav)")
        if path:
            self.file.setText(path)

    def _test(self):
        custom = self.sound.currentText() == "Custom WAV…"
        self.guard.play(self.file.text() if custom else "", self.sound.currentText(), self.volume.value())

    def values(self):
        custom = self.sound.currentText() == "Custom WAV…"
        show_window = self.show_window.isChecked()
        tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
        show_tray = tray_ok and (self.show_tray.isChecked() or not show_window)
        if not show_tray:
            show_window = True  # never leave the app with no visible handle
        return {
            "auto_remute": self.auto_remute.isChecked(),
            "alarm_on_unmute": self.alarm_on_unmute.isChecked(),
            "alarm_on_mute": self.alarm_on_mute.isChecked(),
            "alarm_sound": self.guard.get("alarm_sound") if custom else self.sound.currentText(),
            "alarm_file": self.file.text().strip() if custom else "",
            "alarm_volume": self.volume.value(),
            "alarm_repeat_s": self.repeat.value(),
            "poll_ms": self.poll.value(),
            "size": self.size.value(),
            "always_on_top": self.on_top.isChecked(),
            "show_window": show_window,
            "show_tray": show_tray,
            "hide_dock": self.hide_dock.isChecked(),
            "mute_on_launch": self.mute_on_launch.isChecked(),
        }


# ---------------------------------------------------------------- controller

class MicGuard:
    def __init__(self, app):
        self.app = app
        self.cfg = QSettings("scottpeterman", "micguard")
        self.backend = None
        self.backend_retry_at = 0.0
        self.locked = False
        self.prev_live = None
        self.last_alarm = 0.0
        self.live = []
        self.error = None
        self._icon_key = None
        self.sfx = QSoundEffect()

        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._fill_menu)
        self.act_toggle = QAction("Mute all")
        self.act_toggle.triggered.connect(self.toggle)
        self.act_settings = QAction("Settings…")
        self.act_settings.triggered.connect(self.open_settings)
        self.act_quit = QAction("Quit Mic Guard")
        self.act_quit.triggered.connect(app.quit)

        self.tray = QSystemTrayIcon()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_activated)
        self.indicator = Indicator(self)

        self.timer = QTimer()
        self.timer.timeout.connect(self.poll)

        self.apply_settings()
        self.place_indicator()

        self._ensure_backend()
        self.live = self._read_live()
        self.locked = self.backend is not None and not self.live
        self.prev_live = bool(self.live)
        if self.get("mute_on_launch"):
            self.set_locked(True)
        self.render()
        self.timer.start()
        app.aboutToQuit.connect(self._on_quit)

    # ---- settings

    def get(self, key):
        default = DEFAULTS[key]
        val = self.cfg.value(key, default)
        if isinstance(default, bool):
            return val if isinstance(val, bool) else str(val).lower() in ("true", "1")
        if isinstance(default, int):
            try:
                return int(val)
            except (TypeError, ValueError):
                return default
        return str(val) if val is not None else default

    def apply_settings(self):
        tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
        show_window = self.get("show_window") or not tray_ok
        self.indicator.apply_flags(self.get("always_on_top"), self.get("size"))
        self.indicator.setVisible(show_window)
        self.tray.setVisible(tray_ok and self.get("show_tray"))
        set_dock_hidden(self.get("hide_dock"))
        self.timer.setInterval(self.get("poll_ms"))

    def place_indicator(self):
        pos = self.cfg.value("pos")
        screen_rects = [s.availableGeometry() for s in QGuiApplication.screens()]
        if isinstance(pos, QPoint) and any(r.contains(pos) for r in screen_rects):
            self.indicator.move(pos)
        else:
            geo = self.app.primaryScreen().availableGeometry()
            self.indicator.move(geo.right() - self.get("size") - 24, geo.top() + 24)

    def open_settings(self):
        dlg = SettingsDialog(self)
        dlg.raise_()
        dlg.activateWindow()
        if dlg.exec() == QDialog.DialogCode.Accepted:
            for key, value in dlg.values().items():
                self.cfg.setValue(key, value)
            self.apply_settings()
            self.render()

    # ---- backend

    def _ensure_backend(self):
        if self.backend is not None:
            return True
        now = time.monotonic()
        if now < self.backend_retry_at:
            return False
        try:
            self.backend = load_backend()
            self.error = None
            return True
        except BackendError as e:
            self.error = str(e)
            self.backend_retry_at = now + BACKEND_RETRY_S
            return False

    def _read_live(self):
        if not self._ensure_backend():
            return []
        try:
            live = [d.label for d in self.backend.live_devices()]
            self.error = None
            return live
        except BackendError as e:
            self.error = str(e)
            return []

    def _mute(self):
        try:
            self.backend.mute_all()
        except BackendError as e:
            self.error = str(e)

    # ---- state machine

    def toggle(self):
        self.set_locked(not self.locked)

    def set_locked(self, locked):
        if not self._ensure_backend():
            self.render()
            return
        try:
            if locked:
                self.backend.mute_all()
            else:
                self.backend.restore_all()
            self.error = None
        except BackendError as e:
            self.error = str(e)
        self.locked = locked
        self.poll(initiated=True)

    def poll(self, initiated=False):
        live = self._read_live()
        if self.error:
            self.render()
            return

        is_live = bool(live)
        if not initiated:
            now = time.monotonic()
            if self.locked and is_live:
                repeat = self.get("alarm_repeat_s")
                first = not self.prev_live
                if self.get("alarm_on_unmute") and (first or (repeat and now - self.last_alarm >= repeat)):
                    self.alarm(now)
                if self.get("auto_remute"):
                    self._mute()
                    live = self._read_live()
                    is_live = bool(live)
            elif not self.locked and not is_live and self.prev_live and self.get("alarm_on_mute"):
                self.alarm(now)

        self.live = live
        self.prev_live = is_live
        self.render()

    # ---- output

    def play(self, file_path, tone, volume):
        path = Path(file_path) if file_path else tone_path(tone)
        if not path.is_file():
            path = tone_path("Double beep")
        url = QUrl.fromLocalFile(str(path))
        if self.sfx.source() != url:
            self.sfx.setSource(url)
        self.sfx.setVolume(volume / 100)
        self.sfx.play()

    def alarm(self, now):
        self.last_alarm = now
        self.play(self.get("alarm_file"), self.get("alarm_sound"), self.get("alarm_volume"))

    def render(self):
        if self.error:
            color, muted, tip = GREY, True, f"Audio backend error: {self.error}"
        elif self.live:
            color, muted, tip = GREEN, False, "LIVE: " + ", ".join(self.live)
        else:
            color, muted, tip = RED, True, "All inputs muted"
        tip += "\nMode: " + ("muted (enforced)" if self.locked else "open")

        self.indicator.set_state(color, muted, tip)
        if (color, muted) != self._icon_key:
            self._icon_key = (color, muted)
            self.tray.setIcon(QIcon(render_mic(64, color, muted)))
        self.tray.setToolTip(tip)
        self.act_toggle.setText("Open mics" if self.locked else "Mute all")

    def _fill_menu(self):
        self.menu.clear()
        self.menu.addAction(self.act_toggle)
        self.menu.addSeparator()
        try:
            devs = self.backend.devices() if self.backend else []
        except BackendError:
            devs = []
        for d in devs:
            state = "no control" if not d.controllable else ("LIVE" if d.live else "muted")
            act = self.menu.addAction(f"{d.name}  —  {state}")
            act.setEnabled(False)
        if devs:
            self.menu.addSeparator()
        self.menu.addAction(self.act_settings)
        self.menu.addAction(self.act_quit)

    def _tray_activated(self, reason):
        # Left-click toggles on Windows/Linux; macOS always opens the menu on click.
        if reason == QSystemTrayIcon.ActivationReason.Trigger and not IS_MAC:
            self.toggle()

    def _on_quit(self):
        self.cfg.setValue("pos", self.indicator.pos())
        if self.backend is not None:
            self.backend.close()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Mic Guard")
    app.setQuitOnLastWindowClosed(False)
    guard = MicGuard(app)  # noqa: F841  (kept alive for the app's lifetime)
    sys.exit(app.exec())
