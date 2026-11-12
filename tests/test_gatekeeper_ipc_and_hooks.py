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

        # 4. F2 regression: toolCall.args.Cwd outside workspace MUST escalate (not auto-allow!)
        outside_cwd_stdin = json.dumps(
            {
                "workspacePaths": [str(ws)],
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": "pytest -q", "Cwd": str(tmp_path)},
                },
            }
        )
        # Configure short approval timeout on a dedicated gatekeeper to observe fail-closed deny
        short_gk = SecurityGatekeeper(
            workspace_resolver=resolver,
            auditor=_StubAuditor(),
            event_bus=bus,
            settings=GatekeeperSettings(approval_timeout_seconds=0.15),
        )
        async with UnixSocketGatekeeperServer(short_gk) as short_ipc:
            short_env = {**clean_env, **short_ipc.child_env()}
            proc_cwd_escape = await asyncio.create_subprocess_exec(
                sys.executable,
                str(hook_script),
                cwd=str(plugin_dir),
                env=short_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            out_cwd_escape, _ = await proc_cwd_escape.communicate(
                outside_cwd_stdin.encode("utf-8")
            )
            parsed_cwd_escape = json.loads(out_cwd_escape.decode("utf-8"))
            assert parsed_cwd_escape["decision"] == "deny"


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
    real_plugin_dir = str(REPO_ROOT / "src" / "nova" / "plugins" / "agy_gatekeeper")
    await asyncio.to_thread(
        fake_agy.write_text,
        f"""
import json, os, pathlib, sys
if sys.argv[1:4] == ["plugin", "list", "--json"]:
    print(json.dumps([{{
        "name": "nova-agy-gatekeeper",
        "enabled": True,
        "path": {real_plugin_dir!r},
    }}]), flush=True)
    raise SystemExit(0)
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
    outside_dir = tmp_path / "outside-target"
    os.makedirs(outside_dir, exist_ok=True)
    os.symlink(outside_dir, ws / "linked_out")
    os.symlink(outside_dir, ws / "@at_link")
    os.symlink(outside_dir, ws / " space_link")
    os.symlink(outside_dir / "secret.png", ws / "Capture 10.00\u202FAM.png")
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
const resWriteInside = await handler({{ toolName: "write", input: {{ path: "a.py" }} }}, ctx);
const resReadOutside = await handler(
  {{ toolName: "read", input: {{ path: "../../outside.txt" }} }},
  ctx,
);
const resWriteHome = await handler(
  {{ toolName: "write", input: {{ path: "~/.bashrc" }} }},
  ctx,
);
const resWriteSymlinkDir = await handler(
  {{ toolName: "write", input: {{ path: "linked_out/pwn.txt" }} }},
  ctx,
);
const resReadAtAbs = await handler(
  {{ toolName: "read", input: {{ path: "@/etc/passwd" }} }},
  ctx,
);
const resWriteAtHome = await handler(
  {{ toolName: "write", input: {{ path: "@~/.bashrc" }} }},
  ctx,
);
const resReadAtRel = await handler(
  {{ toolName: "read", input: {{ path: "@\u00A0../../outside.txt" }} }},
  ctx,
);
const resReadFileUri = await handler(
  {{ toolName: "read", input: {{ path: "file:///etc/passwd" }} }},
  ctx,
);
const resReadDoubleAtLink = await handler(
  {{ toolName: "read", input: {{ path: "@@at_link/passwd" }} }},
  ctx,
);
const resReadLeadingSpaceLink = await handler(
  {{ toolName: "read", input: {{ path: " space_link/passwd" }} }},
  ctx,
);
const resReadAtSpaceLink = await handler(
  {{ toolName: "read", input: {{ path: "@ space_link/passwd" }} }},
  ctx,
);
const resReadAmPmVariant = await handler(
  {{ toolName: "read", input: {{ path: "Capture 10.00 AM.png" }} }},
  ctx,
);
const resPowershellDeny = await handler(
  {{ toolName: "powershell", input: {{ command: "rm -rf /" }} }},
  ctx,
);
const resUnknownTool = await handler(
  {{ toolName: "custom_untrusted_exec", input: {{ script: "id" }} }},
  ctx,
);
const resSafe = await handler({{ toolName: "bash", input: {{ command: "pytest -q" }} }}, ctx);
const resDeny = await handler({{ toolName: "bash", input: {{ command: "rm -rf /" }} }}, ctx);
console.log(JSON.stringify({{
  resWriteInside: resWriteInside ?? null,
  resReadOutside,
  resWriteHome,
  resWriteSymlinkDir,
  resReadAtAbs,
  resWriteAtHome,
  resReadAtRel,
  resReadFileUri,
  resReadDoubleAtLink,
  resReadLeadingSpaceLink,
  resReadAtSpaceLink,
  resReadAmPmVariant,
  resPowershellDeny,
  resUnknownTool,
  resSafe: resSafe ?? null,
  resDeny,
}}));
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
    assert out["resWriteInside"] is None
    assert out["resReadOutside"]["block"] is True
    assert out["resWriteHome"]["block"] is True
    assert out["resWriteSymlinkDir"]["block"] is True
    assert out["resReadAtAbs"]["block"] is True
    assert out["resWriteAtHome"]["block"] is True
    assert out["resReadAtRel"]["block"] is True
    assert out["resReadFileUri"]["block"] is True
    assert out["resReadDoubleAtLink"]["block"] is True
    assert out["resReadLeadingSpaceLink"]["block"] is True
    assert out["resReadAtSpaceLink"]["block"] is True
    assert out["resReadAmPmVariant"]["block"] is True
    assert out["resPowershellDeny"]["block"] is True
    assert "CATASTROPHIC" in out["resPowershellDeny"]["reason"]
    assert out["resUnknownTool"]["block"] is True
    assert out["resSafe"] is None
    assert out["resDeny"]["block"] is True
    assert "CATASTROPHIC" in out["resDeny"]["reason"]


@pytest.mark.asyncio
async def test_f9_early_ipc_client_disconnect_releases_pending_approval_slot(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "Proyectos" / "f9-ws"
    os.makedirs(ws, exist_ok=True)
    bus = OrchestratorEventBus()
    resolver = LocalWorkspaceResolver(allowed_roots=[tmp_path / "Proyectos"], event_bus=bus)
    await resolver.resolve(
        ResolveWorkspaceRequest(correlation_id="c-f9", query=str(ws), mode="exact_path")
    )
    gatekeeper = SecurityGatekeeper(
        workspace_resolver=resolver,
        auditor=_StubAuditor(),
        event_bus=bus,
        settings=GatekeeperSettings(approval_timeout_seconds=30.0, max_pending_approvals=1),
    )

    entered_pending = asyncio.Event()

    async def on_event(ev: OrchestratorEvent) -> None:
        if isinstance(ev, CommandApprovalRequestedEvent):
            entered_pending.set()

    bus.subscribe(on_event)

    async with UnixSocketGatekeeperServer(gatekeeper) as ipc:
        env = ipc.child_env()
        _, writer = await asyncio.open_unix_connection(ipc.socket_path)
        req_bytes = (
            json.dumps(
                {
                    "protocol_version": "1.0",
                    "token": env["NOVA_GATEKEEPER_TOKEN"],
                    "correlation_id": "c-drop",
                    "source": "tier1_pi",
                    "tool_name": "bash",
                    "command": "sudo whoami",
                    "cwd": str(ws),
                }
            )
            + "\n"
        ).encode()
        writer.write(req_bytes)
        await writer.drain()
        await asyncio.wait_for(entered_pending.wait(), timeout=2.0)

        # Simulate abrupt client disconnect (e.g. barge-in abort)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        assert gatekeeper.pending_count == 0


@pytest.mark.asyncio
async def test_f3_and_f4_agy_plugin_preflight_and_non_dict_hook_json(tmp_path: Path) -> None:
    from nova.domain.errors import CodingRunnerError

    hook_script = REPO_ROOT / "src" / "nova" / "plugins" / "agy_gatekeeper" / "pre_tool_hook.py"
    plugin_dir = hook_script.parent
    live_like_env = {
        **os.environ,
        "NOVA_GATEKEEPER_SOCKET": "/tmp/nonexistent-nova-gk2.sock",
        "NOVA_GATEKEEPER_TOKEN": "tok",
    }

    for scalar_or_list_stdin in ("[]", "null", '"just-a-string"', "123"):
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(hook_script),
            cwd=str(plugin_dir),
            env=live_like_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        out_b, _ = await proc.communicate(scalar_or_list_stdin.encode("utf-8"))
        assert proc.returncode == 0
        parsed = json.loads(out_b.decode("utf-8"))
        assert parsed["decision"] == "deny"

    fake_agy_missing = tmp_path / "fake_agy_missing_plugin.py"
    await asyncio.to_thread(
        fake_agy_missing.write_text,
        """
import json, sys
if sys.argv[1:4] == ["plugin", "list", "--json"]:
    print(json.dumps([{"name": "other-plugin", "enabled": True}]))
    raise SystemExit(0)
print(json.dumps({"event": "init", "conversation_id": "c-x"}))
""",
        "utf-8",
    )
    missing_adapter = AntigravityCLIAdapter(
        AntigravityRunnerConfig(
            executable=sys.executable,
            extra_leading_args=(str(fake_agy_missing),),
            gatekeeper_ipc_env={
                "NOVA_GATEKEEPER_SOCKET": "/tmp/synt.sock",
                "NOVA_GATEKEEPER_TOKEN": "synt-tok",
            },
        )
    )
    with pytest.raises(CodingRunnerError) as exc_info:
        _ = [
            ev
            async for ev in missing_adapter.execute(
                CodingTaskRequest(
                    correlation_id="c-miss",
                    workspace_path=str(tmp_path),
                    prompt="check",
                )
            )
        ]
    assert exc_info.value.code == "GATEKEEPER_PLUGIN_MISSING"

    # F6 regression: truncated IPC response without '\n' or 'null' JSON in pre_tool_hook / pi ext
    raw_sock_path = str(tmp_path / "trunc.sock")

    async def _handle_truncated(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.readline()
        writer.write(b'{"allowed": true}')  # deliberately missing trailing '\n'!
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    trunc_server = await asyncio.start_unix_server(_handle_truncated, path=raw_sock_path)
    try:
        trunc_env = {
            **os.environ,
            "NOVA_GATEKEEPER_SOCKET": raw_sock_path,
            "NOVA_GATEKEEPER_TOKEN": "tok",
        }
        valid_stdin = json.dumps(
            {
                "workspacePaths": [str(tmp_path)],
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pwd"}},
            }
        )
        proc_trunc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(hook_script),
            cwd=str(plugin_dir),
            env=trunc_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        out_trunc, _ = await proc_trunc.communicate(valid_stdin.encode("utf-8"))
        assert proc_trunc.returncode == 0
        assert json.loads(out_trunc.decode("utf-8"))["decision"] == "deny"
    finally:
        trunc_server.close()
        await trunc_server.wait_closed()

    null_sock_path = str(tmp_path / "null.sock")

    async def _handle_null(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.readline()
        writer.write(b"null\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    null_server = await asyncio.start_unix_server(_handle_null, path=null_sock_path)
    try:
        node_bin = shutil.which("node")
        assert node_bin is not None
        null_mjs = tmp_path / "null_ext.mjs"
        await asyncio.to_thread(
            null_mjs.write_text,
            f"""
import extFactory from {json.dumps(PI_GATEKEEPER_EXTENSION_PATH.as_uri())};
let handler = null;
extFactory({{ on(ev, fn) {{ if (ev === "tool_call") handler = fn; }} }});
const res = await handler(
  {{ toolName: "bash", input: {{ command: "pwd" }} }},
  {{ cwd: {json.dumps(str(tmp_path))} }},
);
console.log(JSON.stringify(res));
""",
            "utf-8",
        )
        proc_null = await asyncio.create_subprocess_exec(
            node_bin,
            str(null_mjs),
            cwd=str(tmp_path),
            env={
                **os.environ,
                "NOVA_GATEKEEPER_SOCKET": null_sock_path,
                "NOVA_GATEKEEPER_TOKEN": "tok",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_null, err_null = await proc_null.communicate()
        assert proc_null.returncode == 0, err_null.decode("utf-8", errors="replace")
        assert json.loads(out_null.decode("utf-8"))["block"] is True
    finally:
        null_server.close()
        await null_server.wait_closed()
