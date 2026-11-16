"""Deterministic TDD tests for NOVA-013 T3: LiveDiagnosticsService (AC-7, AC-8, NFR-1, NFR-5)."""

from __future__ import annotations

import array
import math
import sys
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nova.adapters.diagnostics_probes import build_live_diagnostics_service
from nova.application.live_diagnostics import LiveDiagnosticsConfig, LiveDiagnosticsService
from nova.domain.errors import DesktopUiError
from nova.domain.models import AudioFrame, SynthesizedAudio, TranscriptionResult


class _FakeAudioCapture:
    def __init__(self, amplitude: int = 8000) -> None:
        self._amplitude = amplitude
        self.stopped = False

    async def frames(self) -> AsyncIterator[AudioFrame]:
        samples = array.array(
            "h",
            [
                int(self._amplitude * math.sin(2.0 * math.pi * 440.0 * (i / 16000.0)))
                for i in range(512)
            ],
        )
        pcm = samples.tobytes()
        for idx in range(20):
            yield AudioFrame(pcm_s16le=pcm, sample_rate_hz=16000, monotonic_ts=float(idx) * 0.032)

    async def stop_capture(self) -> None:
        self.stopped = True


class _FakeAudioOutput:
    def __init__(self) -> None:
        self.played_bytes = 0

    @property
    def is_playing(self) -> bool:
        return False

    async def play_pcm(self, audio: SynthesizedAudio) -> None:
        self.played_bytes += len(audio.pcm_s16le)

    async def stop_playback(self) -> None:
        return None


_REAL_PLUGIN_DIR = str(
    Path(__file__).resolve().parents[1] / "src" / "nova" / "plugins" / "agy_gatekeeper"
)


def _list_wavs(directory: Path) -> list[Path]:
    return list(directory.glob("*.wav"))


def _write_script(target: Path, source: str) -> None:
    target.write_text(textwrap.dedent(source), encoding="utf-8")


class _FakeStt:
    async def transcribe(
        self,
        pcm_s16le: bytes,
        sample_rate_hz: int = 16000,
    ) -> TranscriptionResult:
        assert len(pcm_s16le) > 0
        assert sample_rate_hz == 16000
        return TranscriptionResult(
            text="Hola Nova token=sk-secret999888",
            language="es",
            confidence=0.95,
            backend_device="cpu",
            duration_ms=25.0,
        )


async def test_ac7_nfr5_microphone_and_audio_output_run_strictly_in_ram_and_redact_preview(
    tmp_path: Path,
) -> None:
    capture = _FakeAudioCapture(amplitude=10000)
    output = _FakeAudioOutput()

    svc = build_live_diagnostics_service(
        config=LiveDiagnosticsConfig(
            pi_executable="pi",
            agy_executable="agy",
        ),
        which_resolver=lambda name: f"/usr/bin/{name}",
        audio_input_factory=lambda _target: capture,
        audio_output_factory=lambda _target: output,
        stt_port=_FakeStt(),
    )

    mic_res = await svc.test_microphone(duration_seconds=0.5)
    assert mic_res.ok is True
    assert mic_res.status == "ok"
    assert mic_res.peak_level > 0.25
    assert mic_res.rms_level > 0.10
    assert mic_res.speech_detected is True
    assert "sk-secret999888" not in mic_res.transcript_preview
    assert "<REDACTED:secret>" in mic_res.transcript_preview
    assert capture.stopped is True
    assert _list_wavs(tmp_path) == []

    # Silent microphone returns ok=False, status="silent"
    silent_svc = build_live_diagnostics_service(
        config=LiveDiagnosticsConfig(),
        which_resolver=lambda name: f"/usr/bin/{name}",
        audio_input_factory=lambda _target: _FakeAudioCapture(amplitude=0),
        audio_output_factory=lambda _target: output,
    )
    silent_res = await silent_svc.test_microphone(duration_seconds=0.5)
    assert silent_res.ok is False
    assert silent_res.status == "silent"

    # Invalid duration raises DesktopUiError DIAGNOSTIC_INVALID_PARAMS
    with pytest.raises(DesktopUiError) as exc_info:
        await svc.test_microphone(duration_seconds=10.0)
    assert exc_info.value.code == "DIAGNOSTIC_INVALID_PARAMS"

    # Audio output plays tone fallback in RAM when TTS port is absent
    out_res = await svc.test_audio_output()
    assert out_res.ok is True
    assert out_res.status == "ok"
    assert out_res.engine == "tone_fallback"
    assert output.played_bytes > 1000


