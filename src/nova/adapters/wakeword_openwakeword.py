"""Local OpenWakeWord ONNX adapter for 'Oye Nova' / 'Hey Nova' activation (NOVA-003 OP-2)."""

from __future__ import annotations

import asyncio
import importlib
import math
import struct
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from nova.domain.errors import VoicePipelineError
from nova.domain.models import AudioFrame, WakeWordDetection


class WakeWordBackendProtocol(Protocol):
    """Minimal synchronous backend contract executed inside asyncio.to_thread."""

    def predict_scores(self, pcm_s16le: bytes) -> dict[str, float]: ...

    def reset(self) -> None: ...


class _OpenWakeWordRuntimeBackend:
    """Thin wrapper around openwakeword.model.Model using 16-bit PCM in RAM."""

    def __init__(self, model_paths: tuple[str, ...]) -> None:
        for raw_path in model_paths:
            if not Path(raw_path).is_file():
                raise VoicePipelineError(
                    code="VOICE_MODEL_UNAVAILABLE",
                    message=f"Wake-word ONNX model file not found: {raw_path}",
                )
        try:
            np_mod = importlib.import_module("numpy")
            oww_mod = importlib.import_module("openwakeword.model")
        except ImportError as exc:
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message=f"Optional package 'openwakeword' is not installed: {exc}",
            ) from exc

        self._np: Any = np_mod
        kwargs: dict[str, Any] = {"inference_framework": "onnx"}
        if model_paths:
            kwargs["wakeword_models"] = list(model_paths)
        try:
            self._model: Any = oww_mod.Model(**kwargs)
        except Exception as exc:
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message=f"Failed to initialize local OpenWakeWord model: {exc}",
            ) from exc

    def predict_scores(self, pcm_s16le: bytes) -> dict[str, float]:
        arr = self._np.frombuffer(pcm_s16le, dtype=self._np.int16)
        raw_scores = self._model.predict(arr)
        if not isinstance(raw_scores, dict):
            return {}
        return {str(k): float(v) for k, v in raw_scores.items()}

    def reset(self) -> None:
        reset_fn = getattr(self._model, "reset", None)
        if callable(reset_fn):
            reset_fn()


class OpenWakeWordSettings(BaseModel):
    """Configuration boundary for OpenWakeWordAdapter (OP-2)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    wake_words: tuple[str, ...] = ("oye_nova", "hey_nova")
    threshold: float = Field(default=0.60, gt=0.0, le=1.0)
    cooldown_seconds: float = Field(default=1.5, ge=0.0, le=30.0)
    inference_timeout_seconds: float = Field(default=1.0, gt=0.0, le=5.0)
    model_paths: tuple[str, ...] = ()


class OpenWakeWordAdapter:
    """Evaluates 16 kHz PCM frames off the event loop with anti-bounce cooldown."""

    def __init__(
        self,
        settings: OpenWakeWordSettings | None = None,
        *,
        predictor_factory: Callable[[], WakeWordBackendProtocol] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings or OpenWakeWordSettings()
        self._predictor_factory = predictor_factory
        self._clock = clock
        self._load_lock = asyncio.Lock()
        self._backend: WakeWordBackendProtocol | None = None
        self._last_detected_at: float | None = None
        self._pending_reset = False

    async def _ensure_backend(self) -> WakeWordBackendProtocol:
        if self._backend is not None:
            return self._backend
        async with self._load_lock:
            if self._backend is None:
                factory = self._predictor_factory or (
                    lambda: _OpenWakeWordRuntimeBackend(self._settings.model_paths)
                )
                try:
                    self._backend = await asyncio.wait_for(
                        asyncio.to_thread(factory),
                        timeout=self._settings.inference_timeout_seconds,
                    )
                except TimeoutError as exc:
                    raise VoicePipelineError(
                        code="VOICE_MODEL_UNAVAILABLE",
                        message="Timed out while loading local OpenWakeWord model.",
                    ) from exc
            return self._backend

    async def detect(self, frame: AudioFrame) -> WakeWordDetection:
        """Evaluate one 16 kHz mono s16le frame and enforce cooldown suppression."""
        if frame.sample_rate_hz != 16000 or len(frame.pcm_s16le) < 2:
            raise VoicePipelineError(
                code="VOICE_INVALID_PCM_FRAME",
                message=f"OpenWakeWord expects 16000 Hz PCM, got {frame.sample_rate_hz} Hz.",
            )
        # Ensure clean 16-bit alignment
        if len(frame.pcm_s16le) % 2 != 0:
            raise VoicePipelineError(
                code="VOICE_INVALID_PCM_FRAME",
                message="PCM byte length must be even.",
            )

        backend = await self._ensure_backend()

        def _run_sync() -> dict[str, float]:
            if self._pending_reset:
                backend.reset()
                self._pending_reset = False
            # Verify buffer unpackability cleanly
            _ = struct.unpack_from("<h", frame.pcm_s16le, 0)
            return backend.predict_scores(frame.pcm_s16le)

        try:
            scores = await asyncio.wait_for(
                asyncio.to_thread(_run_sync),
                timeout=self._settings.inference_timeout_seconds,
            )
        except VoicePipelineError:
            raise
        except TimeoutError as exc:
            raise VoicePipelineError(
                code="VOICE_VAD_FAILED",
                message="Wake-word frame inference exceeded timeout.",
            ) from exc
        except Exception as exc:
            raise VoicePipelineError(
                code="VOICE_VAD_FAILED",
                message=f"Wake-word inference failed: {exc}",
            ) from exc

        allowed = {w.lower() for w in self._settings.wake_words}
        best_word: str | None = None
        best_score = 0.0
        for name, raw_val in scores.items():
            norm_name = name.lower()
            if allowed and norm_name not in allowed:
                continue
            clamped = 0.0 if math.isnan(raw_val) else min(1.0, max(0.0, float(raw_val)))
            if clamped >= best_score:
                best_score = clamped
                best_word = norm_name

        now = self._clock()
        in_cooldown = (
            self._last_detected_at is not None
            and (now - self._last_detected_at) < self._settings.cooldown_seconds
        )
        detected = best_score >= self._settings.threshold and not in_cooldown
        if detected:
            self._last_detected_at = now
            await asyncio.to_thread(backend.reset)

        return WakeWordDetection(
            detected=detected,
            wake_word=best_word if detected else None,
            score=best_score,
        )

    def reset(self) -> None:
        """Idempotently reset wake-word state."""
        if self._backend is not None:
            self._backend.reset()
        else:
            self._pending_reset = True

    async def close(self) -> None:
        """Release loaded wake-word model reference from memory."""
        async with self._load_lock:
            self._backend = None
