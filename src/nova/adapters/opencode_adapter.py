"""Unified Tier 1 & Tier 2 adapter for OpenCode CLI (`opencode run --format json`)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nova.domain.errors import CodingRunnerError
from nova.domain.models import AgentTier, CodingTaskRequest, RunnerProgressEvent

MAX_STDERR_BYTES = 16 * 1024
DEFAULT_MAX_LINE_BYTES = 8 * 1024 * 1024
_BUNDLED_OPENCODE_PLUGIN = str(
    Path(__file__).resolve().parents[1] / "plugins" / "opencode_gatekeeper" / "nova_gatekeeper.ts"
)


class OpenCodeConfig(BaseModel):
    """Configuration boundary for OpenCodeAdapter."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    executable: str = Field(default="opencode", min_length=1)
    extra_leading_args: tuple[str, ...] = ()
    provider: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=160)
    workspace_path: str = Field(default_factory=os.getcwd, min_length=1)
    tier: AgentTier = "tier1"
    standalone: bool = False
    timeout_seconds: float = Field(default=300.0, gt=0.0)
    max_line_bytes: int = Field(default=DEFAULT_MAX_LINE_BYTES, gt=0)
    gatekeeper_ipc_env: dict[str, str] | None = None
    provider_env: dict[str, str] | None = None
    enable_gatekeeper_plugin: bool = True
    bundled_plugin_path: str = Field(default=_BUNDLED_OPENCODE_PLUGIN, min_length=1)


def _format_opencode_model(provider: str, model: str) -> str:
    clean_model = model.strip()
    clean_provider = provider.strip()
    if not clean_model:
        return ""
    if "/" in clean_model or not clean_provider:
        return clean_model
    return f"{clean_provider}/{clean_model}"


def _extract_opencode_text(record: dict[str, Any]) -> str:
    ev_type = str(record.get("type") or "")
    if ev_type == "text":
        part = record.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            return str(part["text"])
        if isinstance(record.get("text"), str):
            return str(record["text"])
    if ev_type in ("message.part.updated", "part"):
        part = record.get("part")
        if isinstance(part, dict) and part.get("type") == "text":
            txt = part.get("text")
            if isinstance(txt, str):
                return txt
    return ""


