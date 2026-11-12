"""End-to-end IPC, permissions, CLI env wiring, and real hook tests (S3, OP-1, OP-5, NFR-2/5)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from nova.adapters.antigravity_adapter import AntigravityCLIAdapter, AntigravityRunnerConfig
from nova.adapters.gatekeeper_ipc import UnixSocketGatekeeperServer
from nova.adapters.pi_rpc_client import PI_GATEKEEPER_EXTENSION_PATH, PiRpcClient, PiRpcConfig
from nova.adapters.workspace_resolver import LocalWorkspaceResolver
from nova.application.security_gatekeeper import GatekeeperSettings, SecurityGatekeeper
from nova.domain.events import (
    CommandApprovalRequestedEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
)
from nova.domain.models import (
    CodingTaskRequest,
    CommandAuditInput,
    CommandAuditResult,
    ResolveWorkspaceRequest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class _StubAuditor:
    async def audit(self, request: CommandAuditInput) -> CommandAuditResult:
        return CommandAuditResult(
            risk_level="high",
            spoken_summary="Necesito tu aprobacion para ejecutar este comando sensible en consola.",
            technical_explanation=f"Regla activada: {request.rule_id}",
            audited_by="deterministic_fallback",
        )


async def _send_ipc_line(sock_path: str, line_bytes: bytes) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(sock_path)
    try:
        writer.write(line_bytes)
        await writer.drain()
        resp_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        return dict(json.loads(resp_line.decode("utf-8")))
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_nfr2_and_ac6_unix_socket_permissions_auth_and_approval_roundtrip(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Proyectos"
    ws = root / "ipc-demo"
    os.makedirs(ws, exist_ok=True)

    bus = OrchestratorEventBus()
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    await resolver.resolve(
        ResolveWorkspaceRequest(correlation_id="c-ipc", query=str(ws), mode="exact_path")
    )
    gatekeeper = SecurityGatekeeper(
        workspace_resolver=resolver,
        auditor=_StubAuditor(),
        event_bus=bus,
        settings=GatekeeperSettings(approval_timeout_seconds=5.0),
    )

    async def approve_on_request(ev: OrchestratorEvent) -> None:
        if isinstance(ev, CommandApprovalRequestedEvent):
            await gatekeeper.resolve_command_approval(ev.audit_id, approved=True)

    bus.subscribe(approve_on_request)

    async with UnixSocketGatekeeperServer(gatekeeper) as ipc:
        sock_path = ipc.socket_path
        assert len(sock_path.encode("utf-8")) <= 100

        mode = stat.S_IMODE(os.stat(sock_path).st_mode)
        assert mode == 0o600

        # 1. Invalid token -> 401 unauthorized
        bad_resp = await _send_ipc_line(
            sock_path,
            (
                json.dumps(
                    {
                        "protocol_version": "1.0",
                        "token": "wrong-token",
                        "correlation_id": "c-bad",
                        "source": "tier1_pi",
                        "tool_name": "bash",
                        "command": "pwd",
                        "cwd": str(ws),
                    }
                )
                + "\n"
            ).encode("utf-8"),
        )
        assert bad_resp["allowed"] is False
        assert bad_resp["verdict"] == "unauthorized"

        # 2. Malformed JSON -> 400 invalid_request
        malformed_resp = await _send_ipc_line(sock_path, b"{not-json}\n")
        assert malformed_resp["allowed"] is False
        assert malformed_resp["verdict"] == "invalid_request"

        # 3. Valid token + dubious command -> paused, audited, human-approved -> allowed
        env = ipc.child_env()
        good_resp = await _send_ipc_line(
            sock_path,
            (
                json.dumps(
                    {
                        "protocol_version": "1.0",
                        "token": env["NOVA_GATEKEEPER_TOKEN"],
                        "correlation_id": "c-ok",
                        "source": "tier1_pi",
                        "tool_name": "bash",
                        "command": "sudo apt update",
                        "cwd": str(ws),
                    }
                )
                + "\n"
            ).encode("utf-8"),
        )
        assert good_resp["allowed"] is True
        assert good_resp["verdict"] == "approved_by_human"

    assert not await asyncio.to_thread(os.path.exists, sock_path)


@pytest.mark.asyncio
async def test_ac5_and_op5_agy_pre_tool_hook_standalone_and_connected(tmp_path: Path) -> None:
    hook_script = REPO_ROOT / "src" / "nova" / "plugins" / "agy_gatekeeper" / "pre_tool_hook.py"
    plugin_dir = hook_script.parent

    # 1. Standalone pass-through when NOVA_GATEKEEPER_SOCKET is absent
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("NOVA_GATEKEEPER_")}
    hook_stdin = json.dumps(
        {
            "conversationId": "conv-1",
            "workspacePaths": [str(tmp_path)],
            "toolCall": {"name": "run_command", "args": {"CommandLine": "sudo rm -rf /"}},
            "stepIdx": 1,
        }
    )
    proc_standalone = await asyncio.create_subprocess_exec(
        sys.executable,
        str(hook_script),
        cwd=str(plugin_dir),
        env=clean_env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    out_standalone, _ = await proc_standalone.communicate(hook_stdin.encode("utf-8"))
    assert json.loads(out_standalone.decode("utf-8")) == {"decision": "allow"}

    # 2. Fail-closed when NOVA_GATEKEEPER_SOCKET points to missing socket
    broken_env = {
        **clean_env,
        "NOVA_GATEKEEPER_SOCKET": "/tmp/nonexistent-nova-gk.sock",
        "NOVA_GATEKEEPER_TOKEN": "tok",
    }
    proc_broken = await asyncio.create_subprocess_exec(
        sys.executable,
        str(hook_script),
        cwd=str(plugin_dir),
        env=broken_env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    out_broken, _ = await proc_broken.communicate(hook_stdin.encode("utf-8"))
    parsed_broken = json.loads(out_broken.decode("utf-8"))
    assert parsed_broken["decision"] == "deny"

    # 3. Connected to live UnixSocketGatekeeperServer
    ws = tmp_path / "Proyectos" / "hook-ws"
    os.makedirs(ws, exist_ok=True)
    bus = OrchestratorEventBus()
    resolver = LocalWorkspaceResolver(allowed_roots=[tmp_path / "Proyectos"], event_bus=bus)
    await resolver.resolve(
        ResolveWorkspaceRequest(correlation_id="c-h", query=str(ws), mode="exact_path")
    )
    gatekeeper = SecurityGatekeeper(
        workspace_resolver=resolver,
        auditor=_StubAuditor(),
        event_bus=bus,
    )
    async with UnixSocketGatekeeperServer(gatekeeper) as ipc:
        live_env = {**clean_env, **ipc.child_env()}

        safe_stdin = json.dumps(
            {
                "workspacePaths": [str(ws)],
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest -q"}},
            }
        )
        proc_safe = await asyncio.create_subprocess_exec(
            sys.executable,
            str(hook_script),
            cwd=str(plugin_dir),
            env=live_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        out_safe, _ = await proc_safe.communicate(safe_stdin.encode("utf-8"))
        assert json.loads(out_safe.decode("utf-8"))["decision"] == "allow"

        deny_stdin = json.dumps(
            {
                "workspacePaths": [str(ws)],
                "toolCall": {"name": "run_command", "args": {"CommandLine": "rm -rf /"}},
            }
        )
        proc_deny = await asyncio.create_subprocess_exec(
            sys.executable,
            str(hook_script),
            cwd=str(plugin_dir),
            env=live_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        out_deny, _ = await proc_deny.communicate(deny_stdin.encode("utf-8"))
        parsed_deny = json.loads(out_deny.decode("utf-8"))
        assert parsed_deny["decision"] == "deny"
        assert "CATASTROPHIC" in parsed_deny["reason"]


@pytest.mark.asyncio
async def test_cli_adapters_inject_gatekeeper_env_and_pi_extension(tmp_path: Path) -> None:
    assert await asyncio.to_thread(os.path.exists, PI_GATEKEEPER_EXTENSION_PATH)

    dump_pi = tmp_path / "pi_env_argv.json"
    fake_pi = tmp_path / "fake_pi_rpc.py"
    await asyncio.to_thread(
        fake_pi.write_text,
        f"""
