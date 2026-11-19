#!/usr/bin/env python3
"""alessa-voice: talk to Alessa. Record, transcribe via Groq (local fallback), clean with LLM, paste.

Usage:
  alessa-voice                     run the daemon (Organik Dalga overlay)
  alessa-voice toggle [en|tr|to-en]  start / stop recording for a mode
  alessa-voice cancel              discard the current recording
  alessa-voice models              list configured Groq and available local models
  alessa-voice history [N]         show last N dictation entries from history
"""
import array
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
SOCK = RUNTIME / "alessa-voice.sock"
CONFIG = Path.home() / ".config/alessa-voice/config.toml"
DICTIONARY_FILE = Path.home() / ".config/alessa-voice/dictionary.toml"
CACHE = Path.home() / ".cache/alessa-voice"
HISTORY_FILE = CACHE / "history.jsonl"
MODEL_DIR = Path.home() / ".local/share/whisper"
RATE = 16000

GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
USER_AGENT = "alessa-voice/2.0"

DEFAULT_CONFIG = """\
# alessa-voice v2 config. Restart the daemon after editing:
#   systemctl --user restart 'app-alessa\\x2dvoice@autostart.service'

backend = "groq"              # "groq" (cloud + local fallback) or "local"
api_keys_file = "~/.config/alessa/api-keys.env"
language = "tr"               # default mode if toggle has no arg: "en", "tr", "to-en"

groq_stt_model_tr = "whisper-large-v3"
groq_stt_model_en = "whisper-large-v3-turbo"
groq_stt_model_to_en = "whisper-large-v3"

cleanup = true                # LLM cleanup / translation pass after STT
cleanup_style = "polish"      # "polish" (fluent spoken->written) or "light" (strict)
groq_llm_models = ["qwen/qwen3.8-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b"]

fallback_local = true         # run one-shot whisper-cli if Groq fails
model = "ggml-large-v3.bin"
model_en = "ggml-distil-large-v3.bin"
threads = 6
history_days = 7
history_max = 100

max_minutes = 60
warn_minutes = 50
on_limit = "transcribe"       # "transcribe" or "discard"
sounds = true
duck = 0.10
fade = 0.3
paste = true
position = "bottom"           # "bottom" or "top" centre of the screen
margin = 6                    # px from screen edge (or panel edge)
"""


def normalize_mode(mode):
    m = (mode or "").strip().lower().replace("_", "-")
    return m if m in ("en", "tr", "to-en") else ""


def load_config():
    if not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(DEFAULT_CONFIG)
    cfg = tomllib.loads(DEFAULT_CONFIG)
    cfg.update(tomllib.loads(CONFIG.read_text()))
    return cfg


def load_groq_api_key(cfg):
    env_key = os.environ.get("GROQ_API_KEY", "").strip().strip("\"'")
    if env_key:
        return env_key
    env_path = Path(cfg.get("api_keys_file", "~/.config/alessa/api-keys.env")).expanduser()
    if not env_path.exists():
        return ""
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "GROQ_API_KEY":
                val = v.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1].strip()
                return val
    except OSError:
        return ""
    return ""


# ---------------------------------------------------------------- client side

def send(cmd):
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(SOCK))
            s.sendall(cmd.encode())
        return True
    except OSError:
        return False


def list_models():
    cfg = load_config()
    print(f"Backend: {cfg.get('backend', 'groq')} (local fallback: {cfg.get('fallback_local', True)})")
    print(f"Groq STT: en={cfg['groq_stt_model_en']}, tr={cfg['groq_stt_model_tr']}, to-en={cfg['groq_stt_model_to_en']}")
    print(f"Groq LLM cleanup ({cfg.get('cleanup', True)}): {', '.join(cfg['groq_llm_models'])}")
    print("\nLocal fallback models (~/.local/share/whisper):")
    active = {cfg["model"], cfg.get("model_en", "")}
    for f in sorted(MODEL_DIR.glob("*.bin")):
        mark = "*" if f.name in active else " "
        print(f"  {mark} {f.name}  ({f.stat().st_size / 1e9:.1f} GB)")


