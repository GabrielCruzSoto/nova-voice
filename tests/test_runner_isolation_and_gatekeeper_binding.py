"""Tests for T2 (NOVA-020 AC-6..AC-8): process groups, heartbeat watchdog, task-bound Gatekeeper."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from nova.adapters._process_group import terminate_process_group
from nova.adapters.agent_registry import (
    AgentAdapterRegistry,
    AntigravityTier1Adapter,
    PiCodingRunnerAdapter,
)
from nova.adapters.antigravity_adapter import AntigravityCLIAdapter, AntigravityRunnerConfig
from nova.adapters.claude_adapter import ClaudeCodeAdapter, ClaudeCodeConfig
from nova.adapters.gatekeeper_ipc import GatekeeperIPCConfig, UnixSocketGatekeeperServer
from nova.adapters.opencode_adapter import OpenCodeAdapter, OpenCodeConfig
from nova.adapters.pi_rpc_client import NOVA_VOICE_SYSTEM_PROMPT
from nova.application.security_gatekeeper import GatekeeperSettings, SecurityGatekeeper
from nova.domain.errors import CodingRunnerError
from nova.domain.events import (
    CommandApprovalRequestedEvent,
    CommandApprovalResolvedEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
)
from nova.domain.models import (
    CodingTaskRequest,
    CommandAuditInput,
    CommandAuditResult,
    CommandInterceptRequest,
    ModelsSettingsSection,
    ResolveWorkspaceRequest,
    WorkspaceContext,
)


class _MutableWorkspaceResolver:
    def __init__(self, initial: WorkspaceContext | None) -> None:
        self._active = initial

    @property
    def active_context(self) -> WorkspaceContext | None:
        return self._active

    def set_active(self, ctx: WorkspaceContext | None) -> None:
        self._active = ctx

    async def resolve(self, request: ResolveWorkspaceRequest) -> WorkspaceContext:
        assert self._active is not None
        return self._active


class _FakeAuditor:
    async def audit(self, request: CommandAuditInput) -> CommandAuditResult:
        return CommandAuditResult(
            risk_level=request.risk_hint,
            spoken_summary="Comando sensible detectado, requiere tu aprobacion.",
            technical_explanation=f"Audited in {request.workspace_path}",
            audited_by="fake_auditor",
        )


def _make_ws(path: Path, name: str) -> WorkspaceContext:
    path.mkdir(parents=True, exist_ok=True)
    return WorkspaceContext(
        workspace_id=f"ws-{name}",
        name=name,
        canonical_path=str(path.resolve()),
        selected_via="exact_path",
    )


@pytest.mark.asyncio
async def test_nova020_ac6_gatekeeper_task_workspace_binding_survives_workspace_switch_and_clear(
    tmp_path: Path,
) -> None:
    ws_alpha = _make_ws(tmp_path / "alpha", "alpha")
    ws_beta = _make_ws(tmp_path / "beta", "beta")
    resolver = _MutableWorkspaceResolver(ws_alpha)
    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def _collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(_collect)
    gk = SecurityGatekeeper(
        workspace_resolver=resolver,
        auditor=_FakeAuditor(),
        event_bus=bus,
        settings=GatekeeperSettings(approval_timeout_seconds=2.0),
    )

    gk.register_task_workspace("tsk-alpha-1", ws_alpha.canonical_path)
    # Switch active global workspace away from alpha, then clear to None
    resolver.set_active(ws_beta)

    # Safe command in alpha still auto-allows via task_id even while active workspace is beta
    dec1 = await gk.evaluate_and_authorize(
        CommandInterceptRequest(
            correlation_id="corr-1",
            source="tier2_claude",
            tool_name="Bash",
            command="pytest -q",
            cwd=ws_alpha.canonical_path,
            task_id="tsk-alpha-1",
        )
    )
    assert dec1.allowed is True
    assert dec1.verdict == "auto_allowed"

    # Even with NO global active workspace, registered task_id still resolves alpha
    resolver.set_active(None)
    dec2 = await gk.evaluate_and_authorize(
        CommandInterceptRequest(
            correlation_id="corr-2",
            source="tier2_claude",
            tool_name="Bash",
            command="pwd",
            cwd=ws_alpha.canonical_path,
            task_id="tsk-alpha-1",
        )
    )
    assert dec2.allowed is True

    # Unknown / unregistered task_id MUST fail-closed
    dec_spoof = await gk.evaluate_and_authorize(
        CommandInterceptRequest(
            correlation_id="corr-spoof",
            source="tier2_claude",
            tool_name="Bash",
            command="pytest -q",
            cwd=ws_alpha.canonical_path,
            task_id="tsk-unregistered",
        )
    )
    assert dec_spoof.allowed is False
    assert dec_spoof.verdict == "invalid_request"

    # Escalated command propagates task_id on requested and resolved events
    eval_task = asyncio.create_task(
        gk.evaluate_and_authorize(
            CommandInterceptRequest(
                correlation_id="corr-esc",
                source="tier2_claude",
                tool_name="Bash",
                command="sudo whoami",
                cwd=ws_alpha.canonical_path,
                task_id="tsk-alpha-1",
            )
        )
    )
    await asyncio.sleep(0.05)
    req_events = [e for e in events if isinstance(e, CommandApprovalRequestedEvent)]
    assert len(req_events) == 1
    assert req_events[0].task_id == "tsk-alpha-1"

    await gk.resolve_command_approval(req_events[0].audit_id, approved=True, resolved_by="test")
    dec_esc = await eval_task
    assert dec_esc.allowed is True
    res_events = [e for e in events if isinstance(e, CommandApprovalResolvedEvent)]
    assert len(res_events) == 1
    assert res_events[0].task_id == "tsk-alpha-1"

    gk.unregister_task_workspace("tsk-alpha-1")


@pytest.mark.asyncio
async def test_nova020_ac6_gatekeeper_ipc_forwards_task_id(tmp_path: Path) -> None:
    ws_alpha = _make_ws(tmp_path / "alpha", "alpha")
    resolver = _MutableWorkspaceResolver(None)
    bus = OrchestratorEventBus()
    gk = SecurityGatekeeper(workspace_resolver=resolver, auditor=_FakeAuditor(), event_bus=bus)
    gk.register_task_workspace("tsk-ipc-1", ws_alpha.canonical_path)

    async with UnixSocketGatekeeperServer(
        gk,
        GatekeeperIPCConfig(runtime_base_dir=str(tmp_path)),
    ) as server:
        env = server.child_env()
        reader, writer = await asyncio.open_unix_connection(env["NOVA_GATEKEEPER_SOCKET"])
        frame = {
            "protocol_version": "1.0",
            "token": env["NOVA_GATEKEEPER_TOKEN"],
            "correlation_id": "corr-ipc-1",
            "source": "tier2_claude",
            "tool_name": "Bash",
            "command": "pytest -q",
            "cwd": ws_alpha.canonical_path,
            "task_id": "tsk-ipc-1",
        }
        writer.write((json.dumps(frame) + "\n").encode("utf-8"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
        writer.close()
        await writer.wait_closed()
        resp = json.loads(raw.decode("utf-8"))
        assert resp["allowed"] is True
        assert resp["verdict"] == "auto_allowed"


@pytest.mark.asyncio
async def test_nova020_ac7_process_group_killpg_reaps_grandchild_on_cancel(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    fake_cli = tmp_path / "fake_claude_tree.py"
    fake_cli.write_text(
        f"""import json, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open({str(pid_file)!r}, "w", encoding="utf-8") as fh:
    fh.write(str(child.pid))
    fh.flush()
