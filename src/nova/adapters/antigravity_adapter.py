"""Tier 2 CodingRunnerPort adapter over `agy --output-format stream-json` (OP-3)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nova.domain.errors import CodingRunnerError
from nova.domain.models import CodingTaskRequest, RunnerProgressEvent

ALLOWED_TIER2_MODELS: frozenset[str] = frozenset(
    {
        "claude-opus-5-5-high",
        "gemini-4-carbon-high",
        "gemini-4-argon-high",
        "claude-sonnet-5-5-high",
    }
)
MAX_STDERR_BYTES = 16 * 1024
DEFAULT_MAX_LINE_BYTES = 8 * 1024 * 1024


class AntigravityRunnerConfig(BaseModel):
    """Configuration boundary for AntigravityCLIAdapter."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    executable: str = Field(default="agy", min_length=1)
    extra_leading_args: tuple[str, ...] = ()
    allowed_models: frozenset[str] = ALLOWED_TIER2_MODELS
    timeout_seconds: float = Field(default=300.0, gt=0.0)
    max_line_bytes: int = Field(default=DEFAULT_MAX_LINE_BYTES, gt=0)


class _AgyStepUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    conversation_id: str = ""
    step_index: int = 0
    state: str = ""
    step_type: str = "agent_response"
    text_delta: str = ""


class _AgyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    conversation_id: str = ""
    status: str = ""
    response: str = ""
    duration_seconds: float = 0.0


class AntigravityCLIAdapter:
    """Executes `agy` non-interactively using verified NDJSON stream-json events."""

    def __init__(self, config: AntigravityRunnerConfig | None = None) -> None:
        self._config = config or AntigravityRunnerConfig()

    async def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
        """Run a heavy coding turn and yield structured RunnerProgressEvent records."""
        prompt = request.prompt.strip()
        if not prompt:
            raise CodingRunnerError(
                code="RUNNER_INVALID_REQUEST",
                message="CodingTaskRequest prompt cannot be empty.",
            )
        if request.model not in self._config.allowed_models:
            raise CodingRunnerError(
                code="RUNNER_INVALID_REQUEST",
                message=f"Model '{request.model}' is not in the approved Tier 2 allowlist.",
            )

        cmd: list[str] = [
            self._config.executable,
            *self._config.extra_leading_args,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--model",
            request.model,
            "--mode",
            "accept-edits",
            "--disable-slash-commands",
        ]
        if request.conversation_id:
            cmd.extend(["--conversation", request.conversation_id])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=request.workspace_path,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self._config.max_line_bytes,
            )
        except OSError as exc:
            raise CodingRunnerError(
                code="RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH",
                message=f"Failed to launch agy CLI: {exc}",
            ) from exc

        stderr_buf = bytearray()
        stderr_task = asyncio.create_task(self._drain_stderr(proc, stderr_buf))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.timeout_seconds
        seen_result = False
        conv_id = request.conversation_id or ""

        try:
            assert proc.stdout is not None
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise CodingRunnerError(
                        code="RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH",
                        message="Timed out waiting for agy stream-json completion.",
                    )
                try:
                    raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                except TimeoutError as exc:
                    raise CodingRunnerError(
                        code="RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH",
                        message="Timed out waiting for agy stream-json output.",
                    ) from exc
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    raise CodingRunnerError(
                        code="RUNNER_EXECUTION_FAILED",
                        message="agy stream-json record exceeded configured max_line_bytes.",
                    ) from exc

                if not raw_line:
                    break

                trimmed = raw_line.rstrip(b"\r\n")
                if not trimmed:
                    continue

                record = self._parse_ndjson_line(trimmed)
                event_name = record.get("event")

                if event_name == "init":
                    raw_conv = record.get("conversation_id")
                    if isinstance(raw_conv, str) and raw_conv:
                        conv_id = raw_conv
                    yield RunnerProgressEvent(
                        kind="init",
                        conversation_id=conv_id or "unknown",
                        step_type="init",
                    )
                elif event_name == "step_update":
                    step_raw = record.get("step_update")
                    if isinstance(step_raw, dict):
                        step = _AgyStepUpdate.model_validate(step_raw)
                        if step.conversation_id:
                            conv_id = step.conversation_id
                        if step.text_delta:
                            yield RunnerProgressEvent(
                                kind="delta",
                                conversation_id=conv_id or "unknown",
                                step_type=step.step_type,
                                text_delta=step.text_delta,
                            )
                        elif step.step_type and step.step_type != "user_input":
                            yield RunnerProgressEvent(
                                kind="milestone",
                                conversation_id=conv_id or "unknown",
                                step_type=step.step_type,
                            )
                elif event_name == "result":
                    res_raw = record.get("result")
                    if not isinstance(res_raw, dict):
                        raise CodingRunnerError(
                            code="RUNNER_EXECUTION_FAILED",
                            message="Malformed agy result payload.",
                        )
                    res = _AgyResult.model_validate(res_raw)
                    if res.conversation_id:
                        conv_id = res.conversation_id
                    if res.status != "SUCCESS":
                        raise CodingRunnerError(
                            code="RUNNER_EXECUTION_FAILED",
                            message=f"agy reported status '{res.status}'.",
                        )
                    seen_result = True
                    yield RunnerProgressEvent(
                        kind="completed",
                        conversation_id=conv_id or "unknown",
                        step_type="result",
                        final_response=res.response,
                        duration_seconds=res.duration_seconds,
                    )

            remaining_wait = max(0.05, deadline - loop.time())
            try:
                exit_code = await asyncio.wait_for(proc.wait(), timeout=remaining_wait)
            except TimeoutError as exc:
                raise CodingRunnerError(
                    code="RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH",
                    message="Timed out waiting for agy process exit.",
                ) from exc

            if exit_code != 0 and not seen_result:
                raise CodingRunnerError(
                    code="RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH",
                    message=f"agy exited unexpectedly with code {exit_code}.",
                )
            if not seen_result:
                raise CodingRunnerError(
                    code="RUNNER_EXECUTION_FAILED",
                    message="agy exited without emitting a terminal result record.",
                )
        finally:
            await self._terminate_process(proc)
            if not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task

    @staticmethod
    def _parse_ndjson_line(trimmed: bytes) -> dict[str, Any]:
        try:
            obj = json.loads(trimmed.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodingRunnerError(
                code="RUNNER_EXECUTION_FAILED",
                message="Corrupted NDJSON line from agy stdout.",
            ) from exc
        if not isinstance(obj, dict):
            raise CodingRunnerError(
                code="RUNNER_EXECUTION_FAILED",
                message="Expected JSON object in agy stream-json output.",
            )
        return obj

    @staticmethod
    async def _drain_stderr(proc: asyncio.subprocess.Process, sink: bytearray) -> None:
        if proc.stderr is None:
            return
        while True:
            chunk = await proc.stderr.read(1024)
            if not chunk:
                break
            sink.extend(chunk)
            if len(sink) > MAX_STDERR_BYTES:
                del sink[:-MAX_STDERR_BYTES]

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
