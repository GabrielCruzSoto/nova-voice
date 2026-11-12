"""TDD tests for T3: Local WakeWord, Silero VAD, Faster-Whisper STT, and ONNX/Piper TTS adapters."""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from nova.adapters.stt_faster_whisper import FasterWhisperSettings, FasterWhisperSttAdapter
from nova.adapters.tts_local_onnx import LocalOnnxTtsAdapter, LocalOnnxTtsSettings
from nova.adapters.vad_silero import SileroVadAdapter, SileroVadSettings
from nova.adapters.wakeword_openwakeword import OpenWakeWordAdapter, OpenWakeWordSettings
from nova.domain.errors import VoicePipelineError
from nova.domain.models import AudioFrame


def _list_dir_names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def _write_placeholder_model(directory: Path, name: str) -> tuple[Path, str]:
    target = directory / name
    target.write_bytes(b"onnx-placeholder")
    return target, str(target.resolve())


def _make_frame(ts: float = 1.0, sample_val: int = 1000, rate: int = 16000) -> AudioFrame:
    pcm = struct.pack("<512h", *([sample_val] * 512))
    return AudioFrame(pcm_s16le=pcm, sample_rate_hz=rate, channels=1, monotonic_ts=ts)


async def test_openwakeword_adapter_enforces_threshold_and_monotonic_cooldown() -> None:
    scores_seq = [
        {"oye_nova": 0.42, "hey_nova": 0.15},
        {"oye_nova": 0.84, "hey_nova": 0.20},
        {"oye_nova": 0.91, "hey_nova": 0.10},  # suppressed during 1.5s cooldown
        {"oye_nova": 0.77, "hey_nova": 0.10},  # allowed after cooldown advances
    ]
    reset_calls = 0

    class _FakeWakeBackend:
        def predict_scores(self, pcm_s16le: bytes) -> dict[str, float]:
            assert len(pcm_s16le) == 1024
            return scores_seq.pop(0)

        def reset(self) -> None:
            nonlocal reset_calls
            reset_calls += 1

    now = 10.0

    def fake_clock() -> float:
        return now

    adapter = OpenWakeWordAdapter(
        settings=OpenWakeWordSettings(threshold=0.60, cooldown_seconds=1.5),
        predictor_factory=_FakeWakeBackend,
        clock=fake_clock,
    )

    d1 = await adapter.detect(_make_frame(ts=10.0))
    assert d1.detected is False
    assert d1.score == pytest.approx(0.42)

    d2 = await adapter.detect(_make_frame(ts=10.1))
    assert d2.detected is True
    assert d2.wake_word == "oye_nova"
    assert d2.score == pytest.approx(0.84)
    assert reset_calls == 1

    now = 10.8  # Still within 1.5s cooldown
    d3 = await adapter.detect(_make_frame(ts=10.8))
    assert d3.detected is False
    assert d3.score == pytest.approx(0.91)

    now = 12.0  # Past 1.5s cooldown
    d4 = await adapter.detect(_make_frame(ts=12.0))
    assert d4.detected is True
    assert d4.wake_word == "oye_nova"

    adapter.reset()
    await adapter.close()


async def test_silero_vad_adapter_normalizes_pcm_and_handles_missing_runtime() -> None:
    seen_lengths: list[int] = []

    class _FakeVadBackend:
        def score_samples(self, samples: list[float], sample_rate_hz: int) -> float:
            seen_lengths.append(len(samples))
            assert sample_rate_hz == 16000
            assert all(-1.0 <= x <= 1.0 for x in samples)
            return 0.87

        def reset(self) -> None:
            seen_lengths.clear()

    vad = SileroVadAdapter(
        settings=SileroVadSettings(),
        scorer_factory=_FakeVadBackend,
    )
    prob = await vad.speech_probability(_make_frame(sample_val=16384))
    assert prob == pytest.approx(0.87)
    assert seen_lengths == [512]
    vad.reset()
    assert seen_lengths == []

    # Invalid sample rate rejected with VOICE_INVALID_PCM_FRAME
    with pytest.raises(VoicePipelineError) as bad_rate:
        await vad.speech_probability(_make_frame(rate=24000))
    assert bad_rate.value.code == "VOICE_INVALID_PCM_FRAME"

    # Missing model file fails closed with VOICE_MODEL_UNAVAILABLE
    missing_vad = SileroVadAdapter(
        settings=SileroVadSettings(model_path="/nonexistent/silero_vad.onnx")
    )
    with pytest.raises(VoicePipelineError) as missing_exc:
        await missing_vad.speech_probability(_make_frame())
    assert missing_exc.value.code == "VOICE_MODEL_UNAVAILABLE"