print(json.dumps({{"type": "system", "subtype": "init", "session_id": "s-1"}}), flush=True)
time.sleep(60)
""",
        encoding="utf-8",
    )

    adapter = ClaudeCodeAdapter(
        ClaudeCodeConfig(
            executable=sys.executable,
            extra_leading_args=(str(fake_cli),),
            workspace_path=str(tmp_path),
            tier="tier2",
            timeout_seconds=30.0,
            stdout_heartbeat_timeout_seconds=10.0,
            enable_gatekeeper_hook=False,
        )
    )
    req = CodingTaskRequest(
        correlation_id="corr-pg",
        workspace_path=str(tmp_path),
        prompt="spawn tree",
        model="sonnet",
    )

    stream = adapter.execute(req)
    first = await stream.__anext__()
    assert first.kind == "init"
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
    # Verify grandchild is alive before cancellation
    os.kill(grandchild_pid, 0)

    await stream.aclose()
    await asyncio.sleep(0.15)

    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


@pytest.mark.asyncio
async def test_nova020_ac8_stdout_heartbeat_watchdog_kills_hung_cli(tmp_path: Path) -> None:
    fake_claude = tmp_path / "fake_claude_hang.py"
    fake_claude.write_text(
        """import json, time
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-hang"}), flush=True)
time.sleep(30)
""",
        encoding="utf-8",
    )
    fake_opencode = tmp_path / "fake_opencode_hang.py"
    fake_opencode.write_text(
        """import json, time