def show_history(limit=5):
    if not HISTORY_FILE.exists():
        print("No dictation history yet.")
        return
    lines = [ln for ln in HISTORY_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for ln in lines[-max(1, limit):]:
        try:
            entry = json.loads(ln)
        except ValueError:
            continue
        print(
            f"[{entry.get('ts', '?')}] mode={entry.get('mode', '?')} "
            f"engine={entry.get('engine', '?')} "
            f"stt={entry.get('stt_ms', 0)}ms llm={entry.get('llm_ms', 0)}ms"
        )
        print(f"  raw: {entry.get('raw', '')}")
        if entry.get("cleaned") != entry.get("raw"):
            print(f"  out: {entry.get('cleaned', '')}")


# ---------------------------------------------------------------- helpers

def make_tone(path, notes, vol=0.18):
    """notes: list of (freq_hz, seconds). Soft sine with fade in/out."""
    data = array.array("h")
    for freq, dur in notes:
        n = int(RATE * dur)
        for i in range(n):
            env = min(1.0, i / 400, (n - i) / 1200)
            data.append(int(32767 * vol * env * math.sin(2 * math.pi * freq * i / RATE)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data.tobytes())


# Phrases Whisper learned from YouTube subtitles and invents on silence or noise.
HALLUCINATIONS = re.compile(
    r"abone ol|altyaz[ıi]|izledi[gğ]iniz i[cç]in te[sş]ekk|be[gğ]enmeyi unutmay|kanal[ıi]ma|"
    r"bir sonraki videoda|subtitles? by|thanks? for watching|amara\.org|please subscribe", re.I)

HALLUCINATION_LOOP = re.compile(
    r"(?:\b(?:abone ol|izledi[gğ]iniz i[cç]in te[sş]ekk(?:ür(?:ler)?)?|be[gğ]enmeyi unutmay(?:[ıi]n)?|"
    r"kanal[ıi]ma abone ol|bir sonraki videoda|thanks? for watching|please subscribe)[.!?,…\s]*){2,}",
    re.I,
)


def drop_hallucinations(text):
    text = re.sub(HALLUCINATION_LOOP, " ", text)
    parts = re.split(r"(?<=[.!?…])\s+", " ".join(text.split()))
    return " ".join(p for p in parts if not (HALLUCINATIONS.search(p) and len(p) < 60))


def collapse_repeats(text):
    """Drop Whisper's hallucination loops: the same sentence repeated back to back."""
    parts = re.split(r"(?<=[.!?…])\s+", " ".join(text.split()))
    out = []
    for p in parts:
        key = re.sub(r"\W+", "", p.lower())
        if out and key and key == re.sub(r"\W+", "", out[-1].lower()):
            continue
        out.append(p)
    return " ".join(out)


def _load_dict_toml():
    if not DICTIONARY_FILE.exists():
        return {}
    try:
        return tomllib.loads(DICTIONARY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_dictionary():
    return _load_dict_toml().get("replacements", {})


def load_glossary():
    data = _load_dict_toml()
    seen = set()
    terms = []
    for v in data.get("replacements", {}).values():
        t = str(v).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            terms.append(t)
    for v in data.get("glossary", {}).get("terms", []):
        t = str(v).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            terms.append(t)
    return terms


def apply_dictionary(text):
    reps = load_dictionary()
    if not reps or not text:
        return text
    for pattern, replacement in sorted(reps.items(), key=lambda kv: -len(kv[0])):
        if not pattern:
            continue
        escaped = re.escape(pattern)
        regex = rf"\b{escaped}\b" if pattern[0].isalnum() and pattern[-1].isalnum() else escaped
        text = re.sub(regex, replacement, text, flags=re.IGNORECASE)
    return text


def append_history(mode, engine, stt_ms, llm_ms, raw, cleaned, cfg=None):
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        max_entries = max(1, int((cfg or {}).get("history_max", 100)))
        max_days = int((cfg or {}).get("history_days", 7))
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=max_days)) if max_days > 0 else None

        kept = []
        if HISTORY_FILE.exists():
            for ln in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                try:
                    prev = json.loads(ln)
                    if cutoff is not None:
                        dt = datetime.fromisoformat(prev["ts"])
                        if dt.tzinfo is None:
                            dt = dt.astimezone()
                        if dt < cutoff:
                            continue
                    kept.append(json.dumps(prev, ensure_ascii=False))
                except Exception:
                    continue

        entry = {
            "ts": now.astimezone().isoformat(timespec="seconds"),
            "mode": mode,
            "engine": engine,
            "stt_ms": int(stt_ms),
            "llm_ms": int(llm_ms),
            "raw": raw,
            "cleaned": cleaned,
        }
        kept.append(json.dumps(entry, ensure_ascii=False))
        HISTORY_FILE.write_text("\n".join(kept[-max_entries:]) + "\n", encoding="utf-8")
        os.chmod(HISTORY_FILE, 0o600)
    except OSError as e:
        print("history write failed:", e, file=sys.stderr)


DUCK_FILE = CACHE / "volume-before-duck"


def get_volume():
    out = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], capture_output=True, text=True).stdout
    return float(out.split()[1])


