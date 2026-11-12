"""Asynchronous subprocess client for Pi Agent RPC mode (OP-2)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nova.domain.errors import PiRpcError

MAX_PENDING_COMMANDS = 32
MAX_STDERR_BYTES = 16 * 1024
DEFAULT_MAX_LINE_BYTES = 8 * 1024 * 1024


class PiRpcConfig(BaseModel):
    """Configuration boundary for Tier 1 Pi Agent subprocess."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    executable: str = Field(default="pi", min_length=1)
    extra_leading_args: tuple[str, ...] = ()
    provider: str = Field(default="nvidia", min_length=1)
    model: str = Field(default="deepseek-ai/deepseek-v4.1-flash", min_length=1)
    workspace_path: str = Field(min_length=1)
    no_session: bool = False
    ack_timeout_seconds: float = Field(default=5.0, gt=0.0)
    run_timeout_seconds: float = Field(default=180.0, gt=0.0)
    max_line_bytes: int = Field(default=DEFAULT_MAX_LINE_BYTES, gt=0)


class _PiRpcResponse(BaseModel):
    """Validated response envelope emitted on Pi RPC stdout."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str | None = None
    type: str
    command: str = ""
    success: bool = False
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class PiRpcClient:
    """Manages a persistent `pi --mode rpc` subprocess with strict LF JSONL framing."""

    def __init__(self, config: PiRpcConfig) -> None:
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[_PiRpcResponse]] = {}
        self._event_queues: list[asyncio.Queue[dict[str, Any] | None]] = []
        self._stderr_tail = bytearray()
        self._write_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        """Return True while the underlying Pi subprocess is alive."""
        return self._process is not None and self._process.returncode is None

    async def __aenter__(self) -> PiRpcClient:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        """Spawn the `pi --mode rpc` subprocess if not already running."""
        if self.is_running:
            return

        cmd: list[str] = [
            self._config.executable,
            *self._config.extra_leading_args,
            "--mode",
            "rpc",
            "--provider",
            self._config.provider,
            "--model",
            self._config.model,
        ]
        if self._config.no_session:
            cmd.append("--no-session")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self._config.workspace_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self._config.max_line_bytes,
            )
        except OSError as exc:
            raise PiRpcError(
                code="PI_RPC_SUBPROCESS_UNAVAILABLE",
                message=f"Failed to start Pi RPC subprocess: {exc}",
            ) from exc

        self._reader_task = asyncio.create_task(self._stdout_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

    async def switch_workspace(self, new_workspace_path: str) -> None:
        """Restart the RPC subprocess cleanly inside a new canonical workspace."""
        if new_workspace_path == self._config.workspace_path and self.is_running:
            return
        await self.close()
        self._config = self._config.model_copy(update={"workspace_path": new_workspace_path})
        await self.start()

    async def close(self) -> None:
        """Close stdin for orderly shutdown and escalate to SIGTERM/SIGKILL if needed."""
        proc = self._process
        self._process = None
        if proc is not None:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
                with contextlib.suppress(Exception):
                    await proc.stdin.wait_closed()
            await self._terminate_process(proc)

        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader_task = None
        self._stderr_task = None
        self._fail_all_waiters("Pi RPC client closed.")

    async def stream_prompt(self, message: str) -> AsyncIterator[str]:
        """Submit a prompt (with 1 restart retry on dead pipe) and yield text deltas."""
        queue, resp = await self._open_prompt_stream(message)
        try:
            disposition = str(resp.data.get("disposition", "started"))
            if disposition == "handled":
                return

            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._config.run_timeout_seconds

            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await self.close()
                    raise PiRpcError(
                        code="PI_RPC_SUBPROCESS_UNAVAILABLE",
                        message="Timed out waiting for Pi RPC agent_settled event.",
                    )
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError as exc:
                    await self.close()
                    raise PiRpcError(
                        code="PI_RPC_SUBPROCESS_UNAVAILABLE",
                        message="Timed out waiting for Pi RPC agent_settled event.",
                    ) from exc

                if record is None:
                    await self.close()
                    raise PiRpcError(
                        code="PI_RPC_SUBPROCESS_UNAVAILABLE",
                        message="Pi RPC subprocess exited or exceeded buffer before agent_settled.",
                    )

                rec_type = record.get("type")
                if rec_type == "message_update":
                    assistant_ev = record.get("assistantMessageEvent")
                    if (
                        isinstance(assistant_ev, dict)
                        and assistant_ev.get("type") == "text_delta"
                    ):
                        delta = assistant_ev.get("delta")
                        if isinstance(delta, str) and delta:
                            yield delta
                elif rec_type == "agent_settled":
                    return
        finally:
            if queue in self._event_queues:
                self._event_queues.remove(queue)

    async def barge_in_abort(self) -> None:
        """Execute contract OP-2 barge-in sequence: clear_queue followed by abort."""
        if not self.is_running:
            return
        await self._send_command({"type": "clear_queue"})
        await self._send_command({"type": "abort"})

    async def _open_prompt_stream(
        self,
        message: str,
    ) -> tuple[asyncio.Queue[dict[str, Any] | None], _PiRpcResponse]:
        """Attach a fresh event queue per attempt so a restart never poisons the active queue."""
        payload = {
            "type": "prompt",
            "message": message,
            "streamingBehavior": "steer",
        }
        for attempt in range(2):
            if not self.is_running:
                await self.start()
            queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
            self._event_queues.append(queue)
            try:
                resp = await self._send_command(payload)
                return queue, resp
            except PiRpcError as exc:
                if queue in self._event_queues:
                    self._event_queues.remove(queue)
                if attempt == 0 and exc.code == "PI_RPC_SUBPROCESS_UNAVAILABLE":
                    await self.close()
                    continue
                raise

        raise PiRpcError(
            code="PI_RPC_SUBPROCESS_UNAVAILABLE",
            message="Failed to submit prompt to Pi RPC after restart.",
        )

    async def _send_command(self, payload: dict[str, Any]) -> _PiRpcResponse:
        if len(self._pending) >= MAX_PENDING_COMMANDS:
            raise PiRpcError(
                code="PI_RPC_COMMAND_REJECTED",
                message="Maximum concurrent Pi RPC commands exceeded.",
            )
        if self._process is None or self._process.stdin is None or not self.is_running:
            raise PiRpcError(
                code="PI_RPC_SUBPROCESS_UNAVAILABLE",
                message="Pi RPC subprocess is not running.",
            )

        req_id = f"req-{uuid.uuid4().hex[:12]}"
        wire_obj = {"id": req_id, **payload}
        encoded = (json.dumps(wire_obj, ensure_ascii=False) + "\n").encode("utf-8")

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[_PiRpcResponse] = loop.create_future()
        self._pending[req_id] = fut

        try:
            async with self._write_lock:
                self._process.stdin.write(encoded)
                await self._process.stdin.drain()

            resp = await asyncio.wait_for(fut, timeout=self._config.ack_timeout_seconds)
        except TimeoutError as exc:
            await self.close()
            raise PiRpcError(
                code="PI_RPC_SUBPROCESS_UNAVAILABLE",
                message=f"Timed out waiting for Pi RPC response to {payload.get('type')}.",
            ) from exc
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise PiRpcError(
                code="PI_RPC_SUBPROCESS_UNAVAILABLE",
                message=f"Pi RPC stdin write failed: {exc}",
            ) from exc
        finally:
            self._pending.pop(req_id, None)

        if not resp.success:
            raise PiRpcError(
                code="PI_RPC_COMMAND_REJECTED",
                message=resp.error or f"Pi RPC rejected command {resp.command}",
            )
        return resp

    async def _stdout_loop(self) -> None:
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                try:
                    raw_line = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    await self._terminate_process(proc)
                    break
                if not raw_line:
                    break
                # Strict LF framing: strip only trailing \n and optional \r
                trimmed = raw_line.rstrip(b"\r\n")
                if not trimmed:
                    continue
                try:
                    parsed = json.loads(trimmed.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(parsed, dict):
                    continue

                if parsed.get("type") == "response":
                    resp = _PiRpcResponse.model_validate(parsed)
                    if resp.id and resp.id in self._pending:
                        fut = self._pending[resp.id]
                        if not fut.done():
                            fut.set_result(resp)
                    continue

                for queue in list(self._event_queues):
                    queue.put_nowait(parsed)
        finally:
            self._fail_all_waiters("Pi RPC stdout stream ended.")

    async def _stderr_loop(self) -> None:
        proc = self._process
        if proc is None or proc.stderr is None:
            return
        while True:
            chunk = await proc.stderr.read(1024)
            if not chunk:
                break
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > MAX_STDERR_BYTES:
                del self._stderr_tail[:-MAX_STDERR_BYTES]

    def _fail_all_waiters(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(
                    PiRpcError(code="PI_RPC_SUBPROCESS_UNAVAILABLE", message=reason)
                )
        self._pending.clear()
        for queue in list(self._event_queues):
            queue.put_nowait(None)

    @staticmethod
    async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        await asyncio.sleep(0)
