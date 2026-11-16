"""Interactive live readiness probes for Microphone, Audio Output, Pi RPC, and Antigravity CLI."""

from __future__ import annotations

import array
import asyncio
import contextlib
import math
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from nova.adapters.antigravity_adapter import (
    ALLOWED_TIER2_MODELS,
    AntigravityCLIAdapter,
    AntigravityRunnerConfig,
)
from nova.adapters.audio_pipewire import PipeWireAudioAdapter, PipeWireAudioSettings
from nova.adapters.pi_rpc_client import PiRpcClient, PiRpcConfig
from nova.domain.errors import (
    CodingRunnerError,
    DesktopUiError,
    NovaDomainError,
    PiRpcError,
    VoicePipelineError,
    redact_sensitive_text,
)
from nova.domain.models import (
    DEFAULT_PI_MODEL,
    AntigravityDiagnosticResult,
    AntigravityDiagnosticStatus,
    AudioOutputDiagnosticResult,
    CodingTaskRequest,
    MicDiagnosticResult,
    PiConnectionDiagnosticResult,
    PiDiagnosticStatus,
    SynthesizedAudio,
)
from nova.domain.ports import (
    AudioInputPort,
    AudioOutputPort,
    SpeechToTextPort,
    TextToSpeechPort,
)

WhichResolver = Callable[[str], str | None]
AudioInputFactory = Callable[[str | None], AudioInputPort]
AudioOutputFactory = Callable[[str | None], AudioOutputPort]

_AUTH_ERROR_HINTS: tuple[str, ...] = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "api key",
    "api_key",
    "apikey",
    "invalid key",
    "authentication",
    "not logged in",
    "login required",
)


def _default_audio_input_factory(capture_target: str | None) -> AudioInputPort:
    return PipeWireAudioAdapter(PipeWireAudioSettings(capture_target=capture_target))


def _default_audio_output_factory(playback_target: str | None) -> AudioOutputPort:
    return PipeWireAudioAdapter(PipeWireAudioSettings(playback_target=playback_target))


def _validate_optional_token(raw: str | None, *, field_name: str) -> str | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.startswith("-") or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise DesktopUiError(
            code="DIAGNOSTIC_INVALID_PARAMS",
            message=f"Invalid parameter '{field_name}'.",
        )
    return cleaned


def _build_chime_pcm_s16le(sample_rate_hz: int = 24000, duration_ms: int = 360) -> bytes:
    """Synthesize a clean two-note (440 Hz -> 660 Hz) s16le test chime strictly in RAM."""
    total_samples = max(2, int(sample_rate_hz * (duration_ms / 1000.0)))
    half = total_samples // 2
    samples = array.array("h")
    for idx in range(total_samples):
        freq = 440.0 if idx < half else 660.0
        env = min(1.0, idx / 400.0, (total_samples - idx) / 400.0)
        val = int(9000.0 * env * math.sin(2.0 * math.pi * freq * (idx / sample_rate_hz)))
        samples.append(val)
    return samples.tobytes()


def _compute_s16le_levels(pcm_bytes: bytes) -> tuple[float, float]:
    """Compute normalized [0.0, 1.0] peak and RMS levels from 16-bit signed PCM bytes."""
    usable_len = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable_len < 2:
        return 0.0, 0.0
    samples = array.array("h")
    samples.frombytes(pcm_bytes[:usable_len])
    if not samples:
        return 0.0, 0.0
    peak_abs = 0
    sum_sq = 0.0
    for s in samples:
        abs_s = abs(s)
        if abs_s > peak_abs:
            peak_abs = abs_s
        sum_sq += float(s) * float(s)
    peak_norm = min(1.0, peak_abs / 32768.0)
    rms_norm = min(1.0, math.sqrt(sum_sq / len(samples)) / 32768.0)
    return round(peak_norm, 4), round(rms_norm, 4)


