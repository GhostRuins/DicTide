"""Global hotkey helpers (toggle / hold) signaled via queue tokens."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import keyboard as _keyboard
except ImportError:
    _keyboard = None  # type: ignore[assignment]


HOTKEY_EVENT_TOGGLE = "toggle"
HOTKEY_EVENT_HOLD_DOWN = "hold_down"
HOTKEY_EVENT_HOLD_UP = "hold_up"


@dataclass(frozen=True)
class HotkeyConfig:
    spec: str = "ctrl"
    activation: str = "toggle"
    double_tap: bool = True
    double_tap_interval_s: float = 0.55


def normalize_hotkey_spec(spec: str) -> str:
    text = (spec or "").strip().lower()
    if not text:
        return "ctrl"
    text = text.replace("escape", "esc")
    text = text.replace("left ctrl", "ctrl")
    text = text.replace("right ctrl", "ctrl")
    text = text.replace("left shift", "shift")
    text = text.replace("right shift", "shift")
    text = text.replace("left alt", "alt")
    text = text.replace("right alt", "alt")
    parts = [p.strip() for p in text.split("+") if p.strip()]
    if not parts:
        return "ctrl"
    return "+".join(parts)


def _single_key_aliases(spec: str) -> set[str]:
    s = normalize_hotkey_spec(spec)
    if "+" in s:
        return set()
    if s == "ctrl":
        return {"ctrl", "left ctrl", "right ctrl"}
    if s == "shift":
        return {"shift", "left shift", "right shift"}
    if s == "alt":
        return {"alt", "left alt", "right alt", "alt gr"}
    return {s}


def capture_next_hotkey(
    *,
    cancel_event: threading.Event | None = None,
    timeout_s: float = 30.0,
) -> str | None:
    """Capture the next pressed hotkey chord, returning normalized spec."""
    if _keyboard is None:
        raise ImportError(
            "Hotkey capture requires the 'keyboard' package. "
            "Install it with: pip install keyboard"
        )
    done = threading.Event()
    picked: dict[str, str | None] = {"value": None}
    started_at = time.monotonic()

    def hook_callback(event: object) -> None:
        if done.is_set():
            return
        try:
            ev_type = getattr(event, "event_type", None)
            name = str(getattr(event, "name", "") or "").strip().lower()
        except Exception:
            return
        if ev_type != _keyboard.KEY_DOWN or not name:
            return
        if name in {"esc", "escape"}:
            picked["value"] = None
            done.set()
            return
        try:
            combo = str(_keyboard.get_hotkey_name() or "").strip().lower()
        except Exception:
            combo = ""
        picked["value"] = normalize_hotkey_spec(combo or name)
        done.set()

    hid = _keyboard.hook(hook_callback, suppress=False)
    try:
        while not done.is_set():
            if cancel_event is not None and cancel_event.is_set():
                break
            if timeout_s > 0 and (time.monotonic() - started_at) > timeout_s:
                break
            time.sleep(0.02)
    finally:
        _keyboard.unhook(hid)

    return picked["value"] if done.is_set() else None


class HotkeyController:
    """Route global keyboard events into queue messages for the UI thread."""

    def __init__(self, signal_queue: "queue.Queue[str]") -> None:
        self._signal_queue = signal_queue
        self._stoppers: list[Callable[[], None]] = []

    def start(self, config: HotkeyConfig) -> None:
        self.stop()
        if _keyboard is None:
            raise ImportError(
                "Hotkeys require the 'keyboard' package (global key hooks). "
                "Install it with: pip install keyboard"
            )
        spec = normalize_hotkey_spec(config.spec)
        mode = (config.activation or "toggle").strip().lower()
        use_double = bool(config.double_tap and ("+" not in spec))
        if mode == "hold":
            self._start_hold(spec)
            return
        if use_double:
            self._start_double_tap(spec, max(0.1, float(config.double_tap_interval_s)))
        else:
            self._start_single_toggle(spec)

    def stop(self) -> None:
        while self._stoppers:
            stop = self._stoppers.pop()
            try:
                stop()
            except Exception:
                pass

    def _start_single_toggle(self, spec: str) -> None:
        assert _keyboard is not None
        handler = _keyboard.add_hotkey(
            spec,
            lambda: self._signal_queue.put(HOTKEY_EVENT_TOGGLE),
            suppress=False,
            trigger_on_release=False,
        )
        self._stoppers.append(lambda: _keyboard.remove_hotkey(handler))

    def _start_hold(self, spec: str) -> None:
        assert _keyboard is not None
        down_handler = _keyboard.add_hotkey(
            spec,
            lambda: self._signal_queue.put(HOTKEY_EVENT_HOLD_DOWN),
            suppress=False,
            trigger_on_release=False,
        )
        up_handler = _keyboard.add_hotkey(
            spec,
            lambda: self._signal_queue.put(HOTKEY_EVENT_HOLD_UP),
            suppress=False,
            trigger_on_release=True,
        )
        self._stoppers.append(lambda: _keyboard.remove_hotkey(down_handler))
        self._stoppers.append(lambda: _keyboard.remove_hotkey(up_handler))

    def _start_double_tap(self, spec: str, interval_s: float) -> None:
        assert _keyboard is not None
        aliases = _single_key_aliases(spec)
        last = 0.0
        lock = threading.Lock()

        def hook_callback(event: object) -> None:
            nonlocal last
            try:
                ev_type = getattr(event, "event_type", None)
                name = str(getattr(event, "name", "") or "").strip().lower()
            except Exception:
                return
            if ev_type != _keyboard.KEY_DOWN:
                return
            if name not in aliases:
                return
            now = time.monotonic()
            with lock:
                if last > 0 and (now - last) <= interval_s:
                    last = 0.0
                    self._signal_queue.put(HOTKEY_EVENT_TOGGLE)
                else:
                    last = now

        hid = _keyboard.hook(hook_callback, suppress=False)
        self._stoppers.append(lambda: _keyboard.unhook(hid))
