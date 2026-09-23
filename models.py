"""Find the local AI models Quilvo can use, wherever they're installed.

  Speech-to-text  faster-whisper models in the Hugging Face cache (plus any
                  CTranslate2 model folder the user browsed to)
  Text cleanup    local LLMs from Ollama (live API, or its model folder on disk
                  when the server isn't running) and LM Studio (live API)
"""
import os, json, glob, time, shutil, subprocess
import requests

OLLAMA_HOST   = "http://127.0.0.1:11434"   # not "localhost": Windows tries IPv6 first (slow)
LMSTUDIO_HOST = "http://127.0.0.1:1234"

# Offered even when not downloaded yet (picking one downloads it).
FEATURED_WHISPER = ["tiny", "base", "small", "medium", "large-v3-turbo"]
APPROX_SIZE = {"tiny": 75e6, "base": 145e6, "small": 485e6, "medium": 1.5e9,
               "large-v3-turbo": 1.6e9, "large-v3": 3.1e9}
ALIASES = {"large", "turbo"}   # duplicate names in faster-whisper's registry


def fmt_size(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ── Whisper ────────────────────────────────────────────────────────────
def _hf_cache():
    from huggingface_hub import constants
    return constants.HF_HUB_CACHE


def whisper_models(custom_paths=()):
    """[{id, label, detail, installed}] — id is a faster-whisper name or a folder path."""
    from faster_whisper.utils import _MODELS
    cache = _hf_cache()
    items = []
    for name, repo in _MODELS.items():
        if name in ALIASES:
            continue
        snaps = glob.glob(os.path.join(cache, "models--" + repo.replace("/", "--"),
                                       "snapshots", "*", "model.bin"))
        installed = bool(snaps)
        if not installed and name not in FEATURED_WHISPER:
            continue
        lang = "English only" if name.endswith(".en") else "Multilingual"
        if installed:
            size = _dir_size(os.path.dirname(snaps[0]))
            detail = f"{lang} · {fmt_size(size)}"
        else:
            detail = f"Download · {fmt_size(APPROX_SIZE.get(name, 0))}" if name in APPROX_SIZE else "Download"
        items.append({"id": name, "label": name, "detail": detail, "installed": installed})
    for path in custom_paths:
        if is_whisper_folder(path):
            items.append({"id": path, "label": os.path.basename(path.rstrip("\\/")),
                          "detail": f"Folder · {fmt_size(_dir_size(path))}", "installed": True})
    # installed first, then smallest → largest as faster-whisper lists them
    items.sort(key=lambda m: not m["installed"])
    return items


def is_whisper_folder(path):
    return os.path.isfile(os.path.join(path, "model.bin"))


# ── download progress (bytes on disk vs. the repo's file sizes) ──
# the files faster_whisper.download_model() fetches
_WHISPER_FILES = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.")


def whisper_repo(model_id):
    """Hugging Face repo for a faster-whisper model name (None for a local folder)."""
    from faster_whisper.utils import _MODELS
    return _MODELS.get(model_id)


def download_total(repo):
    """Total bytes a download of `repo` will write, or None if the Hub can't say."""
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(repo, files_metadata=True, timeout=10)
        return sum(s.size or 0 for s in info.siblings
                   if s.rfilename.startswith(_WHISPER_FILES)) or None
    except Exception:
        return None


def download_done(repo):
    """Bytes of `repo` already in the local cache (partial downloads included)."""
    return _dir_size(os.path.join(_hf_cache(), "models--" + repo.replace("/", "--")))


# ── Local LLMs ─────────────────────────────────────────────────────────
def _ollama_live():
    r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=0.6)
    return [(m["name"], m.get("size", 0)) for m in r.json().get("models", [])]


def _ollama_on_disk():
    """Read Ollama's manifests directly — works while the server is stopped."""
    root = os.environ.get("OLLAMA_MODELS") or os.path.join(os.path.expanduser("~"), ".ollama", "models")
    out = []
    for f in glob.glob(os.path.join(root, "manifests", "*", "*", "*", "*")):
        ns, model, tag = f.split(os.sep)[-3:]
        try:
            size = sum(l.get("size", 0) for l in json.load(open(f, encoding="utf-8")).get("layers", []))
        except (OSError, ValueError):
            size = 0
        out.append((f"{model}:{tag}" if ns == "library" else f"{ns}/{model}:{tag}", size))
    return out


def _lmstudio_live():
    r = requests.get(f"{LMSTUDIO_HOST}/v1/models", timeout=0.6)
    return [m["id"] for m in r.json().get("data", [])]


def llm_models():
    """[{id, label, detail}] — id is 'ollama:<name>' or 'lmstudio:<name>'."""
    items = []
    try:
        found = _ollama_live()
    except Exception:
        found = _ollama_on_disk()
    for name, size in sorted(found):
        if "embed" in name:            # embedding models can't clean text
            continue
        items.append({"id": f"ollama:{name}", "label": name,
                      "detail": f"Ollama · {fmt_size(size)}" if size else "Ollama"})
    try:
        for name in _lmstudio_live():
            if "embed" not in name:
                items.append({"id": f"lmstudio:{name}", "label": name, "detail": "LM Studio"})
    except Exception:
        pass
    return items


