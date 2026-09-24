"""Quilvo — dictate-anywhere voice-to-text for Windows, fully local.

Press the hotkey (default Alt+PageDown) anywhere -> the screen edges glow and a
"dynamic island" appears top-center (hover it to pick models). Speak, stop, and
the text is typed straight into whatever field your cursor is in.

Pipeline:  mic -> faster-whisper (STT) -> optional local LLM cleanup -> paste at cursor

Run:   pyw flow.py   (or double-click Quilvo.bat; the packaged build is Quilvo.exe)
Stays running in the background, idle until the hotkey is pressed.
"""
__version__ = "1.1.0"

import os
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# Plain HTTP instead of Hugging Face's Xet transfer: measured 78 MB in 2.3 s vs 54 s,
# and it writes the file as it goes, which is what the island's progress ring reads.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
import sys, time, json, logging, threading, subprocess
from logging.handlers import RotatingFileHandler
import numpy as np
import requests
import sounddevice as sd
from pynput import keyboard
from pynput.keyboard import Controller, Key
from faster_whisper import WhisperModel

from PySide6.QtCore import (Qt, QObject, Signal, Slot, QUrl, QByteArray, QBuffer, QTimer, QRect,
                            QMimeData, QLockFile)
from PySide6.QtGui import QIcon, QAction, QCursor, QRegion
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout,
                               QSystemTrayIcon, QMenu, QFileDialog)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel

import models

# ── Config ──────────────────────────────────────────────
# HOTKEY / WHISPER_MODEL / LLM_MODEL / CLEANUP are first-run defaults; after that
# settings.json wins (models picked in the island; hotkey edited by hand).
HOTKEY        = "<alt>+<page_down>"     # pynput format
LLM_MODEL     = "llama3.1:8b"
KEEP_ALIVE    = "30m"
WHISPER_MODEL = "small"                 # multilingual STT (English + Spanish); "medium" = more accurate, ~3x slower
# Languages are picked in the island and saved in settings.json. Detection only
# chooses among the picked ones: full auto once heard Spanish/English as Russian.
# Whisper tends to translate everything into one language when you mix them.
# For English + Spanish, a mixed prompt keeps each word in the language it was spoken.
MIXED_PROMPTS = {frozenset({"en", "es"}): ("Hola, ¿qué tal? Today I'm working on el proyecto, "
                 "luego vamos a revisar the code y después hacemos el deploy.")}
CLEANUP       = False                    # False = paste exactly what you said (pure dictation)
                                         # True = llama tidies punctuation/filler (NOT a chatbot)
SAMPLE_RATE   = 16000

CLEANUP_PROMPT = (
    "You are a dictation cleanup engine. The input is raw speech-to-text.\n"
    "Return ONLY the cleaned text with correct punctuation and capitalization, "
    "filler words (um, uh, like, you know) removed, and obvious transcription "
    "slips fixed. Do NOT answer questions, add commentary, translate, or wrap "
    "in quotes. Preserve the speaker's wording and meaning, and keep every word "
    "in the language it was spoken in."
)

STYLE         = "edge"                  # "edge" = full-screen reactive edge glow
                                         # "pill" = floating liquid-glass pill (previous look)

# bundled, read-only files live next to the script (or in PyInstaller's unpack
# dir); settings + log go to %APPDATA%\Quilvo, which stays writable once installed
HERE         = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DATA_DIR     = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Quilvo")
os.makedirs(DATA_DIR, exist_ok=True)
LOG_PATH     = os.path.join(DATA_DIR, "flow.log")
ICON_PATH    = os.path.join(HERE, "assets", "icon.png")
OVERLAY_HTML = os.path.join(HERE, "overlay.html")   # pill
EDGE_HTML    = os.path.join(HERE, "edge.html")      # edge glow
EDGE_FADE_MS = 450                                  # glow shrink-back time before the window hides
ISLAND_HTML  = os.path.join(HERE, "island.html")    # model picker, top-center
ISLAND_MARGIN = 12                                  # = --m in island.html
ISLAND_W, ISLAND_H = 216, 38                        # collapsed size (= --cw / --ch)
ISLAND_MAX_W, ISLAND_MAX_H = 380, 620               # expanded width (= --ew) / panel height cap
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
LEGACY_SETTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
PILL_W, PILL_H = 248, 48
PAD    = 24     # transparent margin around the pill for its drop shadow (= --pad in overlay.html)
MARGIN = 40     # extra desktop grabbed past the window so the blur has clean edges (= --m)
WIN_W, WIN_H = PILL_W + 2 * PAD, PILL_H + 2 * PAD


