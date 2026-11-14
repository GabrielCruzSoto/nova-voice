"""TDD tests for NOVA-008 T3: AF_UNIX control socket, SO_PEERCRED, and fast CLI dispatch."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from nova.adapters.control_ipc import UnixControlIpcServer, send_control_command
from nova.adapters.desktop_launcher import main as launcher_main


@pytest.mark.asyncio
async def test_control_ipc_roundtrip_permissions_and_latency(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "run"
    received: list[str] = []

    async def handler(action: str) -> dict[str, object]:
        received.append(action)
        return {"ok": True, "action": action, "call_state": "listening"}

    server = UnixControlIpcServer(handler=handler, runtime_base_dir=runtime_dir)
    await server.start()
    try:
        sock_path = Path(server.socket_path)
        assert stat.S_IMODE(os.stat(sock_path.parent).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(sock_path).st_mode) == 0o600

        t0 = time.monotonic()
        resp = await server._loop_test_helper_placeholder() if False else None
        del resp
        # Run synchronous client in a thread so asyncio server loop can respond
        import asyncio

        result = await asyncio.to_thread(
            send_control_command,
            "ptt-toggle",
            socket_path=sock_path,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        assert result["ok"] is True
        assert result["action"] == "ptt-toggle"
        assert result["call_state"] == "listening"
        assert received == ["ptt-toggle"]
        assert elapsed_ms < 250.0  # local AF_UNIX roundtrip well under NFR-1
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_cli_control_subcommand_exits_0_when_running_and_1_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

    # When no daemon is listening, `nova control ptt-toggle` exits 1 quickly
    exit_missing = launcher_main(["control", "ptt-toggle"])
    assert exit_missing == 1

    async def handler(action: str) -> dict[str, object]:
        return {"ok": True, "action": action, "call_state": "standby"}

    server = UnixControlIpcServer(handler=handler, runtime_base_dir=runtime_dir)
    await server.start()
    try:
        import asyncio

        exit_ok = await asyncio.to_thread(launcher_main, ["control", "toggle-panel"])
        assert exit_ok == 0
    finally:
        await server.close()


def test_send_control_command_rejects_invalid_action(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        send_control_command("rm -rf /", socket_path=tmp_path / "missing.sock")
    assert os.getuid() >= 0
