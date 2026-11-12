"""TDD tests for T2: PipeWireAudioAdapter (OP-1, AC-4, AC-5, NFR-1, NFR-2, NFR-3)."""

from __future__ import annotations

import asyncio
import time
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from nova.adapters.audio_pipewire import PipeWireAudioAdapter, PipeWireAudioSettings
from nova.domain.errors import VoicePipelineError
from nova.domain.models import SynthesizedAudio


class _FakeStdout:
    def __init__(self, chunks: list[bytes], *, hang_after: bool = False) -> None:
        self._chunks = list(chunks)
        self._hang_after = hang_after
        self._closed = asyncio.Event()

    def close_stream(self) -> None:
        self._closed.set()

    async def read(self, n: int = -1) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        if self._hang_after and not self._closed.is_set():
            await self._closed.wait()
        return b""


class _FakeStdin:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


class _FakeAudioProcess:
    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        stdout_chunks: list[bytes] | None = None,
        hang_until_terminated: bool = False,
        exit_code: int = 0,
        stderr_bytes: bytes = b"",
        ignore_sigterm: bool = False,
    ) -> None:
        self.argv = argv
        self.returncode: int | None = None
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(stdout_chunks or [], hang_after=hang_until_terminated)
        self.stderr = _FakeStdout([stderr_bytes] if stderr_bytes else [])
        self._hang = hang_until_terminated
        self._exit_code = exit_code
        self._ignore_sigterm = ignore_sigterm
        self._done = asyncio.Event()
        if not hang_until_terminated and not stdout_chunks:
            self.returncode = exit_code
            self._done.set()

    def terminate(self) -> None:
        if self._ignore_sigterm:
            return
        self.returncode = -15
        self.stdout.close_stream()
        self._done.set()

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.close_stream()
        self._done.set()

    async def wait(self) -> int:
        if not self._hang and self.returncode is None:
            # Allow stdout chunks to be drained first
            await asyncio.sleep(0.01)
            self.returncode = self._exit_code
            self._done.set()
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode


def test_pyproject_declares_optional_voice_dependencies() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    parsed = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    voice_deps = parsed["project"]["optional-dependencies"]["voice"]
    joined = " ".join(voice_deps)
    for expected in ("faster-whisper", "openwakeword", "onnxruntime", "kokoro-onnx", "piper-tts"):
        assert expected in joined


def test_pipewire_settings_rejects_flag_injection_in_target_names() -> None:
    with pytest.raises(ValidationError):
        PipeWireAudioSettings(capture_target="--evil-flag")
    with pytest.raises(ValidationError):
        PipeWireAudioSettings(playback_target="sink\ninjected")


async def test_capture_uses_pw_record_s16_and_reassembles_exact_1024_byte_frames() -> None:
    spawned: list[_FakeAudioProcess] = []
    # 300 + 900 + 848 = 2048 bytes -> exactly 2 frames of 1024 bytes!
    raw_chunks = [b"\x01\x00" * 150, b"\x02\x00" * 450, b"\x03\x00" * 424]

    async def spawner(*argv: str) -> _FakeAudioProcess:
        proc = _FakeAudioProcess(argv, stdout_chunks=raw_chunks, hang_until_terminated=True)
        spawned.append(proc)
        return proc

    def fake_which(cmd: str) -> str | None:
        return "/usr/bin/pw-record" if cmd == "pw-record" else None

    adapter = PipeWireAudioAdapter(
        which_resolver=fake_which,
        subprocess_spawner=spawner,
    )

    received = []
    async for frame in adapter.frames():
        received.append(frame)
        if len(received) == 2:
            await adapter.stop_capture()

    assert len(received) == 2
    assert all(len(f.pcm_s16le) == 1024 for f in received)
    assert spawned[0].argv[:9] == (
        "/usr/bin/pw-record",
        "--raw",
        "--format",
        "s16",
        "--rate",
        "16000",
        "--channels",
        "1",
        "--latency",
    )
    # Idempotent second stop
    await adapter.stop_capture()


