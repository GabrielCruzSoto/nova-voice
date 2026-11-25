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
        config_path=str(tmp_path / "config_ac6_degraded.toml"),
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
        config_path=str(tmp_path / "config_live_voice.toml"),
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
    opened_webview = await open_local_browser_window(
        "http://127.0.0.1:8765",
        preference="webview",
        env={"WAYLAND_DISPLAY": "wayland-0"},
        spawner=_fake_spawner,
    )
    assert opened_webview is True
    assert spawned[-1] == (
        sys.executable,
        "-m",
        "nova.adapters.desktop_window",
        "http://127.0.0.1:8765",
    )


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
        "--config",
        str(tmp_path / "cli_config.toml"),
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


async def test_ac1_auto_preference_prefers_native_shell_and_falls_back() -> None:
    spawned: list[tuple[str, ...]] = []

    class _DummyProc:
        async def wait(self) -> int:
            return 0

    async def _fake_spawner(*argv: str) -> _DummyProc:
        spawned.append(argv)
        return _DummyProc()

    # 1. When native desktop shell is available, preference="auto" launches desktop_window
    opened_native = await open_local_browser_window(
        "http://127.0.0.1:8765",
        preference="auto",
        env={"WAYLAND_DISPLAY": "wayland-0"},
        which_resolver=lambda name: f"/usr/bin/{name}",
        spawner=_fake_spawner,
        native_shell_available=lambda: True,
    )
    assert opened_native is True
    assert list(spawned[-1]) == [
        sys.executable,
        "-m",
        "nova.adapters.desktop_window",
        "http://127.0.0.1:8765",
    ]

    # 2. When native shell is unavailable, preference="auto" falls back to xdg-open -> firefox
    opened_fallback = await open_local_browser_window(
        "http://127.0.0.1:8765",
        preference="auto",
        env={"WAYLAND_DISPLAY": "wayland-0"},
        which_resolver=lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
        spawner=_fake_spawner,
        native_shell_available=lambda: False,
    )
    assert opened_fallback is True
    assert list(spawned[-1]) == ["/usr/bin/xdg-open", "http://127.0.0.1:8765"]