print(json.dumps({"type": "text", "sessionID": "oc-hang", "part": {"text": "hi"}}), flush=True)
time.sleep(30)
""",
        encoding="utf-8",
    )
    fake_agy = tmp_path / "fake_agy_hang.py"
    fake_agy.write_text(
        """import json, time
print(json.dumps({"event": "init", "conversation_id": "agy-hang"}), flush=True)
time.sleep(30)
""",
        encoding="utf-8",
    )

    claude = ClaudeCodeAdapter(
        ClaudeCodeConfig(
            executable=sys.executable,
            extra_leading_args=(str(fake_claude),),
            workspace_path=str(tmp_path),
            tier="tier2",
            timeout_seconds=30.0,
            stdout_heartbeat_timeout_seconds=0.25,
            enable_gatekeeper_hook=False,
        )
    )
    with pytest.raises(CodingRunnerError) as exc_claude:
        async for _ in claude.execute(
            CodingTaskRequest(
                correlation_id="c-hb-1",
                workspace_path=str(tmp_path),
                prompt="hang",
                model="sonnet",
            )
        ):
            pass
    assert exc_claude.value.code == "RUNNER_STDOUT_HEARTBEAT_TIMEOUT"

    opencode = OpenCodeAdapter(
        OpenCodeConfig(
            executable=sys.executable,
            extra_leading_args=(str(fake_opencode),),
            workspace_path=str(tmp_path),
            tier="tier2",
            timeout_seconds=30.0,
            stdout_heartbeat_timeout_seconds=0.25,
            enable_gatekeeper_plugin=False,
        )
    )
    with pytest.raises(CodingRunnerError) as exc_oc:
        async for _ in opencode.execute(
            CodingTaskRequest(
                correlation_id="c-hb-2",
                workspace_path=str(tmp_path),
                prompt="hang",
                model="default",
            )
        ):
            pass
    assert exc_oc.value.code == "RUNNER_STDOUT_HEARTBEAT_TIMEOUT"

    agy = AntigravityCLIAdapter(
        AntigravityRunnerConfig(
            executable=sys.executable,
            extra_leading_args=(str(fake_agy),),
            timeout_seconds=30.0,
            stdout_heartbeat_timeout_seconds=0.25,
            require_gatekeeper_plugin=False,
        )
    )
    with pytest.raises(CodingRunnerError) as exc_agy:
        async for _ in agy.execute(
            CodingTaskRequest(
                correlation_id="c-hb-3",
                workspace_path=str(tmp_path),
                prompt="hang",
                model="claude-opus-5-5-high",
            )
        ):
            pass
    assert exc_agy.value.code == "RUNNER_STDOUT_HEARTBEAT_TIMEOUT"


def test_nova020_ac5_registry_builds_isolated_per_task_runners(tmp_path: Path) -> None:
    registry = AgentAdapterRegistry(which_resolver=lambda name: f"/usr/bin/{name}")
    models = ModelsSettingsSection()
    r1 = registry.build_tier2_runner_for_task(
        "claude",
        models=models,
        workspace_path=str(tmp_path),
        task_id="tsk-1",
        gatekeeper_ipc_env={"NOVA_GATEKEEPER_SOCKET": "/tmp/s", "NOVA_GATEKEEPER_TOKEN": "tok"},
        stdout_heartbeat_timeout_seconds=90.0,
        timeout_seconds=600.0,
    )
    r2 = registry.build_tier2_runner_for_task(
        "claude",
        models=models,
        workspace_path=str(tmp_path),
        task_id="tsk-2",
        gatekeeper_ipc_env={"NOVA_GATEKEEPER_SOCKET": "/tmp/s", "NOVA_GATEKEEPER_TOKEN": "tok"},
        stdout_heartbeat_timeout_seconds=90.0,
        timeout_seconds=600.0,
    )
    assert r1 is not r2
    assert isinstance(r1, ClaudeCodeAdapter)
    assert r1._config.gatekeeper_ipc_env is not None
    assert r1._config.gatekeeper_ipc_env["NOVA_TASK_ID"] == "tsk-1"
    assert r1._config.stdout_heartbeat_timeout_seconds == 90.0
    assert r1._config.timeout_seconds == 600.0


@pytest.mark.asyncio
async def test_nova020_f2_stdout_heartbeat_pauses_during_waiting_approval_and_resumes_after(
    tmp_path: Path,
) -> None:
    # Case 1: CLI stays silent for 0.55s (> heartbeat 0.22s) while paused=True, then completes.
    fake_slow = tmp_path / "fake_claude_approval_wait.py"
    fake_slow.write_text(
        """import json, time
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-wait"}), flush=True)
time.sleep(0.55)
res = {"type": "result", "subtype": "success", "session_id": "s-wait", "result": "Approved."}
print(json.dumps(res), flush=True)
""",
        encoding="utf-8",
    )
    paused = [True]
    adapter_ok = ClaudeCodeAdapter(
        ClaudeCodeConfig(
            executable=sys.executable,
            extra_leading_args=(str(fake_slow),),
            workspace_path=str(tmp_path),
            tier="tier2",
            timeout_seconds=5.0,
            stdout_heartbeat_timeout_seconds=0.22,
            enable_gatekeeper_hook=False,
        ),
        heartbeat_pause_predicate=lambda: paused[0],
    )
    events = []
    async for ev in adapter_ok.execute(
        CodingTaskRequest(
            correlation_id="c-f2-ok",
            workspace_path=str(tmp_path),
            prompt="wait approval",
            model="sonnet",
        )
    ):
        events.append(ev)
    assert any(e.kind == "completed" and e.final_response == "Approved." for e in events)

    # Case 2: paused flips False after 0.3s while CLI hangs -> heartbeat fires after unpause.
    fake_hang = tmp_path / "fake_claude_unpause_hang.py"
    fake_hang.write_text(
        """import json, time
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-hang2"}), flush=True)
time.sleep(30)
""",
        encoding="utf-8",
    )
    paused2 = [True]

    async def _unpause_later() -> None:
        await asyncio.sleep(0.32)
        paused2[0] = False

    unpause_task = asyncio.create_task(_unpause_later())
    adapter_hang = ClaudeCodeAdapter(
        ClaudeCodeConfig(
            executable=sys.executable,
            extra_leading_args=(str(fake_hang),),
            workspace_path=str(tmp_path),
            tier="tier2",
            timeout_seconds=5.0,
            stdout_heartbeat_timeout_seconds=0.22,
            enable_gatekeeper_hook=False,
        ),
        heartbeat_pause_predicate=lambda: paused2[0],
    )
    try:
        with pytest.raises(CodingRunnerError) as exc:
            async for _ in adapter_hang.execute(
                CodingTaskRequest(
                    correlation_id="c-f2-hang",
                    workspace_path=str(tmp_path),
                    prompt="hang after approval",
                    model="sonnet",
                )
            ):
                pass
        assert exc.value.code == "RUNNER_STDOUT_HEARTBEAT_TIMEOUT"
        assert paused2[0] is False
    finally:
        await unpause_task


@pytest.mark.asyncio
async def test_nova020_f7_bounded_exit_when_stdout_closes_early(tmp_path: Path) -> None:
    fake_close_stdout = tmp_path / "fake_close_stdout.py"
    fake_close_stdout.write_text(
        """import json, os, time
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-eof"}), flush=True)
os.close(1)
time.sleep(30)
""",
        encoding="utf-8",
    )
    claude = ClaudeCodeAdapter(
        ClaudeCodeConfig(
            executable=sys.executable,
            extra_leading_args=(str(fake_close_stdout),),
            workspace_path=str(tmp_path),
            tier="tier2",
            timeout_seconds=0.4,
            stdout_heartbeat_timeout_seconds=None,
            enable_gatekeeper_hook=False,
        )
    )
    with pytest.raises(CodingRunnerError) as exc_c:
        async for _ in claude.execute(
            CodingTaskRequest(
                correlation_id="c-f7-claude",
                workspace_path=str(tmp_path),
                prompt="close stdout",
                model="sonnet",
            )
        ):
            pass
    assert exc_c.value.code == "RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH"

    opencode = OpenCodeAdapter(
        OpenCodeConfig(
            executable=sys.executable,
            extra_leading_args=(str(fake_close_stdout),),
            workspace_path=str(tmp_path),
            tier="tier2",
            timeout_seconds=0.4,
            stdout_heartbeat_timeout_seconds=None,
            enable_gatekeeper_plugin=False,
        )
    )
    with pytest.raises(CodingRunnerError) as exc_o:
        async for _ in opencode.execute(
            CodingTaskRequest(
                correlation_id="c-f7-opencode",
                workspace_path=str(tmp_path),
                prompt="close stdout",
                model="default",
            )
        ):
            pass
    assert exc_o.value.code == "RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH"


@pytest.mark.asyncio
async def test_nova020_f9_terminate_process_group_kills_sigterm_ignoring_grandchild(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "stubborn.pid"
    parent_script = tmp_path / "spawn_stubborn.py"
    child_code = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    )
    parent_script.write_text(
        f"""import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", {child_code!r}])
