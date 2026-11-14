"""Local Spanish faster-whisper STT adapter with automatic CUDA -> CPU fallback (NOVA-003 OP-3)."""

from __future__ import annotations

import asyncio
import importlib
import math
import time
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from nova.adapters.vad_silero import pcm_s16le_to_float_list
from nova.domain.errors import VoicePipelineError
from nova.domain.models import InferenceDevice, TranscriptionResult


class WhisperModelProtocol(Protocol):
    """Minimal synchronous faster-whisper model contract executed inside asyncio.to_thread."""

    def transcribe(
        self,
        audio_samples: Any,
        *,
        language: str | None,
        beam_size: int,
    ) -> tuple[Iterable[Any], Any]: ...


WhisperModelFactory = Callable[[str, str, str, bool], WhisperModelProtocol]


def _default_whisper_factory(
    model_size_or_path: str,
    device: str,
    compute_type: str,
    local_files_only: bool,
) -> WhisperModelProtocol:
    try:
        fw_mod = importlib.import_module("faster_whisper")
    except ImportError as exc:
        raise VoicePipelineError(
            code="VOICE_MODEL_UNAVAILABLE",
            message=f"Optional package 'faster-whisper' is not installed: {exc}",
        ) from exc

    model_cls: Any = fw_mod.WhisperModel
    instance: WhisperModelProtocol = model_cls(
        model_size_or_path,
        device=device,
        compute_type=compute_type,
        local_files_only=local_files_only,
    )
    return instance


def _resample_linear(samples: list[float], src_rate: int, dst_rate: int) -> list[float]:
    if src_rate == dst_rate or not samples:
        return samples
    out_len = max(1, int(round(len(samples) * dst_rate / src_rate)))
    if out_len == 1:
        return [samples[0]]
    scale = (len(samples) - 1) / (out_len - 1)
    out: list[float] = []
    for idx in range(out_len):
        pos = idx * scale
        lo = int(math.floor(pos))
        hi = min(len(samples) - 1, lo + 1)
        frac = pos - lo
        out.append(samples[lo] * (1.0 - frac) + samples[hi] * frac)
    return out


def _prepare_audio_array(samples: list[float]) -> Any:
    try:
        np_mod = importlib.import_module("numpy")
        return np_mod.asarray(samples, dtype=np_mod.float32)
    except ImportError:
        return samples