async def test_ac8_pi_and_antigravity_connectivity_probes_classify_and_redact_secrets(
    tmp_path: Path,
) -> None:
    # 1. Missing binaries classify as binary_missing
    missing_svc = build_live_diagnostics_service(
        config=LiveDiagnosticsConfig(
            pi_executable="nonexistent-pi",
            agy_executable="nonexistent-agy",
        ),
        which_resolver=lambda _name: None,
    )
    pi_miss = await missing_svc.test_pi_connection()
    assert pi_miss.ok is False
    assert pi_miss.status == "binary_missing"

    agy_miss = await missing_svc.test_antigravity_connection()
    assert agy_miss.ok is False
    assert agy_miss.status == "binary_missing"

    # 2. Live fake Pi (auth error leaking secret -> redacted) and connected Pi
    fake_pi_auth = tmp_path / "fake_pi_auth.py"
    _write_script(
        fake_pi_auth,
        """
        import json, sys
        raw = sys.stdin.buffer.readline()
        req = json.loads(raw.decode("utf-8"))
        err = {
            "id": req.get("id"),
            "type": "response",
            "command": "prompt",
            "success": False,
            "error": "401 Unauthorized: invalid key nvapi-supersecret999999",
        }
        sys.stdout.buffer.write((json.dumps(err) + "\\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        """,
    )
    auth_svc = build_live_diagnostics_service(
        config=LiveDiagnosticsConfig(
            pi_executable=sys.executable,
            pi_extra_args=("-u", str(fake_pi_auth)),
        ),
        which_resolver=lambda name: name,
    )
    pi_auth = await auth_svc.test_pi_connection()
    assert pi_auth.ok is False
    assert pi_auth.status == "auth_error"
    assert "nvapi-supersecret999999" not in pi_auth.message
    assert "<REDACTED:secret>" in pi_auth.message

    fake_pi_ok = tmp_path / "fake_pi_ok.py"
    _write_script(
        fake_pi_ok,
        """
        import json, sys
        assert "--no-tools" in sys.argv
        assert "--no-session" in sys.argv
        raw = sys.stdin.buffer.readline()
        req = json.loads(raw.decode("utf-8"))
        ack = {
            "id": req.get("id"),
            "type": "response",
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
        ev = {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "OK"},
        }
        sys.stdout.buffer.write((json.dumps(ack) + "\\n").encode("utf-8"))
        sys.stdout.buffer.write((json.dumps(ev) + "\\n").encode("utf-8"))
        sys.stdout.buffer.write((json.dumps({"type": "agent_settled"}) + "\\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        """,
    )

    fake_agy_ok = tmp_path / "fake_agy_ok.py"
    _write_script(
        fake_agy_ok,
        f"""
        import json, sys
        if sys.argv[1:4] == ["plugin", "list", "--json"]:
            print(json.dumps([{{
                "name": "nova-agy-gatekeeper",
                "enabled": True,
                "path": {_REAL_PLUGIN_DIR!r},
            }}]))
            raise SystemExit(0)
        print(json.dumps({{"event": "init", "conversation_id": "c-diag"}}), flush=True)
        print(json.dumps({{
            "event": "result",
            "result": {{"conversation_id": "c-diag", "status": "SUCCESS", "response": "OK"}},
        }}), flush=True)
        """,
    )

    ok_svc = build_live_diagnostics_service(
        config=LiveDiagnosticsConfig(
            pi_executable=sys.executable,
            pi_extra_args=("-u", str(fake_pi_ok)),
            agy_executable=sys.executable,
            agy_extra_args=(str(fake_agy_ok),),
        ),
        which_resolver=lambda name: name,
    )
    pi_ok = await ok_svc.test_pi_connection()
    assert pi_ok.ok is True
    assert pi_ok.status == "connected"
    assert pi_ok.latency_ms >= 0.0

    agy_ok = await ok_svc.test_antigravity_connection()
    assert agy_ok.ok is True
    assert agy_ok.status == "connected"
    assert agy_ok.plugin_verified is True


async def test_nova013_agy_generic_errors_and_hot_config_update() -> None:
    async def _failing_agy_runner(
        _exe: str,
        _extra: tuple[str, ...],
        _model: str,
        _probe_dir: str,
        _ipc_env: dict[str, str] | None,
    ) -> None:
        raise OSError("agy pipe broken with sk-abcdefghijklmnopqrstuvwxyz1234")

    svc = LiveDiagnosticsService(
        config=LiveDiagnosticsConfig(
            pi_provider="github-copilot",
            pi_model="gpt-4.1",
            tier2_model="claude-opus-5-5-high",
            tts_engine_label="kokoro",
        ),
        which_resolver=lambda name: f"/usr/bin/{name}",
        agy_probe_runner=_failing_agy_runner,
    )
    agy_err = await svc.test_antigravity_connection()
    assert agy_err.ok is False
    assert agy_err.status == "error"
    assert "sk-abcdefghijklmnopqrstuvwxyz1234" not in agy_err.message

    svc.update_config(
        pi_provider="openai",
        pi_model="gpt-4.1-mini",
        tier2_model="gemini-3.1-pro",
        tts_engine_label="piper",
    )
    pi_missing = await svc.test_pi_connection()
    assert pi_missing.provider == "openai"
    assert pi_missing.model == "gpt-4.1-mini"

