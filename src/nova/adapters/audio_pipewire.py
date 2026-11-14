"""Linux PipeWire / PulseAudio in-memory PCM capture and playback adapter (NOVA-003 OP-1)."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nova.domain.errors import VoicePipelineError
from nova.domain.models import AudioFrame, SynthesizedAudio


class _AsyncReadStream(Protocol):
    async def read(self, n: int = -1) -> bytes: ...


class _AsyncWriteStream(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...
    async def wait_closed(self) -> None: ...


class AudioSubprocessProtocol(Protocol):
    """Minimal protocol for asyncio subprocess streams used by PipeWireAudioAdapter."""

    @property
    def returncode(self) -> int | None: ...

    @property
    def stdin(self) -> _AsyncWriteStream | None: ...

    @property
    def stdout(self) -> _AsyncReadStream | None: ...

    @property
    def stderr(self) -> _AsyncReadStream | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


AudioSpawner = Callable[..., Awaitable[AudioSubprocessProtocol]]
WhichResolver = Callable[[str], str | None]


async def _default_audio_spawner(*argv: str) -> AudioSubprocessProtocol:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


def _validate_audio_node_target(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("-") or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("Audio target name must not start with '-' or contain control chars")
    return cleaned


class PipeWireAudioSettings(BaseModel):
    """Configuration boundary for PipeWireAudioAdapter (OP-1)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    capture_sample_rate_hz: int = Field(default=16000, ge=8000, le=48000)
    frame_bytes: int = Field(default=1024, ge=2, le=16384)
    max_frame_queue: int = Field(default=128, ge=4, le=1024)
    startup_timeout_seconds: float = Field(default=3.0, gt=0.0, le=15.0)
    playback_timeout_seconds: float = Field(default=60.0, gt=0.0, le=300.0)
    stop_grace_seconds: float = Field(default=0.05, gt=0.0, le=0.09)
    capture_target: str | None = None
    playback_target: str | None = None

    @field_validator("frame_bytes")
    @classmethod
    def _validate_even_frame_bytes(cls, value: int) -> int:
        if value % 2 != 0:
            raise ValueError("frame_bytes must be an even s16le byte count")
        return value

    @field_validator("capture_target", "playback_target")
    @classmethod
    def _validate_targets(cls, value: str | None) -> str | None:
        return _validate_audio_node_target(value)