import json, os, pathlib, sys
pathlib.Path({str(dump_pi)!r}).write_text(json.dumps({{
    "argv": sys.argv[1:],
    "sock": os.environ.get("NOVA_GATEKEEPER_SOCKET"),
}}))
for line in sys.stdin:
    req = json.loads(line)
    if req.get("type") == "prompt":
        ack = {{
            "id": req["id"],
            "type": "response",
            "command": "prompt",
            "success": True,
            "data": {{"disposition": "started"}},
        }}
        print(json.dumps(ack), flush=True)
        print(json.dumps({{"type": "agent_settled"}}), flush=True)
""",
        "utf-8",
    )

    client = PiRpcClient(
        PiRpcConfig(
            executable=sys.executable,
            extra_leading_args=(str(fake_pi),),
            workspace_path=str(tmp_path),
            gatekeeper_ipc_env={
                "NOVA_GATEKEEPER_SOCKET": "/tmp/synt.sock",
                "NOVA_GATEKEEPER_TOKEN": "synt-tok",
            },
        )
    )
    async with client:
        chunks = [c async for c in client.stream_prompt("hola")]
    assert chunks == []
    recorded_pi = json.loads(await asyncio.to_thread(dump_pi.read_text, "utf-8"))
    assert recorded_pi["sock"] == "/tmp/synt.sock"
    assert "-e" in recorded_pi["argv"]
    assert str(PI_GATEKEEPER_EXTENSION_PATH) in recorded_pi["argv"]

    dump_agy = tmp_path / "agy_env.json"
    fake_agy = tmp_path / "fake_agy.py"
    await asyncio.to_thread(
        fake_agy.write_text,
        f"""
