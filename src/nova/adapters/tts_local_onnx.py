"""Local Spanish ONNX TTS adapter supporting Kokoro and Piper in RAM (NOVA-003 OP-3)."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import shutil
import struct
import threading
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nova.domain.errors import VoicePipelineError
from nova.domain.models import MAX_SYNTHESIZED_PCM_BYTES, SynthesizedAudio


class KokoroBackendProtocol(Protocol):
    """Minimal synchronous Kokoro ONNX contract executed inside asyncio.to_thread."""

    def create(
        self,
        text: str,
        *,
        voice: str,
        speed: float,
        lang: str,
    ) -> tuple[Sequence[float] | Any, int]: ...


class PiperProcessProtocol(Protocol):
    """Minimal protocol for a short-lived in-memory Piper subprocess."""

    @property
    def returncode(self) -> int | None: ...

    async def communicate(self, input_bytes: bytes | None = None) -> tuple[bytes, bytes]: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


class _AsyncProcWrapper:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def communicate(self, input_bytes: bytes | None = None) -> tuple[bytes, bytes]:
        return await self._proc.communicate(input=input_bytes)

    def kill(self) -> None:
        self._proc.kill()

    async def wait(self) -> int:
        return await self._proc.wait()


PiperSpawner = Callable[..., Awaitable[PiperProcessProtocol]]


async def _default_piper_spawner(*argv: str) -> PiperProcessProtocol:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return _AsyncProcWrapper(proc)


def _floats_to_pcm_s16le(samples: Sequence[float] | Any) -> bytes:
    """Pack floating-point waveform samples in [-1.0, 1.0] into mono s16le bytes."""
    tolist = getattr(samples, "tolist", None)
    seq: Sequence[float] = tolist() if callable(tolist) else samples
    max_samples = MAX_SYNTHESIZED_PCM_BYTES // 2
    clamped_ints = [
        max(-32768, min(32767, int(round(float(val) * 32767.0))))
        for val in list(seq)[:max_samples]
    ]
    if not clamped_ints:
        raise VoicePipelineError(
            code="VOICE_TTS_FAILED",
            message="TTS backend returned zero audio samples.",
        )
    return struct.pack(f"<{len(clamped_ints)}h", *clamped_ints)


def _validate_safe_cli_token(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("-") or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("CLI path or token must not start with '-' or contain control chars")
    return cleaned


class LocalOnnxTtsSettings(BaseModel):
    """Configuration boundary for LocalOnnxTtsAdapter (OP-3)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    engine: Literal["kokoro", "piper"] = "kokoro"
    kokoro_model_path: str | None = None
    kokoro_voices_path: str | None = None
    kokoro_voice: str = Field(default="ef_dora", min_length=1)
    kokoro_lang: str = Field(default="es", min_length=1)
    piper_binary: str = Field(default="piper", min_length=1)
    piper_model_path: str | None = None
    piper_sample_rate_hz: int = Field(default=22050, ge=8000, le=48000)
    speed: float = Field(default=1.0, gt=0.2, le=3.0)
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)

    @field_validator(
        "kokoro_model_path",
        "kokoro_voices_path",
        "piper_model_path",
        "piper_binary",
        "kokoro_voice",
        "kokoro_lang",
    )
    @classmethod
    def _validate_tokens(cls, value: str | None) -> str | None:
        res = _validate_safe_cli_token(value)
        return res