class LiveDiagnosticsConfig(BaseModel):
    """Configuration boundary for interactive readiness probes (NOVA-013)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    pi_executable: str = Field(default="pi", min_length=1)
    pi_extra_args: tuple[str, ...] = ()
    pi_provider: str = Field(default="nvidia", min_length=1)
    pi_model: str = Field(default=DEFAULT_PI_MODEL, min_length=1)
    agy_executable: str = Field(default="agy", min_length=1)
    agy_extra_args: tuple[str, ...] = ()
    tier2_model: str = Field(default="claude-opus-5-5-high", min_length=1)
    tts_engine_label: str = "piper"
    gatekeeper_ipc_env: dict[str, str] | None = None


class LiveDiagnosticsService:
    """Executes bounded, RAM-confined live readiness checks for audio and AI engines."""

    def __init__(
        self,
        *,
        config: LiveDiagnosticsConfig | None = None,
        which_resolver: WhichResolver = shutil.which,
        audio_input_factory: AudioInputFactory = _default_audio_input_factory,
        audio_output_factory: AudioOutputFactory = _default_audio_output_factory,
        stt_port: SpeechToTextPort | None = None,
        tts_port: TextToSpeechPort | None = None,
        extra_secrets_provider: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        self._config = config or LiveDiagnosticsConfig()
        self._which = which_resolver
        self._audio_input_factory = audio_input_factory
        self._audio_output_factory = audio_output_factory
        self._stt_port = stt_port
        self._tts_port = tts_port
        self._extra_secrets_provider = extra_secrets_provider
        self._audio_lock = asyncio.Lock()

    def _redact(self, text: str) -> str:
        secrets = (
            self._extra_secrets_provider() if self._extra_secrets_provider is not None else ()
        )
        return redact_sensitive_text(text, extra_secrets=secrets)

    def _resolve_binary(
        self,
        executable: str,
        *,
        default_name: str,
        fallbacks: tuple[Path, ...] = (),
    ) -> str | None:
        candidate_file = Path(executable).expanduser()
        if candidate_file.is_file() and os.access(candidate_file, os.X_OK):
            return str(candidate_file.resolve())
        found = self._which(executable)
        if found:
            return found
        if executable == default_name:
            for fb in fallbacks:
                expanded = fb.expanduser()
                if expanded.is_file() and os.access(expanded, os.X_OK):
                    return str(expanded.resolve())
        return None

    async def test_microphone(
        self,
        capture_target: str | None = None,
        duration_seconds: float = 2.0,
    ) -> MicDiagnosticResult:
        """Capture bounded RAM-only PCM audio and measure voice energy + optional STT preview."""
        safe_target = _validate_optional_token(capture_target, field_name="capture_target")
        if not (0.5 <= duration_seconds <= 4.0):
            raise DesktopUiError(
                code="DIAGNOSTIC_INVALID_PARAMS",
                message="duration_seconds must be between 0.5 and 4.0.",
            )

        if not (self._which("pw-record") or self._which("parecord")):
            return MicDiagnosticResult(
                ok=False,
                status="binary_missing",
                capture_target=safe_target,
                duration_ms=0.0,
                peak_level=0.0,
                rms_level=0.0,
                speech_detected=False,
                transcript_preview="",
                message=(
                    "No se encontró 'pw-record' ni 'parecord'. "
                    "Instale pipewire-bin o pulseaudio-utils."
                ),
            )

        target_bytes = int(duration_seconds * 16000 * 2)
        pcm_buffer = bytearray()
        started = time.monotonic()

        async with self._audio_lock:
            audio_in = self._audio_input_factory(safe_target)
            try:
                async def _collect() -> None:
                    async for frame in audio_in.frames():
                        pcm_buffer.extend(frame.pcm_s16le)
                        if len(pcm_buffer) >= target_bytes:
                            break

                await asyncio.wait_for(_collect(), timeout=duration_seconds + 1.5)
            except TimeoutError:
                pass
            except (VoicePipelineError, OSError) as exc:
                elapsed_err = round((time.monotonic() - started) * 1000.0, 1)
                return MicDiagnosticResult(
                    ok=False,
                    status="device_error",
                    capture_target=safe_target,
                    duration_ms=elapsed_err,
                    peak_level=0.0,
                    rms_level=0.0,
                    speech_detected=False,
                    transcript_preview="",
                    message=self._redact(f"Error al capturar del micrófono: {exc}"),
                )
            finally:
                with contextlib.suppress(Exception):
                    await audio_in.stop_capture()

        elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        peak, rms = _compute_s16le_levels(bytes(pcm_buffer))
        if peak < 0.005:
            return MicDiagnosticResult(
                ok=False,
                status="silent",
                capture_target=safe_target,
                duration_ms=elapsed_ms,
                peak_level=peak,
                rms_level=rms,
                speech_detected=False,
                transcript_preview="",
                message=(
                    "No se detectó señal de entrada. Verifique que el micrófono no esté silenciado."
                ),
            )

        speech_detected = peak >= 0.02
        preview = ""
        if speech_detected and self._stt_port is not None and len(pcm_buffer) >= 2:
            with contextlib.suppress(Exception):
                tx = await asyncio.wait_for(
                    self._stt_port.transcribe(bytes(pcm_buffer), sample_rate_hz=16000),
                    timeout=3.0,
                )
                preview = self._redact(tx.text.strip())[:160]

        return MicDiagnosticResult(
            ok=True,
            status="ok",
            capture_target=safe_target,
            duration_ms=elapsed_ms,
            peak_level=peak,
            rms_level=rms,
            speech_detected=speech_detected,
            transcript_preview=preview,
            message="Micrófono operativo: señal de audio capturada correctamente.",
        )

    async def test_audio_output(
        self,
        playback_target: str | None = None,
    ) -> AudioOutputDiagnosticResult:
        """Play a short synthesized greeting or clean two-tone test chime in RAM."""
        safe_target = _validate_optional_token(playback_target, field_name="playback_target")
        if not (self._which("pw-play") or self._which("paplay")):
            return AudioOutputDiagnosticResult(
                ok=False,
                status="binary_missing",
                engine="none",
                latency_ms=0.0,
                message=(
                    "No se encontró 'pw-play' ni 'paplay'. "
                    "Instale pipewire-bin o pulseaudio-utils."
                ),
            )

        started = time.monotonic()
        engine_used = self._config.tts_engine_label
        synthesized: SynthesizedAudio | None = None
        if self._tts_port is not None:
            with contextlib.suppress(Exception):
                synthesized = await asyncio.wait_for(
                    self._tts_port.synthesize("Hola, soy Nova. Prueba de audio exitosa."),
                    timeout=3.0,
                )
        if synthesized is None:
            engine_used = "tone_fallback"
            synthesized = SynthesizedAudio(
                pcm_s16le=_build_chime_pcm_s16le(sample_rate_hz=24000),
                sample_rate_hz=24000,
            )

        async with self._audio_lock:
            audio_out = self._audio_output_factory(safe_target)
            try:
                await asyncio.wait_for(audio_out.play_pcm(synthesized), timeout=5.0)
            except (TimeoutError, VoicePipelineError, OSError) as exc:
                elapsed_err = round((time.monotonic() - started) * 1000.0, 1)
                return AudioOutputDiagnosticResult(
                    ok=False,
                    status="device_error",
                    engine=engine_used,
                    latency_ms=elapsed_err,
                    message=self._redact(f"Error al reproducir audio de prueba: {exc}"),
                )
            finally:
                with contextlib.suppress(Exception):
                    await audio_out.stop_playback()

        elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        return AudioOutputDiagnosticResult(
            ok=True,
            status="ok",
            engine=engine_used,
            latency_ms=elapsed_ms,
            message="Audio de prueba reproducido correctamente.",
        )

    async def test_pi_connection(
        self,
        provider: str | None = None,
        model: str | None = None,
    ) -> PiConnectionDiagnosticResult:
        """Run an isolated zero-tool RPC handshake + ping against Pi."""
        eff_provider = (
            _validate_optional_token(provider, field_name="provider") or self._config.pi_provider
        )
        eff_model = _validate_optional_token(model, field_name="model") or self._config.pi_model

        resolved_bin = self._resolve_binary(
            self._config.pi_executable,
            default_name="pi",
            fallbacks=(
                Path.home() / ".local" / "bin" / "pi",
                Path.home() / ".pi" / "agent" / "bin" / "pi",
            ),
        )
        if resolved_bin is None:
            return PiConnectionDiagnosticResult(
                ok=False,
                status="binary_missing",
                provider=eff_provider,
                model=eff_model,
                latency_ms=0.0,
                message=f"Ejecutable '{self._config.pi_executable}' no encontrado en el sistema.",
            )

        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="nova-pi-probe-") as probe_dir:
            client = PiRpcClient(
                PiRpcConfig(
                    executable=resolved_bin,
                    extra_leading_args=self._config.pi_extra_args,
                    provider=eff_provider,
                    model=eff_model,
                    workspace_path=probe_dir,
                    workspace_active=False,
                    no_session=True,
                    enable_gatekeeper_extension=False,
                    ack_timeout_seconds=5.0,
                    run_timeout_seconds=8.0,
                )
            )
            try:
                async with client:
                    chunks = [
                        delta
                        async for delta in client.stream_prompt("Responde unicamente OK")
                    ]
                elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
                _ = chunks
                return PiConnectionDiagnosticResult(
                    ok=True,
                    status="connected",
                    provider=eff_provider,
                    model=eff_model,
                    latency_ms=elapsed_ms,
                    message="Conexión verificada entre Nova y Pi.",
                )
            except (PiRpcError, NovaDomainError, TimeoutError, OSError) as exc:
                elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
                raw_err = str(exc)
                low_err = raw_err.lower()
                if "timed out" in low_err or isinstance(exc, TimeoutError):
                    status: PiDiagnosticStatus = "timeout"
                elif any(hint in low_err for hint in _AUTH_ERROR_HINTS):
                    status = "auth_error"
                else:
                    status = "error"
                return PiConnectionDiagnosticResult(
                    ok=False,
                    status=status,
                    provider=eff_provider,
                    model=eff_model,
                    latency_ms=elapsed_ms,
                    message=self._redact(f"Fallo al conectar con Pi: {raw_err}"),
                )

    async def test_antigravity_connection(
        self,
        model: str | None = None,
    ) -> AntigravityDiagnosticResult:
        """Verify Antigravity binary, gatekeeper plugin, and bounded stream-json handshake."""
        eff_model = _validate_optional_token(model, field_name="model") or self._config.tier2_model
        resolved_bin = self._resolve_binary(
            self._config.agy_executable,
            default_name="agy",
            fallbacks=(Path.home() / ".local" / "bin" / "agy",),
        )
        if resolved_bin is None:
            return AntigravityDiagnosticResult(
                ok=False,
                status="binary_missing",
                model=eff_model,
                plugin_verified=False,
                latency_ms=0.0,
                message=(
                    f"Ejecutable '{self._config.agy_executable}' no encontrado en el sistema."
                ),
            )

        allowed = set(ALLOWED_TIER2_MODELS) | {eff_model}
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="nova-agy-probe-") as probe_dir:
            adapter = AntigravityCLIAdapter(
                AntigravityRunnerConfig(
                    executable=resolved_bin,
                    extra_leading_args=self._config.agy_extra_args,
                    allowed_models=frozenset(allowed),
                    timeout_seconds=12.0,
                    gatekeeper_ipc_env=self._config.gatekeeper_ipc_env or {
                        "NOVA_GATEKEEPER_SOCKET": "/tmp/nova-probe.sock",
                        "NOVA_GATEKEEPER_TOKEN": "probe-token",
                    },
                    require_gatekeeper_plugin=True,
                )
            )
            try:
                events = [
                    ev
                    async for ev in adapter.execute(
                        CodingTaskRequest(
                            correlation_id="corr-diag-agy",
                            workspace_path=probe_dir,
                            prompt="Responde unicamente OK",
                            model=eff_model,
                        )
                    )
                ]
                elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
                _ = events
                return AntigravityDiagnosticResult(
                    ok=True,
                    status="connected",
                    model=eff_model,
                    plugin_verified=True,
                    latency_ms=elapsed_ms,
                    message="Conexión verificada entre Nova y Antigravity.",
                )
            except CodingRunnerError as exc:
                elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
                raw_err = exc.sanitized_message
                low_err = raw_err.lower()
                plugin_ok = exc.code != "GATEKEEPER_PLUGIN_MISSING"
                if exc.code == "GATEKEEPER_PLUGIN_MISSING":
                    status: AntigravityDiagnosticStatus = "plugin_missing"
                elif "timed out" in low_err:
                    status = "timeout"
                elif any(hint in low_err for hint in _AUTH_ERROR_HINTS):
                    status = "auth_error"
                else:
                    status = "error"
                return AntigravityDiagnosticResult(
                    ok=False,
                    status=status,
                    model=eff_model,
                    plugin_verified=plugin_ok,
                    latency_ms=elapsed_ms,
                    message=self._redact(f"Fallo al conectar con Antigravity: {raw_err}"),
                )