with open({str(pid_file)!r}, "w", encoding="utf-8") as fh:
    fh.write(str(child.pid))
    fh.flush()
""",
        encoding="utf-8",
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(parent_script),
        start_new_session=True,
    )
    await asyncio.wait_for(proc.wait(), timeout=2.0)
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
    # Give grandchild a moment to register SIG_IGN and verify it is alive
    await asyncio.sleep(0.1)
    os.kill(grandchild_pid, 0)

    await terminate_process_group(proc, grace_seconds=0.15)
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


def test_nova020_f5_f11_registry_model_defaults_and_tier1_system_prompt(tmp_path: Path) -> None:
    registry = AgentAdapterRegistry(which_resolver=lambda name: f"/usr/bin/{name}")
    models = ModelsSettingsSection(
        tier2_engine="agy",
        tier2_model="claude-opus-5-5-high",
    )
    r_claude = registry.build_tier2_runner_for_task(
        "claude",
        models=models,
        workspace_path=str(tmp_path),
        task_id="tsk-m1",
    )
    r_opencode = registry.build_tier2_runner_for_task(
        "opencode",
        models=models,
        workspace_path=str(tmp_path),
        task_id="tsk-m2",
    )
    r_agy = registry.build_tier2_runner_for_task(
        "agy",
        models=models,
        workspace_path=str(tmp_path),
        task_id="tsk-m3",
    )
    r_pi = registry.build_tier2_runner_for_task(
        "pi",
        models=models,
        workspace_path=str(tmp_path),
        task_id="tsk-m4",
    )
    assert isinstance(r_claude, ClaudeCodeAdapter)
    assert r_claude._config.model == "opus"
    assert isinstance(r_opencode, OpenCodeAdapter)
    assert r_opencode._config.model == "anthropic/claude-sonnet-4-5"
    assert isinstance(r_agy, AntigravityCLIAdapter)
    assert isinstance(r_pi, PiCodingRunnerAdapter)
    assert r_pi._pi_client._config.model == models.pi_model

    # F11: Tier 1 adapters for opencode and agy receive NOVA_VOICE_SYSTEM_PROMPT
    t1_opencode = registry.build_tier1_adapter(
        "opencode",
        models=models,
        workspace_path=str(tmp_path),
    )
    assert isinstance(t1_opencode, OpenCodeAdapter)
    assert t1_opencode._config.append_system_prompt == NOVA_VOICE_SYSTEM_PROMPT
    assert "[[NOVA_ACTION:" in (t1_opencode._config.append_system_prompt or "")

    t1_agy = registry.build_tier1_adapter(
        "agy",
        models=models,
        workspace_path=str(tmp_path),
    )
    assert isinstance(t1_agy, AntigravityTier1Adapter)
    assert t1_agy._append_system_prompt == NOVA_VOICE_SYSTEM_PROMPT
