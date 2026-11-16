"""Deterministic fake-subprocess tests for T2: PiRpcClient (AC-2, AC-6, OP-2)."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from nova.adapters.pi_rpc_client import PiRpcClient, PiRpcConfig
from nova.domain.errors import PiRpcError


def _write_fake_pi_script(script_path: Path, body: str) -> list[str]:
    script_path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, "-u", str(script_path)]


async def test_ac2_pi_rpc_streams_deltas_preserves_u2028_and_waits_for_agent_settled(
    tmp_path: Path,
) -> None:
    fake_pi = tmp_path / "fake_pi.py"
    argv = _write_fake_pi_script(
        fake_pi,
        """
        import json, sys

        # Verify default provider/model CLI flags are passed
        assert "--mode" in sys.argv and "rpc" in sys.argv
        assert "--provider" in sys.argv and "nvidia" in sys.argv

        while raw := sys.stdin.buffer.readline():
            req = json.loads(raw.decode("utf-8"))
            req_id = req.get("id")
            cmd = req.get("type")
            if cmd == "prompt":
                ack = {
                    "id": req_id,
                    "type": "response",
                    "command": "prompt",
                    "success": True,
                    "data": {"disposition": "started"},
                }
                sys.stdout.buffer.write((json.dumps(ack) + "\\n").encode("utf-8"))
                # Include U+2028 inside JSON string to verify strict LF framing
                ev1 = {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "delta": "Hola\u2028mundo",
                    },
                }
                line1 = json.dumps(ev1, ensure_ascii=False) + "\\n"
                line2 = json.dumps({"type": "agent_settled"}) + "\\n"
                sys.stdout.buffer.write(line1.encode("utf-8"))
                sys.stdout.buffer.write(line2.encode("utf-8"))
                sys.stdout.buffer.flush()
            elif cmd in ("clear_queue", "abort"):
                resp = {"id": req_id, "type": "response", "command": cmd, "success": True}
                sys.stdout.buffer.write((json.dumps(resp) + "\\n").encode("utf-8"))
                sys.stdout.buffer.flush()
        """,
    )

    config = PiRpcConfig(
        executable=argv[0],
        extra_leading_args=tuple(argv[1:]),
        workspace_path=str(tmp_path),
    )

    deltas: list[str] = []
    async with PiRpcClient(config) as client:
        async for delta in client.stream_prompt("Saluda al proyecto"):
            deltas.append(delta)
        await client.barge_in_abort()

    assert "".join(deltas) == "Hola\u2028mundo"


async def test_op2_pi_rpc_surfaces_rejected_command_code(tmp_path: Path) -> None:
    fake_pi = tmp_path / "fake_pi_reject.py"
    argv = _write_fake_pi_script(
        fake_pi,
        """
        import json, sys
        raw = sys.stdin.buffer.readline()
        req = json.loads(raw.decode("utf-8"))
        err = {
            "id": req.get("id"),
            "type": "response",
            "command": "prompt",
            "success": False,
            "error": "Model not found",
        }
        sys.stdout.buffer.write((json.dumps(err) + "\\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        """,
    )

    config = PiRpcConfig(
        executable=argv[0],
        extra_leading_args=tuple(argv[1:]),
        workspace_path=str(tmp_path),
    )

    async with PiRpcClient(config) as client:
        with pytest.raises(PiRpcError) as exc_info:
            async for _ in client.stream_prompt("Falla"):
                pass
        assert exc_info.value.code == "PI_RPC_COMMAND_REJECTED"


async def test_ac6_pi_rpc_handles_timeout_and_cleans_up_orphan_process(tmp_path: Path) -> None:
    fake_pi = tmp_path / "fake_pi_hang.py"
    argv = _write_fake_pi_script(
        fake_pi,
        """
        import json, sys, time
        raw = sys.stdin.buffer.readline()
        req = json.loads(raw.decode("utf-8"))
        ack = {
            "id": req.get("id"),
            "type": "response",
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
        sys.stdout.buffer.write((json.dumps(ack) + "\\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        time.sleep(30)
        """,
    )

    config = PiRpcConfig(
        executable=argv[0],
        extra_leading_args=tuple(argv[1:]),
        workspace_path=str(tmp_path),
        ack_timeout_seconds=1.0,
        run_timeout_seconds=0.25,
    )

    async with PiRpcClient(config) as client:
        with pytest.raises(PiRpcError) as exc_info:
            async for _ in client.stream_prompt("Cuelgate"):
                pass
        assert exc_info.value.code == "PI_RPC_SUBPROCESS_UNAVAILABLE"

    assert not client.is_running


async def test_f2_pi_rpc_recovers_cleanly_on_dead_process_retry_without_poisoned_queue(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "attempt.txt"
    fake_pi = tmp_path / "fake_pi_flaky.py"
    argv = _write_fake_pi_script(
        fake_pi,
        f"""
        import json, pathlib, sys

        marker = pathlib.Path({str(marker)!r})
        if not marker.exists():
            marker.write_text("1", encoding="utf-8")
            sys.exit(1)

        raw = sys.stdin.buffer.readline()
        req = json.loads(raw.decode("utf-8"))
        ack = {{
            "id": req.get("id"),
            "type": "response",
            "command": "prompt",
            "success": True,
            "data": {{"disposition": "started"}},
        }}
        ev = {{
            "type": "message_update",
            "assistantMessageEvent": {{"type": "text_delta", "delta": "Recuperado"}},
        }}
        sys.stdout.buffer.write((json.dumps(ack) + "\\n").encode("utf-8"))
        sys.stdout.buffer.write((json.dumps(ev) + "\\n").encode("utf-8"))
        sys.stdout.buffer.write((json.dumps({{"type": "agent_settled"}}) + "\\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        """,
    )

    config = PiRpcConfig(
        executable=argv[0],
        extra_leading_args=tuple(argv[1:]),
        workspace_path=str(tmp_path),
    )
    async with PiRpcClient(config) as client:
        chunks = [c async for c in client.stream_prompt("Reintenta")]
    assert "".join(chunks) == "Recuperado"


async def test_f3_pi_rpc_handles_jsonl_over_64kib_and_fails_cleanly_on_overrun(
    tmp_path: Path,
) -> None:
    fake_pi = tmp_path / "fake_pi_large.py"
    argv = _write_fake_pi_script(
        fake_pi,
        """
        import json, sys
        raw = sys.stdin.buffer.readline()
        req = json.loads(raw.decode("utf-8"))
        ack = {
            "id": req.get("id"),
            "type": "response",
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
        big_delta = "A" * 90000
        ev = {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": big_delta},
        }
        sys.stdout.buffer.write((json.dumps(ack) + "\\n").encode("utf-8"))
        sys.stdout.buffer.write((json.dumps(ev) + "\\n").encode("utf-8"))
        sys.stdout.buffer.write((json.dumps({"type": "agent_settled"}) + "\\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        """,
    )

    ok_cfg = PiRpcConfig(
        executable=argv[0],
        extra_leading_args=tuple(argv[1:]),
        workspace_path=str(tmp_path),
    )
    async with PiRpcClient(ok_cfg) as client:
        out = "".join([c async for c in client.stream_prompt("Grande")])
    assert len(out) == 90000

    tiny_limit_cfg = PiRpcConfig(
        executable=argv[0],
        extra_leading_args=tuple(argv[1:]),
        workspace_path=str(tmp_path),
        max_line_bytes=4096,
    )
    async with PiRpcClient(tiny_limit_cfg) as client2:
        with pytest.raises(PiRpcError) as exc_info:
            async for _ in client2.stream_prompt("Demasiado grande"):
                pass
        assert exc_info.value.code == "PI_RPC_SUBPROCESS_UNAVAILABLE"


async def test_nova012_ac4_pi_rpc_passes_headless_isolation_flags_and_default_model(
    tmp_path: Path,
) -> None:
    argv_dump = tmp_path / "rpc_argv.txt"
    fake_pi = tmp_path / "fake_pi_isolated.py"
    argv = _write_fake_pi_script(
        fake_pi,
        f"""
        import json, pathlib, sys
        pathlib.Path({str(argv_dump)!r}).write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
        raw = sys.stdin.buffer.readline()
        if raw:
            req = json.loads(raw.decode("utf-8"))
            ack = {{
                "id": req.get("id"),
                "type": "response",
                "command": "prompt",
                "success": True,
                "data": {{"disposition": "started"}},
            }}
            ev = {{
                "type": "message_update",
                "assistantMessageEvent": {{"type": "text_delta", "delta": "Hola"}},
            }}
            settled = {{"type": "agent_settled"}}
            sys.stdout.buffer.write((json.dumps(ack) + "\\n").encode("utf-8"))
            sys.stdout.buffer.write((json.dumps(ev) + "\\n").encode("utf-8"))
            sys.stdout.buffer.write((json.dumps(settled) + "\\n").encode("utf-8"))
            sys.stdout.buffer.flush()
        """,
    )

    cfg = PiRpcConfig(
        executable=argv[0],
        extra_leading_args=tuple(argv[1:]),
        workspace_path=str(tmp_path),
        gatekeeper_ipc_env={"NOVA_GATEKEEPER_SOCK": str(tmp_path / "gk.sock")},
    )
    assert cfg.model == "nvidia/nemotron-3.5-lightning-30b-a3b"

    async with PiRpcClient(cfg) as client:
        chunks = [c async for c in client.stream_prompt("Hola")]
    assert "".join(chunks) == "Hola"

    recorded = argv_dump.read_text(encoding="utf-8").splitlines()
    assert "--no-extensions" in recorded
    assert "--no-mcp" in recorded
    thinking_idx = recorded.index("--thinking")
    assert recorded[thinking_idx + 1] == "off"
    ext_idx = recorded.index("-e")
    assert recorded[ext_idx + 1].endswith("pi_gatekeeper.ts")


async def test_nova013_ac3_ac5_unscoped_mode_injects_no_tools_and_switches_to_isolated_workspace(
    tmp_path: Path,
) -> None:
    dump_file = tmp_path / "spawn_records.jsonl"
    fake_pi = tmp_path / "fake_pi_unscoped.py"
    argv = _write_fake_pi_script(
        fake_pi,
        f"""
        import json, os, pathlib, sys
        rec = {{
            "cwd": os.getcwd(),
            "argv": sys.argv[1:],
            "ws_active": os.environ.get("NOVA_WORKSPACE_ACTIVE"),
            "allowed_projects": os.environ.get("NOVA_ALLOWED_PROJECT_PATHS"),
        }}
        with pathlib.Path({str(dump_file)!r}).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\\n")
        while raw := sys.stdin.buffer.readline():
            req = json.loads(raw.decode("utf-8"))
            ack = {{
                "id": req.get("id"),
                "type": "response",
                "command": req.get("type", ""),
                "success": True,
                "data": {{"disposition": "started"}},
            }}
            sys.stdout.buffer.write((json.dumps(ack) + "\\n").encode("utf-8"))
            if req.get("type") == "prompt":
                ev = {{
                    "type": "message_update",
                    "assistantMessageEvent": {{"type": "text_delta", "delta": "ok"}},
                }}
                sys.stdout.buffer.write((json.dumps(ev) + "\\n").encode("utf-8"))
                settled = json.dumps({{"type": "agent_settled"}}) + "\\n"
                sys.stdout.buffer.write(settled.encode("utf-8"))
            sys.stdout.buffer.flush()
        """,
    )
    home_sim = tmp_path / "home_sim"
    home_sim.mkdir()
    ws_dir = tmp_path / "ws_alfa"
    proj_a = ws_dir / "repositories" / "api"
    proj_a.mkdir(parents=True)

    cfg = PiRpcConfig(
        executable=argv[0],
        extra_leading_args=tuple(argv[1:]),
        workspace_path=str(home_sim),
        workspace_active=False,
        gatekeeper_ipc_env={"NOVA_GATEKEEPER_SOCKET": str(tmp_path / "gk.sock")},
    )
    async with PiRpcClient(cfg) as client:
        _ = [c async for c in client.stream_prompt("Hola")]
        await client.switch_workspace(
            str(ws_dir),
            workspace_active=True,
            allowed_project_paths=(str(proj_a.resolve()),),
        )
        _ = [c async for c in client.stream_prompt("Analiza api")]

    import json as _json

    spawns = [_json.loads(line) for line in dump_file.read_text(encoding="utf-8").splitlines()]
    assert len(spawns) == 2
    assert spawns[0]["cwd"] == str(home_sim.resolve())
    assert "--no-tools" in spawns[0]["argv"]
    assert "--no-session" in spawns[0]["argv"]
    assert spawns[0]["ws_active"] == "0"

    assert spawns[1]["cwd"] == str(ws_dir.resolve())
    assert "--no-tools" not in spawns[1]["argv"]
    assert "--no-session" in spawns[1]["argv"]
    assert spawns[1]["ws_active"] == "1"
    assert _json.loads(spawns[1]["allowed_projects"]) == [str(proj_a.resolve())]

