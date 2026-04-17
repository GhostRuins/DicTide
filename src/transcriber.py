"""
Offline speech-to-text using faster-whisper (WhisperModel).
"""

from __future__ import annotations

import difflib
import gc
import os
import re
import ctranslate2
import numpy as np
from faster_whisper import WhisperModel


def _dedupe_segment_list(parts: list[str]) -> list[str]:
    out: list[str] = []
    for p in parts:
        t = (p or "").strip()
        if not t:
            continue
        if out and t.casefold() == out[-1].casefold():
            continue
        out.append(t)
    return out


def _normalize_whisper_output(raw_segments: list[str]) -> str:
    """Join Whisper segment texts and strip common duplicate phrasing."""
    parts = _dedupe_segment_list(raw_segments)
    joined = "".join(parts).strip()
    joined = _dedupe_repeated_halves_and_sentences(joined)
    return _dedupe_back_to_back_utterance(joined)


def _dedupe_repeated_halves_and_sentences(text: str) -> str:
    """Remove 'foo foo' full-string repeats and consecutive duplicate sentences."""
    s = (text or "").strip()
    if len(s) < 4:
        return s
    n = len(s)
    # `range(n//2, 12, -1)` is empty when n//2 <= 12, so short/medium duplicates were never removed.
    min_half = 3
    for half_len in range(n // 2, min_half - 1, -1):
        if half_len * 2 > n:
            continue
        a = s[:half_len].strip()
        b = s[half_len : half_len * 2].strip()
        if len(b) < half_len:
            continue
        if a and a.casefold() == b.casefold():
            return _dedupe_repeated_halves_and_sentences(s[:half_len].strip())
    segs = re.split(r"(?<=[.!?])\s+", s)
    merged: list[str] = []
    for seg in segs:
        seg = seg.strip()
        if not seg:
            continue
        if merged and seg.casefold() == merged[-1].casefold():
            continue
        merged.append(seg)
    return " ".join(merged)


def _dedupe_back_to_back_utterance(text: str) -> str:
    """
    Whisper often returns the same spoken passage twice in one decode (A then A again).
    The split is rarely exactly len/2 (spaces/punctuation), so we scan a band around
    the midpoint with stripped segments and a high similarity threshold.
    """
    s = " ".join((text or "").split()).strip()
    if len(s) < 24:
        return (text or "").strip()

    for _ in range(4):
        n = len(s)
        if n < 24:
            break
        center = n // 2
        span = min(160, max(24, n // 3))
        lo = max(12, center - span)
        hi = min(n - 12, center + span)
        best_k: int | None = None
        best_r = 0.0
        for k in range(lo, hi + 1):
            a = s[:k].strip()
            b = s[k:].strip()
            if len(a) < 12 or len(b) < 12:
                continue
            af, bf = a.casefold(), b.casefold()
            if af == bf:
                best_k, best_r = k, 1.0
                break
            r = difflib.SequenceMatcher(None, af, bf).ratio()
            if r > best_r:
                best_r = r
                best_k = k
        if best_k is not None and best_r >= 0.965:
            s = s[:best_k].strip()
            continue
        break

    return s


def _strip_initial_prompt_echo(text: str, prompt: str) -> str:
    """Remove leading text when Whisper repeats vocabulary from ``initial_prompt``."""
    t = (text or "").strip()
    p = " ".join((prompt or "").split()).strip()
    if not t or not p:
        return t
    pattern = r"^\s*" + re.escape(p) + r"\s*"
    for _ in range(3):
        new_t = re.sub(pattern, "", t, count=1, flags=re.IGNORECASE)
        if new_t == t:
            break
        t = new_t.strip()
    return t


MODEL_PRESETS: tuple[str, ...] = (
    "tiny",
    "tiny.en",
    "base",
    "base.en",
    "small",
    "small.en",
    "distil-small.en",
    "medium",
    "medium.en",
    "distil-medium.en",
    "large-v1",
    "large-v2",
    "large-v3",
    "large",
    "distil-large-v2",
    "distil-large-v3",
    "large-v3-turbo",
    "turbo",
)


def _cuda_device_available() -> bool:
    try:
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _resolve_device_and_compute_type(
    device: str | None,
    compute_type: str | None,
) -> tuple[str, str]:
    """Pick device/compute_type; full auto when both are None."""
    if device is not None and compute_type is not None:
        return device, compute_type

    if device is None and compute_type is None:
        if _cuda_device_available():
            return "cuda", "float16"
        return "cpu", "int8"

    if device is None:
        # compute_type specified; prefer CUDA if it looks like a GPU type
        gpu_hints = ("float16", "float32", "int8_float16", "bfloat16")
        if compute_type in gpu_hints and _cuda_device_available():
            return "cuda", compute_type
        return "cpu", compute_type

    # device specified, compute_type None
    if device == "cuda":
        return "cuda", "float16"
    return device, "int8"


class Transcriber:
    """
    Load a Whisper model once (or swap with set_model) and transcribe 16 kHz mono float32 audio.
    """

    def __init__(
        self,
        model_size: str = "base",
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        model_size = (model_size or "").strip()
        if not model_size:
            raise ValueError("model_size must be a non-empty model name, HF ID, or local path")

        resolved_device, resolved_compute = _resolve_device_and_compute_type(
            device, compute_type
        )
        self.device = resolved_device
        self.compute_type = resolved_compute
        self._model_size = model_size
        self._model: WhisperModel | None = None
        self._load_model_with_fallback(device is None and compute_type is None)

    def _load_model_with_fallback(self, allow_cuda_fallback: bool) -> None:
        """Instantiate WhisperModel; optionally fall back from CUDA to CPU on failure."""
        last_error: Exception | None = None
        try:
            self._model = WhisperModel(
                self._model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
            return
        except Exception as e:
            last_error = e
            if (
                allow_cuda_fallback
                and self.device == "cuda"
                and self.compute_type == "float16"
            ):
                self.device = "cpu"
                self.compute_type = "int8"
                try:
                    self._model = WhisperModel(
                        self._model_size,
                        device=self.device,
                        compute_type=self.compute_type,
                    )
                    return
                except Exception as e2:
                    raise RuntimeError(
                        f"Failed to load Whisper model {self._model_size!r} on CUDA "
                        f"and on CPU fallback. CUDA error: {last_error!r}. "
                        f"CPU error: {e2!r}"
                    ) from e2
            raise RuntimeError(
                f"Failed to load Whisper model {self._model_size!r} with "
                f"device={self.device!r}, compute_type={self.compute_type!r}: {e!r}"
            ) from last_error

    def set_model(self, model_size: str) -> None:
        model_size = (model_size or "").strip()
        if not model_size:
            raise ValueError("model_size must be a non-empty model name, HF ID, or local path")
        self._release_model()
        self._model_size = model_size
        try:
            self._model = WhisperModel(
                self._model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to load Whisper model {model_size!r} with "
                f"device={self.device!r}, compute_type={self.compute_type!r}: {e!r}"
            ) from e

    @property
    def model_label(self) -> str:
        """Display-friendly model name for logs/UI metadata."""
        value = (self._model_size or "").strip()
        if not value:
            return ""
        if os.path.isdir(value):
            return os.path.basename(value.rstrip("\\/")) or value
        return value

    def _release_model(self) -> None:
        self._model = None
        gc.collect()

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = "en",
        vad_filter: bool = True,
        initial_prompt: str | None = None,
    ) -> str:
        if self._model is None:
            raise RuntimeError("No model loaded; call set_model or fix initialization.")

        arr = np.asarray(audio, dtype=np.float32)
        if arr.ndim != 1:
            raise ValueError(
                f"audio must be a 1D array (mono16 kHz), got shape {arr.shape}"
            )

        # Single pass over full utterance; no cross-chunk conditioning.
        kw: dict = dict(
            language=language,
            vad_filter=vad_filter,
            condition_on_previous_text=False,
            without_timestamps=True,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
        )
        if initial_prompt:
            kw["initial_prompt"] = initial_prompt
        segments, _info = self._model.transcribe(arr, **kw)
        raw = [seg.text for seg in segments]
        out = _normalize_whisper_output(raw)
        if initial_prompt:
            stripped = _strip_initial_prompt_echo(out, initial_prompt)
            if stripped:
                out = stripped
        return out

    def __del__(self) -> None:
        try:
            self._release_model()
        except Exception:
            pass
