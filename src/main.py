"""
Desktop UI: record audio, transcribe once on Stop, clipboard + optional injection.
Run from repo root: python -m src.main
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
from collections import deque
from typing import Any

import customtkinter as ctk
import numpy as np
import sounddevice as sd

from .audio_capture import AudioChunker
from .hotkey import (
    HOTKEY_EVENT_HOLD_DOWN,
    HOTKEY_EVENT_HOLD_UP,
    HOTKEY_EVENT_TOGGLE,
    HotkeyConfig,
    HotkeyController,
    capture_next_hotkey,
    normalize_hotkey_spec,
)
from . import logging_setup
from . import settings_store
from . import single_instance
from . import __version__
from .text_inject import (
    copy_transcript_to_clipboard,
    inject_text,
    inject_via_clipboard_paste,
)
from .transcriber import MODEL_PRESETS, Transcriber
from .tray_support import TrayIcon

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

log = logging.getLogger(__name__)

_INJECT_MODES = ("Off", "Type (Unicode)", "Paste (Ctrl+V)", "Clipboard only")
_MIN_PCM_SAMPLES = 3200  # ~0.2 s at 16 kHz; skip Whisper below this
_DEFAULT_MODEL = "small"
_HOTKEY_ACTIVATION_VALUES = ("toggle", "hold")
_DEFAULT_HOTKEY_SPEC = "ctrl"
_DEFAULT_HOTKEY_DOUBLE_TAP_INTERVAL_S = 0.55


def _parse_device_index_from_label(label: str) -> int | None:
    m = re.match(r"^(\d+)\s*:", (label or "").strip())
    if not m:
        return None
    return int(m.group(1))


class DictationApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"DicTide v{__version__}")
        self.geometry("960x640")
        self.minsize(720, 480)

        self._settings: dict[str, Any] = settings_store.load()

        self._audio_queue: queue.Queue[np.ndarray] = queue.Queue()
        self._meter_queue: queue.Queue[float] = queue.Queue(maxsize=1)
        self._chunker = self._make_chunker(self._settings.get("input_device_index"))

        self._transcriber: Transcriber | None = None
        self._transcriber_lock = threading.Lock()
        self._recording = False
        self._stop_capture = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._recording_chunks: list[np.ndarray] = []
        self._last_transcript: str = ""
        self._bootstrap_error: BaseException | None = None
        self._transcribe_worker: threading.Thread | None = None

        self._force_cpu_var = ctk.BooleanVar(value=bool(self._settings.get("force_cpu", False)))
        inj_default = self._settings.get("inject_mode", "Paste (Ctrl+V)")
        if inj_default not in _INJECT_MODES:
            inj_default = "Paste (Ctrl+V)"
        self._inject_mode = tk.StringVar(value=inj_default)
        delay_default = self._settings.get("inject_delay_ms", 300)
        try:
            delay_default = max(0, int(delay_default))
        except (TypeError, ValueError):
            delay_default = 300
        self._inject_delay_ms = tk.StringVar(value=str(delay_default))
        self._confirm_inject_var = ctk.BooleanVar(
            value=bool(self._settings.get("confirm_before_inject", False))
        )
        model_default = str(self._settings.get("model", _DEFAULT_MODEL)).strip() or _DEFAULT_MODEL
        self._model_var = tk.StringVar(value=model_default)
        self._custom_model_var = tk.StringVar(
            value=str(self._settings.get("custom_model", "") or "").strip()
        )
        if not self._custom_model_var.get() and model_default not in MODEL_PRESETS:
            self._custom_model_var.set(model_default)
            self._model_var.set(_DEFAULT_MODEL)

        activation_default = str(self._settings.get("hotkey_activation", "toggle")).strip().lower()
        if activation_default not in _HOTKEY_ACTIVATION_VALUES:
            activation_default = "toggle"
        self._hotkey_activation_var = tk.StringVar(value=activation_default)
        self._hotkey_spec_var = tk.StringVar(
            value=normalize_hotkey_spec(str(self._settings.get("hotkey_spec", _DEFAULT_HOTKEY_SPEC)))
        )
        self._hotkey_double_tap_var = ctk.BooleanVar(
            value=bool(self._settings.get("hotkey_double_tap", True))
        )

        self._hotkey: HotkeyController | None = None
        self._hotkey_debounce_mono = 0.0
        self._hotkey_queue: queue.Queue[str] = queue.Queue()
        self._hotkey_capture_cancel = threading.Event()
        self._hotkey_capture_thread: threading.Thread | None = None
        self._tray_queue: queue.Queue[str] = queue.Queue()
        self._tray: TrayIcon | None = None

        self._history: deque[str] = deque(maxlen=10)
        self._context_visible = ctk.BooleanVar(
            value=bool(self._settings.get("show_context", False))
        )
        self._device_snapshot_while_recording: str | None = None

        self._build_ui()
        self._apply_loaded_prompt()
        self._populate_input_devices(select_index=self._settings.get("input_device_index"))
        self._refresh_history_combo()

        self.protocol("WM_DELETE_WINDOW", self._on_close_to_tray)
        self.after(50, self._poll_async_actions)
        self.after(50, self._poll_meter)
        self._ensure_hotkey_listener()
        self._ensure_tray()
        self._start_bootstrap()

    def _make_chunker(self, device: int | None) -> AudioChunker:
        return AudioChunker(
            self._audio_queue,
            chunk_duration_s=2.0,
            device=device,
            meter_queue=self._meter_queue,
        )

    def _apply_loaded_prompt(self) -> None:
        p = self._settings.get("initial_prompt", "")
        if isinstance(p, str) and p.strip():
            self._prompt_text.insert("1.0", p)

    def _save_settings(self) -> None:
        try:
            settings_store.save(
                {
                    "initial_prompt": self._prompt_text.get("1.0", "end").strip(),
                    "input_device_index": self._parse_device_from_combo(),
                    "model": self._current_model_value(),
                    "custom_model": self._custom_model_var.get().strip(),
                    "inject_mode": self._inject_combo.get(),
                    "inject_delay_ms": self._parse_inject_delay_ms(),
                    "confirm_before_inject": self._confirm_inject_var.get(),
                    "force_cpu": self._force_cpu_var.get(),
                    "show_context": self._context_visible.get(),
                    "hotkey_spec": normalize_hotkey_spec(self._hotkey_spec_var.get()),
                    "hotkey_activation": self._hotkey_activation_var.get(),
                    "hotkey_double_tap": self._hotkey_double_tap_var.get(),
                    "hotkey_double_tap_interval_s": _DEFAULT_HOTKEY_DOUBLE_TAP_INTERVAL_S,
                }
            )
        except OSError as e:
            log.warning("Could not save settings: %s", e)

    def _current_model_value(self) -> str:
        custom = self._custom_model_var.get().strip()
        if custom:
            return custom
        chosen = self._model_var.get().strip()
        return chosen or _DEFAULT_MODEL

    def _parse_device_from_combo(self) -> int | None:
        return _parse_device_index_from_label(self._device_combo.get())

    def _build_ui(self) -> None:
        body = ctk.CTkFrame(self)
        body.pack(fill="both", expand=True, padx=10, pady=10)

        left = ctk.CTkFrame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        right = ctk.CTkFrame(body, width=220)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        top = ctk.CTkFrame(left)
        top.pack(fill="x", pady=(0, 4))

        self._model_combo = ctk.CTkComboBox(
            top,
            values=list(MODEL_PRESETS),
            width=140,
            variable=self._model_var,
            command=self._on_model_selected,
        )
        if self._model_var.get() not in MODEL_PRESETS:
            self._model_var.set(_DEFAULT_MODEL)
        self._model_combo.set(self._model_var.get())

        self._start_btn = ctk.CTkButton(
            top, text="Start", width=90, command=self._on_start, state="disabled"
        )
        self._stop_btn = ctk.CTkButton(
            top, text="Stop", width=90, command=self._on_stop, state="disabled"
        )
        self._cancel_btn = ctk.CTkButton(
            top, text="Cancel", width=90, command=self._on_cancel, state="disabled"
        )

        self._cpu_switch = ctk.CTkSwitch(
            top,
            text="Force CPU",
            variable=self._force_cpu_var,
            command=self._on_force_cpu_toggle,
        )

        ctk.CTkLabel(top, text="Model:").pack(side="left", padx=(0, 6))
        self._model_combo.pack(side="left", padx=(0, 8))
        self._start_btn.pack(side="left", padx=4)
        self._stop_btn.pack(side="left", padx=4)
        self._cancel_btn.pack(side="left", padx=4)
        self._cpu_switch.pack(side="left", padx=(16, 8))

        model_row = ctk.CTkFrame(left)
        model_row.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(model_row, text="Custom model:").pack(side="left", padx=(0, 6))
        self._custom_model_entry = ctk.CTkEntry(
            model_row,
            width=420,
            textvariable=self._custom_model_var,
            placeholder_text="Path to converted model folder, or Hugging Face model ID",
        )
        self._custom_model_entry.pack(side="left", padx=4)
        self._custom_model_entry.bind("<FocusOut>", lambda _e: self._on_custom_model_changed())
        self._custom_model_entry.bind("<Return>", lambda _e: self._apply_custom_model())
        ctk.CTkButton(model_row, text="Browse…", width=90, command=self._browse_custom_model).pack(
            side="left", padx=(6, 0)
        )
        ctk.CTkButton(
            model_row, text="Use custom", width=100, command=self._apply_custom_model
        ).pack(side="left", padx=(6, 0))

        hotkey_row = ctk.CTkFrame(left)
        hotkey_row.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(hotkey_row, text="Hotkey:").pack(side="left", padx=(0, 6))
        self._hotkey_display = ctk.CTkLabel(
            hotkey_row, text=normalize_hotkey_spec(self._hotkey_spec_var.get()), width=130, anchor="w"
        )
        self._hotkey_display.pack(side="left", padx=4)
        self._hotkey_change_btn = ctk.CTkButton(
            hotkey_row, text="Change hotkey…", width=130, command=self._on_change_hotkey_clicked
        )
        self._hotkey_change_btn.pack(side="left", padx=(6, 10))
        ctk.CTkLabel(hotkey_row, text="Mode:").pack(side="left", padx=(0, 6))
        self._hotkey_mode_combo = ctk.CTkComboBox(
            hotkey_row,
            values=list(_HOTKEY_ACTIVATION_VALUES),
            width=100,
            variable=self._hotkey_activation_var,
            command=self._on_hotkey_mode_changed,
        )
        self._hotkey_mode_combo.pack(side="left", padx=4)
        self._hotkey_double_tap = ctk.CTkCheckBox(
            hotkey_row,
            text="Double-tap key",
            variable=self._hotkey_double_tap_var,
            command=self._on_hotkey_double_tap_changed,
        )
        self._hotkey_double_tap.pack(side="left", padx=(12, 4))

        dev_row = ctk.CTkFrame(left)
        dev_row.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(dev_row, text="Microphone:").pack(side="left", padx=(0, 6))
        self._device_combo = ctk.CTkComboBox(
            dev_row,
            values=["(loading…)"],
            width=420,
            command=self._on_device_selected,
        )
        self._device_combo.pack(side="left", padx=4)

        self._meter_bar = ctk.CTkProgressBar(left, width=360)
        self._meter_bar.pack(anchor="w", pady=(0, 4))
        self._meter_bar.set(0)
        ctk.CTkLabel(left, text="Input level", text_color="gray").pack(anchor="w")

        row2 = ctk.CTkFrame(left)
        row2.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(row2, text="Inject:").pack(side="left", padx=(0, 6))
        self._inject_combo = ctk.CTkComboBox(
            row2,
            values=list(_INJECT_MODES),
            variable=self._inject_mode,
            width=200,
            command=lambda _v: self._save_settings(),
        )
        self._inject_combo.pack(side="left", padx=4)
        ctk.CTkLabel(row2, text="Delay (ms):").pack(side="left", padx=(12, 4))
        self._inject_delay_entry = ctk.CTkEntry(
            row2, width=56, textvariable=self._inject_delay_ms
        )
        self._inject_delay_entry.pack(side="left", padx=4)
        self._inject_delay_entry.bind("<FocusOut>", lambda _e: self._save_settings())

        self._confirm_inject = ctk.CTkCheckBox(
            row2,
            text="Confirm before inject",
            variable=self._confirm_inject_var,
            command=self._save_settings,
        )
        self._confirm_inject.pack(side="left", padx=(16, 4))

        ctx_toggle = ctk.CTkCheckBox(
            left,
            text="Show context / vocabulary (Whisper initial prompt)",
            variable=self._context_visible,
            command=self._toggle_context_panel,
        )
        ctx_toggle.pack(anchor="w", pady=(4, 0))

        self._context_frame = ctk.CTkFrame(left)
        self._prompt_text = ctk.CTkTextbox(
            self._context_frame, height=72, font=ctk.CTkFont(family="Segoe UI", size=12)
        )
        self._prompt_text.pack(fill="x", padx=4, pady=4)
        self._prompt_text.bind("<FocusOut>", lambda _e: self._save_settings())

        self._status = ctk.CTkLabel(left, text="Starting…", anchor="w")
        self._status.pack(fill="x", pady=(4, 4))

        self._meta = ctk.CTkLabel(left, text="", anchor="w", text_color="gray")
        self._meta.pack(fill="x", pady=(0, 4))

        self._text = ctk.CTkTextbox(left, font=ctk.CTkFont(family="Segoe UI", size=14))
        self._text.pack(fill="both", expand=True, pady=(0, 8))

        clear_row = ctk.CTkFrame(left)
        clear_row.pack(fill="x", pady=(0, 4))
        ctk.CTkButton(clear_row, text="Clear transcript", command=self._clear_transcript).pack(
            side="left"
        )

        ctk.CTkLabel(right, text="Recent transcripts").pack(anchor="w", pady=(0, 6))
        self._history_combo = ctk.CTkComboBox(right, values=["(none)"], width=200)
        self._history_combo.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(
            right, text="Insert selected", width=200, command=self._insert_history_selection
        ).pack(fill="x")

        if self._context_visible.get():
            self._context_frame.pack(fill="x", pady=(0, 4))
        else:
            self._context_frame.pack_forget()
        self._refresh_hotkey_display()
        self._sync_hotkey_controls()

    def _toggle_context_panel(self) -> None:
        if self._context_visible.get():
            self._context_frame.pack(fill="x", pady=(0, 4))
        else:
            self._context_frame.pack_forget()
        self._save_settings()

    def _hotkey_config(self) -> HotkeyConfig:
        return HotkeyConfig(
            spec=normalize_hotkey_spec(self._hotkey_spec_var.get()),
            activation=self._hotkey_activation_var.get(),
            double_tap=self._hotkey_double_tap_var.get(),
            double_tap_interval_s=_DEFAULT_HOTKEY_DOUBLE_TAP_INTERVAL_S,
        )

    def _refresh_hotkey_display(self) -> None:
        self._hotkey_display.configure(text=normalize_hotkey_spec(self._hotkey_spec_var.get()))

    def _on_change_hotkey_clicked(self) -> None:
        if self._hotkey_capture_thread is not None and self._hotkey_capture_thread.is_alive():
            self._hotkey_capture_cancel.set()
            self._hotkey_change_btn.configure(state="disabled")
            return
        self._hotkey_capture_cancel = threading.Event()
        self._hotkey_change_btn.configure(text="Cancel capture", state="normal")
        self._status.configure(text="Press the new hotkey now (Esc cancels).")

        def run_capture() -> None:
            try:
                captured = capture_next_hotkey(
                    cancel_event=self._hotkey_capture_cancel,
                    timeout_s=30.0,
                )
            except Exception as e:
                self.after(0, lambda err=e: self._on_hotkey_capture_failed(err))
                return
            self.after(0, lambda val=captured: self._on_hotkey_capture_done(val))

        self._hotkey_capture_thread = threading.Thread(target=run_capture, daemon=True)
        self._hotkey_capture_thread.start()

    def _on_hotkey_capture_failed(self, err: BaseException) -> None:
        self._hotkey_change_btn.configure(text="Change hotkey…", state="normal")
        self._status.configure(text="Hotkey capture failed.")
        messagebox.showwarning(
            "Hotkey",
            f"Could not capture hotkey ({err}). Install keyboard package and try admin if needed.",
        )

    def _on_hotkey_capture_done(self, captured: str | None) -> None:
        self._hotkey_change_btn.configure(text="Change hotkey…", state="normal")
        if not captured:
            self._status.configure(text="Hotkey capture cancelled.")
            return
        self._hotkey_spec_var.set(normalize_hotkey_spec(captured))
        self._refresh_hotkey_display()
        self._save_settings()
        self._restart_hotkey_listener()
        self._status.configure(text=f"Hotkey updated to {self._hotkey_spec_var.get()}.")

    def _on_hotkey_mode_changed(self, _choice: str) -> None:
        self._sync_hotkey_controls()
        self._save_settings()
        self._restart_hotkey_listener()

    def _on_hotkey_double_tap_changed(self) -> None:
        self._save_settings()
        self._restart_hotkey_listener()

    def _sync_hotkey_controls(self) -> None:
        if self._hotkey_activation_var.get() == "hold":
            self._hotkey_double_tap.deselect()
            self._hotkey_double_tap.configure(state="disabled")
        else:
            self._hotkey_double_tap.configure(state="normal")

    def _browse_custom_model(self) -> None:
        selected = filedialog.askdirectory(title="Choose converted Whisper model folder")
        if not selected:
            return
        self._custom_model_var.set(selected)
        self._apply_custom_model()

    def _on_custom_model_changed(self) -> None:
        self._save_settings()

    def _apply_custom_model(self) -> None:
        custom = self._custom_model_var.get().strip()
        if not custom:
            return
        self._save_settings()
        self._status.configure(text=f"Loading custom model '{custom}'…")
        self._reload_transcriber()

    def _populate_input_devices(self, *, select_index: Any = None) -> None:
        labels: list[str] = []
        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
        except Exception as e:
            log.warning("query_devices failed: %s", e)
            self._device_combo.configure(values=["(no devices)"])
            return

        for i, d in enumerate(devices):
            if int(d.get("max_input_channels", 0) or 0) < 1:
                continue
            name = str(d.get("name", f"device {i}"))
            api = ""
            ha = d.get("hostapi")
            if ha is not None and 0 <= int(ha) < len(hostapis):
                api = hostapis[int(ha)].get("name", "")
            suffix = f" — {api}" if api else ""
            labels.append(f"{i}: {name}{suffix}")

        if not labels:
            self._device_combo.configure(values=["(no input devices)"])
            return

        self._device_combo.configure(values=labels)
        want: int | None
        if select_index is not None:
            try:
                want = int(select_index)
            except (TypeError, ValueError):
                want = None
        else:
            want = self._parse_device_from_combo()

        chosen = None
        for lab in labels:
            if want is not None and lab.startswith(f"{want}:"):
                chosen = lab
                break
        if chosen is None:
            try:
                def_in = sd.default.device[0]
                if def_in is not None and def_in >= 0:
                    for lab in labels:
                        if lab.startswith(f"{int(def_in)}:"):
                            chosen = lab
                            break
            except Exception:
                pass
        if chosen is None:
            chosen = labels[0]
        self._device_combo.set(chosen)

        if not self._recording:
            self._apply_chunker_device(_parse_device_index_from_label(chosen))

    def _on_device_selected(self, _choice: str) -> None:
        if self._recording:
            snap = self._device_snapshot_while_recording
            if snap:
                self._device_combo.set(snap)
            self._status.configure(
                text="Recording… (microphone change applies after Stop or Cancel.)"
            )
            return
        idx = self._parse_device_from_combo()
        self._apply_chunker_device(idx)
        self._save_settings()
        log.info("Input device set to index %s", idx)

    def _apply_chunker_device(self, device: int | None) -> None:
        if self._chunker is not None and self._recording:
            return
        self._chunker = self._make_chunker(device)

    def _refresh_history_combo(self) -> None:
        if not self._history:
            self._history_combo.configure(values=["(none)"])
            self._history_combo.set("(none)")
            return
        vals = list(reversed(self._history))
        short = []
        for i, t in enumerate(vals):
            one = t.replace("\n", " ").strip()
            if len(one) > 42:
                one = one[:39] + "…"
            short.append(f"{i + 1}. {one}")
        self._history_combo.configure(values=short)
        self._history_combo.set(short[0])

    def _insert_history_selection(self) -> None:
        sel = self._history_combo.get()
        if sel in ("(none)", "(loading…)"):
            return
        m = re.match(r"^\d+\.\s*", sel)
        if not m:
            return
        idx = int(sel.split(".", 1)[0]) - 1
        vals = list(reversed(self._history))
        if 0 <= idx < len(vals):
            self._text.delete("1.0", "end")
            self._text.insert("1.0", vals[idx])

    def _append_history(self, text: str) -> None:
        t = (text or "").strip()
        if not t:
            return
        if self._history and self._history[-1] == t:
            return
        self._history.append(t)
        self._refresh_history_combo()

    def _parse_inject_delay_ms(self) -> int:
        try:
            return max(0, int(self._inject_delay_ms.get().strip()))
        except ValueError:
            return 300

    def _model_for_loading(self) -> str:
        model = self._current_model_value().strip()
        return model or _DEFAULT_MODEL

    def _start_bootstrap(self) -> None:
        model = self._model_for_loading()
        force_cpu = self._force_cpu_var.get()

        def load() -> None:
            try:
                device = "cpu" if force_cpu else None
                compute_type = "int8" if force_cpu else None
                t = Transcriber(
                    model_size=model,
                    device=device,
                    compute_type=compute_type,
                )
                with self._transcriber_lock:
                    self._transcriber = t
            except BaseException as e:
                self._bootstrap_error = e

        threading.Thread(target=load, daemon=True).start()
        self.after(300, self._finish_bootstrap)

    def _finish_bootstrap(self) -> None:
        if self._bootstrap_error is not None:
            err = self._bootstrap_error
            self._bootstrap_error = None
            self._status.configure(text="Failed to load model.")
            log.error("Model load failed: %s", err, exc_info=True)
            messagebox.showerror("Model load error", str(err))
            self._start_btn.configure(state="disabled")
            return

        if self._transcriber is None:
            self.after(300, self._finish_bootstrap)
            return

        self._refresh_meta()
        self._status.configure(
            text=(
                "Ready. Record with Start or configured hotkey; Stop runs transcription once. "
                "Cancel discards the clip. Close hides to tray."
            )
        )
        log.info(
            "Model ready size=%s infer_device=%s compute=%s",
            self._model_for_loading(),
            self._transcriber.device,
            self._transcriber.compute_type,
        )
        self._start_btn.configure(state="normal")

    def _poll_async_actions(self) -> None:
        try:
            while True:
                action = self._hotkey_queue.get_nowait()
                if action == HOTKEY_EVENT_TOGGLE:
                    self._handle_hotkey_toggle()
                elif action == HOTKEY_EVENT_HOLD_DOWN:
                    self._handle_hotkey_hold_down()
                elif action == HOTKEY_EVENT_HOLD_UP:
                    self._handle_hotkey_hold_up()
        except queue.Empty:
            pass
        try:
            while True:
                act = self._tray_queue.get_nowait()
                if act == "show":
                    self._show_from_tray()
                elif act == "hide":
                    self._hide_to_tray()
                elif act == "quit":
                    self._quit_fully()
        except queue.Empty:
            pass
        self.after(80, self._poll_async_actions)

    def _poll_meter(self) -> None:
        if self._recording:
            last = None
            try:
                while True:
                    last = self._meter_queue.get_nowait()
            except queue.Empty:
                pass
            if last is not None:
                self._meter_bar.set(float(last))
        else:
            self._meter_bar.set(0)
        self.after(50, self._poll_meter)

    def _refresh_meta(self) -> None:
        if self._transcriber:
            mic = self._device_combo.get()
            self._meta.configure(
                text=(
                    f"Mic: {mic}  |  Model: {self._transcriber.model_label}  "
                    f"|  Whisper device: {self._transcriber.device}  "
                    f"|  compute: {self._transcriber.compute_type}"
                )
            )

    def _on_force_cpu_toggle(self) -> None:
        if self._recording:
            messagebox.showinfo(
                "Force CPU",
                "Stop recording first, then toggle and the app will reload the model.",
            )
            self._force_cpu_var.set(not self._force_cpu_var.get())
            return
        self._save_settings()
        self._reload_transcriber()

    def _reload_transcriber(self) -> None:
        self._bootstrap_error = None
        self._transcriber = None
        self._start_btn.configure(state="disabled")
        self._status.configure(text="Reloading model…")

        model = self._model_for_loading()
        force_cpu = self._force_cpu_var.get()

        def load() -> None:
            try:
                device = "cpu" if force_cpu else None
                compute_type = "int8" if force_cpu else None
                t = Transcriber(
                    model_size=model,
                    device=device,
                    compute_type=compute_type,
                )
                with self._transcriber_lock:
                    self._transcriber = t
            except BaseException as e:
                self._bootstrap_error = e

        threading.Thread(target=load, daemon=True).start()
        self.after(300, self._finish_bootstrap)

    def _on_model_selected(self, choice: str) -> None:
        self._model_var.set(choice)
        if self._custom_model_var.get().strip():
            self._custom_model_var.set("")
        self._save_settings()
        if self._transcriber is None:
            return

        def swap() -> None:
            try:
                with self._transcriber_lock:
                    if self._transcriber is not None:
                        self._transcriber.set_model(choice)
                self.after(0, self._on_model_swap_ok)
            except Exception as e:
                self.after(0, lambda: self._on_model_swap_err(e))

        self._status.configure(text=f"Loading model '{choice}'…")
        threading.Thread(target=swap, daemon=True).start()

    def _on_model_swap_ok(self) -> None:
        self._refresh_meta()
        self._status.configure(text="Model loaded.")
        log.info("Model swapped to %s", self._model_for_loading())

    def _on_model_swap_err(self, e: Exception) -> None:
        self._status.configure(text="Model switch failed.")
        log.error("Model swap failed: %s", e, exc_info=True)
        messagebox.showerror("Model error", str(e))

    def _ensure_hotkey_listener(self) -> None:
        try:
            if self._hotkey is None:
                self._hotkey = HotkeyController(self._hotkey_queue)
            self._hotkey.start(self._hotkey_config())
        except Exception as e:
            messagebox.showwarning(
                "Hotkey",
                f"Global hotkey could not start ({e}). Use Start/Stop buttons.\n"
                "Install: pip install keyboard. Try Run as administrator if hooks are blocked.",
            )

    def _restart_hotkey_listener(self) -> None:
        self._ensure_hotkey_listener()

    def _handle_hotkey_toggle(self) -> None:
        if self._transcriber is None:
            return
        now = time.monotonic()
        if now - self._hotkey_debounce_mono < 0.4:
            return
        self._hotkey_debounce_mono = now
        if self._recording:
            self._on_stop()
        else:
            self._on_start()

    def _handle_hotkey_hold_down(self) -> None:
        if self._transcriber is None:
            return
        if self._hotkey_activation_var.get() != "hold":
            return
        if self._recording:
            return
        self._on_start()

    def _handle_hotkey_hold_up(self) -> None:
        if self._hotkey_activation_var.get() != "hold":
            return
        if self._recording:
            self._on_stop()

    def _ensure_tray(self) -> None:
        if self._tray is not None:
            return
        try:
            self._tray = TrayIcon(
                on_show=lambda: self._tray_queue.put("show"),
                on_hide=lambda: self._tray_queue.put("hide"),
                on_quit=lambda: self._tray_queue.put("quit"),
            )
            self._tray.start()
        except Exception as e:
            messagebox.showwarning(
                "System tray",
                f"Tray icon could not start ({e}). Install: pip install pystray Pillow",
            )

    def _on_close_to_tray(self) -> None:
        self._save_settings()
        self._ensure_tray()
        self._hide_to_tray()

    def _show_from_tray(self) -> None:
        self.deiconify()
        self.after(10, self.lift)
        self.after(20, self.focus_force)

    def _hide_to_tray(self) -> None:
        self.withdraw()

    def _wait_transcribe_worker(self, timeout: float = 120.0) -> None:
        t = self._transcribe_worker
        if t is not None and t.is_alive():
            t.join(timeout=timeout)

    def _quit_fully(self) -> None:
        self._save_settings()
        if self._hotkey_capture_thread is not None and self._hotkey_capture_thread.is_alive():
            self._hotkey_capture_cancel.set()
            self._hotkey_capture_thread.join(timeout=1.0)
        self._wait_transcribe_worker()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None
        if self._recording:
            self._on_stop()
            self._wait_transcribe_worker()
        self.quit()
        self.destroy()

    def _capture_drain_loop(self) -> None:
        while not self._stop_capture.is_set():
            try:
                chunk = self._audio_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            self._recording_chunks.append(chunk)

    def _reset_buttons_after_pipeline(self) -> None:
        if self._transcriber is not None:
            self._start_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")
        self._cancel_btn.configure(state="disabled")
        self._cpu_switch.configure(state="normal")
        self._device_combo.configure(state="normal")

    def _drain_audio_queues_discard(self) -> None:
        while True:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break
        while True:
            try:
                self._meter_queue.get_nowait()
            except queue.Empty:
                break

    def _on_start(self) -> None:
        if self._transcriber is None:
            return
        self._wait_transcribe_worker()
        self._drain_audio_queues_discard()
        self._recording_chunks = []
        self._last_transcript = ""
        self._text.delete("1.0", "end")
        self._text.insert("1.0", "Recording… Transcript appears after Stop.\n")
        self._stop_capture.clear()
        self._recording = True
        self._device_snapshot_while_recording = self._device_combo.get()
        self._chunker.start()
        self._capture_thread = threading.Thread(target=self._capture_drain_loop, daemon=True)
        self._capture_thread.start()
        self._start_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._cancel_btn.configure(state="normal")
        self._cpu_switch.configure(state="disabled")
        self._device_combo.configure(state="disabled")
        self._status.configure(text="Recording…")
        self._refresh_meta()

    def _on_cancel(self) -> None:
        if not self._recording:
            return
        self._stop_capture.set()
        self._chunker.stop()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=5.0)
        self._capture_thread = None

        self._drain_audio_queues_discard()
        self._chunker.flush_partial()
        self._recording_chunks = []
        self._recording = False
        self._device_snapshot_while_recording = None

        self._text.delete("1.0", "end")
        self._status.configure(text="Recording cancelled.")
        self._reset_buttons_after_pipeline()
        log.info("Recording cancelled by user")

    def _on_stop(self) -> None:
        if not self._recording:
            return
        self._stop_capture.set()
        self._chunker.stop()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=5.0)
        self._capture_thread = None

        while True:
            try:
                self._recording_chunks.append(self._audio_queue.get_nowait())
            except queue.Empty:
                break
        tail = self._chunker.flush_partial()
        if tail.size > 0:
            self._recording_chunks.append(tail)

        self._recording = False
        self._device_snapshot_while_recording = None
        self._stop_btn.configure(state="disabled")
        self._cancel_btn.configure(state="disabled")
        self._cpu_switch.configure(state="normal")
        self._device_combo.configure(state="normal")

        if not self._recording_chunks:
            self._apply_transcript("")
            return

        pcm = np.concatenate(self._recording_chunks)
        self._recording_chunks = []

        if pcm.size < _MIN_PCM_SAMPLES:
            self._apply_transcript("")
            return

        self._status.configure(text="Transcribing…")
        prompt = (self._prompt_text.get("1.0", "end") or "").strip()
        initial_prompt = prompt if prompt else None

        def work() -> None:
            t0 = time.perf_counter()
            try:
                with self._transcriber_lock:
                    tr = self._transcriber
                    if tr is None:
                        self.after(0, lambda: self._apply_transcript(""))
                        return
                    text = tr.transcribe(pcm, initial_prompt=initial_prompt)
            except Exception as e:
                self.after(0, lambda err=e: self._transcribe_failed(err))
                return
            elapsed = time.perf_counter() - t0
            log.info("Transcription done in %.2fs chars=%s", elapsed, len(text or ""))
            self.after(0, lambda t=text: self._apply_transcript(t))

        self._transcribe_worker = threading.Thread(target=work, daemon=True)
        self._transcribe_worker.start()

    def _transcribe_failed(self, err: BaseException) -> None:
        messagebox.showerror("Transcription error", str(err))
        self._status.configure(text="Transcription failed.")
        log.error("Transcription failed: %s", err, exc_info=True)
        self._reset_buttons_after_pipeline()

    def _apply_transcript(self, text: str) -> None:
        self._last_transcript = (text or "").strip()
        self._text.delete("1.0", "end")
        if self._last_transcript:
            self._text.insert("1.0", self._last_transcript)
            self._append_history(self._last_transcript)
        self._finish_output_pipeline()

    def _schedule_inject(self, full: str, mode: str, delay_ms: int) -> None:
        self._status.configure(
            text=f"Click the target field — injecting in {delay_ms} ms… ({mode})"
        )

        def do_inject() -> None:
            try:
                if mode.startswith("Type"):
                    inject_text(full + " ")
                else:
                    inject_via_clipboard_paste(full + " ", restore_clipboard=False)
            except OSError as ex:
                messagebox.showwarning("Injection", str(ex))
                log.warning("Inject failed: %s", ex)
            try:
                copy_transcript_to_clipboard(full)
            except OSError:
                pass
            self._status.configure(text="Stopped. Injected (if possible) and clipboard updated.")
            self._reset_buttons_after_pipeline()

        self.after(delay_ms, do_inject)

    def _finish_output_pipeline(self) -> None:
        full = self._last_transcript
        mode = self._inject_combo.get()
        delay_ms = self._parse_inject_delay_ms()

        if not full:
            self._status.configure(text="Stopped. No speech transcribed.")
            self._reset_buttons_after_pipeline()
            return

        try:
            copy_transcript_to_clipboard(full)
        except OSError as e:
            messagebox.showwarning("Clipboard", str(e))
            log.warning("Clipboard: %s", e)

        log.info("Output pipeline mode=%s delay_ms=%s", mode, delay_ms)

        if mode in ("Off", "Clipboard only"):
            if mode == "Clipboard only":
                msg = "Stopped. Transcript in app and clipboard (inject skipped)."
            else:
                msg = "Stopped. Transcript in app and clipboard."
            self._status.configure(text=msg)
            self._reset_buttons_after_pipeline()
            return

        # If this app still has focus, injection usually targets our own textbox and appears duplicated.
        focus_widget = self.focus_get()
        try:
            focused_in_app = (
                focus_widget is not None and focus_widget.winfo_toplevel() == self
            )
        except Exception:
            focused_in_app = False
        if focused_in_app:
            self._status.configure(
                text="Stopped. Transcript in app and clipboard (inject skipped: app is focused)."
            )
            self._reset_buttons_after_pipeline()
            return

        if self._confirm_inject_var.get():

            def ask() -> None:
                if not messagebox.askyesno(
                    "Inject transcript",
                    "Inject the transcript into the focused field now?\n\n"
                    "Click the target field first if needed.",
                ):
                    self._status.configure(text="Stopped. Clipboard updated; inject skipped.")
                    self._reset_buttons_after_pipeline()
                    return
                self._schedule_inject(full, mode, delay_ms)

            self.after(0, ask)
            return

        self._schedule_inject(full, mode, delay_ms)

    def _clear_transcript(self) -> None:
        self._text.delete("1.0", "end")
        self._last_transcript = ""


def main() -> None:
    import sys

    if sys.platform == "win32" and not single_instance.acquire():
        single_instance.notify_already_running()
        return

    log_dir = settings_store.data_dir()
    logging_setup.configure(log_dir)
    logging.getLogger(__name__).info("DicTide starting (log dir: %s)", log_dir)

    try:
        app = DictationApp()
        app.mainloop()
    finally:
        single_instance.release()
        logging.getLogger(__name__).info("DicTide exit")


if __name__ == "__main__":
    main()
