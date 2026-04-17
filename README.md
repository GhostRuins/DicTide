# DicTide

Offline dictation with [faster-whisper](https://github.com/SYSTRAN/faster-whisper). You **record** first; **Stop** runs **one** Whisper pass on the full clip (no live chunk transcription, so no duplicated sentences from chunk boundaries). The transcript appears in the app, is copied to the **clipboard**, and can be **injected** into the focused field.

Landing page: [https://ghostruins.github.io/DicTide/](https://ghostruins.github.io/DicTide/)

The global hotkey is configurable from the UI. You can switch between **Toggle** mode (press to start/stop) and **Hold** mode (hold to record, release to stop). **Closing the window (X)** hides the app to the **system tray**; use tray **Quit** to exit.

Only **one instance** of DicTide may run at a time; starting a second copy shows a message (check the tray if you “lost” the window).

## Requirements

- Windows 10 or 11
- Python 3.10+ (3.11–3.12 recommended for PyInstaller portability)
- Microphone (Settings → Privacy → Microphone)
- **NVIDIA GPU (optional):** `nvidia-smi`; CUDA is used unless **Force CPU** is on

## Setup

```powershell
cd path\to\Transcript
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
```

First run downloads Whisper weights for the selected model. The default model is **small**.

## Run

```powershell
.\.venv\Scripts\activate
python -m src.main
```

Or: `python run_app.py`

## Usage

1. **Microphone:** pick the input device in the dropdown (host API is shown). While recording, changes are ignored until Stop or **Cancel**.
2. **Model:** pick any built-in faster-whisper preset (`tiny` through `turbo`) from the dropdown, or enter a custom model path / Hugging Face model ID. The status line shows model label, **Whisper device** (CUDA/CPU), and **compute** type.
3. **Context / vocabulary:** optional checkbox opens a field whose text is passed to Whisper as `initial_prompt` (helps names, jargon, and domain wording). Settings (including this text) are saved under `%LOCALAPPDATA%\DicTide\settings.json`.
4. **Inject:** **Off** | **Type (Unicode)** | **Paste (Ctrl+V)** | **Clipboard only** — **Paste** works in more apps (browsers, Electron). **Clipboard only** copies to the clipboard and skips inject. **Delay (ms)** waits before inject so you can **click the target text field** after Stop.
5. **Confirm before inject:** when enabled, you get a yes/no prompt before typing or pasting (clipboard is already updated).
6. **Hotkey:** click **Change hotkey…** and press the next key/chord to bind it (Esc cancels). Choose **toggle** or **hold** mode. In toggle mode, **Double-tap key** can require two quick presses before toggling.
7. **Start** (or hotkey), speak, **Stop**. **Cancel** discards the recording without transcribing. Status shows **Transcribing…**, then the transcript fills once.
8. **Input level** bar shows approximate mic level while recording.
9. **Recent transcripts:** in-memory list (last 10); use **Insert selected** to copy one into the editor.
10. **Force CPU** reloads the model; do not toggle while recording.

**Hotkey** uses the **`keyboard`** package. If it never fires: `pip install keyboard`, try **Run as administrator**, and check antivirus/security software hooks.

**Tray:** `pip install pystray Pillow`. **Quit** from the tray stops the app (if recording, Stop runs first).

## Logs

Rotating log file: `%LOCALAPPDATA%\DicTide\app.log` (model load, transcription timing, inject mode, errors).

## Build executable

```powershell
python -m pip install -r requirements.txt -r requirements-build.txt
python -m PyInstaller DicTide.spec --clean --noconfirm
```

Output: **`dist\DicTide\`** — ship the **whole folder** (`DicTide.exe` + `_internal`).

Or: `.\build_windows.ps1` (expects `.venv`).

## Build single installer

This project now includes an Inno Setup script: `DicTideInstaller.iss`.

1. Build the app folder first (`dist\DicTide\`).
2. Install [Inno Setup 6](https://jrsoftware.org/isinfo.php).
3. Run:

```powershell
.\build_windows.ps1 -Installer
```

Installer output: **`dist\installer\DicTideSetup-<version>.exe`**.

## Start at Windows logon

```powershell
cd scripts
.\add_startup_shortcut.ps1
```

Or `-TargetPath "C:\path\to\DicTide.exe"`. Remove `DicTide.lnk` from your Startup folder to disable.

## Troubleshooting

### Failed to load Python DLL (frozen app)

Ship the full **`DicTide`** folder including **`_internal`**. Install [VC++ x64 redist](https://aka.ms/vs/17/release/vc_redist.x64.exe). Prefer building with Python 3.11/3.12.

### Injection does nothing

Use **Paste (Ctrl+V)** and increase **Delay**; click the target field after Stop. Admin-only targets may block input (UIPI).

### Hotkey does not trigger reliably

Try a less common binding (for example `f9`), and avoid keys heavily used by other apps. Global hooks may require elevated rights on some systems.

### Large model load is slow

`large-*` and `turbo` can require large downloads and significantly more RAM/VRAM than `base`/`small`.

### Silent failures

See `%LOCALAPPDATA%\DicTide\app.log`.

## Project layout

- `src/audio_capture.py` — mic → 16 kHz mono chunks + `flush_partial()` + optional level meter queue
- `src/transcriber.py` — faster-whisper
- `src/text_inject.py` — Unicode typing, clipboard, Ctrl+V paste
- `src/hotkey.py` — configurable global hotkey controller + next-key capture
- `src/tray_support.py` — pystray
- `src/main.py` — UI
- `src/single_instance.py` — Windows single-instance mutex
- `src/logging_setup.py` — rotating file log
- `src/settings_store.py` — JSON settings in `%LOCALAPPDATA%\DicTide\`
- `scripts/add_startup_shortcut.ps1`

## License

MIT. See [LICENSE](LICENSE).