async def test_faster_whisper_adapter_falls_back_cuda_to_cpu_and_writes_zero_files(
    tmp_path: Path,
) -> None:
    init_calls: list[tuple[str, str]] = []

    class _FakeWhisperModel:
        def __init__(self, device: str, compute_type: str) -> None:
            init_calls.append((device, compute_type))
            if device == "cuda":
                raise RuntimeError("CUDA driver library libcublas.so.12 is not available")
            self.device = device

        def transcribe(
            self,
            audio_samples: list[float],
            *,
            language: str,
            beam_size: int,
        ) -> tuple[list[object], object]:
            assert language == "es"
            assert beam_size == 1
            assert len(audio_samples) == 512
            seg = SimpleNamespace(text=" cambia al proyecto nova workspace ", avg_logprob=-0.08)
            info = SimpleNamespace(language="es")
            return [seg], info

    def model_factory(
        model_id: str,
        device: str,
        compute_type: str,
        local_files_only: bool,
    ) -> _FakeWhisperModel:
        assert model_id == "small"
        assert local_files_only is True
        return _FakeWhisperModel(device=device, compute_type=compute_type)

    stt = FasterWhisperSttAdapter(
        settings=FasterWhisperSettings(preferred_device="cuda"),
        model_factory=model_factory,
    )
    pcm = struct.pack("<512h", *([2048] * 512))
    result = await stt.transcribe(pcm, sample_rate_hz=16000)

    assert init_calls == [("cuda", "float16"), ("cpu", "int8")]
    assert result.backend_device == "cpu"
    assert result.text == "cambia al proyecto nova workspace"
    assert result.language == "es"
    assert 0.85 <= result.confidence <= 1.0
    assert _list_dir_names(tmp_path) == []
    await stt.close()


async def test_faster_whisper_timeout_raises_voice_transcription_timeout() -> None:
    import time as time_mod

    class _SlowModel:
        def transcribe(
            self,
            audio_samples: list[float],
            *,
            language: str,
            beam_size: int,
        ) -> tuple[list[object], object]:
            time_mod.sleep(0.2)
            return [], SimpleNamespace(language="es")

    stt = FasterWhisperSttAdapter(
        settings=FasterWhisperSettings(preferred_device="cpu", timeout_seconds=0.03),
        model_factory=lambda *_args: _SlowModel(),
    )
    with pytest.raises(VoicePipelineError) as exc_info:
        await stt.transcribe(b"\x00\x00" * 512)
    assert exc_info.value.code == "VOICE_TRANSCRIPTION_TIMEOUT"


