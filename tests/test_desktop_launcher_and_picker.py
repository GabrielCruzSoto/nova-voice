"""TDD tests for NOVA-004 T5: unified nova-desktop launcher, CLI lifecycle, and safe browser."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from nova.adapters.desktop_launcher import (
    NovaDesktopConfig,
    NovaDesktopRuntime,
    build_arg_parser,
    config_from_namespace,
    open_local_browser_window,
)
from nova.adapters.pi_rpc_client import PiRpcClient, PiRpcConfig
from nova.domain.errors import DesktopUiError, VoicePipelineError
from nova.domain.models import CallSession

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = str(_REPO_ROOT / "src")
_NOVA_DESKTOP_BIN = str(_REPO_ROOT / "nova-desktop")


async def _raw_http_request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        req_headers = {"Host": f"127.0.0.1:{port}", "Connection": "close"}
        if headers:
            req_headers.update(headers)
        if body:
            req_headers.setdefault("Content-Length", str(len(body)))
        lines = [f"{method} {path} HTTP/1.1\r\n"]
        for k, v in req_headers.items():
            lines.append(f"{k}: {v}\r\n")
        lines.append("\r\n")
        writer.write("".join(lines).encode("latin-1") + body)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout=5.0)
    finally:
        writer.close()
        await writer.wait_closed()

    head_bytes, _, payload = raw.partition(b"\r\n\r\n")
    head_lines = head_bytes.decode("latin-1").split("\r\n")
    status_code = int(head_lines[0].split(" ", 2)[1])
    parsed_headers: dict[str, str] = {}
    for line in head_lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            parsed_headers[k.strip().lower()] = v.strip()
    return status_code, parsed_headers, payload


def _extract_cookie_pair(set_cookie_header: str) -> str:
    return set_cookie_header.split(";", 1)[0].strip()


def _path_exists(path: Path) -> bool:
    return path.exists()


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _list_sockets(sock_dir: Path) -> list[Path]:
    return list(sock_dir.glob("*.sock")) if sock_dir.exists() else []


def test_s4_cli_arg_parser_builds_validated_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo-alpha"
    repo.mkdir()
    parser = build_arg_parser()
    ns = parser.parse_args(
        [
            "--port",
            "0",
            "--allowed-root",
            str(tmp_path),
            "--workspace",
            str(repo),
            "--no-browser",
            "--no-voice",
            "--browser",
            "firefox",
        ]
    )
    cfg = config_from_namespace(ns)
    assert cfg.port == 0
    assert cfg.allowed_roots == (str(tmp_path),)
    assert cfg.initial_workspace == str(repo)
    assert cfg.open_browser is False
    assert cfg.enable_voice is False
    assert cfg.browser_preference == "firefox"


async def test_ac6_desktop_runtime_degrades_gracefully_without_voice_and_serves_full_ui(
    tmp_path: Path,
) -> None:
    root_dir = tmp_path / "projects"
    root_dir.mkdir()
    repo_a = root_dir / "alpha-service"
    repo_a.mkdir()
    repo_b = root_dir / "beta-service"
    repo_b.mkdir()
    sock_dir = tmp_path / "sock"

    def _failing_voice_factory(**_: Any) -> Any:
        raise VoicePipelineError(
            code="VOICE_MODEL_UNAVAILABLE",
            message="Synthetic missing optional [voice] package in test.",
        )

    cfg = NovaDesktopConfig(
        port=0,
        allowed_roots=(str(root_dir),),
        initial_workspace=str(repo_a),
        open_browser=False,
        enable_voice=True,
        ipc_runtime_dir=str(sock_dir),
        pi_executable="nonexistent-pi-binary-for-lazy-test",
    )

    runtime = NovaDesktopRuntime(cfg, voice_factory=_failing_voice_factory)
    async with runtime:
        assert runtime.port > 0
        assert runtime.voice_enabled is False
        sock_path = Path(runtime.gatekeeper_socket_path)
        assert _path_exists(sock_path)
        mode = stat.S_IMODE(os.lstat(sock_path).st_mode)
        assert mode == 0o600

        # 1. Bootstrap GET / issues HttpOnly; SameSite=Strict cookie
        st_index, hdr_index, body_index = await _raw_http_request(runtime.port, "GET", "/")
        assert st_index == 200
        assert b"Nova" in body_index
        cookie_pair = _extract_cookie_pair(hdr_index["set-cookie"])

        # 2. Static ES module bundle is served from package static dir
        st_js, hdr_js, body_js = await _raw_http_request(runtime.port, "GET", "/static/app.js")
        assert st_js == 200
        assert "javascript" in hdr_js["content-type"]
        assert len(body_js) > 50

        # 3. Authenticated GET /api/state reflects active_workspace and voice_enabled=False
        st_state, _, body_state = await _raw_http_request(
            runtime.port,
            "GET",
            "/api/state",
            headers={"Cookie": cookie_pair},
        )
        assert st_state == 200
        snap = json.loads(body_state.decode("utf-8"))
        assert snap["voice_enabled"] is False
        assert snap["active_workspace"]["display_name"] == "alpha-service"
        csrf_token = snap["csrf_token"]

        # 4. Switching workspace via POST /api/workspace/select succeeds lazily without pi binary
        st_ws, _, body_ws = await _raw_http_request(
            runtime.port,
            "POST",
            "/api/workspace/select",
            headers={
                "Cookie": cookie_pair,
                "Origin": runtime.base_url,
                "X-Nova-CSRF": csrf_token,
                "Content-Type": "application/json",
            },
            body=json.dumps({"path_or_query": str(repo_b)}).encode("utf-8"),
        )
        assert st_ws == 200
        ws_payload = json.loads(body_ws.decode("utf-8"))
        assert ws_payload["workspace"]["display_name"] == "beta-service"

    # After exiting context, gatekeeper socket is removed and server is closed
    assert not _path_exists(sock_path)


async def test_ac6_desktop_runtime_starts_and_shuts_down_live_voice_coordinator(
    tmp_path: Path,
) -> None:
    class _FakeVoiceRuntime:
        def __init__(self) -> None:
            self._session = CallSession(
                call_id="call-live-1",
                state="standby",
                turn_count=0,
                last_activity_monotonic=0.0,
            )
            self.started = False
            self.shut_down = False

        @property
        def session(self) -> CallSession:
            return self._session

        async def start(self) -> None:
            self.started = True

        async def start_call(self, reason: str = "ui_call_start") -> None:
            self._session = CallSession(
                call_id=self._session.call_id,
                state="listening",
                turn_count=self._session.turn_count,
                last_activity_monotonic=1.0,
            )

        async def end_call(self, reason: str = "ui_call_end") -> None:
            self._session = CallSession(
                call_id=self._session.call_id,
                state="standby",
                turn_count=self._session.turn_count,
                last_activity_monotonic=2.0,
            )

        async def barge_in(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.shut_down = True

    fake_voice = _FakeVoiceRuntime()
    cfg = NovaDesktopConfig(
        port=0,
        allowed_roots=(str(tmp_path),),
        open_browser=False,
        enable_voice=True,
        ipc_runtime_dir=str(tmp_path / "sock"),
    )

    async with NovaDesktopRuntime(cfg, voice_factory=lambda **_: fake_voice) as runtime:
        assert runtime.voice_enabled is True
        assert fake_voice.started is True

    assert fake_voice.shut_down is True


def _setup_fake_pi_workspace_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    cwd_log = tmp_path / "cwds.txt"
    fake_pi = tmp_path / "fake_pi_cwd.py"
    fake_pi.write_text(
        textwrap.dedent(
            f"""
            import json, os, pathlib, sys
            log_file = pathlib.Path({str(cwd_log)!r})
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(os.getcwd() + "\\n")
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
                    ev = {{"type": "agent_settled"}}
                    sys.stdout.buffer.write((json.dumps(ev) + "\\n").encode("utf-8"))
                sys.stdout.buffer.flush()
            """
        ),
        encoding="utf-8",
    )
    return ws1, ws2, cwd_log, fake_pi


async def test_pi_rpc_switch_workspace_restarts_when_running_and_stays_lazy_when_idle(
    tmp_path: Path,
) -> None:
    ws1, ws2, cwd_log, fake_pi = _setup_fake_pi_workspace_fixture(tmp_path)

    client = PiRpcClient(
        PiRpcConfig(
            executable=sys.executable,
            extra_leading_args=("-u", str(fake_pi)),
            workspace_path=str(ws1),
        )
    )
    try:
        # 1. Idle switch_workspace updates target cwd without spawning subprocess eagerly
        await client.switch_workspace(str(ws2))
        assert not client.is_running
        assert not _path_exists(cwd_log)

        # 2. First prompt spawns inside ws2
        async for _ in client.stream_prompt("hola"):
            pass
        assert client.is_running

        # 3. Switching while running restarts immediately inside ws1
        await client.switch_workspace(str(ws1))
        assert client.is_running
        async for _ in client.stream_prompt("hola 2"):
            pass
    finally:
        await client.close()

    recorded_cwds = _read_lines(cwd_log)
    assert len(recorded_cwds) == 2
    assert Path(recorded_cwds[0]) == ws2
    assert Path(recorded_cwds[1]) == ws1


async def test_s4_open_local_browser_window_enforces_loopback_and_shell_false() -> None:
    spawned: list[tuple[str, ...]] = []

    class _DummyProc:
        async def wait(self) -> int:
            return 0

    async def _fake_spawner(*argv: str) -> _DummyProc:
        spawned.append(argv)
        return _DummyProc()

    # Reject non-loopback or flag-injection URLs
    with pytest.raises(DesktopUiError) as exc_info:
        await open_local_browser_window(
            "https://evil.example/phish",
            env={"DISPLAY": ":0"},
            which_resolver=lambda name: f"/usr/bin/{name}",
            spawner=_fake_spawner,
        )
    assert exc_info.value.code == "UI_INVALID_BROWSER_URL"

    # Headless environment skips cleanly without raising
    opened_headless = await open_local_browser_window(
        "http://127.0.0.1:8765",
        env={},
        which_resolver=lambda name: f"/usr/bin/{name}",
        spawner=_fake_spawner,
    )
    assert opened_headless is False
    assert spawned == []

    # Firefox preference invokes firefox --new-window with exact argv (no shell)
    opened_ff = await open_local_browser_window(
        "http://127.0.0.1:8765",
        preference="firefox",
        env={"WAYLAND_DISPLAY": "wayland-0"},
        which_resolver=lambda name: "/usr/bin/firefox" if name == "firefox" else None,
        spawner=_fake_spawner,
    )
    assert opened_ff is True
    assert spawned == [("/usr/bin/firefox", "--new-window", "http://127.0.0.1:8765")]


async def test_ac6_python_m_nova_cli_starts_and_exits_cleanly_on_sigterm(
    tmp_path: Path,
) -> None:
    child_env = {**os.environ, "PYTHONPATH": _SRC_DIR}

    # 1. --help exits 0 for both `python -m nova` and `./nova-desktop`
    for cmd_argv in ((sys.executable, "-m", "nova", "--help"), (_NOVA_DESKTOP_BIN, "--help")):
        help_proc = await asyncio.create_subprocess_exec(
            *cmd_argv,
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        help_out, _ = await asyncio.wait_for(help_proc.communicate(), timeout=5.0)
        assert help_proc.returncode == 0
        assert b"--no-browser" in help_out

    # 2. Live daemon starts on 127.0.0.1, emits readiness URL, and shuts down cleanly on SIGTERM
    sock_dir = tmp_path / "cli_sock"
    daemon_proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-m",
        "nova",
        "--no-browser",
        "--no-voice",
        "--port",
        "0",
        "--allowed-root",
        str(tmp_path),
        "--ipc-runtime-dir",
        str(sock_dir),
        env=child_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert daemon_proc.stdout is not None
        ready_line = await asyncio.wait_for(daemon_proc.stdout.readline(), timeout=5.0)
        decoded = ready_line.decode("utf-8")
        assert "http://127.0.0.1:" in decoded

        daemon_proc.send_signal(signal.SIGTERM)
        exit_code = await asyncio.wait_for(daemon_proc.wait(), timeout=5.0)
        assert exit_code == 0
    finally:
        if daemon_proc.returncode is None:
            daemon_proc.kill()
            await daemon_proc.wait()

    # Socket file inside sock_dir must be cleaned up on graceful shutdown
    remaining_sockets = _list_sockets(sock_dir)
    assert remaining_sockets == []
