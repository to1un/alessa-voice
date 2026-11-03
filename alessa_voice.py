#!/usr/bin/env python3
"""alessa-voice: talk to Alessa. Record, transcribe locally, paste at the cursor.

Usage:
  alessa-voice            run the daemon (overlay + whisper-server)
  alessa-voice toggle     start / stop recording
  alessa-voice cancel     discard the current recording
  alessa-voice models     list available local models
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
import urllib.request
import uuid
import wave
from pathlib import Path

RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
SOCK = RUNTIME / "alessa-voice.sock"
CONFIG = Path.home() / ".config/alessa-voice/config.toml"
CACHE = Path.home() / ".cache/alessa-voice"
MODEL_DIR = Path.home() / ".local/share/whisper"
RATE = 16000

DEFAULT_CONFIG = """\
# alessa-voice config. Restart the daemon after editing:
#   pkill -f 'alessa_voice.py$'; alessa-voice &

model = "ggml-large-v3.bin"   # a file in ~/.local/share/whisper (see: alessa-voice models)
language = "tr"               # "tr", "en" or "auto"
max_minutes = 60              # recording limit
warn_minutes = 50             # from here the voicebox turns red and pulses; a chime at limit-5
on_limit = "transcribe"       # "transcribe" or "discard" when the limit is reached
sounds = true                 # start/stop/warning tones
duck = 0.30                   # loudness of playing media while recording (0-1, 1 = off)
paste = true                  # paste at the cursor (false = clipboard only)
position = "top"              # "top" or "bottom" centre of the screen
margin = 110                  # px from the screen edge
port = 45556                  # local whisper-server port
"""


def load_config():
    if not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(DEFAULT_CONFIG)
    cfg = tomllib.loads(DEFAULT_CONFIG)
    cfg.update(tomllib.loads(CONFIG.read_text()))
    return cfg


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
    active = load_config()["model"]
    for f in sorted(MODEL_DIR.glob("*.bin")):
        mark = "*" if f.name == active else " "
        print(f"{mark} {f.name}  ({f.stat().st_size / 1e9:.1f} GB)")


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


def drop_hallucinations(text):
    parts = re.split(r"(?<=[.!?…])\s+", text)
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


DUCK_FILE = CACHE / "volume-before-duck"


def get_volume():
    out = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], capture_output=True, text=True).stdout
    return float(out.split()[1])


def duck(level):
    """Turn playing apps down (not the system volume, so Plasma shows no OSD) until unduck()."""
    if level >= 1 or DUCK_FILE.exists():
        return
    factor = min(1.0, level / max(get_volume(), 0.01))
    if factor >= 1:
        return
    streams = json.loads(subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"],
                                        capture_output=True, text=True).stdout or "[]")
    saved = {}
    for st in streams:
        if st["properties"].get("application.process.id") == str(os.getpid()):
            continue
        vols = [ch["value"] for ch in st["volume"].values()]
        saved[st["index"]] = vols
        subprocess.run(["pactl", "set-sink-input-volume", str(st["index"]), *[str(int(v * factor)) for v in vols]])
    DUCK_FILE.write_text(json.dumps(saved))  # survives a crash, restored on next start


def unduck():
    if not DUCK_FILE.exists():
        return
    for idx, vols in json.loads(DUCK_FILE.read_text()).items():
        subprocess.run(["pactl", "set-sink-input-volume", idx, *map(str, vols)],
                       stderr=subprocess.DEVNULL)  # the stream may be gone by now
    DUCK_FILE.unlink()


def post_wav(port, path, language):
    boundary = uuid.uuid4().hex
    fields = {"response_format": "json", "temperature": "0.0"}
    if language != "auto":
        fields["language"] = language
    body = b""
    for k, v in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
             "Content-Type: audio/wav\r\n\r\n").encode()
    body += Path(path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/inference", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["text"]


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
        os.environ["QT_QPA_PLATFORM"] = "xcb"  # fallback: XWayland override-redirect window

    from PyQt6.QtCore import QObject, QProcess, QRectF, Qt, QTimer, pyqtSignal
    from PyQt6.QtGui import QColor, QFont, QPainter
    from PyQt6.QtNetwork import QLocalServer
    from PyQt6.QtWidgets import QApplication, QWidget

    cfg = load_config()
    CACHE.mkdir(parents=True, exist_ok=True)
    unduck()
    tones = {"start": [(660, 0.07), (880, 0.10)], "stop": [(880, 0.07), (660, 0.10)],
             "warn": [(988, 0.12), (0, 0.08), (988, 0.12), (0, 0.08), (988, 0.18)],
             "error": [(330, 0.25)]}
    for name, notes in tones.items():
        make_tone(CACHE / f"{name}.wav", notes)

    server = subprocess.Popen(
        ["whisper-server", "-m", str(MODEL_DIR / cfg["model"]), "--host", "127.0.0.1",
         "--port", str(cfg["port"]), "-l", cfg["language"], "-mc", "0", "-sns",
         # Silero VAD: only speech reaches Whisper, so silence can't turn into invented subtitles
         "--vad", "-vm", str(MODEL_DIR / "vad/ggml-silero-v5.1.2.bin"), "-vt", "0.35", "-vp", "400", "-vsd", "300"],
        stdout=open(CACHE / "whisper-server.log", "w"), stderr=subprocess.STDOUT)

    def server_ready():
        try:
            with socket.create_connection(("127.0.0.1", cfg["port"]), timeout=0.5):
                return True
        except OSError:
            return False

    RED, HOT = (255, 36, 56), (255, 110, 30)

    class Voicebox(QWidget):
        """KITT-style voice box: a row of LED columns rippling out from the centre, ALESSA below."""
        COLS, SEGS, SW, GAP, SH, SGAP = 21, 7, 7, 5, 3, 2
        W, H = 21 * 12 + 16, 66

        def __init__(self):
            super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                             | Qt.WindowType.Tool | Qt.WindowType.X11BypassWindowManagerHint
                             | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setFixedSize(self.W, self.H)
            self.state = "idle"     # listening | thinking | done | error
            self.level = 0.0
            self.warn = 0.0         # 0..1 how close to the limit
            self.minutes_left = None
            self.t0 = time.monotonic()
            # LED brightness persists between frames so lights fade like lamps instead of blinking
            self.leds = [[0.0] * self.SEGS for _ in range(self.COLS)]
            self.timer = QTimer(self, interval=16, timeout=self.update)
            if lsq:
                self.layer_shell()

        def layer_shell(self):
            from PyQt6 import sip
            self.winId()
            win = ctypes.c_void_p(lsq._ZN12LayerShellQt6Window3getEP7QWindow(
                ctypes.c_void_p(sip.unwrapinstance(self.windowHandle()))))
            top = cfg["position"] == "top"
            margins = (ctypes.c_int * 4)(0, cfg["margin"] if top else 0, 0, 0 if top else cfg["margin"])
            lsq._ZN12LayerShellQt6Window8setLayerENS0_5LayerE(win, 3)                     # overlay
            lsq._ZN12LayerShellQt6Window10setAnchorsE6QFlagsINS0_6AnchorEE(win, 1 if top else 2)
            lsq._ZN12LayerShellQt6Window10setMarginsERK8QMargins(win, margins)
            lsq._ZN12LayerShellQt6Window24setKeyboardInteractivityENS0_21KeyboardInteractivityE(win, 0)
            lsq._ZN12LayerShellQt6Window16setExclusiveZoneEi(win, -1)                  # ignore panels

        def place(self):
            geo = QApplication.primaryScreen().geometry()
            y = geo.top() + cfg["margin"] if cfg["position"] == "top" else geo.bottom() - self.H - cfg["margin"]
            self.move(geo.center().x() - self.W // 2, y)

        def set_state(self, state):
            self.state, self.t0 = state, time.monotonic()
            if state == "idle":
                self.timer.stop()
                self.hide()
                self.leds = [[0.0] * self.SEGS for _ in range(self.COLS)]
                return
            self.place()
            self.show()
            self.timer.start()

        def rgb(self):
            if self.state == "error":
                return RED
            if self.warn > 0:
                k = min(1.0, self.warn) * (0.5 + 0.5 * math.sin(time.monotonic() * 5))
                return tuple(int(r + (h - r) * k) for r, h in zip(RED, HOT))
            return RED

        def column_target(self, i, t):
            n = self.COLS
            d = abs(i - (n - 1) / 2) / ((n - 1) / 2)
            if self.state == "thinking":
                # two lights start at the edges, meet in the middle, then spread back out
                ph = (math.sin(t * 4.2) + 1) / 2
                return max(0.0, 1 - abs((1 - d) - ph) * 4.5) * 0.9
            if self.state == "done":
                return 1.0
            if self.state == "error":
                return 1.0 if int(t * 6) % 2 == 0 else 0.0
            return self.level * (1 - d * 0.7) * (0.82 + 0.18 * math.sin(t * 7 - d * 5))

        def paintEvent(self, _):
            t = time.monotonic() - self.t0
            alpha = 1.0
            if self.state in ("done", "error"):
                alpha = max(0.0, 1.0 - t / 0.9)
                if alpha == 0:
                    self.set_state("idle")
                    return
            p = QPainter(self)
            p.setOpacity(alpha)
            p.setPen(Qt.PenStyle.NoPen)
            r, g, b = self.rgb()
            n, segs = self.COLS, self.SEGS
            cx, cy = self.W / 2, 24
            for i in range(n):
                v = self.column_target(i, t)
                x = cx + (i - (n - 1) / 2) * (self.SW + self.GAP) - self.SW / 2
                for j in range(segs):
                    dj = abs(j - (segs - 1) / 2) / ((segs - 1) / 2 + 0.5)
                    target = max(0.0, min(1.0, (v * 1.25 - dj) * 3))
                    cur = self.leds[i][j]
                    cur += (target - cur) * (0.45 if target > cur else 0.10)
                    self.leds[i][j] = cur
                    if cur < 0.02:
                        continue
                    p.setBrush(QColor(r, g, b, int(255 * cur)))
                    y = cy + (j - (segs - 1) / 2) * (self.SH + self.SGAP) - self.SH / 2
                    p.drawRect(QRectF(x, y, self.SW, self.SH))
            p.setPen(QColor(128, 128, 134))  # neutral: readable on light and dark windows
            f = QFont("sans-serif", 10, QFont.Weight.DemiBold)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 5)
            p.setFont(f)
            label = "ALESSA" if self.minutes_left is None else f"ALESSA · {self.minutes_left}m"
            p.drawText(QRectF(0, cy + 22, self.W, 20), Qt.AlignmentFlag.AlignHCenter, label)
            p.end()

    class Daemon(QObject):
        transcribed = pyqtSignal(str, str)  # text, error

        def __init__(self):
            super().__init__()
            self.box = Voicebox()
            self.rec = None
            self.busy = False
            self.raw_path = CACHE / "current.raw"
            self.raw = None
            self.started = 0.0
            self.peak_db = -120.0
            self.warned = False
            self.clock = QTimer(self, interval=1000, timeout=self.tick)
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
            cmd = bytes(c.readAll()).decode().strip()
            c.disconnectFromServer()
            if cmd == "toggle":
                self.stop() if self.rec else self.start()
            elif cmd == "cancel":
                self.cancel()

        def start(self):
            if self.busy:
                return
            self.raw = open(self.raw_path, "wb")
            self.peak_db, self.warned, self.box.warn, self.box.minutes_left = -120.0, False, 0.0, None
            self.rec = QProcess(self)
            self.rec.readyReadStandardOutput.connect(self.on_audio)
            self.rec.start("parecord", ["--raw", "--format=s16le", f"--rate={RATE}", "--channels=1",
                                        "--latency-msec=30"])
            self.started = time.monotonic()
            self.clock.start()
            self.box.set_state("listening")
            self.play("start")
            QTimer.singleShot(250, lambda: duck(cfg["duck"]) if self.rec else None)

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
            k = 0.6 if target > self.box.level else 0.15  # fast attack, slow decay
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

        def cancel(self):
            if not self.rec:
                return
            self.end_recording()
            self.raw_path.unlink(missing_ok=True)
            unduck()
            self.play("stop")
            self.box.set_state("error")

        def stop(self):
            if not self.rec:
                return
            self.end_recording()
            self.play("stop")
            if self.peak_db < -50:  # nothing but silence: Whisper would only invent text
                unduck()
                self.box.set_state("error")
                return
            wav = CACHE / "last.wav"  # kept until the next recording, so nothing is lost on failure
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(self.raw_path.read_bytes())
            self.raw_path.unlink(missing_ok=True)
            self.busy = True
            self.box.set_state("thinking")
            threading.Thread(target=self.transcribe, args=(wav,), daemon=True).start()

        def transcribe(self, wav):
            try:
                while not server_ready():
                    if server.poll() is not None:
                        raise RuntimeError("whisper-server exited, see ~/.cache/alessa-voice/whisper-server.log")
                    time.sleep(0.3)
                text = drop_hallucinations(collapse_repeats(post_wav(cfg["port"], wav, cfg["language"])))
                self.transcribed.emit(text, "")
            except Exception as e:
                self.transcribed.emit("", str(e))

        def on_transcribed(self, text, error):
            self.busy = False
            unduck()
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
    QTimer(app, interval=500, timeout=lambda: None).start()  # let Python see signals
    try:
        app.exec()
    finally:
        server.terminate()
        del daemon


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "daemon"
    if cmd == "daemon":
        run_daemon()
    elif cmd in ("toggle", "cancel"):
        if not send(cmd):
            sys.exit("alessa-voice daemon is not running")
    elif cmd == "models":
        list_models()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