# ── JS<->Python bridge so a click on the pill can reach Python ──
class WebBridge(QObject):
    clicked = Signal()

    @Slot()
    def onClick(self):
        self.clicked.emit()


# ── Base: an HTML page rendered in a transparent, non-focusable web view ──
class WebOverlay(QWidget):
    def __init__(self, html, extra_flags=Qt.WindowType(0)):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.Tool | Qt.WindowDoesNotAcceptFocus | extra_flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)   # keep target field focused
        self._ready = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView(self)
        self.view.page().setBackgroundColor(Qt.transparent)
        self.view.setAttribute(Qt.WA_TranslucentBackground)

        # web channel: pill click in JS -> web.clicked signal in Python
        self.web = WebBridge()
        self.channel = QWebChannel()
        self.channel.registerObject("flow", self.web)
        self.view.page().setWebChannel(self.channel)

        self._pending = []                   # calls made before the page finished loading
        self.view.loadFinished.connect(self._on_loaded)
        self.view.load(QUrl.fromLocalFile(html))
        lay.addWidget(self.view)

    def _on_loaded(self, ok):
        self._ready = ok
        if ok:
            for code in self._pending:
                self.view.page().runJavaScript(code)
        self._pending.clear()

    def _js(self, code):
        if self._ready:
            self.view.page().runJavaScript(code)
        elif code.startswith("setModels"):   # only the catalog matters once loaded
            self._pending.append(code)

    def set_level(self, lvl):
        self._js(f"setLevel({lvl:.3f})")

    def set_state(self, s):
        self._js(f"setState('{s}')")

    def dismiss(self):
        self.hide()


# ── The record pill: liquid-glass capsule above the taskbar ──
class Overlay(WebOverlay):
    def __init__(self):
        super().__init__(OVERLAY_HTML)
        self.setFixedSize(WIN_W, WIN_H)

    def _capture_backdrop(self, x, y):
        """Snapshot the desktop under (and around) the pill so the glass can frost it."""
        try:
            screen = QApplication.primaryScreen()
            pm = screen.grabWindow(0, x - MARGIN, y - MARGIN,
                                   self.width() + 2 * MARGIN, self.height() + 2 * MARGIN)
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QBuffer.OpenModeFlag.WriteOnly)
            pm.save(buf, "PNG")
            buf.close()
            uri = "data:image/png;base64," + bytes(ba.toBase64()).decode()
            self._js(f"setBackdrop('{uri}')")
        except Exception as e:
            log.warning("backdrop capture failed: %s", e)

    def present(self):
        scr = QApplication.primaryScreen().availableGeometry()
        x = scr.center().x() - self.width() // 2
        y = scr.bottom() - 150 - PAD     # pill itself sits where it always did
        self.move(x, y)
        self._capture_backdrop(x, y)     # grab BEFORE show (pill not yet visible)
        self.show()
        self.raise_()
        self._js("start()")


# ── The edge glow: covers the whole primary screen, click-through ──
class EdgeOverlay(WebOverlay):
    def __init__(self):
        super().__init__(EDGE_HTML, Qt.WindowTransparentForInput)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def present(self):
        self._hide_timer.stop()          # a new take cancels a pending fade-out
        self.setGeometry(QApplication.primaryScreen().geometry())
        self.show()
        self.raise_()
        self._js("start()")

    def dismiss(self):
        self._js("leave()")              # glow shrinks back into the edges...
        self._hide_timer.start(EDGE_FADE_MS)   # ...then the window goes away