class OpenCodeAdapter:
    """Implements both Tier1ConversationPort and CodingRunnerPort over OpenCode CLI."""

    def __init__(self, config: OpenCodeConfig | None = None) -> None:
        self._config = config or OpenCodeConfig()
        self._workspace_path = self._config.workspace_path
        self._active_session_id: str | None = None
        self._active_proc: asyncio.subprocess.Process | None = None

    @property
    def active_session_id(self) -> str | None:
        return self._active_session_id

    async def switch_workspace(
        self,
        new_workspace_path: str,
        *,
        workspace_active: bool = True,
        allowed_project_paths: tuple[str, ...] = (),
        session_id: str | None = None,
    ) -> None:
        _ = (workspace_active, allowed_project_paths)
        await self.barge_in_abort()
        self._workspace_path = new_workspace_path
        self._active_session_id = session_id

    async def barge_in_abort(self) -> None:
        proc = self._active_proc
        self._active_proc = None
        if proc is not None:
            await self._terminate_process(proc)

    def _build_env(self, *, tier: AgentTier) -> dict[str, str]:
        env = dict(os.environ)
        if self._config.provider_env:
            env.update(self._config.provider_env)
        if self._config.gatekeeper_ipc_env:
            env.update(self._config.gatekeeper_ipc_env)
            env["NOVA_GATEKEEPER_SOURCE"] = (
                "tier1_opencode" if tier == "tier1" else "tier2_opencode"
            )
            if self._config.enable_gatekeeper_plugin:
                plugin_uri = Path(self._config.bundled_plugin_path).resolve().as_uri()
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
                    {"plugin": [plugin_uri]},
                    ensure_ascii=False,
                )
        return env

    async def stream_prompt(self, message: str) -> AsyncIterator[str]:
        """Tier1ConversationPort: stream text deltas for a conversational turn."""
        req = CodingTaskRequest(
            correlation_id="corr-tier1-opencode",
            workspace_path=self._workspace_path,
            prompt=message,
            model=_format_opencode_model(self._config.provider, self._config.model) or "default",
            conversation_id=self._active_session_id,
        )
        async for ev in self._run_stream(req, tier="tier1"):
            if ev.conversation_id and ev.conversation_id != "unknown":
                self._active_session_id = ev.conversation_id
            if ev.kind == "delta" and ev.text_delta:
                yield ev.text_delta

    async def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
        """CodingRunnerPort: stream structured RunnerProgressEvent records."""
        async for ev in self._run_stream(request, tier="tier2"):
            yield ev

    async def _run_stream(
        self,
        request: CodingTaskRequest,
        *,
        tier: AgentTier,
    ) -> AsyncIterator[RunnerProgressEvent]:
        prompt = request.prompt.strip()
        if not prompt:
            raise CodingRunnerError(
                code="RUNNER_INVALID_REQUEST",
                message="OpenCode prompt cannot be empty.",
            )

        cmd: list[str] = [
            self._config.executable,
            *self._config.extra_leading_args,
            "run",
            "--format",
            "json",
        ]
        if self._config.standalone:
            cmd.append("--standalone")
        formatted_model = _format_opencode_model(self._config.provider, request.model)
        if formatted_model and formatted_model != "default":
            cmd.extend(["--model", formatted_model])
        if request.conversation_id:
            cmd.extend(["--session", request.conversation_id])
        cmd.append(prompt)

        child_env = self._build_env(tier=tier)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=request.workspace_path,
                env=child_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self._config.max_line_bytes,
            )
        except OSError as exc:
            raise CodingRunnerError(
                code="RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH",
                message=f"Failed to launch opencode CLI: {exc}",
            ) from exc

        self._active_proc = proc
        stderr_buf = bytearray()
        stderr_task = asyncio.create_task(self._drain_stderr(proc, stderr_buf))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.timeout_seconds
        session_id = request.conversation_id or ""
        emitted_init = False
        collected_parts: list[str] = []

        try:
            assert proc.stdout is not None
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise CodingRunnerError(
                        code="RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH",
                        message="Timed out waiting for opencode JSON completion.",
                    )
                try:
                    raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                except TimeoutError as exc:
                    raise CodingRunnerError(
                        code="RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH",
                        message="Timed out waiting for opencode JSON output.",
                    ) from exc

                if not raw_line:
                    break
                trimmed = raw_line.rstrip(b"\r\n")
                if not trimmed:
                    continue

                try:
                    parsed: Any = json.loads(trimmed.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(parsed, dict):
                    continue

                raw_sid = parsed.get("sessionID") or parsed.get("session_id")
                if isinstance(raw_sid, str) and raw_sid.strip():
                    session_id = raw_sid.strip()

                if not emitted_init:
                    emitted_init = True
                    yield RunnerProgressEvent(
                        kind="init",
                        conversation_id=session_id or "unknown",
                        step_type="init",
                    )

                text_chunk = _extract_opencode_text(parsed)
                if text_chunk:
                    collected_parts.append(text_chunk)
                    yield RunnerProgressEvent(
                        kind="delta",
                        conversation_id=session_id or "unknown",
                        step_type="agent_response",
                        text_delta=text_chunk,
                    )

            await proc.wait()
            await stderr_task
            if proc.returncode != 0:
                stderr_excerpt = stderr_buf.decode("utf-8", errors="replace").strip()
                raise CodingRunnerError(
                    code="RUNNER_EXECUTION_FAILED",
                    message=(
                        f"opencode CLI exited with status {proc.returncode}: "
                        f"{stderr_excerpt or 'unknown error'}"
                    ),
                )
            if not emitted_init:
                yield RunnerProgressEvent(
                    kind="init",
                    conversation_id=session_id or "unknown",
                    step_type="init",
                )
            yield RunnerProgressEvent(
                kind="completed",
                conversation_id=session_id or "unknown",
                step_type="result",
                final_response="".join(collected_parts),
            )
        finally:
            if self._active_proc is proc:
                self._active_proc = None
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            await self._terminate_process(proc)

    @staticmethod
    async def _drain_stderr(proc: asyncio.subprocess.Process, buf: bytearray) -> None:
        if proc.stderr is None:
            return
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            if len(buf) < MAX_STDERR_BYTES:
                buf.extend(chunk[: MAX_STDERR_BYTES - len(buf)])

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
                    await proc.wait()