class PipeWireAudioAdapter:
    """Streams 16 kHz mono s16le frames via pw-record/parecord and plays TTS via pw-play/paplay."""

    def __init__(
        self,
        settings: PipeWireAudioSettings | None = None,
        *,
        which_resolver: WhichResolver = shutil.which,
        subprocess_spawner: AudioSpawner = _default_audio_spawner,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings or PipeWireAudioSettings()
        self._which = which_resolver
        self._spawner = subprocess_spawner
        self._clock = clock

        self._capture_proc: AudioSubprocessProtocol | None = None
        self._capture_reader_task: asyncio.Task[None] | None = None
        self._capture_queue: (
            asyncio.Queue[AudioFrame | BaseException | Literal["eof"]] | None
        ) = None
        self._stopping_capture = False
        self._capture_stop_lock = asyncio.Lock()

        self._playback_proc: AudioSubprocessProtocol | None = None
        self._stopping_playback = False
        self._playback_stop_lock = asyncio.Lock()

    def update_settings(
        self,
        *,
        capture_target: str | None = None,
        playback_target: str | None = None,
    ) -> None:
        """Hot-update PipeWire/PulseAudio target devices for future streams."""
        self._settings = self._settings.model_copy(
            update={
                "capture_target": capture_target,
                "playback_target": playback_target,
            }
        )

    @property
    def is_playing(self) -> bool:
        """Return True while a playback subprocess is active."""
        proc = self._playback_proc
        return proc is not None and proc.returncode is None

    def _resolve_capture_argv(self) -> tuple[str, ...]:
        rate = str(self._settings.capture_sample_rate_hz)
        pw_record = self._which("pw-record")
        if pw_record:
            argv = [
                pw_record,
                "--raw",
                "--format",
                "s16",
                "--rate",
                rate,
                "--channels",
                "1",
                "--latency",
                "32ms",
            ]
            if self._settings.capture_target is not None:
                argv.extend(["--target", self._settings.capture_target])
            argv.append("-")
            return tuple(argv)

        parecord = self._which("parecord")
        if parecord:
            argv = [
                parecord,
                "--raw",
                "--format=s16le",
                f"--rate={rate}",
                "--channels=1",
                "--latency-msec=32",
            ]
            if self._settings.capture_target is not None:
                argv.append(f"--device={self._settings.capture_target}")
            return tuple(argv)

        raise VoicePipelineError(
            code="VOICE_DEVICE_UNAVAILABLE",
            message="Neither 'pw-record' nor 'parecord' is available on this Linux host.",
        )

    def _resolve_playback_argv(self, sample_rate_hz: int) -> tuple[str, ...]:
        rate = str(sample_rate_hz)
        pw_play = self._which("pw-play")
        if pw_play:
            argv = [
                pw_play,
                "--raw",
                "--format",
                "s16",
                "--rate",
                rate,
                "--channels",
                "1",
                "--latency",
                "32ms",
            ]
            if self._settings.playback_target is not None:
                argv.extend(["--target", self._settings.playback_target])
            argv.append("-")
            return tuple(argv)

        paplay = self._which("paplay")
        if paplay:
            argv = [
                paplay,
                "--raw",
                "--format=s16le",
                f"--rate={rate}",
                "--channels=1",
                "--latency-msec=32",
            ]
            if self._settings.playback_target is not None:
                argv.append(f"--device={self._settings.playback_target}")
            return tuple(argv)

        raise VoicePipelineError(
            code="VOICE_DEVICE_UNAVAILABLE",
            message="Neither 'pw-play' nor 'paplay' is available on this Linux host.",
        )

    async def frames(self) -> AsyncIterator[AudioFrame]:
        """Capture and yield bounded 32 ms (1024-byte) mono PCM frames in RAM."""
        await self.stop_capture()
        argv = self._resolve_capture_argv()
        self._stopping_capture = False

        try:
            proc = await asyncio.wait_for(
                self._spawner(*argv),
                timeout=self._settings.startup_timeout_seconds,
            )
        except (TimeoutError, OSError) as exc:
            raise VoicePipelineError(
                code="VOICE_DEVICE_UNAVAILABLE",
                message=f"Failed to launch audio capture process: {exc}",
            ) from exc

        self._capture_proc = proc
        queue: asyncio.Queue[AudioFrame | BaseException | Literal["eof"]] = asyncio.Queue(
            maxsize=self._settings.max_frame_queue
        )
        self._capture_queue = queue
        stderr_tail = bytearray()

        async def _drain_stderr_continuously() -> None:
            stderr = proc.stderr
            if stderr is None:
                return
            with contextlib.suppress(OSError, asyncio.CancelledError):
                while not self._stopping_capture:
                    chunk = await stderr.read(2048)
                    if not chunk:
                        break
                    stderr_tail.extend(chunk)
                    if len(stderr_tail) > 2048:
                        del stderr_tail[:-2048]

        stderr_task = asyncio.create_task(_drain_stderr_continuously())

        async def _pump_stdout() -> None:
            buf = bytearray()
            frame_bytes = self._settings.frame_bytes
            rate_hz = self._settings.capture_sample_rate_hz
            try:
                stdout = proc.stdout
                if stdout is None:
                    raise VoicePipelineError(
                        code="VOICE_DEVICE_UNAVAILABLE",
                        message="Audio capture process exposed no stdout pipe.",
                    )
                while not self._stopping_capture:
                    chunk = await stdout.read(frame_bytes * 4)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    while len(buf) >= frame_bytes:
                        raw_frame = bytes(buf[:frame_bytes])
                        del buf[:frame_bytes]
                        frame = AudioFrame(
                            pcm_s16le=raw_frame,
                            sample_rate_hz=rate_hz,
                            channels=1,
                            monotonic_ts=self._clock(),
                        )
                        self._enqueue_bounded(queue, frame)

                rc = await proc.wait()
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(stderr_task, timeout=0.05)
                if not self._stopping_capture and rc != 0:
                    stderr_msg = (
                        bytes(stderr_tail).decode("utf-8", errors="replace").strip()
                        or "audio stream terminated"
                    )
                    self._enqueue_bounded(
                        queue,
                        VoicePipelineError(
                            code="VOICE_AUDIO_STREAM_FAILED",
                            message=(
                                f"Audio capture exited unexpectedly (code={rc}): {stderr_msg}"
                            ),
                        ),
                    )
                else:
                    self._enqueue_bounded(queue, "eof")
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                if not self._stopping_capture:
                    wrapped = (
                        exc
                        if isinstance(exc, VoicePipelineError)
                        else VoicePipelineError(
                            code="VOICE_AUDIO_STREAM_FAILED",
                            message=f"Audio capture stream failure: {exc}",
                        )
                    )
                    self._enqueue_bounded(queue, wrapped)
            finally:
                if not stderr_task.done():
                    stderr_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stderr_task

        self._capture_reader_task = asyncio.create_task(_pump_stdout())
        try:
            while True:
                item = await queue.get()
                if item == "eof":
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            await self.stop_capture()

    @staticmethod
    def _enqueue_bounded(
        queue: asyncio.Queue[AudioFrame | BaseException | Literal["eof"]],
        item: AudioFrame | BaseException | Literal["eof"],
    ) -> None:
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                _ = queue.get_nowait()
        queue.put_nowait(item)

    async def stop_capture(self) -> None:
        """Idempotently terminate active microphone capture subprocess and unblock consumers."""
        async with self._capture_stop_lock:
            self._stopping_capture = True
            proc = self._capture_proc
            reader_task = self._capture_reader_task
            queue = self._capture_queue
            self._capture_proc = None
            self._capture_reader_task = None
            self._capture_queue = None

            if proc is not None:
                await self._terminate_fast(proc)
            if reader_task is not None and not reader_task.done():
                reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader_task
            if queue is not None:
                self._enqueue_bounded(queue, "eof")

    async def play_pcm(self, audio: SynthesizedAudio) -> None:
        """Play an in-memory PCM utterance until completion or immediate barge-in cut."""
        await self.stop_playback()
        argv = self._resolve_playback_argv(audio.sample_rate_hz)
        self._stopping_playback = False

        try:
            proc = await asyncio.wait_for(
                self._spawner(*argv),
                timeout=self._settings.startup_timeout_seconds,
            )
        except (TimeoutError, OSError) as exc:
            raise VoicePipelineError(
                code="VOICE_DEVICE_UNAVAILABLE",
                message=f"Failed to launch audio playback process: {exc}",
            ) from exc

        self._playback_proc = proc
        try:
            async with asyncio.timeout(self._settings.playback_timeout_seconds):
                stdin = proc.stdin
                if stdin is not None:
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                        stdin.write(audio.pcm_s16le)
                        await stdin.drain()
                        stdin.close()
                        await stdin.wait_closed()
                rc = await proc.wait()
        except TimeoutError as exc:
            await self.stop_playback()
            raise VoicePipelineError(
                code="VOICE_AUDIO_STREAM_FAILED",
                message="Audio playback exceeded configured timeout.",
            ) from exc
        except asyncio.CancelledError:
            await self.stop_playback()
            raise
        finally:
            if self._playback_proc is proc:
                self._playback_proc = None

        if not self._stopping_playback and rc != 0:
            stderr_msg = await self._read_bounded_stderr(proc)
            raise VoicePipelineError(
                code="VOICE_AUDIO_STREAM_FAILED",
                message=f"Audio playback failed (code={rc}): {stderr_msg}",
            )

    async def stop_playback(self) -> None:
        """Idempotently cut active audio playback in < 100 ms (SIGTERM -> SIGKILL)."""
        async with self._playback_stop_lock:
            self._stopping_playback = True
            proc = self._playback_proc
            self._playback_proc = None
            if proc is not None:
                await self._terminate_fast(proc)

    async def _terminate_fast(self, proc: AudioSubprocessProtocol) -> None:
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._settings.stop_grace_seconds)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(TimeoutError, OSError):
                await asyncio.wait_for(proc.wait(), timeout=0.04)

    @staticmethod
    async def _read_bounded_stderr(proc: AudioSubprocessProtocol) -> str:
        stderr = proc.stderr
        if stderr is None:
            return "no stderr"
        with contextlib.suppress(TimeoutError, OSError):
            raw = await asyncio.wait_for(stderr.read(2048), timeout=0.1)
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                return text
        return "audio stream terminated"
