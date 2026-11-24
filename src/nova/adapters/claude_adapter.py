"""Unified Tier 1 & Tier 2 adapter for Claude Code CLI (`claude -p --output-format stream-json`)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nova.domain.errors import CodingRunnerError
from nova.domain.models import AgentTier, CodingTaskRequest, RunnerProgressEvent

MAX_STDERR_BYTES = 16 * 1024
DEFAULT_MAX_LINE_BYTES = 8 * 1024 * 1024
_BUNDLED_CLAUDE_HOOK = str(
    Path(__file__).resolve().parents[1] / "plugins" / "claude_gatekeeper" / "pre_tool_hook.py"
)


class ClaudeCodeConfig(BaseModel):
    """Configuration boundary for ClaudeCodeAdapter."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    executable: str = Field(default="claude", min_length=1)
    extra_leading_args: tuple[str, ...] = ()
    model: str = Field(default="sonnet", max_length=160)
    workspace_path: str = Field(default_factory=os.getcwd, min_length=1)
    tier: AgentTier = "tier1"
    timeout_seconds: float = Field(default=300.0, gt=0.0)
    max_line_bytes: int = Field(default=DEFAULT_MAX_LINE_BYTES, gt=0)
    gatekeeper_ipc_env: dict[str, str] | None = None
    provider_env: dict[str, str] | None = None
    enable_gatekeeper_hook: bool = True
    bundled_hook_path: str = Field(default=_BUNDLED_CLAUDE_HOOK, min_length=1)
    append_system_prompt: str | None = None


def _extract_claude_text_delta(record: dict[str, Any]) -> str:
    ev_type = record.get("type")
    if ev_type == "content_block_delta":
        delta = record.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            return str(delta["text"])
    if ev_type == "assistant":
        msg = record.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        txt = block.get("text")
                        if isinstance(txt, str) and txt:
                            parts.append(txt)
                return "".join(parts)
    if ev_type == "stream_event":
        inner = record.get("event")
        if isinstance(inner, dict):
            return _extract_claude_text_delta(inner)
    return ""


class ClaudeCodeAdapter:
    """Implements both Tier1ConversationPort and CodingRunnerPort over Claude Code CLI."""

    def __init__(self, config: ClaudeCodeConfig | None = None) -> None:
        self._config = config or ClaudeCodeConfig()
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

    def _create_ephemeral_hook_settings(self) -> Path | None:
        if not (self._config.enable_gatekeeper_hook and self._config.gatekeeper_ipc_env):
            return None
        hook_cmd = f"{sys.executable} {self._config.bundled_hook_path}"
        settings_payload = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": hook_cmd}],
                    }
                ]
            }
        }
        fd, raw_path = tempfile.mkstemp(prefix="nova-claude-settings-", suffix=".json")
        settings_path = Path(raw_path)
        os.chmod(settings_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings_payload, handle, ensure_ascii=False)
        return settings_path

    def _build_env(self, *, tier: AgentTier) -> dict[str, str]:
        env = dict(os.environ)
        if self._config.provider_env:
            env.update(self._config.provider_env)
        if self._config.gatekeeper_ipc_env:
            env.update(self._config.gatekeeper_ipc_env)
            env["NOVA_GATEKEEPER_SOURCE"] = "tier1_claude" if tier == "tier1" else "tier2_claude"
        return env

    async def stream_prompt(self, message: str) -> AsyncIterator[str]:
        """Tier1ConversationPort: stream text deltas for a conversational turn."""
        req = CodingTaskRequest(
            correlation_id="corr-tier1-claude",
            workspace_path=self._workspace_path,
            prompt=message,
            model=self._config.model or "haiku",
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
                message="Claude Code prompt cannot be empty.",
            )

        settings_file = self._create_ephemeral_hook_settings()
        cmd: list[str] = [
            self._config.executable,
            *self._config.extra_leading_args,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if request.model.strip():
            cmd.extend(["--model", request.model.strip()])
        if request.conversation_id:
            cmd.extend(["--resume", request.conversation_id])
        if self._config.append_system_prompt and self._config.append_system_prompt.strip():
            cmd.extend(["--append-system-prompt", self._config.append_system_prompt.strip()])
        if settings_file is not None:
            cmd.extend(["--settings", str(settings_file)])

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
            if settings_file is not None:
                with contextlib.suppress(OSError):
                    settings_file.unlink()
            raise CodingRunnerError(
                code="RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH",
                message=f"Failed to launch claude CLI: {exc}",
            ) from exc

        self._active_proc = proc
        stderr_buf = bytearray()
        stderr_task = asyncio.create_task(self._drain_stderr(proc, stderr_buf))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.timeout_seconds
        session_id = request.conversation_id or ""
        emitted_text = False
        seen_result = False

        try:
            assert proc.stdout is not None
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise CodingRunnerError(
                        code="RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH",
                        message="Timed out waiting for claude stream-json completion.",
                    )
                try:
                    raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                except TimeoutError as exc:
                    raise CodingRunnerError(
                        code="RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH",
                        message="Timed out waiting for claude stream-json output.",
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

                raw_sid = parsed.get("session_id")
                if isinstance(raw_sid, str) and raw_sid.strip():
                    session_id = raw_sid.strip()

                ev_type = parsed.get("type")
                if ev_type == "system" and parsed.get("subtype") == "init":
                    yield RunnerProgressEvent(
                        kind="init",
                        conversation_id=session_id or "unknown",
                        step_type="init",
                    )
                else:
                    delta_txt = _extract_claude_text_delta(parsed)
                    if delta_txt:
                        emitted_text = True
                        yield RunnerProgressEvent(
                            kind="delta",
                            conversation_id=session_id or "unknown",
                            step_type="agent_response",
                            text_delta=delta_txt,
                        )
                    if ev_type == "result":
                        seen_result = True
                        final_resp = str(parsed.get("result") or "")
                        if final_resp and not emitted_text:
                            emitted_text = True
                            yield RunnerProgressEvent(
                                kind="delta",
                                conversation_id=session_id or "unknown",
                                step_type="agent_response",
                                text_delta=final_resp,
                            )
                        yield RunnerProgressEvent(
                            kind="completed",
                            conversation_id=session_id or "unknown",
                            step_type="result",
                            final_response=final_resp,
                        )

            await proc.wait()
            await stderr_task
            if proc.returncode != 0:
                stderr_excerpt = stderr_buf.decode("utf-8", errors="replace").strip()
                raise CodingRunnerError(
                    code="RUNNER_EXECUTION_FAILED",
                    message=(
                        f"claude CLI exited with status {proc.returncode}: "
                        f"{stderr_excerpt or 'unknown error'}"
                    ),
                )
            if not seen_result:
                yield RunnerProgressEvent(
                    kind="completed",
                    conversation_id=session_id or "unknown",
                    step_type="result",
                )
        finally:
            if self._active_proc is proc:
                self._active_proc = None
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            await self._terminate_process(proc)
            if settings_file is not None:
                with contextlib.suppress(OSError):
                    settings_file.unlink()

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
