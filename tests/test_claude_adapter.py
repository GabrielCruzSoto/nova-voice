"""TDD tests for NOVA-017 T3: ClaudeCodeAdapter (Tier 1 & Tier 2) and PreToolUse Gatekeeper hook."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nova.adapters.claude_adapter import ClaudeCodeAdapter, ClaudeCodeConfig
from nova.adapters.gatekeeper_ipc import GatekeeperIPCConfig, UnixSocketGatekeeperServer
from nova.application.security_gatekeeper import GatekeeperSettings, SecurityGatekeeper
from nova.domain.events import OrchestratorEventBus
from nova.domain.models import (
    CodingTaskRequest,
    CommandAuditInput,
    CommandAuditResult,
    WorkspaceContext,
)

_CLAUDE_HOOK_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "nova"
    / "plugins"
    / "claude_gatekeeper"
    / "pre_tool_hook.py"
)


class _StubWorkspaceResolver:
    def __init__(self, ws_path: str) -> None:
        self._ctx = WorkspaceContext(
            workspace_id="ws-1",
            name="demo",
            canonical_path=ws_path,
            resolved_by="exact_path",
        )

    @property
    def active_context(self) -> WorkspaceContext | None:
        return self._ctx

    async def resolve(self, request: object) -> WorkspaceContext:
        return self._ctx


class _StubAuditor:
    async def audit(self, request: CommandAuditInput) -> CommandAuditResult:
        return CommandAuditResult(
            risk_level="medium",
            spoken_summary="Requiere aprobacion.",
            technical_explanation="Comando fuera de lista blanca.",
            audited_by="deterministic_fallback",
        )


def _write_fake_claude_cli(script_path: Path) -> None:
    script_path.write_text(
        """#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
settings_path = None
if "--settings" in args:
    settings_path = args[args.index("--settings") + 1]
    with open(settings_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    assert "hooks" in cfg and "PreToolUse" in cfg["hooks"]

resume_id = args[args.index("--resume") + 1] if "--resume" in args else "claude-sess-new-1"

print(json.dumps({"type": "system", "subtype": "init", "session_id": resume_id}), flush=True)
print(
    json.dumps({
        "type": "assistant",
        "session_id": resume_id,
        "message": {"content": [{"type": "text", "text": "Hola desde Claude"}]},
    }),
    flush=True,
)
print(
    json.dumps({
        "type": "result",
        "subtype": "success",
        "session_id": resume_id,
        "result": "Hola desde Claude",
    }),
    flush=True,
)
""",
        encoding="utf-8",
    )
    os.chmod(script_path, 0o755)


@pytest.mark.asyncio
async def test_claude_adapter_tier1_and_tier2_stream_and_resume(tmp_path: Path) -> None:
    fake_claude = tmp_path / "fake_claude.py"
    _write_fake_claude_cli(fake_claude)

    adapter = ClaudeCodeAdapter(
        ClaudeCodeConfig(
            executable=sys.executable,
            extra_leading_args=(str(fake_claude),),
            model="haiku",
            workspace_path=str(tmp_path),
            gatekeeper_ipc_env={
                "NOVA_GATEKEEPER_SOCKET": str(tmp_path / "gk.sock"),
                "NOVA_GATEKEEPER_TOKEN": "tok-synth",
            },
        )
    )

    # Tier 1 conversational streaming + session capture
    chunks = [delta async for delta in adapter.stream_prompt("Hola")]
    assert "".join(chunks) == "Hola desde Claude"
    assert adapter.active_session_id == "claude-sess-new-1"

    # Tier 2 coding execution with explicit conversation_id (--resume)
    events = [
        ev
        async for ev in adapter.execute(
            CodingTaskRequest(
                correlation_id="corr-claude-t2",
                workspace_path=str(tmp_path),
                prompt="Refactoriza modulo",
                model="opus",
                conversation_id="claude-sess-resumed-42",
            )
        )
    ]
    assert any(
        ev.kind == "init" and ev.conversation_id == "claude-sess-resumed-42" for ev in events
    )
    assert any(ev.kind == "delta" and "Hola desde Claude" in ev.text_delta for ev in events)
    assert any(
        ev.kind == "completed" and ev.conversation_id == "claude-sess-resumed-42"
        for ev in events
    )


def _run_hook_sync(
    hook_script: Path,
    payload: dict[str, Any],
    env: dict[str, str],
) -> dict[str, Any]:
    res = subprocess.run(
        [sys.executable, str(hook_script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert res.returncode == 0
    parsed: dict[str, Any] = json.loads(res.stdout)
    return parsed


@pytest.mark.asyncio
async def test_claude_pre_tool_hook_enforces_gatekeeper_and_fail_closed(tmp_path: Path) -> None:
    ws_dir = str(tmp_path)

    # 1. Fail-closed when socket is unreachable
    bad_env = {
        **os.environ,
        "NOVA_GATEKEEPER_SOCKET": os.path.join(ws_dir, "missing.sock"),
        "NOVA_GATEKEEPER_TOKEN": "tok-test",
    }
    out_fail = await asyncio.to_thread(
        _run_hook_sync,
        _CLAUDE_HOOK_SCRIPT,
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": ws_dir},
        bad_env,
    )
    assert out_fail["hookSpecificOutput"]["permissionDecision"] == "deny"

    # 2. Live UnixSocketGatekeeperServer allow safe command & deny catastrophic command
    bus = OrchestratorEventBus()
    gk = SecurityGatekeeper(
        workspace_resolver=_StubWorkspaceResolver(ws_dir),
        auditor=_StubAuditor(),
        event_bus=bus,
        settings=GatekeeperSettings(approval_timeout_seconds=1.0),
    )
    async with UnixSocketGatekeeperServer(
        gk, GatekeeperIPCConfig(runtime_base_dir=ws_dir)
    ) as ipc:
        env_ok = {**os.environ, **ipc.child_env(), "NOVA_GATEKEEPER_SOURCE": "tier2_claude"}

        allowed_resp = await asyncio.to_thread(
            _run_hook_sync,
            _CLAUDE_HOOK_SCRIPT,
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": ws_dir},
            env_ok,
        )
        assert allowed_resp["hookSpecificOutput"]["permissionDecision"] == "allow"

        denied_resp = await asyncio.to_thread(
            _run_hook_sync,
            _CLAUDE_HOOK_SCRIPT,
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}, "cwd": ws_dir},
            env_ok,
        )
        assert denied_resp["hookSpecificOutput"]["permissionDecision"] == "deny"