class LocalOnnxTtsAdapter:
    """Synthesizes concise Spanish spoken milestones locally via Kokoro ONNX or Piper."""

    def __init__(
        self,
        settings: LocalOnnxTtsSettings | None = None,
        *,
        kokoro_factory: Callable[[], KokoroBackendProtocol] | None = None,
        which_resolver: Callable[[str], str | None] = shutil.which,
        piper_spawner: PiperSpawner = _default_piper_spawner,
    ) -> None:
        self._settings = settings or LocalOnnxTtsSettings()
        self._kokoro_factory = kokoro_factory
        self._which = which_resolver
        self._piper_spawner = piper_spawner
        self._load_lock = asyncio.Lock()
        self._kokoro_infer_lock = threading.Lock()
        self._kokoro: KokoroBackendProtocol | None = None

    def update_settings(
        self,
        *,
        engine: Literal["kokoro", "piper"] | None = None,
        speed: float | None = None,
        kokoro_voice: str | None = None,
        kokoro_lang: str | None = None,
    ) -> None:
        """Hot-update TTS engine, voice, language, and speed for future synthesis."""
        updates: dict[str, Any] = {}
        if engine is not None:
            updates["engine"] = engine
        if speed is not None:
            updates["speed"] = speed
        if kokoro_voice is not None:
            updates["kokoro_voice"] = kokoro_voice
        if kokoro_lang is not None:
            updates["kokoro_lang"] = kokoro_lang
        if updates:
            self._settings = self._settings.model_copy(update=updates)

    def _build_default_kokoro(self) -> KokoroBackendProtocol:
        model_p = self._settings.kokoro_model_path
        voices_p = self._settings.kokoro_voices_path
        if (
            model_p is None
            or voices_p is None
            or not Path(model_p).is_file()
            or not Path(voices_p).is_file()
        ):
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message="Kokoro ONNX model_path and voices_path must exist locally.",
            )
        try:
            kokoro_mod = importlib.import_module("kokoro_onnx")
        except ImportError as exc:
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message=f"Optional package 'kokoro-onnx' is not installed: {exc}",
            ) from exc
        try:
            instance: KokoroBackendProtocol = kokoro_mod.Kokoro(
                str(Path(model_p).resolve()),
                str(Path(voices_p).resolve()),
            )
            return instance
        except Exception as exc:
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message=f"Failed to initialize Kokoro ONNX model: {exc}",
            ) from exc

    async def _ensure_kokoro(self) -> KokoroBackendProtocol:
        if self._kokoro is not None:
            return self._kokoro
        async with self._load_lock:
            if self._kokoro is None:
                factory = self._kokoro_factory or self._build_default_kokoro
                try:
                    self._kokoro = await asyncio.wait_for(
                        asyncio.to_thread(factory),
                        timeout=self._settings.timeout_seconds,
                    )
                except TimeoutError as exc:
                    raise VoicePipelineError(
                        code="VOICE_TTS_TIMEOUT",
                        message="Timed out while loading local Kokoro ONNX model.",
                    ) from exc
            return self._kokoro

    async def synthesize(self, text: str) -> SynthesizedAudio:
        """Synthesize non-empty spoken text into an in-memory s16le PCM buffer."""
        cleaned = " ".join(text.strip().split())
        if not cleaned:
            raise VoicePipelineError(
                code="VOICE_EMPTY_TTS_TEXT",
                message="Cannot synthesize empty text.",
            )

        if self._settings.engine == "kokoro":
            return await self._synthesize_kokoro(cleaned)
        return await self._synthesize_piper(cleaned)

    async def _synthesize_kokoro(self, cleaned_text: str) -> SynthesizedAudio:
        backend = await self._ensure_kokoro()

        def _run_sync() -> SynthesizedAudio:
            with self._kokoro_infer_lock:
                samples, sample_rate = backend.create(
                    cleaned_text,
                    voice=self._settings.kokoro_voice,
                    speed=self._settings.speed,
                    lang=self._settings.kokoro_lang,
                )
            pcm = _floats_to_pcm_s16le(samples)
            return SynthesizedAudio(
                pcm_s16le=pcm,
                sample_rate_hz=int(sample_rate),
                channels=1,
            )

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_run_sync),
                timeout=self._settings.timeout_seconds,
            )
        except VoicePipelineError:
            raise
        except TimeoutError as exc:
            raise VoicePipelineError(
                code="VOICE_TTS_TIMEOUT",
                message=f"Kokoro TTS exceeded {self._settings.timeout_seconds}s timeout.",
            ) from exc
        except Exception as exc:
            raise VoicePipelineError(
                code="VOICE_TTS_FAILED",
                message=f"Kokoro TTS synthesis failed: {exc}",
            ) from exc

    def _resolve_piper_argv_sync(self) -> tuple[str, ...]:
        piper_bin = self._which(self._settings.piper_binary)
        model_raw = self._settings.piper_model_path
        if not piper_bin or not model_raw:
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message="Piper binary or local ONNX model path is not available.",
            )
        resolved_model = Path(os.path.realpath(model_raw))
        if not resolved_model.is_file() or str(resolved_model).startswith("-"):
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message=f"Piper ONNX model file does not exist: {model_raw}",
            )
        return (piper_bin, "--model", str(resolved_model), "--output-raw")

    async def _synthesize_piper(self, cleaned_text: str) -> SynthesizedAudio:
        argv = await asyncio.to_thread(self._resolve_piper_argv_sync)
        try:
            proc = await asyncio.wait_for(self._piper_spawner(*argv), timeout=3.0)
        except (TimeoutError, OSError) as exc:
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message=f"Unable to start Piper binary: {exc}",
            ) from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate((cleaned_text + "\n").encode("utf-8")),
                timeout=self._settings.timeout_seconds,
            )
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(TimeoutError, OSError):
                await asyncio.wait_for(proc.wait(), timeout=0.05)
            raise VoicePipelineError(
                code="VOICE_TTS_TIMEOUT",
                message=f"Piper TTS exceeded {self._settings.timeout_seconds}s timeout.",
            ) from exc
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(TimeoutError, OSError):
                await asyncio.wait_for(proc.wait(), timeout=0.05)
            raise

        if (proc.returncode or 0) != 0:
            err_msg = stderr_bytes[:1024].decode("utf-8", errors="replace").strip()
            raise VoicePipelineError(
                code="VOICE_TTS_FAILED",
                message=f"Piper TTS failed (code={proc.returncode}): {err_msg}",
            )

        capped_pcm = stdout_bytes[:MAX_SYNTHESIZED_PCM_BYTES]
        if len(capped_pcm) % 2 != 0:
            capped_pcm = capped_pcm[:-1]
        if len(capped_pcm) < 2:
            raise VoicePipelineError(
                code="VOICE_TTS_FAILED",
                message="Piper TTS produced empty PCM output.",
            )
        return SynthesizedAudio(
            pcm_s16le=capped_pcm,
            sample_rate_hz=self._settings.piper_sample_rate_hz,
            channels=1,
        )

    async def close(self) -> None:
        """Release loaded Kokoro model reference."""
        async with self._load_lock:
            self._kokoro = None