def ensure_ollama(wait_s=12):
    """Start the Ollama server in the background if it isn't answering."""
    try:
        _ollama_live()
        return True
    except Exception:
        pass
    exe = shutil.which("ollama") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
    if not os.path.isfile(exe):
        return False
    subprocess.Popen([exe, "serve"], creationflags=subprocess.CREATE_NO_WINDOW,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            _ollama_live()
            return True
        except Exception:
            time.sleep(0.4)
    return False


def cleanup(model_id, system, text, keep_alive="30m"):
    """Run the dictation-cleanup prompt on whichever local server hosts model_id."""
    provider, name = model_id.split(":", 1)
    if provider == "ollama":
        r = requests.post(f"{OLLAMA_HOST}/api/generate", json={
            "model": name, "system": system, "prompt": text, "stream": False,
            "keep_alive": keep_alive, "options": {"temperature": 0.0},
        }, timeout=60)
        return r.json().get("response", "").strip()
    if provider == "lmstudio":
        r = requests.post(f"{LMSTUDIO_HOST}/v1/chat/completions", json={
            "model": name, "temperature": 0.0,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": text}],
        }, timeout=60)
        return r.json()["choices"][0]["message"]["content"].strip()
    raise ValueError(f"unknown provider {provider!r}")


# Whisper's languages, by the English name people will look for.
LANGUAGE_NAMES = {
    "af": "Afrikaans", "am": "Amharic", "ar": "Arabic", "as": "Assamese", "az": "Azerbaijani",
    "ba": "Bashkir", "be": "Belarusian", "bg": "Bulgarian", "bn": "Bengali", "bo": "Tibetan",
    "br": "Breton", "bs": "Bosnian", "ca": "Catalan", "cs": "Czech", "cy": "Welsh",
    "da": "Danish", "de": "German", "el": "Greek", "en": "English", "es": "Spanish",
    "et": "Estonian", "eu": "Basque", "fa": "Persian", "fi": "Finnish", "fo": "Faroese",
    "fr": "French", "gl": "Galician", "gu": "Gujarati", "ha": "Hausa", "haw": "Hawaiian",
    "he": "Hebrew", "hi": "Hindi", "hr": "Croatian", "ht": "Haitian Creole", "hu": "Hungarian",
    "hy": "Armenian", "id": "Indonesian", "is": "Icelandic", "it": "Italian", "ja": "Japanese",
    "jw": "Javanese", "ka": "Georgian", "kk": "Kazakh", "km": "Khmer", "kn": "Kannada",
    "ko": "Korean", "la": "Latin", "lb": "Luxembourgish", "ln": "Lingala", "lo": "Lao",
    "lt": "Lithuanian", "lv": "Latvian", "mg": "Malagasy", "mi": "Maori", "mk": "Macedonian",
    "ml": "Malayalam", "mn": "Mongolian", "mr": "Marathi", "ms": "Malay", "mt": "Maltese",
    "my": "Burmese", "ne": "Nepali", "nl": "Dutch", "nn": "Norwegian Nynorsk", "no": "Norwegian",
    "oc": "Occitan", "pa": "Punjabi", "pl": "Polish", "ps": "Pashto", "pt": "Portuguese",
    "ro": "Romanian", "ru": "Russian", "sa": "Sanskrit", "sd": "Sindhi", "si": "Sinhala",
    "sk": "Slovak", "sl": "Slovenian", "sn": "Shona", "so": "Somali", "sq": "Albanian",
    "sr": "Serbian", "su": "Sundanese", "sv": "Swedish", "sw": "Swahili", "ta": "Tamil",
    "te": "Telugu", "tg": "Tajik", "th": "Thai", "tk": "Turkmen", "tl": "Tagalog",
    "tr": "Turkish", "tt": "Tatar", "uk": "Ukrainian", "ur": "Urdu", "uz": "Uzbek",
    "vi": "Vietnamese", "yi": "Yiddish", "yo": "Yoruba", "zh": "Chinese", "yue": "Cantonese",
}


def whisper_languages():
    """[{id, label}] for every language Whisper supports, sorted by name."""
    from faster_whisper.tokenizer import _LANGUAGE_CODES
    return sorted(({"id": c, "label": LANGUAGE_NAMES.get(c, c)} for c in _LANGUAGE_CODES),
                  key=lambda l: l["label"])


def system_language():
    """The Windows display language as a Whisper code, e.g. 'es', or None."""
    try:
        import ctypes, locale
        name = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), "")
        code = name.split("_")[0].lower()
        return code if code in LANGUAGE_NAMES else None
    except Exception:
        return None