async def test_capture_falls_back_to_parecord_and_bounds_queue_under_backpressure() -> None:
    spawned: list[_FakeAudioProcess] = []
    # 8 frames of 1024 bytes pushed into max_frame_queue=4
    eight_frames = [bytes([idx, 0]) * 512 for idx in range(1, 9)]

    async def spawner(*argv: str) -> _FakeAudioProcess:
        proc = _FakeAudioProcess(argv, stdout_chunks=eight_frames, hang_until_terminated=True)
        spawned.append(proc)
        return proc

    def fake_which(cmd: str) -> str | None:
        return "/usr/bin/parecord" if cmd == "parecord" else None

    adapter = PipeWireAudioAdapter(
        settings=PipeWireAudioSettings(max_frame_queue=4),
        which_resolver=fake_which,
        subprocess_spawner=spawner,
    )

    collected = []
    async for frame in adapter.frames():
        collected.append(frame)
        if len(collected) == 1:
            await asyncio.sleep(0.03)
        if len(collected) == 4:
            await adapter.stop_capture()

    assert spawned[0].argv[0] == "/usr/bin/parecord"
    assert "--format=s16le" in spawned[0].argv
    # Latest frame (index 8) was retained instead of blocking or OOMing
    assert collected[-1].pcm_s16le[0] == 8


async def test_missing_binaries_and_unexpected_stream_crash_raise_typed_errors() -> None:
    adapter_missing = PipeWireAudioAdapter(which_resolver=lambda _cmd: None)
    with pytest.raises(VoicePipelineError) as exc_info:
        async for _ in adapter_missing.frames():
            pass
    assert exc_info.value.code == "VOICE_DEVICE_UNAVAILABLE"

    async def crashing_spawner(*argv: str) -> _FakeAudioProcess:
        return _FakeAudioProcess(
            argv,
            stdout_chunks=[],
            hang_until_terminated=False,
            exit_code=1,
            stderr_bytes=b"PipeWire daemon connection refused token=secretValue1234",
        )

    adapter_crash = PipeWireAudioAdapter(
        which_resolver=lambda cmd: f"/usr/bin/{cmd}",
        subprocess_spawner=crashing_spawner,
    )
    with pytest.raises(VoicePipelineError) as crash_exc:
        async for _ in adapter_crash.frames():
            pass
    assert crash_exc.value.code == "VOICE_AUDIO_STREAM_FAILED"
    assert "secretValue1234" not in crash_exc.value.sanitized_message


async def test_playback_and_barge_in_stop_playback_under_100ms_even_when_sigterm_ignored() -> None:
    spawned: list[_FakeAudioProcess] = []

    async def spawner(*argv: str) -> _FakeAudioProcess:
        proc = _FakeAudioProcess(
            argv,
            hang_until_terminated=True,
            ignore_sigterm=True,
        )
        spawned.append(proc)
        return proc

    adapter = PipeWireAudioAdapter(
        settings=PipeWireAudioSettings(stop_grace_seconds=0.03),
        which_resolver=lambda cmd: "/usr/bin/pw-play" if cmd == "pw-play" else None,
        subprocess_spawner=spawner,
    )

    synth = SynthesizedAudio(pcm_s16le=b"\x10\x00" * 2400, sample_rate_hz=24000, channels=1)
    play_task = asyncio.create_task(adapter.play_pcm(synth))
    await asyncio.sleep(0.01)
    assert adapter.is_playing is True

    t0 = time.perf_counter()
    await asyncio.gather(adapter.stop_playback(), adapter.stop_playback())
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    await asyncio.wait_for(play_task, timeout=0.2)

    assert elapsed_ms < 100.0, f"stop_playback took {elapsed_ms:.2f} ms (>= 100 ms)"
    assert adapter.is_playing is False
    assert spawned[0].returncode == -9
    assert bytes(spawned[0].stdin.buffer) == synth.pcm_s16le


async def test_continuous_bounded_stderr_drain_during_long_capture() -> None:
    heavy_stderr_chunks = [b"PipeWire xrun warning line\n" * 80 for _ in range(80)]
    spawned: list[_FakeAudioProcess] = []

    async def spawner(*argv: str) -> _FakeAudioProcess:
        proc = _FakeAudioProcess(
            argv,
            stdout_chunks=[b"\x01\x00" * 512, b"\x02\x00" * 512],
            hang_until_terminated=True,
        )
        proc.stderr = _FakeStdout(heavy_stderr_chunks, hang_after=True)
        spawned.append(proc)
        return proc

    adapter = PipeWireAudioAdapter(
        which_resolver=lambda cmd: "/usr/bin/pw-record" if cmd == "pw-record" else None,
        subprocess_spawner=spawner,
    )

    seen = 0
    async for _frame in adapter.frames():
        seen += 1
        if seen == 2:
            await asyncio.sleep(0.02)
            await adapter.stop_capture()

    assert seen == 2
    assert spawned[0].stderr._chunks == []