_fade_gen = 0  # a newer fade cancels an older one still running
_fading = False


def fade(start, end, seconds):
    """Slide the system volume from start to end in 5% steps in the background."""
    global _fade_gen, _fading
    _fade_gen += 1
    gen, steps = _fade_gen, max(1, math.ceil(round(abs(end - start), 4) / 0.05))
    _fading = True

    def run():
        global _fading
        for i in range(1, steps + 1):
            if gen != _fade_gen:
                return
            v = min(start + 0.05 * i, end) if end > start else max(start - 0.05 * i, end)
            subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{v:.2f}"])
            if i < steps:
                time.sleep(seconds / steps)
        _fading = False
    threading.Thread(target=run, daemon=True).start()


def duck(level, seconds=0):
    """Turn the system volume down to level while recording, until unduck()."""
    if level >= 1 or DUCK_FILE.exists():
        return
    before = get_volume()
    if before <= level:
        return
    DUCK_FILE.write_text(json.dumps({"before": before, "ducked": level}))  # survives a crash, restored on next start
    fade(before, level, seconds)


def unduck(seconds=0):
    if not DUCK_FILE.exists():
        return
    try:
        saved = json.loads(DUCK_FILE.read_text())
        before, ducked = saved["before"], saved["ducked"]
    except (ValueError, KeyError, TypeError):  # unreadable or an old format
        DUCK_FILE.unlink()
        return
    DUCK_FILE.unlink()
    now = get_volume()
    if _fading or abs(now - ducked) < 0.01:  # otherwise the volume keys were used while recording: keep that
        fade(now, before, seconds)


# ---------------------------------------------------------------- STT & LLM pipeline

def encode_ogg(wav_path, ogg_path):
    try:
        res = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
             "-c:a", "libopus", "-b:a", "32k", "-application", "voip", str(ogg_path)],
            capture_output=True, timeout=15,
        )
        if res.returncode == 0 and ogg_path.exists() and ogg_path.stat().st_size > 0:
            return ogg_path, "audio/ogg", "audio.ogg"
    except Exception as e:
        print("ffmpeg ogg encode failed, falling back to wav:", e, file=sys.stderr)
    return wav_path, "audio/wav", "audio.wav"


def groq_transcribe(audio_path, mime, filename, mode, cfg, api_key):
    model = {
        "en": cfg["groq_stt_model_en"],
        "tr": cfg["groq_stt_model_tr"],
        "to-en": cfg["groq_stt_model_to_en"],
    }[mode]
    fields = {"model": model, "response_format": "json", "temperature": "0.0"}
    if mode in ("en", "tr"):
        fields["language"] = mode

    boundary = uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    body.extend(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode()
    )
    body.extend(Path(audio_path).read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        GROQ_STT_URL,
        data=bytes(body),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=35) as r:
        return json.loads(r.read())["text"].strip(), model


def build_cleanup_prompt(mode, glossary, style="polish"):
    if mode == "tr":
        if style == "polish":
            base = (
                "Rewrite this Turkish speech-to-text transcript into clean, fluent, natural everyday Turkish "
                "as if the speaker typed it themselves. Keep the speaker's casual personal tone (never stiff "
                "or formal) and keep 100% of their distinct facts, preferences, names, and questions. "
                "Remove speech fillers (şey, yani, hani, ıı, ee, işte), stutters, and thinking-out-loud "
                "duplicates where the same point is repeated back-to-back while phrasing it (keep the clearest "
                "phrasing once). Connect choppy spoken fragments into well-punctuated sentences, and fix obvious "
                "phonetic STT mistakes on technical/English words from context (e.g. branch, merge, master, "
                "on/off, common.md, STATE.md). Never summarize away details, never invent new meaning, and "
                "never answer the speaker. Output ONLY the cleaned Turkish text."
            )
        else:
            base = (
                "You clean Turkish speech-to-text transcripts. Remove speech fillers "
                "(şey, yani, hani, ıı, ee, işte) and immediate stutters or repeated words. "
                "Fix punctuation, capitalization, and obvious phonetic speech recognition mistakes "
                "while keeping English technical terms in English. Never summarize, omit sentences, "
                "answer questions, or add new ideas. Output ONLY the cleaned Turkish text."
            )
    elif mode == "en":
        if style == "polish":
            base = (
                "Rewrite this English speech-to-text transcript into clean, fluent, natural everyday English "
                "as if the speaker typed it themselves. Keep the speaker's casual personal tone and keep 100% "
                "of their distinct facts, preferences, names, and questions. Remove speech fillers (um, uh, like, "
                "you know, I mean), stutters, and thinking-out-loud duplicates where the same point is repeated "
                "back-to-back while phrasing it (keep the clearest phrasing once). Connect choppy spoken fragments "
                "into well-punctuated sentences and fix obvious phonetic STT errors. Never summarize away details, "
                "never invent new meaning, and never answer the speaker. Output ONLY the cleaned English text."
            )
        else:
            base = (
                "You clean English speech-to-text transcripts. Remove speech fillers "
                "(um, uh, like, you know, I mean) and immediate stutters or repeated words. "
                "Fix punctuation, capitalization, and obvious phonetic speech recognition mistakes. "
                "Never summarize, omit sentences, answer questions, or add new ideas. "
                "Output ONLY the cleaned English text."
            )
    else:
        base = (
            "You clean and translate Turkish, English, or mixed Turkish-English speech-to-text "
            "transcripts into clear, natural English. Remove speech fillers and immediate stutters "
            "or repeated words, fix obvious phonetic mistakes, and preserve exact technical terms "
            "and full meaning. Never summarize, omit sentences, answer questions, or add new ideas. "
            "Output ONLY the final English text."
        )
    if glossary:
        base += (
            " Known project/technical terms glossary (prefer exact spelling when context matches): "
            + ", ".join(glossary) + "."
        )
    return base