async def test_ac10_runtime_close_terminates_and_reaps_spawned_ui_subprocess_without_orphans(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "ui_child.pid"
    sleeper_script = tmp_path / "fake_ui_sleeper.py"
    sleeper_script.write_text(
        textwrap.dedent(
            f"""
            import os, pathlib, time
            pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(60)
            """
        ),
        encoding="utf-8",
    )

    async def _real_detached_spawner(*_argv: str) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable,
            str(sleeper_script),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

    cfg = NovaDesktopConfig(
        port=0,
        allowed_roots=(str(tmp_path),),
        open_browser=True,
        browser_preference="webview",
        enable_voice=False,
        ipc_runtime_dir=str(tmp_path / "sock"),
        config_path=str(tmp_path / "config_ac10.toml"),
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("WAYLAND_DISPLAY", "wayland-0")
        async with NovaDesktopRuntime(cfg, browser_spawner=_real_detached_spawner):
            for _ in range(50):
                if _path_exists(pid_file):
                    break
                await asyncio.sleep(0.05)
            assert _path_exists(pid_file), "Fake UI child must have started and written its PID"
            child_pid = int(pid_file.read_text(encoding="utf-8").strip())
            # Process is alive while runtime is active
            os.kill(child_pid, 0)

    try:
        # Immediately after runtime.close(), the detached UI child process must be dead (no orphan)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_runtime_runs_preflight_health_check_and_publishes_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: empty_home)
    from nova.domain.events import RunnerFailureEvent

    cfg = NovaDesktopConfig(
        port=0,
        allowed_roots=(str(tmp_path),),
        open_browser=False,
        enable_voice=False,
        pi_executable="non_existent_pi_binary",
        ipc_runtime_dir=str(tmp_path / "sock"),
        config_path=str(tmp_path / "config_preflight.toml"),
    )

    events: list[object] = []

    async def collect(ev: object) -> None:
        events.append(ev)

    runtime = NovaDesktopRuntime(
        cfg,
        which_resolver=lambda _c: None,
    )
    runtime.event_bus.subscribe(collect)

    async with runtime:
        assert runtime.health_report is not None
        assert runtime.health_report.all_ok is False
        assert runtime.health_report.missing_critical_count >= 1

        failures = [e for e in events if isinstance(e, RunnerFailureEvent)]
        assert len(failures) == 1
        assert failures[0].error_code == "COMPONENTS_MISSING"
        assert "agent_runtime" in failures[0].sanitized_message


async def test_run_desktop_daemon_exits_cleanly_via_http_shutdown_and_control_quit(
    tmp_path: Path,
) -> None:
    from nova.adapters.control_ipc import resolve_control_socket_path, send_control_command
    from nova.adapters.desktop_launcher import run_desktop_daemon

    # 1. Shutdown via `nova control quit` (AC-4 & AC-6)
    sock_dir_ipc = tmp_path / "sock_ipc"
    cfg_ipc = NovaDesktopConfig(
        port=0,
        allowed_roots=(str(tmp_path),),
        open_browser=False,
        enable_voice=False,
        ipc_runtime_dir=str(sock_dir_ipc),
        config_path=str(tmp_path / "config_ipc.toml"),
    )
    control_sock = resolve_control_socket_path(runtime_base_dir=sock_dir_ipc)
    daemon_task = asyncio.create_task(run_desktop_daemon(cfg_ipc))
    try:
        for _ in range(50):
            if _path_exists(control_sock):
                break
            await asyncio.sleep(0.05)
        assert _path_exists(control_sock)

        resp = await asyncio.to_thread(
            send_control_command,
            "quit",
            socket_path=control_sock,
        )
        assert resp == {
            "schema": "nova.control.ipc.v1",
            "ok": True,
            "action": "quit",
            "status": "shutting_down",
        }
        rc = await asyncio.wait_for(daemon_task, timeout=5.0)
        assert rc == 0
        assert not _path_exists(control_sock)
    finally:
        if not daemon_task.done():
            daemon_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await daemon_task

    # 2. Shutdown via `POST /api/shutdown` with voice disabled (AC-1 & AC-6)
    sock_dir_http = tmp_path / "sock_http"
    cfg_http = NovaDesktopConfig(
        port=0,
        allowed_roots=(str(tmp_path),),
        open_browser=False,
        enable_voice=False,
        ipc_runtime_dir=str(sock_dir_http),
        config_path=str(tmp_path / "config_http.toml"),
    )
    runtime = NovaDesktopRuntime(cfg_http)
    async with runtime:
        import http.client

        def _send_http_shutdown(port: int, base_url: str) -> tuple[int, dict[str, object]]:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
            try:
                conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
                r_idx = conn.getresponse()
                cookie = (r_idx.getheader("Set-Cookie") or "").split(";")[0]
                r_idx.read()
                conn.request(
                    "GET",
                    "/api/state",
                    headers={"Host": f"127.0.0.1:{port}", "Cookie": cookie},
                )
                r_st = conn.getresponse()
                csrf = json.loads(r_st.read().decode("utf-8"))["csrf_token"]
                conn.request(
                    "POST",
                    "/api/shutdown",
                    body=b"{}",
                    headers={
                        "Host": f"127.0.0.1:{port}",
                        "Origin": base_url,
                        "Cookie": cookie,
                        "X-Nova-CSRF": csrf,
                        "Content-Type": "application/json",
                    },
                )
                r_sh = conn.getresponse()
                parsed: dict[str, object] = json.loads(r_sh.read().decode("utf-8"))
                return (r_sh.status, parsed)
            finally:
                conn.close()

        status_code, body_dict = await asyncio.to_thread(
            _send_http_shutdown, runtime.port, runtime.base_url
        )
        assert status_code == 202
        assert body_dict == {"status": "shutting_down"}

        await asyncio.wait_for(runtime.wait_for_shutdown(), timeout=2.0)
        # Idempotent repeated request_shutdown
        runtime.request_shutdown("duplicate_check")
        assert runtime.shutdown_requested is True


def test_nova012_ac4_config_from_namespace_resolves_cli_fallbacks_and_default_model(
    tmp_path: Path,
) -> None:
    from nova.adapters.desktop_launcher import build_arg_parser, config_from_namespace

    fake_home = tmp_path / "home"
    pi_fallback = fake_home / ".pi" / "agent" / "bin" / "pi"
    agy_fallback = fake_home / ".local" / "bin" / "agy"
    pi_fallback.parent.mkdir(parents=True, exist_ok=True)
    agy_fallback.parent.mkdir(parents=True, exist_ok=True)
    pi_fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pi_fallback.chmod(0o755)
    agy_fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agy_fallback.chmod(0o755)

    parser = build_arg_parser()
    ns = parser.parse_args(
        [
            "--config",
            str(tmp_path / "config.toml"),
            "--allowed-root",
            str(tmp_path),
            "--no-voice",
        ]
    )
    cfg = config_from_namespace(
        ns,
        env={},
        which_resolver=lambda _name: None,
        home_dir=fake_home,
    )
    assert cfg.pi_model == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert cfg.pi_executable == str(pi_fallback)
    assert cfg.agy_executable == str(agy_fallback)


async def test_nova012_ac5_runtime_tests_never_touch_default_home_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "protected_home"
    host_cfg = fake_home / ".config" / "nova" / "config.toml"
    host_cfg.parent.mkdir(parents=True, exist_ok=True)
    sentinel = 'schema_version = "1.0"\n[general]\nlast_active_workspace = "/keep/intact"\n'
    host_cfg.write_text(sentinel, encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # Even if a caller omits config_path inside pytest, autouse XDG_CONFIG_HOME isolation
    # must prevent touching ~/.config/nova/config.toml, AND explicit config_path writes
    # exclusively to the isolated file.
    proj = tmp_path / "isolated_proj"
    proj.mkdir()
    isolated_cfg_file = tmp_path / "local_cfg" / "config.toml"
    cfg = NovaDesktopConfig(
        port=0,
        allowed_roots=(str(tmp_path),),
        initial_workspace=str(proj),
        open_browser=False,
        enable_voice=False,
        ipc_runtime_dir=str(tmp_path / "sock"),
        config_path=str(isolated_cfg_file),
    )
    async with NovaDesktopRuntime(cfg) as runtime:
        st_idx, hdr_idx, _ = await _raw_http_request(runtime.port, "GET", "/")
        assert st_idx == 200
        cookie_pair = _extract_cookie_pair(hdr_idx["set-cookie"])
        _, _, raw_state = await _raw_http_request(
            runtime.port,
            "GET",
            "/api/state",
            headers={"Cookie": cookie_pair},
        )
        csrf = json.loads(raw_state.decode("utf-8"))["csrf_token"]
        st_ws, _, _ = await _raw_http_request(
            runtime.port,
            "POST",
            "/api/workspace/select",
            headers={
                "Cookie": cookie_pair,
                "Origin": runtime.base_url,
                "X-Nova-CSRF": csrf,
                "Content-Type": "application/json",
            },
            body=json.dumps({"path_or_query": str(proj)}).encode("utf-8"),
        )
        assert st_ws == 200

    assert _path_exists(isolated_cfg_file)
    assert host_cfg.read_text(encoding="utf-8") == sentinel

    # Also verify default resolve_config_file_path() inside pytest points to tmp_path, NOT fake_home
    from nova.adapters.config_store import resolve_config_file_path

    default_resolved = resolve_config_file_path()
    assert not default_resolved.is_relative_to(fake_home)


async def test_nova013_ac1_initial_startup_uses_home_cwd_and_excludes_home_from_allowed_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nova.adapters.desktop_launcher import _default_allowed_roots, _resolve_initial_pi_cwd

    fake_home = tmp_path / "fresh_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.chdir(fake_home)

    roots = _default_allowed_roots()
    assert len(roots) >= 1
    assert str(fake_home.resolve()) not in roots

    initial_cwd = _resolve_initial_pi_cwd(roots, None)
    assert initial_cwd == str(fake_home.resolve())

    cfg = NovaDesktopConfig(
        port=0,
        allowed_roots=roots,
        initial_workspace=None,
        open_browser=False,
        enable_voice=False,
        ipc_runtime_dir=str(tmp_path / "sock_ac1"),
        config_path=str(tmp_path / "cfg_ac1" / "config.toml"),
    )
    async with NovaDesktopRuntime(cfg) as runtime:
        assert runtime._pi_client is not None
        assert runtime._pi_client._config.workspace_path == str(fake_home.resolve())
        assert runtime._pi_client._config.workspace_active is False


async def test_nova018_ac10_desktop_runtime_wires_half_duplex_confirmation_and_hot_reload(
    tmp_path: Path,
) -> None:
    from nova.adapters.desktop_launcher import build_arg_parser, config_from_namespace

    cfg_file = tmp_path / "cfg_hd" / "config.toml"
    cfg_file.parent.mkdir(parents=True)
    # Legacy v1.0 config without half_duplex_turn_mode / require_voice_confirmation
    cfg_file.write_text(
        'schema_version = "1.0"\n\n[voice]\nenable_voice = false\ntts_engine = "piper"\n',
        encoding="utf-8",
    )
    ns = build_arg_parser().parse_args(["--no-browser", "--config", str(cfg_file)])
    desktop_cfg = config_from_namespace(ns, env={})
    assert desktop_cfg.half_duplex_turn_mode is True
    assert desktop_cfg.require_voice_confirmation is True

    proj = tmp_path / "demo_hd"
    proj.mkdir()

    class _HotVoiceStub:
        def __init__(self) -> None:
            self.session = CallSession(
                call_id="call-hot",
                state="standby",
                last_activity_monotonic=0.0,
            )
            self.updated_half_duplex: list[bool | None] = []

        async def start(self) -> None:
            pass

        async def start_call(self, reason: str = "ui_call_start") -> None:
            del reason

        async def end_call(self, reason: str = "ui_call_end") -> None:
            del reason

        async def barge_in(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

        def update_settings(self, **kwargs: object) -> None:
            raw_hd = kwargs.get("half_duplex_turn_mode")
            self.updated_half_duplex.append(raw_hd if isinstance(raw_hd, bool) else None)

    voice_stub = _HotVoiceStub()
    runtime_cfg = NovaDesktopConfig(
        port=0,
        allowed_roots=(str(tmp_path),),
        initial_workspace=str(proj),
        open_browser=False,
        enable_voice=True,
        ipc_runtime_dir=str(tmp_path / "sock_hd"),
        config_path=str(cfg_file),
    )
    async with NovaDesktopRuntime(
        runtime_cfg,
        voice_factory=lambda **_kw: voice_stub,
    ) as runtime:
        assert runtime._orchestrator is not None
        assert runtime._orchestrator._settings.require_voice_confirmation is True
        assert runtime._settings_service is not None

        res = await runtime._settings_service.update_settings(
            {"voice": {"require_voice_confirmation": False, "half_duplex_turn_mode": False}}
        )
        assert res["requires_restart"] is False
        assert runtime._orchestrator._settings.require_voice_confirmation is False
        assert voice_stub.updated_half_duplex == [False]

    # F1 regression: verify settings actually persisted to disk and survive reload
    from nova.adapters.config_store import TomlConfigStore

    reloaded = TomlConfigStore(cfg_file).load()
    assert reloaded.voice.half_duplex_turn_mode is False
    assert reloaded.voice.require_voice_confirmation is False


async def test_nova019_open_local_browser_accepts_whiteboard_query_and_rejects_injection() -> None:
    launched: list[tuple[str, ...]] = []

    class _DoneProc:
        async def wait(self) -> int:
            return 0

    async def _spawner(*argv: str) -> _DoneProc:
        launched.append(argv)
        return _DoneProc()

    ok = await open_local_browser_window(
        "http://127.0.0.1:41234/?tab=whiteboard&deck=deck-abc_1",
        preference="xdg-open",
        env={"DISPLAY": ":0"},
        which_resolver=lambda cmd: "/usr/bin/xdg-open" if cmd == "xdg-open" else None,
        spawner=_spawner,
    )
    assert ok is True
    assert launched == [
        ("/usr/bin/xdg-open", "http://127.0.0.1:41234/?tab=whiteboard&deck=deck-abc_1")
    ]

    for bad_url in (
        "http://127.0.0.1:41234/?tab=whiteboard&evil=1",
        "http://127.0.0.1:41234/?tab=other",
        "https://evil.example/?tab=whiteboard",
    ):
        with pytest.raises(DesktopUiError) as exc_info:
            await open_local_browser_window(
                bad_url,
                preference="xdg-open",
                env={"DISPLAY": ":0"},
                which_resolver=lambda cmd: "/usr/bin/xdg-open",
                spawner=_spawner,
            )
        assert exc_info.value.code == "UI_INVALID_BROWSER_URL"






