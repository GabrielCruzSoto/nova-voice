"""Local Silero VAD ONNX adapter for speech onset, turn end, and barge-in (NOVA-003 OP-2)."""

from __future__ import annotations

import asyncio
import importlib
import math
import struct
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from nova.domain.errors import VoicePipelineError
from nova.domain.models import AudioFrame


def pcm_s16le_to_float_list(pcm_s16le: bytes) -> list[float]:
    """Convert raw mono s16le PCM bytes to normalized [-1.0, 1.0] floats in RAM."""
    sample_count = len(pcm_s16le) // 2
    ints = struct.unpack(f"<{sample_count}h", pcm_s16le)
    return [val / 32768.0 for val in ints]


class VadBackendProtocol(Protocol):
    """Minimal synchronous VAD backend contract executed inside asyncio.to_thread."""

    def score_samples(self, samples: list[float], sample_rate_hz: int) -> float: ...

    def reset(self) -> None: ...


class _OnnxSileroVadBackend:
    """ONNX Runtime session wrapper for local Silero VAD weights."""

    def __init__(self, model_path: str | None) -> None:
        if not model_path or not Path(model_path).is_file():
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message="Local Silero VAD ONNX model path is missing or does not exist.",
            )
        try:
            np_mod = importlib.import_module("numpy")
            ort_mod = importlib.import_module("onnxruntime")
        except ImportError as exc:
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message=f"Optional package 'onnxruntime'/'numpy' is not installed: {exc}",
            ) from exc

        self._np: Any = np_mod
        try:
            opts = ort_mod.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            self._session: Any = ort_mod.InferenceSession(
                str(Path(model_path).resolve()),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message=f"Failed to load Silero VAD ONNX session: {exc}",
            ) from exc
        self._state: Any = self._np.zeros((2, 1, 128), dtype=self._np.float32)

    def score_samples(self, samples: list[float], sample_rate_hz: int) -> float:
        x = self._np.asarray([samples], dtype=self._np.float32)
        sr = self._np.asarray(sample_rate_hz, dtype=self._np.int64)
        ort_inputs = {"input": x, "state": self._state, "sr": sr}
        out, new_state = self._session.run(None, ort_inputs)
        self._state = new_state
        return float(out[0][0])

    def reset(self) -> None:
        self._state = self._np.zeros((2, 1, 128), dtype=self._np.float32)


class SileroVadSettings(BaseModel):
    """Configuration boundary for SileroVadAdapter (OP-2)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    model_path: str | None = None
    sample_rate_hz: int = Field(default=16000, ge=8000, le=16000)
    inference_timeout_seconds: float = Field(default=1.0, gt=0.0, le=5.0)
    model_load_timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)


class SileroVadAdapter:
    """Evaluates frame-level voice probability off the asyncio event loop."""

    def __init__(
        self,
        settings: SileroVadSettings | None = None,
        *,
        scorer_factory: Callable[[], VadBackendProtocol] | None = None,
    ) -> None:
        self._settings = settings or SileroVadSettings()
        self._scorer_factory = scorer_factory
        self._load_lock = asyncio.Lock()
        self._infer_lock = threading.Lock()
        self._backend: VadBackendProtocol | None = None
        self._pending_reset = False

    async def _ensure_backend(self) -> VadBackendProtocol:
        if self._backend is not None:
            return self._backend
        async with self._load_lock:
            if self._backend is None:
                factory = self._scorer_factory or (
                    lambda: _OnnxSileroVadBackend(self._settings.model_path)
                )
                try:
                    self._backend = await asyncio.wait_for(
                        asyncio.to_thread(factory),
                        timeout=self._settings.model_load_timeout_seconds,
                    )
                except TimeoutError as exc:
                    raise VoicePipelineError(
                        code="VOICE_MODEL_UNAVAILABLE",
                        message="Timed out while loading local Silero VAD model.",
                    ) from exc
            return self._backend

    async def speech_probability(self, frame: AudioFrame) -> float:
        """Compute voice activity probability in [0.0, 1.0] for one PCM frame."""
        if frame.sample_rate_hz != self._settings.sample_rate_hz:
            raise VoicePipelineError(
                code="VOICE_INVALID_PCM_FRAME",
                message=(
                    f"Silero VAD expects {self._settings.sample_rate_hz} Hz PCM, "
                    f"got {frame.sample_rate_hz} Hz."
                ),
            )

        backend = await self._ensure_backend()

        def _score_sync() -> float:
            with self._infer_lock:
                if self._pending_reset:
                    self._pending_reset = False
                    backend.reset()
                samples = pcm_s16le_to_float_list(frame.pcm_s16le)
                score = backend.score_samples(samples, frame.sample_rate_hz)
                if self._pending_reset:
                    self._pending_reset = False
                    backend.reset()
                return score

        try:
            raw_prob = await asyncio.wait_for(
                asyncio.to_thread(_score_sync),
                timeout=self._settings.inference_timeout_seconds,
            )
        except VoicePipelineError:
            raise
        except TimeoutError as exc:
            raise VoicePipelineError(
                code="VOICE_VAD_FAILED",
                message="Silero VAD frame inference exceeded timeout.",
            ) from exc
        except Exception as exc:
            raise VoicePipelineError(
                code="VOICE_VAD_FAILED",
                message=f"Silero VAD inference failed: {exc}",
            ) from exc

        if math.isnan(raw_prob):
            return 0.0
        return min(1.0, max(0.0, float(raw_prob)))

    def reset(self) -> None:
        """Idempotently clear internal recurrent VAD state without racing worker threads."""
        backend = self._backend
        if backend is None:
            self._pending_reset = True
            return
        if self._infer_lock.acquire(blocking=False):
            try:
                self._pending_reset = False
                backend.reset()
            finally:
                self._infer_lock.release()
        else:
            self._pending_reset = True

    async def close(self) -> None:
        """Release loaded ONNX session reference."""
        async with self._load_lock:
            self._backend = None