def groq_cleanup(text, mode, glossary, cfg, api_key):
    style = str(cfg.get("cleanup_style", "polish")).strip().lower()
    system_prompt = build_cleanup_prompt(mode, glossary, style=style)
    models = cfg.get("groq_llm_models") or ["qwen/qwen3.8-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    max_tokens = max(512, min(4096, int(len(text) * 1.8) + 128))

    for model in models:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        lower_m = model.lower()
        if "qwen" in lower_m:
            payload["reasoning_effort"] = "none"
        elif "gpt-oss" in lower_m:
            payload["reasoning_effort"] = "low"

        req = urllib.request.Request(
            GROQ_CHAT_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            content = (data["choices"][0]["message"].get("content") or "").strip()
            if len(content) >= 2 and content[0] == content[-1] and content[0] in ('"', "'"):
                if not text.startswith(content[0]):
                    content = content[1:-1].strip()
            if not content:
                continue
            if mode in ("tr", "en") and len(text) >= 80 and len(content) < 0.35 * len(text):
                print(f"cleanup guardrail rejected {model}: too short ({len(content)} vs {len(text)})", file=sys.stderr)
                return text, "guardrail-rejected"
            return content, model
        except Exception as e:
            print(f"groq_cleanup {model} failed:", e, file=sys.stderr)
            continue
    return text, "none"


def local_whisper_fallback(wav_path, mode, cfg):
    model_name = cfg.get("model_en", "ggml-distil-large-v3.bin") if mode == "en" else cfg["model"]
    model_path = MODEL_DIR / model_name
    if not model_path.exists():
        model_path = MODEL_DIR / cfg["model"]
    if not model_path.exists():
        raise RuntimeError(f"local whisper model not found: {model_path}")

    cmd = [
        "whisper-cli", "-m", str(model_path),
        "-t", str(cfg.get("threads", 6)),
        "-nt", "-np", "-f", str(wav_path),
    ]
    silero = MODEL_DIR / "vad/ggml-silero-v5.1.2.bin"
    if silero.exists():
        cmd += ["--vad", "-vm", str(silero), "-vt", "0.35", "-vp", "400", "-vsd", "300"]
    if mode == "en":
        cmd += ["-l", "en"]
    elif mode == "tr":
        cmd += ["-l", "tr"]
    else:
        cmd += ["-l", "auto", "-tr"]

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"whisper-cli exited with {res.returncode}")
    return " ".join(ln.strip() for ln in res.stdout.splitlines() if ln.strip())


# ---------------------------------------------------------------- overlay

MODE_PALETTES = {
    "en":    ((2, 132, 199), (56, 189, 248), (3, 105, 161), "EN"),
    "tr":    ((225, 29, 72), (249, 115, 22), (159, 18, 57), "TR"),
    "to-en": ((5, 150, 105), (16, 185, 129), (4, 120, 87),  "→EN"),
}


def _qc(rgb, a):
    from PyQt6.QtGui import QColor
    return QColor(int(rgb[0]), int(rgb[1]), int(rgb[2]), max(0, min(255, int(round(a * 255)))))


def _mix(a, b, k):
    return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k)