async def test_local_onnx_tts_supports_kokoro_and_piper_in_ram_without_disk_writes(
    tmp_path: Path,
) -> None:
    # 1. Kokoro engine in memory
    class _FakeKokoro:
        def create(
            self,
            text: str,
            *,
            voice: str,
            speed: float,
            lang: str,
        ) -> tuple[list[float], int]:
            assert text == "Comando aprobado y en ejecucion."
            assert voice == "ef_dora"
            assert lang == "es"
            return [0.25, -0.25] * 400, 24000

    kokoro_tts = LocalOnnxTtsAdapter(
        settings=LocalOnnxTtsSettings(engine="kokoro"),
        kokoro_factory=_FakeKokoro,
    )
    synth_k = await kokoro_tts.synthesize("  Comando aprobado y en ejecucion. ")
    assert synth_k.sample_rate_hz == 24000
    assert len(synth_k.pcm_s16le) == 1600

    # Empty text rejected with VOICE_EMPTY_TTS_TEXT
    with pytest.raises(VoicePipelineError) as empty_exc:
        await kokoro_tts.synthesize("   ")
    assert empty_exc.value.code == "VOICE_EMPTY_TTS_TEXT"

    # 2. Piper engine over stdin/stdout subprocess (shell=False) with zero temp files
    fake_model, resolved_model_str = _write_placeholder_model(
        tmp_path,
        "es_ES-davefx-medium.onnx",
    )
    captured_argv: list[tuple[str, ...]] = []
    captured_stdin = bytearray()

    class _FakePiperProc:
        returncode: int | None = 0

        async def communicate(self, input_bytes: bytes | None = None) -> tuple[bytes, bytes]:
            if input_bytes:
                captured_stdin.extend(input_bytes)
            await asyncio.sleep(0)
            return b"\x05\x00" * 600, b""

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    async def fake_piper_spawner(*argv: str) -> _FakePiperProc:
        captured_argv.append(argv)
        return _FakePiperProc()

    piper_tts = LocalOnnxTtsAdapter(
        settings=LocalOnnxTtsSettings(
            engine="piper",
            piper_model_path=str(fake_model),
            piper_sample_rate_hz=22050,
        ),
        which_resolver=lambda cmd: "/usr/bin/piper" if cmd == "piper" else None,
        piper_spawner=fake_piper_spawner,
    )
    synth_p = await piper_tts.synthesize("Listo, pruebas en verde.")
    assert synth_p.sample_rate_hz == 22050
    assert len(synth_p.pcm_s16le) == 1200
    assert captured_argv[0] == (
        "/usr/bin/piper",
        "--model",
        resolved_model_str,
        "--output-raw",
    )
    assert captured_stdin.decode("utf-8").strip() == "Listo, pruebas en verde."
    # Verify no .wav or extra files were created in tmp_path
    assert _list_dir_names(tmp_path) == ["es_ES-davefx-medium.onnx"]

    with pytest.raises(ValidationError):
        LocalOnnxTtsSettings(piper_binary="--malicious")


async def test_f8_lazy_model_loading_enforces_explicit_timeout() -> None:
    import time as time_mod

    def hung_loader() -> object:
        time_mod.sleep(0.2)
        raise AssertionError("unreachable")

    vad = SileroVadAdapter(
        settings=SileroVadSettings(
            inference_timeout_seconds=0.03,
            model_load_timeout_seconds=0.03,
        ),
        scorer_factory=hung_loader,  # type: ignore[arg-type]
    )
    with pytest.raises(VoicePipelineError) as exc_info:
        await vad.speech_probability(_make_frame())
    assert exc_info.value.code == "VOICE_MODEL_UNAVAILABLE"

    # Cold model load slower than per-frame inference_timeout_seconds still succeeds
    # within dedicated model_load_timeout_seconds, for both SileroVad and OpenWakeWord
    class _ColdVadBackend:
        def __init__(self) -> None:
            time_mod.sleep(0.06)

        def score_samples(self, samples: list[float], sample_rate_hz: int) -> float:
            return 0.5

        def reset(self) -> None:
            return None

    cold_vad = SileroVadAdapter(
        settings=SileroVadSettings(
            inference_timeout_seconds=0.02,
            model_load_timeout_seconds=0.5,
        ),
        scorer_factory=_ColdVadBackend,
    )
    assert await cold_vad.speech_probability(_make_frame()) == pytest.approx(0.5)

    class _ColdWakeBackend:
        def __init__(self) -> None:
            time_mod.sleep(0.06)

        def predict_scores(self, pcm_s16le: bytes) -> dict[str, float]:
            return {"oye_nova": 0.1}

        def reset(self) -> None:
            return None

    cold_wake = OpenWakeWordAdapter(
        settings=OpenWakeWordSettings(
            inference_timeout_seconds=0.02,
            model_load_timeout_seconds=0.5,
        ),
        predictor_factory=_ColdWakeBackend,
    )
    det = await cold_wake.detect(_make_frame())
    assert det.detected is False