import json, os, pathlib
pathlib.Path({str(dump_agy)!r}).write_text(json.dumps({{
    "sock": os.environ.get("NOVA_GATEKEEPER_SOCKET"),
}}))
print(json.dumps({{"event": "init", "conversation_id": "c-1"}}), flush=True)
res_obj = {{
    "event": "result",
    "result": {{"conversation_id": "c-1", "status": "SUCCESS", "response": "ok"}},
}}
print(json.dumps(res_obj), flush=True)
""",
        "utf-8",
    )
    agy_adapter = AntigravityCLIAdapter(
        AntigravityRunnerConfig(
            executable=sys.executable,
            extra_leading_args=(str(fake_agy),),
            gatekeeper_ipc_env={
                "NOVA_GATEKEEPER_SOCKET": "/tmp/synt-agy.sock",
                "NOVA_GATEKEEPER_TOKEN": "synt-tok-2",
            },
        )
    )
    events = [
        ev
        async for ev in agy_adapter.execute(
            CodingTaskRequest(
                correlation_id="c-agy",
                workspace_path=str(tmp_path),
                prompt="check",
            )
        )
    ]
    assert any(e.kind == "completed" for e in events)
    recorded_agy = json.loads(await asyncio.to_thread(dump_agy.read_text, "utf-8"))
    assert recorded_agy["sock"] == "/tmp/synt-agy.sock"


def test_nfr5_agy_plugin_validate_strict_passes() -> None:
    agy_bin = shutil.which("agy")
    assert agy_bin is not None, "agy binary must exist on reference Linux workstation"
    plugin_dir = REPO_ROOT / "src" / "nova" / "plugins" / "agy_gatekeeper"
    completed = subprocess.run(
        [agy_bin, "plugin", "validate", str(plugin_dir), "--strict"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"agy plugin validate failed: {completed.stdout} {completed.stderr}"
    )


@pytest.mark.asyncio
async def test_op5_pi_gatekeeper_ts_extension_executes_over_real_unix_socket(
    tmp_path: Path,
) -> None:
    node_bin = shutil.which("node")
    assert node_bin is not None, "node binary must exist on reference Linux workstation"

    ws = tmp_path / "Proyectos" / "ts-ws"
    os.makedirs(ws, exist_ok=True)
    bus = OrchestratorEventBus()
    resolver = LocalWorkspaceResolver(allowed_roots=[tmp_path / "Proyectos"], event_bus=bus)
    await resolver.resolve(
        ResolveWorkspaceRequest(correlation_id="c-ts", query=str(ws), mode="exact_path")
    )
    gatekeeper = SecurityGatekeeper(
        workspace_resolver=resolver,
        auditor=_StubAuditor(),
        event_bus=bus,
    )

    harness_mjs = tmp_path / "run_ext.mjs"
    await asyncio.to_thread(
        harness_mjs.write_text,
        f"""
import extFactory from {json.dumps(PI_GATEKEEPER_EXTENSION_PATH.as_uri())};

let handler = null;
const fakePi = {{
  on(ev, fn) {{
    if (ev === "tool_call") handler = fn;
  }},
}};
extFactory(fakePi);

const ctx = {{ cwd: {json.dumps(str(ws))} }};
const resWrite = await handler({{ toolName: "write", input: {{ path: "a.py" }} }}, ctx);
const resSafe = await handler({{ toolName: "bash", input: {{ command: "pytest -q" }} }}, ctx);
const resDeny = await handler({{ toolName: "bash", input: {{ command: "rm -rf /" }} }}, ctx);
console.log(JSON.stringify({{ resWrite: resWrite ?? null, resSafe: resSafe ?? null, resDeny }}));
""",
        "utf-8",
    )

    async with UnixSocketGatekeeperServer(gatekeeper) as ipc:
        env = {**os.environ, **ipc.child_env()}
        proc = await asyncio.create_subprocess_exec(
            node_bin,
            str(harness_mjs),
            cwd=str(ws),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        assert proc.returncode == 0, stderr_b.decode("utf-8", errors="replace")

    out = json.loads(stdout_b.decode("utf-8"))
    assert out["resWrite"] is None
    assert out["resSafe"] is None
    assert out["resDeny"]["block"] is True
    assert "CATASTROPHIC" in out["resDeny"]["reason"]
