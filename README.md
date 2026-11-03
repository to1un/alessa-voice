# alessa-voice

Minimal local voice dictation for KDE Plasma (Wayland). Press a key, talk, press it again: the text is pasted where your cursor is.

Built for talking to an AI assistant without staring at an empty screen: a KITT-style voice box (yes, Knight Rider) listens while you speak.

![Listening](docs/listening.png)
![Thinking](docs/thinking.png)

## Features

- **Fully local**: [whisper.cpp](https://github.com/ggml-org/whisper.cpp) `whisper-server` on the GPU (Vulkan), model kept loaded.
- **Voice box overlay** in KWin's top layer: LEDs follow your voice, two lights meet in the middle while transcribing. Never steals focus or clicks.
- **Radio-style ducking**: playing media is turned down to 30% while you talk (per app, so no volume popup) and restored after the text is pasted.
- **Long recordings**: 60 min limit by default; from minute 50 the box turns hot and pulses, a chime at 55. At the limit it transcribes (or discards, if you prefer).
- **Fewer hallucinations**: Silero VAD, no text context carry-over (stops repeat loops), filter for invented YouTube-subtitle phrases.
- The last recording is kept in `~/.cache/alessa-voice/last.wav` until the next one.

## Install (Arch / CachyOS)

```bash
sudo pacman -S python-pyqt6 layer-shell-qt whisper-cpp libpulse pipewire wl-clipboard ydotool
systemctl --user enable --now ydotool
# whisper-cpp must be built with the Vulkan backend (CachyOS/Arch extra: yes)
```

Models go in `~/.local/share/whisper/`:

```bash
mkdir -p ~/.local/share/whisper/vad
curl -L -o ~/.local/share/whisper/ggml-large-v3.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin
curl -L -o ~/.local/share/whisper/vad/ggml-silero-v5.1.2.bin https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin
```

Then:

```bash
git clone https://github.com/to1un/alessa-voice ~/projects/alessa-voice
ln -s ~/projects/alessa-voice/alessa_voice.py ~/.local/bin/alessa-voice
alessa-voice &
```

Bind two global shortcuts in *System Settings → Keyboard → Shortcuts → Add New → Command*: `alessa-voice toggle` (e.g. F8) and `alessa-voice cancel` (e.g. Ctrl+F8). Add `alessa-voice` to autostart.

`ydotool` needs write access to `/dev/uinput` (the `input` group or a udev rule).

## Usage

| | |
|---|---|
| `alessa-voice` | run the daemon |
| `alessa-voice toggle` | start / stop recording |
| `alessa-voice cancel` | discard the recording |
| `alessa-voice models` | list local models |

Settings live in `~/.config/alessa-voice/config.toml` (created on first run): model, language, limit and warning minutes, ducking level, position, sounds.

## License

MIT
