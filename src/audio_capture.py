"""Non-blocking microphone capture: mono float32 PCM at 16 kHz into fixed-duration chunks."""

from __future__ import annotations

import queue
import threading
from typing import Optional

import numpy as np
import sounddevice as sd

TARGET_SAMPLE_RATE = 16000


def _to_mono_float32(indata: np.ndarray) -> np.ndarray:
    """Convert input block to 1-D float32 mono (mean across channels if needed)."""
    x = np.asarray(indata, dtype=np.float32)
    if x.ndim == 2 and x.shape[1] > 1:
        x = x.mean(axis=1)
    elif x.ndim == 2:
        x = x[:, 0]
    return np.ascontiguousarray(x)


def _resample_linear(mono: np.ndarray, sr_in: float, sr_out: float = TARGET_SAMPLE_RATE) -> np.ndarray:
    """Resample 1-D audio with linear interpolation (time-domain)."""
    if sr_in == sr_out:
        return np.asarray(mono, dtype=np.float32, order="C")
    x = np.asarray(mono, dtype=np.float64)
    n_in = int(x.size)
    if n_in == 0:
        return np.array([], dtype=np.float32)
    t_in = np.arange(n_in, dtype=np.float64) / float(sr_in)
    t_end = float(t_in[-1])
    n_out = max(1, int(round(n_in * sr_out / sr_in)))
    t_out = np.linspace(0.0, t_end, n_out, dtype=np.float64)
    y = np.interp(t_out, t_in, x)
    return y.astype(np.float32)


class AudioChunker:
    """Capture from the default (or selected) input device and enqueue mono 16 kHz float32 chunks.

    Each queued item is a ``numpy.ndarray`` of dtype float32 and shape ``(n_samples,)`` where
    ``n_samples == round(chunk_duration_s * 16000)``.

    ``stop()`` closes the stream; any partial audio left in the internal buffer remains until you
    call ``flush_partial()`` or ``start()`` (which resets the buffer). Restart with ``start()`` for
    a fresh capture session.
    """

    def __init__(
        self,
        out_queue: queue.Queue,
        chunk_duration_s: float = 1.0,
        *,
        device: Optional[int | str] = None,
        blocksize: int = 0,
        meter_queue: Optional["queue.Queue[float]"] = None,
    ) -> None:
        """
        Args:
            out_queue: Queue receiving ``np.ndarray`` float32 mono chunks at 16 kHz.
            chunk_duration_s: Length of each chunk in seconds (after resampling).
            device: ``sounddevice`` input device id or name; ``None`` uses default input.
            blocksize: PortAudio block size; ``0`` lets the host pick.
            meter_queue: Optional queue (``maxsize=1`` recommended). RMS level 0..1 is pushed
                with replace-if-full so the audio thread never blocks.
        """
        self._out_queue = out_queue
        self._chunk_samples = max(1, int(round(float(chunk_duration_s) * TARGET_SAMPLE_RATE)))
        self._device = device
        self._blocksize = int(blocksize)
        self._meter_queue = meter_queue

        self._stream: Optional[sd.InputStream] = None
        self._device_sr: float = float(TARGET_SAMPLE_RATE)
        self._lock = threading.Lock()
        self._accum = np.empty(0, dtype=np.float32)

    @property
    def chunk_samples(self) -> int:
        """Number of samples per emitted chunk at 16 kHz."""
        return self._chunk_samples

    def start(self) -> None:
        """Open an input stream and begin enqueueing full chunks (non-blocking callback)."""
        if self._stream is not None:
            raise RuntimeError("AudioChunker already started; call stop() first.")

        dev_id = self._device
        if dev_id is None:
            dev_id = sd.default.device[0]
            if dev_id is None or dev_id < 0:
                raise RuntimeError("No default input device.")

        info = sd.query_devices(dev_id, "input")
        self._device_sr = float(info["default_samplerate"])
        max_ch = int(info["max_input_channels"])
        if max_ch < 1:
            raise RuntimeError("Input device has no input channels.")
        channels = min(2, max_ch)

        with self._lock:
            self._accum = np.empty(0, dtype=np.float32)

        def _push_meter_level(mono_block: np.ndarray) -> None:
            q = self._meter_queue
            if q is None or mono_block.size == 0:
                return
            rms = float(np.sqrt(np.mean(mono_block * mono_block, dtype=np.float64)))
            level = min(1.0, rms * 4.0)
            try:
                q.put_nowait(level)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(level)
                except queue.Full:
                    pass

        def callback(indata, _frames, _time_info, _status) -> None:
            mono = _to_mono_float32(indata)
            _push_meter_level(mono)
            block = _resample_linear(mono, self._device_sr, TARGET_SAMPLE_RATE)
            to_put: list[np.ndarray] = []
            with self._lock:
                if self._accum.size:
                    self._accum = np.concatenate((self._accum, block))
                else:
                    self._accum = block
                while self._accum.size >= self._chunk_samples:
                    chunk = self._accum[: self._chunk_samples].copy()
                    self._accum = self._accum[self._chunk_samples :]
                    to_put.append(chunk)
            for c in to_put:
                self._out_queue.put(c)

        self._stream = sd.InputStream(
            device=dev_id,
            channels=channels,
            samplerate=self._device_sr,
            dtype="float32",
            blocksize=self._blocksize if self._blocksize > 0 else None,
            callback=callback,
        )
        self._stream.start()

    def stop(self) -> None:
        """Stop the stream. Partial audio stays in the buffer; use ``flush_partial()`` to retrieve it."""
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.stop()
            stream.close()

    def flush_partial(self) -> np.ndarray:
        """Return and clear any samples buffered since the last full chunk (may be empty)."""
        with self._lock:
            if self._accum.size == 0:
                return np.empty(0, dtype=np.float32)
            out = np.ascontiguousarray(self._accum.copy())
            self._accum = np.empty(0, dtype=np.float32)
            return out
