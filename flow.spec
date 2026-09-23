# PyInstaller recipe for Quilvo — build with:  .\build.ps1   (or: pyinstaller flow.spec)
# One-folder build (dist\Quilvo\Quilvo.exe). No AI models are bundled: Whisper models
# download on first run into the user's Hugging Face cache.
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = [
    ("edge.html", "."),
    ("island.html", "."),
    ("overlay.html", "."),
    ("assets/icon.png", "assets"),
]
datas += collect_data_files("faster_whisper")          # Silero VAD model used by faster-whisper
binaries = collect_dynamic_libs("ctranslate2")          # CTranslate2 DLLs (inference engine)

a = Analysis(
    ["flow.py"],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "pynput.keyboard._win32", "pynput.mouse._win32", "pynput._util.win32",  # picked at runtime
    ],
    excludes=[
        "tkinter", "matplotlib", "torch", "tensorflow", "IPython", "pytest",
        "hf_xet",                                        # Quilvo downloads over plain HTTP
        "onnxruntime",                                   # only for faster-whisper's VAD filter (unused)
    ],
    noarchive=False,
)


def keep(entry):
    """Drop Qt parts Quilvo never loads: QML modules and UI translations (~80 MB).
    QtWebEngine still gets its en-US locale pack, which it falls back to."""
    dest = entry[0].replace("\\", "/")
    if dest.startswith("PySide6/qml/"):
        return False
    if dest.startswith("PySide6/translations/"):
        return dest.endswith("qtwebengine_locales/en-US.pak")
    return True


a.datas = [d for d in a.datas if keep(d)]
a.binaries = [b for b in a.binaries if keep(b)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Quilvo",
    icon="assets/icon.ico",
    console=False,                                       # tray app: no console window
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Quilvo", upx=False)