class FasterWhisperSettings(BaseModel):
    """Configuration boundary for FasterWhisperSttAdapter (OP-3 & NFR-4)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    model_size_or_path: str = Field(default="small", min_length=1)
    language: str | None = Field(default="es", min_length=2, max_length=10)
    preferred_device: InferenceDevice = "cuda"
    cuda_compute_type: str = Field(default="float16", min_length=1)
    cpu_compute_type: str = Field(default="int8", min_length=1)
    beam_size: int = Field(default=1, ge=1, le=10)
    timeout_seconds: float = Field(default=15.0, gt=0.0, le=60.0)
    local_files_only: bool = True


class FasterWhisperSttAdapter:
    """Transcribes in-memory mono PCM utterances locally with GPU-first fallback."""

    def __init__(
        self,
        settings: FasterWhisperSettings | None = None,
        *,
        model_factory: WhisperModelFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings or FasterWhisperSettings()
        self._factory: WhisperModelFactory = model_factory or _default_whisper_factory
        self._clock = clock
        self._load_lock = asyncio.Lock()
        self._model: WhisperModelProtocol | None = None
        self._active_device: InferenceDevice = self._settings.preferred_device

    def update_settings(self, *, language: str | None = None) -> None:
        """Hot-update Whisper language hint (`None` means auto-detect)."""
        self._settings = self._settings.model_copy(update={"language": language})

    def _instantiate_sync(self, device: InferenceDevice) -> WhisperModelProtocol:
        compute = (
            self._settings.cuda_compute_type
            if device == "cuda"
            else self._settings.cpu_compute_type
        )
        return self._factory(
            self._settings.model_size_or_path,
            device,
            compute,
            self._settings.local_files_only,
        )

    async def _ensure_model(self) -> tuple[WhisperModelProtocol, InferenceDevice]:
        if self._model is not None:
            return self._model, self._active_device

        async with self._load_lock:
            if self._model is not None:
                return self._model, self._active_device

            if self._settings.preferred_device == "cuda":
                try:
                    self._model = await asyncio.wait_for(
                        asyncio.to_thread(self._instantiate_sync, "cuda"),
                        timeout=self._settings.timeout_seconds,
                    )
                    self._active_device = "cuda"
                    return self._model, "cuda"
                except VoicePipelineError:
                    raise
                except (TimeoutError, RuntimeError, OSError, ValueError):
                    # Automatic fallback to CPU/int8 when CUDA runtime or VRAM fails/hangs
                    pass

            try:
                self._model = await asyncio.wait_for(
                    asyncio.to_thread(self._instantiate_sync, "cpu"),
                    timeout=self._settings.timeout_seconds,
                )
                self._active_device = "cpu"
                return self._model, "cpu"
            except VoicePipelineError:
                raise
            except TimeoutError as exc:
                raise VoicePipelineError(
                    code="VOICE_TRANSCRIPTION_TIMEOUT",
                    message="Timed out while loading local faster-whisper model.",
                ) from exc
            except Exception as exc:
                raise VoicePipelineError(
                    code="VOICE_MODEL_UNAVAILABLE",
                    message=f"Unable to load local faster-whisper model: {exc}",
                ) from exc

    async def _fallback_to_cpu_model(self) -> tuple[WhisperModelProtocol, InferenceDevice]:
        async with self._load_lock:
            try:
                self._model = await asyncio.wait_for(
                    asyncio.to_thread(self._instantiate_sync, "cpu"),
                    timeout=self._settings.timeout_seconds,
                )
                self._active_device = "cpu"
                return self._model, "cpu"
            except VoicePipelineError:
                raise
            except TimeoutError as exc:
                raise VoicePipelineError(
                    code="VOICE_TRANSCRIPTION_TIMEOUT",
                    message="Timed out during CUDA->CPU fallback initialization.",
                ) from exc
            except Exception as exc:
                raise VoicePipelineError(
                    code="VOICE_MODEL_UNAVAILABLE",
                    message=f"CUDA->CPU fallback failed for faster-whisper: {exc}",
                ) from exc

    async def transcribe(
        self,
        pcm_s16le: bytes,
        sample_rate_hz: int = 16000,
    ) -> TranscriptionResult:
        """Transcribe an in-memory PCM buffer without writing temporary audio files."""
        if sample_rate_hz <= 0 or len(pcm_s16le) < 2 or len(pcm_s16le) % 2 != 0:
            raise VoicePipelineError(
                code="VOICE_INVALID_PCM_FRAME",
                message="Invalid PCM buffer or sample rate passed to transcribe().",
            )

        t0 = self._clock()
        model, device = await self._ensure_model()
        use_numpy = self._factory is _default_whisper_factory

        def _transcribe_with(
            active_model: WhisperModelProtocol,
        ) -> tuple[str, str, float]:
            float_samples = pcm_s16le_to_float_list(pcm_s16le)
            resampled = _resample_linear(float_samples, sample_rate_hz, 16000)
            audio_arg = _prepare_audio_array(resampled) if use_numpy else resampled
            segments_iter, info = active_model.transcribe(
                audio_arg,
                language=self._settings.language,
                beam_size=self._settings.beam_size,
            )
            texts: list[str] = []
            logprobs: list[float] = []
            for seg in segments_iter:
                seg_text = str(getattr(seg, "text", "")).strip()
                if seg_text:
                    texts.append(seg_text)
                lp = getattr(seg, "avg_logprob", None)
                if isinstance(lp, (int, float)) and not math.isnan(lp):
                    logprobs.append(float(lp))

            joined = " ".join(texts).strip()
            lang = str(getattr(info, "language", self._settings.language) or "es")
            if logprobs:
                mean_lp = sum(logprobs) / len(logprobs)
                confidence = min(1.0, max(0.0, math.exp(mean_lp)))
            else:
                confidence = 1.0 if joined else 0.0
            return joined, lang, confidence

        try:
            text, lang, conf = await asyncio.wait_for(
                asyncio.to_thread(_transcribe_with, model),
                timeout=self._settings.timeout_seconds,
            )
        except TimeoutError as exc:
            raise VoicePipelineError(
                code="VOICE_TRANSCRIPTION_TIMEOUT",
                message=f"Speech transcription exceeded {self._settings.timeout_seconds}s.",
            ) from exc
        except (RuntimeError, OSError, ValueError) as exc:
            if device == "cuda":
                cpu_model, device = await self._fallback_to_cpu_model()
                try:
                    text, lang, conf = await asyncio.wait_for(
                        asyncio.to_thread(_transcribe_with, cpu_model),
                        timeout=self._settings.timeout_seconds,
                    )
                except TimeoutError as timeout_exc:
                    raise VoicePipelineError(
                        code="VOICE_TRANSCRIPTION_TIMEOUT",
                        message="CPU fallback transcription timed out.",
                    ) from timeout_exc
                except Exception as cpu_exc:
                    raise VoicePipelineError(
                        code="VOICE_STT_FAILED",
                        message=f"Faster-Whisper transcription failed on CPU fallback: {cpu_exc}",
                    ) from cpu_exc
            else:
                raise VoicePipelineError(
                    code="VOICE_STT_FAILED",
                    message=f"Faster-Whisper transcription failed: {exc}",
                ) from exc
        except VoicePipelineError:
            raise
        except Exception as exc:
            raise VoicePipelineError(
                code="VOICE_STT_FAILED",
                message=f"Faster-Whisper transcription failed: {exc}",
            ) from exc

        elapsed_ms = max(0.0, (self._clock() - t0) * 1000.0)
        return TranscriptionResult(
            text=text,
            language=lang,
            confidence=conf,
            backend_device=device,
            duration_ms=elapsed_ms,
        )

    async def close(self) -> None:
        """Release loaded Whisper model weights."""
        async with self._load_lock:
            self._model = None