# ── The island: model picker, top-center. Interactive (hover/click) but never
#    takes focus, so the paste still lands in the field you were typing in. ──
class IslandApi(QObject):
    selected = Signal(str, str)          # kind ("stt" | "llm"), model id
    browse_requested = Signal(str)
    resized = Signal(int, int)
    finished = Signal()
    loaded = Signal()

    @Slot(str, str)
    def select(self, kind, model_id):
        self.selected.emit(kind, model_id)

    @Slot(str)
    def browse(self, kind):
        self.browse_requested.emit(kind)

    @Slot(int, int)
    def resize(self, w, h):
        self.resized.emit(w, h)

    @Slot()
    def done(self):
        self.finished.emit()

    @Slot()
    def ready(self):
        self.loaded.emit()


class IslandOverlay(WebOverlay):
    def __init__(self):
        super().__init__(ISLAND_HTML)
        self.api = IslandApi()
        self.channel.registerObject("island", self.api)
        # the window hugs the island (grows while expanded), so the transparent
        # area around it never swallows clicks meant for the apps behind
        # The window is created once at the island's largest size and never resized
        # (resizing makes Chromium drop a frame → the island blinked out mid-morph).
        # Instead a window region clips it to the island's current footprint, so the
        # transparent area around it still lets clicks through to the apps behind.
        m = ISLAND_MARGIN
        self.setFixedSize(ISLAND_MAX_W + 2 * m, ISLAND_MAX_H + 2 * m)
        self._shape = QRect()
        self.api.resized.connect(self._set_shape)
        self.api.finished.connect(self._gone)
        self._set_shape(ISLAND_W + 2 * m, ISLAND_H + 2 * m)
        # hover is decided here from the real cursor, not by Chromium (which
        # reports false mouse-leaves while the window resizes under the pointer)
        self._hover = False
        self._hover_timer = QTimer(self)
        self._hover_timer.timeout.connect(self._poll_hover)

    def _poll_hover(self):
        m = ISLAND_MARGIN
        island = self._shape.adjusted(m, m, -m, -m).translated(self.pos())
        inside = island.contains(QCursor.pos())
        if inside != self._hover:
            self._hover = inside
            self._js(f"setHover({'true' if inside else 'false'})")

    def _gone(self):
        self._hover_timer.stop()
        self._hover = False
        self.hide()

    def _place(self):
        scr = QApplication.primaryScreen().geometry()
        self.move(scr.center().x() - self.width() // 2, scr.top())

    def _set_shape(self, w, h):
        """Clip the window to the island's footprint (top-centered, w×h incl. margin)."""
        self._shape = QRect((self.width() - w) // 2, 0, w, h)
        self.setMask(QRegion(self._shape))

    def set_models(self, payload):
        self._js(f"setModels({json.dumps(payload)})")

    def set_model_status(self, kind, model_id, status):
        self._js(f"setModelStatus({json.dumps(kind)}, {json.dumps(model_id)}, {json.dumps(status)})")

    def set_model_progress(self, kind, model_id, progress):
        self._js(f"setModelProgress({json.dumps(kind)}, {json.dumps(model_id)}, {progress:.4f})")

    def present(self):
        self._place()
        self._set_shape(ISLAND_W + 2 * ISLAND_MARGIN, ISLAND_H + 2 * ISLAND_MARGIN)
        self.show()
        self.raise_()                    # above the edge glow
        self._hover = False
        self._js("start()")
        self._hover_timer.start(30)

    def dismiss(self):
        self._js("leave()")              # JS calls done() once it has animated away


log = logging.getLogger("flow")


AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command():
    """How Windows should start this Quilvo: the packaged exe, or pythonw + this script."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            winreg.QueryValueEx(k, "Quilvo")
            return True
    except OSError:
        return False


def set_autostart(on):
    """Per-user 'Start with Windows' (HKCU Run key) — no installer or admin needed."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, "Quilvo", 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(k, "Quilvo")
                except FileNotFoundError:
                    pass
    except OSError as e:
        log.warning("could not change autostart: %s", e)


def disable_power_throttling():
    """Windows 11 parks background processes (Quilvo's windows are hidden most of
    the time) on efficiency cores; measured under pythonw, that made transcription
    erratic and up to ~2x slower. Opt this process out of that throttling."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                    ("StateMask", wintypes.ULONG)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetProcessInformation.restype = wintypes.BOOL
    # 4 = ProcessPowerThrottling; control EXECUTION_SPEED (0x1) with state 0 = never throttle
    state = PROCESS_POWER_THROTTLING_STATE(1, 0x1, 0)
    if not k32.SetProcessInformation(k32.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state)):
        log.warning("could not disable power throttling (error %d)", ctypes.get_last_error())


def setup_logging():
    """pythonw / a packaged build has no console — errors go to %APPDATA%\\Quilvo\\flow.log."""
    handlers = [RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "urllib3", "faster_whisper"):   # chatty; Quilvo logs its own timings
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # warns "unauthenticated requests" on every model download — expected, no account needed
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    sys.excepthook = lambda *exc: log.critical("uncaught", exc_info=exc)
    threading.excepthook = lambda a: log.critical("uncaught in thread %s", a.thread.name,
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


# ── Settings: which models the user picked ──
def default_languages():
    """English plus the Windows display language."""
    sys_lang = models.system_language()
    return ["en"] if sys_lang in (None, "en") else ["en", sys_lang]


def load_settings():
    s = {"hotkey": HOTKEY, "stt": WHISPER_MODEL, "stt_paths": [],
         "llm": f"ollama:{LLM_MODEL}" if CLEANUP else ""}
    existed = os.path.exists(SETTINGS_PATH) or os.path.exists(LEGACY_SETTINGS)
    if not os.path.exists(SETTINGS_PATH) and os.path.exists(LEGACY_SETTINGS):
        try:                                   # one-time move from the script folder
            os.replace(LEGACY_SETTINGS, SETTINGS_PATH)
        except OSError:
            pass
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    if "languages" not in s:          # [] = detect any language
        s["languages"] = ["en", "es"] if existed else default_languages()   # 1.0 was en + es
    return s


def valid_hotkey(hk):
    """The hotkey from settings.json, or the default if it isn't valid pynput syntax."""
    try:
        if isinstance(hk, str) and keyboard.HotKey.parse(hk):
            return hk
    except ValueError:
        pass
    log.warning("invalid hotkey %r in settings.json; using %s", hk, HOTKEY)
    return HOTKEY


def hotkey_label(hk):
    """'<ctrl>+<shift>+d' -> 'Ctrl+Shift+D', for the tray tooltip and messages."""
    return "+".join(p.strip("<>").replace("_", " ").title().replace(" ", "") if len(p) > 1 else p.upper()
                    for p in hk.split("+"))


def save_settings(s):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except OSError as e:
        log.warning("could not save settings: %s", e)


# ── Bridge: worker thread -> GUI (Qt signals are thread-safe to emit) ──
class Bridge(QObject):
    trigger = Signal()       # hotkey pressed
    state = Signal(str)
    level = Signal(float)
    hide = Signal()
    models = Signal(str)                 # model catalog JSON for the island
    model_status = Signal(str, str, str) # kind, id, "loading" | "ready" | "error"
    model_progress = Signal(str, str, float)   # kind, id, 0..1 download progress (-1 = unknown)
    paste = Signal(str)                  # clipboard work must happen on the GUI thread
    notify = Signal(str)                 # tray message (errors the user should see)
    info = Signal(str)                   # tray message (first-run download, ready)


class FlowApp:
    def __init__(self):
        self.bridge = Bridge()
        self.overlay = EdgeOverlay() if STYLE == "edge" else Overlay()
        self.island = IslandOverlay()
        self.kb = Controller()

        self.bridge.trigger.connect(self._on_trigger)
        for view in (self.overlay, self.island):
            self.bridge.state.connect(view.set_state)
            self.bridge.level.connect(view.set_level)
            self.bridge.hide.connect(view.dismiss)
        self.overlay.web.clicked.connect(self._on_trigger)   # click pill = toggle

        self.bridge.models.connect(self.island.set_models)
        self.bridge.model_status.connect(self.island.set_model_status)
        self.bridge.model_progress.connect(self.island.set_model_progress)
        self.island.api.selected.connect(self._select_model)
        self.island.api.browse_requested.connect(self._browse_model)
        self.island.api.loaded.connect(self._push_models)
        self.bridge.paste.connect(self._paste)
        self.bridge.notify.connect(lambda msg: self.tray.showMessage(
            "Quilvo", msg, QSystemTrayIcon.MessageIcon.Warning, 5000))
        self.bridge.info.connect(lambda msg: self.tray.showMessage(
            "Quilvo", msg, QSystemTrayIcon.MessageIcon.Information, 5000))

        self._busy = False
        self._stop = threading.Event()      # stop recording (hotkey again)
        self._cancel = threading.Event()    # discard (Esc)
        self.settings = load_settings()
        self.hotkey = valid_hotkey(self.settings["hotkey"])
        self.restart = False                # tray "Restart Quilvo": relaunch after quitting
        self.catalog = {"stt": [], "llm": []}
        self.languages = models.whisper_languages()
        self.stt = None
        self.stt_id = None
        self._stt_wanted = None             # newest pick; older loads that finish late are dropped
        self._lang_model = None             # tiny Whisper used only to detect the language

        self._build_tray()
        # load whisper + scan for models in the background so startup is instant
        self._spawn(self._load_stt, self.settings["stt"])
        self._spawn(self._load_lang_detector)
        self._spawn(self._refresh_catalog)
        # global hotkey + Esc listeners
        self._hotkeys = keyboard.GlobalHotKeys({self.hotkey: self.bridge.trigger.emit})
        self._hotkeys.start()
        keyboard.Listener(on_press=self._on_key, daemon=True).start()

    def _build_tray(self):
        self.tray = QSystemTrayIcon(QIcon(ICON_PATH))
        self.tray.setToolTip(f"Quilvo {__version__} — {hotkey_label(self.hotkey)} to dictate")
        self.menu = QMenu()                       # keep refs so they aren't GC'd
        self.autostart_act = QAction("Start with Windows", self.menu, checkable=True)
        self.autostart_act.setChecked(autostart_enabled())
        self.autostart_act.toggled.connect(set_autostart)
        self.settings_act = QAction("Open settings", self.menu)
        self.settings_act.triggered.connect(self._open_settings)
        self.logs_act = QAction("Open log folder", self.menu)
        self.logs_act.triggered.connect(lambda: os.startfile(DATA_DIR))
        self.restart_act = QAction("Restart Quilvo", self.menu)
        self.restart_act.triggered.connect(self._restart)
        self.quit_act = QAction("Quit Quilvo", self.menu)
        self.quit_act.triggered.connect(lambda: QApplication.quit())
        self.menu.addActions([self.autostart_act, self.settings_act, self.logs_act])
        self.menu.addSeparator()
        self.menu.addActions([self.restart_act, self.quit_act])
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_tray_click)
        self.tray.show()

    def _open_settings(self):
        # Notepad, not os.startfile: .json often has no app associated
        save_settings(self.settings)                # make sure the file exists
        subprocess.Popen(["notepad.exe", SETTINGS_PATH])

    def _restart(self):
        self.restart = True                         # relaunched in __main__ once the lock is free
        QApplication.quit()

    def _on_tray_click(self, reason):
        # left-click also opens the menu (handy if right-click is finicky)
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.menu.popup(self.tray.geometry().center())

    def selftest(self):
        """Smoke test for a packaged build (`Quilvo.exe --selftest`): once the speech
        model is ready, start a take without the keyboard, check the overlays and
        mic, cancel it and quit. Results go to the log."""
        if self.stt is None:
            QTimer.singleShot(500, self.selftest)
            return
        log.info("selftest: hotkey listener running=%s", self._hotkeys.is_alive())
        self._on_trigger()

        def check():
            log.info("selftest: glow visible=%s, island visible=%s, recording=%s",
                     self.overlay.isVisible(), self.island.isVisible(), self._busy)
            self._cancel.set()
            self._stop.set()
            QTimer.singleShot(1500, QApplication.quit)
        QTimer.singleShot(2500, check)

    @staticmethod
    def _spawn(fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    # ── models ──
    def _push_models(self):
        self.bridge.models.emit(json.dumps({
            "stt": {"current": self.stt_id or self.settings["stt"], "items": self.catalog["stt"]},
            "llm": {"current": self.settings["llm"], "items": self.catalog["llm"]},
            "lang": {"current": self.settings["languages"], "items": self.languages},
        }))

    def _refresh_catalog(self):
        try:
            self.catalog = {"stt": models.whisper_models(self.settings["stt_paths"]),
                            "llm": models.llm_models()}
        except Exception as e:
            log.warning("model scan failed: %s", e)
        self._push_models()

    def _load_stt(self, model_id):
        """Load (downloading if needed) a Whisper model; the old one keeps working meanwhile."""
        self._stt_wanted = model_id
        first_download = False
        self.bridge.model_status.emit("stt", model_id, "loading")
        try:
            try:        # already downloaded: load offline, no Hugging Face round-trip
                m = WhisperModel(model_id, device="cpu", compute_type="int8", local_files_only=True)
            except Exception:
                downloading = threading.Event()
                downloading.set()
                self._spawn(self._report_download, model_id, downloading)
                if self.stt is None:                # first run: nothing to dictate with yet
                    size = models.APPROX_SIZE.get(model_id)
                    self.bridge.info.emit(f"Downloading the speech model ({models.fmt_size(size) if size else model_id}). "
                                          "Quilvo will be ready in a moment.")
                    first_download = True
                try:
                    m = WhisperModel(model_id, device="cpu", compute_type="int8")   # download it
                finally:
                    downloading.clear()
        except Exception as e:
            log.error("could not load speech model %s: %s", model_id, e)
            self.bridge.model_status.emit("stt", model_id, "error")
            if self.stt is None and model_id != WHISPER_MODEL:
                self._load_stt(WHISPER_MODEL)       # never leave Quilvo without a model
            return
        if self._stt_wanted != model_id:
            return                                  # a newer pick superseded this one
        self.stt, self.stt_id = m, model_id
        self.settings["stt"] = model_id
        save_settings(self.settings)
        self.bridge.model_status.emit("stt", model_id, "ready")
        log.info("speech model ready: %s", model_id)
        if first_download:
            self.bridge.info.emit(f"Quilvo is ready — press {hotkey_label(self.hotkey)} anywhere to dictate.")
        self._refresh_catalog()                     # a download is now "installed"

    def _report_download(self, model_id, downloading):
        """While a Whisper model downloads, send its progress to the island's ring."""
        repo = models.whisper_repo(model_id)
        total = models.download_total(repo) if repo else None
        while downloading.is_set():
            p = min(models.download_done(repo) / total, 0.999) if total else -1.0
            self.bridge.model_progress.emit("stt", model_id, p)
            time.sleep(0.25)

    def _load_llm(self, model_id):
        self.bridge.model_status.emit("llm", model_id, "loading")
        ok = True
        if model_id.startswith("ollama:"):
            ok = models.ensure_ollama()
            if ok:                                  # warm it up so the first cleanup is quick
                try:
                    requests.post(f"{models.OLLAMA_HOST}/api/generate", timeout=120, json={
                        "model": model_id.split(":", 1)[1], "keep_alive": KEEP_ALIVE})
                except Exception as e:
                    log.warning("LLM warm-up failed: %s", e)
                    ok = False
        if not ok:
            self.bridge.model_status.emit("llm", model_id, "error")
            return
        self.settings["llm"] = model_id
        save_settings(self.settings)
        self.bridge.model_status.emit("llm", model_id, "ready")
        self._push_models()

    def _select_model(self, kind, model_id):
        if kind == "lang":
            self._toggle_language(model_id)
        elif kind == "stt":
            if model_id != self.stt_id:
                self._spawn(self._load_stt, model_id)
        elif kind == "llm":
            if not model_id:                        # "Off"
                self.settings["llm"] = ""
                save_settings(self.settings)
                self._push_models()
            elif model_id != self.settings["llm"]:
                self._spawn(self._load_llm, model_id)

    def _toggle_language(self, code):
        """"" = detect any language; a code toggles it (the last one can't be removed)."""
        langs = self.settings["languages"]
        if not code:
            langs = []
        elif code in langs:
            if len(langs) == 1:
                return
            langs = [l for l in langs if l != code]
        else:
            langs = langs + [code]
        self.settings["languages"] = langs
        save_settings(self.settings)
        self._push_models()

    def _browse_model(self, kind):
        path = QFileDialog.getExistingDirectory(
            None, "Choose a Whisper model folder (CTranslate2 — contains model.bin)",
            os.path.expanduser("~"))
        if not path:
            return
        path = os.path.normpath(path)
        if not models.is_whisper_folder(path):
            self.tray.showMessage("Quilvo", "That folder doesn't contain a Whisper model "
                                  "(no model.bin). Pick a faster-whisper / CTranslate2 model folder.")
            return
        if path not in self.settings["stt_paths"]:
            self.settings["stt_paths"].append(path)
            save_settings(self.settings)
        self._spawn(self._refresh_catalog)
        self._select_model("stt", path)

    # ── key handling ──
    def _on_key(self, key):
        if key == Key.esc and self._busy:
            self._cancel.set()
            self._stop.set()

    def _on_trigger(self):
        if self._busy:
            self._stop.set()          # second press -> stop recording now
            return
        if self.stt is None:
            self.tray.showMessage("Quilvo", "Still loading the speech model — try again in a moment.",
                                  QSystemTrayIcon.MessageIcon.Information, 2500)
            return
        self._busy = True
        self._stop.clear()
        self._cancel.clear()
        self.overlay.set_state("listening")
        self.overlay.present()
        self.island.present()               # after the glow, so it sits on top
        self._spawn(self._refresh_catalog)  # pick up models installed since last time
        threading.Thread(target=self._session, daemon=True).start()

    # ── full dictation session (worker thread) ──
    def _session(self):
        try:
            audio = self._record()
            if self._cancel.is_set() or audio is None or audio.size < SAMPLE_RATE // 2:
                return
            self.bridge.state.emit("transcribing")
            text = self._transcribe(audio)
            if not text or self._cancel.is_set():
                return
            llm = self.settings["llm"]
            if llm:
                self.bridge.state.emit("cleaning")
                text = self._clean(llm, text) or text
            if self._cancel.is_set():
                return
            self.bridge.state.emit("pasting")
            self.bridge.paste.emit(text)
        except sd.PortAudioError as e:
            log.exception("microphone error")
            self.bridge.notify.emit(f"Couldn't use the microphone: {e}")
        except Exception as e:
            log.exception("dictation failed")
            self.bridge.notify.emit(f"Dictation failed: {e}. Details in {LOG_PATH}")
        finally:
            self.bridge.hide.emit()
            self._busy = False

    def _record(self):
        # Manual mode: record until the user stops (hotkey again / click) or the
        # safety cap. No auto silence cut-off, so pauses never end the take.
        MAX_SEC = 120
        block = 1280
        max_chunks = int(MAX_SEC * SAMPLE_RATE / block)

        frames = []
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                            dtype="int16", blocksize=block) as st:
            while len(frames) < max_chunks:
                if self._stop.is_set() or self._cancel.is_set():
                    break
                chunk, _ = st.read(block)
                c = chunk.flatten()
                frames.append(c)
                vol = float(np.abs(c).mean())
                self.bridge.level.emit(min(1.0, (vol / 1400.0) ** 0.8))
        if self._cancel.is_set() or not frames:
            return None
        return np.concatenate(frames).astype(np.float32) / 32768.0

    def _load_lang_detector(self):
        """Whisper 'tiny' just for picking the language: its 30 s encoder pass costs
        ~0.2 s vs ~1.3 s for 'small'. Measured per 5 s take: auto-detect 3.2 s →
        tiny-detect + fixed language 2.0 s."""
        try:
            try:
                self._lang_model = WhisperModel("tiny", device="cpu", compute_type="int8",
                                                local_files_only=True)
            except Exception:
                self._lang_model = WhisperModel("tiny", device="cpu", compute_type="int8")
        except Exception as e:
            log.warning("no tiny language detector, using the main model: %s", e)

    def _pick_language(self, stt, audio, langs):
        """The picked language, or detect among the picked ones ([] = any)."""
        if not stt.model.is_multilingual:
            return "en"
        if len(langs) == 1:
            return langs[0]
        _, _, probs = (self._lang_model or stt).detect_language(audio)
        probs = dict(probs)
        return max(langs or probs, key=lambda lang: probs.get(lang, 0.0))

    def _transcribe(self, audio):
        stt, t0 = self.stt, time.perf_counter()        # keep one model even if a switch lands mid-take
        langs = list(self.settings["languages"])
        lang = self._pick_language(stt, audio, langs)
        segs, _ = stt.transcribe(audio, beam_size=1, language=lang,
                                 initial_prompt=MIXED_PROMPTS.get(frozenset(langs)),
                                 condition_on_previous_text=False)
        text = " ".join(s.text for s in segs).strip()
        log.info("transcribed %.1fs of audio in %.2fs (%s, %s)",
                 len(audio) / SAMPLE_RATE, time.perf_counter() - t0, self.stt_id, lang)
        return text

    def _clean(self, model_id, text):
        try:
            if model_id.startswith("ollama:"):
                models.ensure_ollama()
            return models.cleanup(model_id, CLEANUP_PROMPT, text, KEEP_ALIVE)
        except Exception as e:
            log.warning("cleanup failed, pasting raw text: %s", e)
            return None

    def _paste(self, text):
        """GUI thread. Put the text on the clipboard, send Ctrl+V, then restore what
        was there before — every format (images, files), not just text."""
        cb = QApplication.clipboard()
        saved = QMimeData()
        src = cb.mimeData()
        if src is not None:
            for fmt in src.formats():
                saved.setData(fmt, src.data(fmt))
            if src.hasImage():                 # images are converted on demand, not stored as raw bytes
                saved.setImageData(src.imageData())
        cb.setText(text)

        def send_paste():
            with self.kb.pressed(Key.ctrl):
                self.kb.press("v")
                self.kb.release("v")
        QTimer.singleShot(50, send_paste)
        # the target app reads the clipboard when it handles Ctrl+V; give slow apps time
        QTimer.singleShot(500, lambda: cb.setMimeData(saved))


if __name__ == "__main__":
    setup_logging()
    # one Quilvo at a time — a second copy would record and paste every take twice
    instance_lock = QLockFile(os.path.join(DATA_DIR, "flow.lock"))
    if not instance_lock.tryLock(100):
        log.info("Quilvo is already running; exiting")
        sys.exit(0)
    disable_power_throttling()
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)   # required by QtWebEngine
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # overlay hides; app keeps running
    flow = FlowApp()
    if "--selftest" in sys.argv:
        flow.selftest()
    code = app.exec()
    if flow.restart:
        instance_lock.unlock()
        # PyInstaller: start the new copy as its own app, not a child of this one
        subprocess.Popen(_launch_command(), env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
    sys.exit(code)