def build_wave_box(cfg, lsq=None):
    import ctypes
    from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
    from PyQt6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
    from PyQt6.QtWidgets import QApplication, QWidget

    class OrganicWaveBox(QWidget):
        """Frameless Concept #2 (Organik Dalga): 3 Gaussian-tapered silky sine ribbons (label-free, panel-safe)."""
        W, H = 360, 72
        W_WAVE = 320
        STEP = 3

        def __init__(self):
            super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                             | Qt.WindowType.Tool | Qt.WindowType.X11BypassWindowManagerHint
                             | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setFixedSize(self.W, self.H)
            self.state = "idle"     # idle | listening | groq | cleaning | done | error
            self.mode = "tr"
            self.level = 0.0
            self.warn = 0.0
            self.minutes_left = None
            self.t0 = time.monotonic()
            self.timer = QTimer(self, interval=16, timeout=self.update)
            if lsq:
                self.layer_shell()

        def layer_shell(self):
            from PyQt6 import sip
            self.winId()
            win = ctypes.c_void_p(lsq._ZN12LayerShellQt6Window3getEP7QWindow(
                ctypes.c_void_p(sip.unwrapinstance(self.windowHandle()))))
            top = cfg.get("position", "bottom") == "top"
            margin = int(cfg.get("margin", 6))
            margins = (ctypes.c_int * 4)(0, margin if top else 0, 0, 0 if top else margin)
            lsq._ZN12LayerShellQt6Window8setLayerENS0_5LayerE(win, 3)                     # overlay
            lsq._ZN12LayerShellQt6Window10setAnchorsE6QFlagsINS0_6AnchorEE(win, 1 if top else 2)
            lsq._ZN12LayerShellQt6Window10setMarginsERK8QMargins(win, margins)
            lsq._ZN12LayerShellQt6Window24setKeyboardInteractivityENS0_21KeyboardInteractivityE(win, 0)
            lsq._ZN12LayerShellQt6Window16setExclusiveZoneEi(win, 0)                   # respect KDE panels

        def place(self):
            geo = QApplication.primaryScreen().availableGeometry()
            top = cfg.get("position", "bottom") == "top"
            margin = int(cfg.get("margin", 6))
            y = geo.top() + margin if top else geo.bottom() - self.H - margin
            self.move(geo.center().x() - self.W // 2, y)

        def set_state(self, state):
            self.state, self.t0 = state, time.monotonic()
            if state == "idle":
                self.timer.stop()
                self.hide()
                return
            self.place()
            self.show()
            self.timer.start()

        def _palette(self, now):
            if self.state == "done":
                return (16, 185, 129), (52, 211, 153), (5, 150, 105)
            if self.state == "error":
                return (225, 29, 72), (251, 113, 133), (159, 18, 57)
            if self.warn > 0:
                p = 0.5 + 0.5 * math.sin(now * 5.2)
                return _mix((234, 88, 12), (220, 38, 38), p), _mix((249, 115, 22), (248, 113, 113), p), (153, 27, 27)
            primary, glow, deep, _ = MODE_PALETTES.get(self.mode, MODE_PALETTES["tr"])
            return primary, glow, deep

        def render_to(self, p):
            now = time.monotonic()
            t = now - self.t0
            alpha = 1.0
            if self.state == "done":
                if t >= 0.85:
                    self.set_state("idle")
                    return
                alpha = max(0.0, 1.0 - max(0.0, t - 0.35) / 0.50)
            elif self.state == "error":
                if t >= 0.90:
                    self.set_state("idle")
                    return
                alpha = max(0.0, 1.0 - t / 0.90)

            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setOpacity(alpha)

            cx, cy = self.W / 2.0, self.H / 2.0
            half = self.W_WAVE / 2.0
            xs = list(range(-int(half), int(half) + 1, self.STEP))
            envs = [math.exp(-((dx / half) / 0.46) ** 2) for dx in xs]
            primary, glow, deep = self._palette(now)

            if self.state == "done":
                pulse = math.sin(min(1.0, t / 0.65) * math.pi) * 16.0
                path = QPainterPath()
                for idx, dx in enumerate(xs):
                    y = cy - pulse * envs[idx] * math.cos((dx / half) * 2.2)
                    if idx == 0:
                        path.moveTo(cx + dx, y)
                    else:
                        path.lineTo(cx + dx, y)
                for idx in range(len(xs) - 1, -1, -1):
                    dx = xs[idx]
                    y = cy + pulse * envs[idx] * math.cos((dx / half) * 2.2)
                    path.lineTo(cx + dx, y)
                path.closeSubpath()
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_qc(primary, 0.45))
                p.drawPath(path)

                lg = QLinearGradient(cx - half, cy, cx + half, cy)
                lg.setColorAt(0.0, _qc(glow, 0.0))
                lg.setColorAt(0.5, _qc(glow, 0.95))
                lg.setColorAt(1.0, _qc(glow, 0.0))
                p.setPen(QPen(QBrush(lg), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(cx - half, cy), QPointF(cx + half, cy))

            elif self.state == "cleaning":
                sweep = ((now * 0.95) % 1.0)
                top_pts, bot_pts = [], []
                for idx, dx in enumerate(xs):
                    nx = dx / half
                    h = (9.0 + 8.0 * math.sin(nx * 3.1 + now * 3.8)) * envs[idx]
                    top_pts.append(QPointF(cx + dx, cy - h))
                    bot_pts.append(QPointF(cx + dx, cy + h))
                path = QPainterPath()
                path.moveTo(top_pts[0])
                for pt in top_pts[1:]:
                    path.lineTo(pt)
                for pt in reversed(bot_pts):
                    path.lineTo(pt)
                path.closeSubpath()

                sg = QLinearGradient(cx - half, cy, cx + half, cy)
                s1 = max(0.15, min(0.85, sweep))
                sg.setColorAt(0.0, _qc(primary, 0.0))
                sg.setColorAt(max(0.05, s1 - 0.16), _qc(primary, 0.72))
                sg.setColorAt(s1, QColor(255, 255, 255, 245))
                sg.setColorAt(min(0.95, s1 + 0.16), _qc(glow, 0.72))
                sg.setColorAt(1.0, _qc(primary, 0.0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(sg)
                p.drawPath(path)

            else:
                if self.state == "groq":
                    base_h = 12.0
                elif self.state == "error":
                    base_h = 15.0 * math.exp(-t * 1.4) + 4.5
                else:
                    base_h = 2.4 + self.level * 30.0

                ribbons = (
                    (2.3,  4.1, 0.0, 1.00, 0.36, deep),
                    (3.4, -5.3, 1.8, 0.78, 0.52, primary),
                    (4.7,  6.8, 3.3, 0.56, 0.66, glow),
                )
                p.setPen(Qt.PenStyle.NoPen)
                for freq, speed, phase, amp, r_alpha, r_col in ribbons:
                    top_pts, bot_pts = [], []
                    for idx, dx in enumerate(xs):
                        nx = dx / half
                        mod = math.sin(nx * 18.0 + now * 24.0) if self.state == "error" else math.sin(
                            nx * freq * math.pi + now * speed + phase
                        )
                        h = max(0.9, (0.45 + 0.55 * mod) * base_h * amp) * envs[idx]
                        top_pts.append(QPointF(cx + dx, cy - h))
                        bot_pts.append(QPointF(cx + dx, cy + h))
                    path = QPainterPath()
                    path.moveTo(top_pts[0])
                    for pt in top_pts[1:]:
                        path.lineTo(pt)
                    for pt in reversed(bot_pts):
                        path.lineTo(pt)
                    path.closeSubpath()
                    p.setBrush(_qc(r_col, r_alpha))
                    p.drawPath(path)

                spine = QPainterPath()
                for idx, dx in enumerate(xs):
                    nx = dx / half
                    y = cy + math.sin(nx * 5.2 + now * 7.5) * base_h * 0.22 * envs[idx]
                    if idx == 0:
                        spine.moveTo(cx + dx, y)
                    else:
                        spine.lineTo(cx + dx, y)
                lg = QLinearGradient(cx - half, cy, cx + half, cy)
                lg.setColorAt(0.0, _qc(primary, 0.0))
                lg.setColorAt(0.5, _qc(glow, 0.96))
                lg.setColorAt(1.0, _qc(primary, 0.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QBrush(lg), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawPath(spine)

                if self.state == "groq":
                    px = cx + math.sin(now * 4.2) * 0.72 * half
                    cg = QRadialGradient(px, cy, 32.0)
                    cg.setColorAt(0.0, QColor(255, 255, 255, 245))
                    cg.setColorAt(0.4, _qc(primary, 0.85))
                    cg.setColorAt(1.0, _qc(primary, 0.0))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(cg)
                    p.drawEllipse(QPointF(px, cy), 32.0, 32.0)

            if self.minutes_left is not None:
                f = QFont("sans-serif", 9, QFont.Weight.DemiBold)
                f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
                p.setFont(f)
                caption = f"{self.minutes_left}m"
                rect = QRectF(0, cy + 12, self.W, 20)
                p.setPen(QColor(8, 12, 22, 165))
                for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    p.drawText(rect.translated(ox, oy), Qt.AlignmentFlag.AlignCenter, caption)
                p.setPen(QColor(254, 226, 226, 244))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, caption)

        def paintEvent(self, _):
            p = QPainter(self)
            self.render_to(p)
            p.end()

    return OrganicWaveBox()


# ---------------------------------------------------------------- daemon

def run_daemon():
    if send("ping"):
        sys.exit("alessa-voice is already running")
    import ctypes
    try:
        # Native Wayland layer-shell: the overlay sits in KWin's top-most layer, above every window
        lsq = ctypes.CDLL("libLayerShellQtInterface.so.6")
        lsq._ZN12LayerShellQt5Shell13useLayerShellEv()
        lsq._ZN12LayerShellQt6Window3getEP7QWindow.restype = ctypes.c_void_p
    except OSError:
        lsq = None
        os.environ["QT_QPA_PLATFORM"] = "xcb"

    from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal
    from PyQt6.QtNetwork import QLocalServer
    from PyQt6.QtWidgets import QApplication

    cfg = load_config()
    CACHE.mkdir(parents=True, exist_ok=True)
    unduck()
    tones = {"start": [(660, 0.07), (880, 0.10)], "stop": [(880, 0.07), (660, 0.10)],
             "warn": [(988, 0.12), (0, 0.08), (988, 0.12), (0, 0.08), (988, 0.18)],
             "error": [(330, 0.25)]}
    for name, notes in tones.items():
        make_tone(CACHE / f"{name}.wav", notes)

    class Daemon(QObject):
        stage_changed = pyqtSignal(str)
        transcribed = pyqtSignal(str, str)  # text, error

        def __init__(self):
            super().__init__()
            self.box = build_wave_box(cfg, lsq)
            self.rec = None
            self.busy = False
            self.mode = normalize_mode(cfg.get("language", "tr")) or "tr"
            self.raw_path = CACHE / "current.raw"
            self.raw = None
            self.started = 0.0
            self.peak_db = -120.0
            self.warned = False
            self.clock = QTimer(self, interval=1000, timeout=self.tick)
            self.stage_changed.connect(self.box.set_state)
            self.transcribed.connect(self.on_transcribed)
            self.ipc = QLocalServer(self)
            QLocalServer.removeServer(str(SOCK))
            self.ipc.listen(str(SOCK))
            self.ipc.newConnection.connect(self.on_conn)

        def play(self, name):
            if cfg["sounds"]:
                subprocess.Popen(["pw-play", str(CACHE / f"{name}.wav")],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def on_conn(self):
            c = self.ipc.nextPendingConnection()
            c.waitForReadyRead(500)
            cmd, _, arg = bytes(c.readAll()).decode().strip().partition(" ")
            c.disconnectFromServer()
            if cmd == "toggle":
                if self.rec:
                    self.stop()
                else:
                    self.mode = normalize_mode(arg) or normalize_mode(cfg.get("language", "tr")) or "tr"
                    self.start()
            elif cmd == "cancel":
                self.cancel()

        def start(self):
            if self.busy:
                return
            self.raw = open(self.raw_path, "wb")
            self.peak_db, self.warned, self.box.warn, self.box.minutes_left = -120.0, False, 0.0, None
            self.box.mode = self.mode
            self.rec = QProcess(self)
            self.rec.readyReadStandardOutput.connect(self.on_audio)
            self.rec.start("parecord", ["--raw", "--format=s16le", f"--rate={RATE}", "--channels=1",
                                        "--latency-msec=30"])
            self.started = time.monotonic()
            self.clock.start()
            self.box.set_state("listening")
            self.play("start")
            QTimer.singleShot(250, lambda: duck(cfg["duck"], cfg["fade"]) if self.rec else None)

        def on_audio(self):
            data = bytes(self.rec.readAllStandardOutput())
            if not data:
                return
            self.raw.write(data)
            samples = array.array("h", data[: len(data) // 2 * 2])
            rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))
            db = 20 * math.log10(max(rms, 1) / 32768)
            self.peak_db = max(self.peak_db, db)
            target = min(1.0, max(0.0, (db + 52) / 34))
            k = 0.6 if target > self.box.level else 0.15
            self.box.level += (target - self.box.level) * k

        def tick(self):
            mins = (time.monotonic() - self.started) / 60
            mx, wn = cfg["max_minutes"], cfg["warn_minutes"]
            if mins >= wn:
                self.box.warn = (mins - wn) / max(0.01, mx - wn)
                self.box.minutes_left = max(0, math.ceil(mx - mins))
            if mins >= mx - 5 and not self.warned:
                self.warned = True
                self.play("warn")
            if mins >= mx:
                self.stop() if cfg["on_limit"] == "transcribe" else self.cancel()

        def end_recording(self):
            self.clock.stop()
            self.rec.readyReadStandardOutput.disconnect()
            self.rec.terminate()
            self.rec.waitForFinished(1000)
            self.raw.write(bytes(self.rec.readAllStandardOutput()))
            self.raw.close()
            self.rec = None

        def unduck_then_stop_tone(self):
            unduck(cfg["fade"])
            QTimer.singleShot(int(cfg["fade"] * 1000), lambda: self.play("stop"))

        def cancel(self):
            if not self.rec:
                return
            self.end_recording()
            self.raw_path.unlink(missing_ok=True)
            self.unduck_then_stop_tone()
            self.box.set_state("error")

        def stop(self):
            if not self.rec:
                return
            self.end_recording()
            self.unduck_then_stop_tone()
            if self.peak_db < -50:
                self.raw_path.unlink(missing_ok=True)
                self.box.set_state("error")
                return
            wav = CACHE / "last.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(self.raw_path.read_bytes())
            self.raw_path.unlink(missing_ok=True)

            self.busy = True
            self.box.set_state("groq")
            threading.Thread(target=self.transcribe, args=(wav, self.mode), daemon=True).start()

        def transcribe(self, wav, mode):
            try:
                api_key = load_groq_api_key(cfg)
                backend = cfg.get("backend", "groq")
                stt_t0 = time.monotonic()
                stt_engine = ""
                raw_text = ""

                if backend == "groq" and api_key:
                    ogg = CACHE / "last.ogg"
                    try:
                        upload_path, mime, fname = encode_ogg(wav, ogg)
                        raw_text, stt_model = groq_transcribe(upload_path, mime, fname, mode, cfg, api_key)
                        stt_engine = f"groq:{stt_model}"
                    except Exception as e:
                        print("groq_transcribe failed:", e, file=sys.stderr)
                        if not cfg.get("fallback_local", True):
                            raise
                    finally:
                        ogg.unlink(missing_ok=True)

                if not raw_text and (backend == "local" or cfg.get("fallback_local", True)):
                    if backend == "groq":
                        print("falling back to local whisper-cli", file=sys.stderr)
                    raw_text = local_whisper_fallback(wav, mode, cfg)
                    stt_engine = "local:whisper-cli"

                if not raw_text and not api_key and backend == "groq":
                    raise RuntimeError("GROQ_API_KEY missing and local fallback disabled")

                stt_ms = (time.monotonic() - stt_t0) * 1000.0
                raw_clean = drop_hallucinations(collapse_repeats(raw_text)).strip()
                if not raw_clean:
                    self.transcribed.emit("", "")
                    return

                pre_dict = apply_dictionary(raw_clean)
                llm_ms = 0.0
                llm_engine = "off"
                final_text = pre_dict

                if cfg.get("cleanup", True) and api_key and pre_dict:
                    self.stage_changed.emit("cleaning")
                    llm_t0 = time.monotonic()
                    cleaned_llm, llm_engine = groq_cleanup(pre_dict, mode, load_glossary(), cfg, api_key)
                    llm_ms = (time.monotonic() - llm_t0) * 1000.0
                    final_text = apply_dictionary(cleaned_llm).strip()

                append_history(mode, f"{stt_engine}+{llm_engine}", stt_ms, llm_ms, raw_clean, final_text, cfg=cfg)
                self.transcribed.emit(final_text, "")
            except Exception as e:
                self.transcribed.emit("", str(e))

        def on_transcribed(self, text, error):
            self.busy = False
            if error or not text:
                print("transcription failed:", error or "empty result", file=sys.stderr)
                self.play("error")
                self.box.set_state("error")
                return
            subprocess.run(["wl-copy"], input=text.encode())
            if cfg["paste"]:
                time.sleep(0.12)
                subprocess.run(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.box.set_state("done")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    daemon = Daemon()
    import signal
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    QTimer(app, interval=500, timeout=lambda: None).start()
    try:
        app.exec()
    finally:
        del daemon


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "daemon"
    if cmd == "daemon":
        run_daemon()
    elif cmd in ("toggle", "cancel"):
        if cmd == "toggle" and len(sys.argv) > 2:
            cmd += " " + sys.argv[2]
        if not send(cmd):
            sys.exit("alessa-voice daemon is not running")
    elif cmd == "models":
        list_models()
    elif cmd == "history":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 5
        show_history(limit)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
