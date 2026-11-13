"""Unit tests for the native desktop shell coordinator (`desktop_window`)."""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
from typing import Any

import pytest

from nova.adapters.desktop_orb_overlay import (
    EXIT_LAYER_SHELL_UNSUPPORTED,
    format_shell_ipc_message,
)
from nova.adapters.desktop_window import (
    EXIT_INVALID_ARGS,
    DesktopShellController,
    LoopbackActionClient,
    build_panel_shell_url,
    main,
    parse_shell_ipc_line,
    parse_window_args,
    should_hide_panel_for_title,
)


def test_parse_window_args_validates_loopback_and_show_panel_flag() -> None:
    """OP-7 / AC-1: CLI accepts loopback URL and optional --show-panel, rejecting others."""
    default_args = parse_window_args(["http://127.0.0.1:8765/"])
    assert default_args.base_url == "http://127.0.0.1:8765"
    assert default_args.show_panel is False

    expanded_args = parse_window_args(["http://127.0.0.1:9123", "--show-panel"])
    assert expanded_args.base_url == "http://127.0.0.1:9123"
    assert expanded_args.show_panel is True

    for bad_argv in (
        [],
        ["https://example.invalid"],
        ["http://localhost:8765"],
        ["http://127.0.0.1:8765", "--unknown-flag"],
    ):
        with pytest.raises(ValueError):
            parse_window_args(bad_argv)

    assert main(["https://evil.invalid"]) == EXIT_INVALID_ARGS


def test_build_panel_shell_url_and_title_hide_signal() -> None:
    """AC-6: Frameless panel loads #desktop-shell and intercepts Nova::HIDE_PANEL title."""
    assert build_panel_shell_url("http://127.0.0.1:8765/") == "http://127.0.0.1:8765/#desktop-shell"
    assert should_hide_panel_for_title("Nova::HIDE_PANEL") is True
    assert should_hide_panel_for_title("  Nova::HIDE_PANEL ") is True
    assert should_hide_panel_for_title("Nova") is False
    assert should_hide_panel_for_title("") is False


def test_parse_shell_ipc_line_handles_valid_and_malformed_frames() -> None:
    """OP-8: Valid shell_ipc.v1 frames parse; malformed or unknown frames return None safely."""
    assert parse_shell_ipc_line(format_shell_ipc_message("toggle_panel")) == "toggle_panel"
    assert parse_shell_ipc_line(format_shell_ipc_message("call_start")) == "call_start"
    assert parse_shell_ipc_line(format_shell_ipc_message("call_end")) == "call_end"
    assert (
        parse_shell_ipc_line(format_shell_ipc_message("ready", backend="gtk4-layer-shell"))
        == "ready"
    )
    assert parse_shell_ipc_line(format_shell_ipc_message("quit")) == "quit"

    assert parse_shell_ipc_line("not-json") is None
    assert parse_shell_ipc_line('{"schema":"other","action":"toggle_panel"}') is None
    assert parse_shell_ipc_line('{"schema":"nova.desktop.shell_ipc.v1","action":"bogus"}') is None
    assert parse_shell_ipc_line("x" * 1024) is None


def test_desktop_shell_controller_toggles_panel_without_starting_call_and_falls_back() -> None:
    """AC-2 & AC-6: toggle_panel never triggers call_start; exit 75 activates PyQt6 fallback."""
    panel_visible = False
    call_actions: list[str] = []
    fallback_activations = 0
    quit_count = 0

    def _get_visible() -> bool:
        return panel_visible

    def _set_visible(visible: bool) -> None:
        nonlocal panel_visible
        panel_visible = visible

    def _on_call(action: str) -> None:
        call_actions.append(action)

    def _on_fallback() -> None:
        nonlocal fallback_activations
        fallback_activations += 1

    def _on_quit() -> None:
        nonlocal quit_count
        quit_count += 1

    controller = DesktopShellController(
        is_panel_visible=_get_visible,
        set_panel_visible=_set_visible,
        trigger_call_action=_on_call,
        activate_fallback_orb=_on_fallback,
        quit_shell=_on_quit,
    )

    assert controller.handle_ipc_line(format_shell_ipc_message("ready", backend="gtk4")) is True
    assert controller.overlay_ready is True

    # Toggle open -> close without any call_start side effect
    assert controller.handle_ipc_line(format_shell_ipc_message("toggle_panel")) is True
    assert panel_visible is True
    assert call_actions == []

    assert controller.handle_ipc_line(format_shell_ipc_message("toggle_panel")) is True
    assert panel_visible is False
    assert call_actions == []

    # Explicit call actions route cleanly
    assert controller.handle_ipc_line(format_shell_ipc_message("call_start")) is True
    assert controller.handle_ipc_line(format_shell_ipc_message("call_end")) is True
    assert call_actions == ["start", "end"]

    # Malformed frame ignored
    assert controller.handle_ipc_line("{invalid") is False

    # Exit 75 activates PyQt6 fallback orb once when not shutting down
    controller.handle_overlay_exit(EXIT_LAYER_SHELL_UNSUPPORTED, shutting_down=False)
    assert controller.fallback_orb_active is True
    assert fallback_activations == 1

    # During shutdown, exit 75 does not spawn fallback
    controller.handle_overlay_exit(EXIT_LAYER_SHELL_UNSUPPORTED, shutting_down=True)
    assert fallback_activations == 1

    assert controller.handle_ipc_line(format_shell_ipc_message("quit")) is True
    assert quit_count == 1


def test_terminate_overlay_process_reaps_child_within_budget() -> None:
    """AC-10: Overlay child process is terminated and reaped without leaving zombies."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.poll() is None
        DesktopShellController.terminate_overlay_process(proc, timeout_sec=2.0)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=1.0)
        if proc.stdout is not None:
            proc.stdout.close()


def test_loopback_action_client_authenticates_cookie_and_csrf_in_ram() -> None:
    """OP-3 / contract §3 & §8: POST /api/call/start sends session cookie, CSRF and Origin."""
    recorded: dict[str, Any] = {}

    class _FakeUiHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                self.send_response(200)
                self.send_header(
                    "Set-Cookie",
                    "nova_ui_session=syn-sess-007; HttpOnly; SameSite=Strict; Path=/",
                )
                self.end_headers()
                self.wfile.write(b"ok")
                return
            if self.path == "/api/state":
                recorded["state_cookie"] = self.headers.get("Cookie")
                body = json.dumps({"csrf_token": "syn-csrf-token-007"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            recorded["post_path"] = self.path
            recorded["post_cookie"] = self.headers.get("Cookie")
            recorded["post_csrf"] = self.headers.get("X-Nova-CSRF")
            recorded["post_origin"] = self.headers.get("Origin")
            recorded["post_corr"] = self.headers.get("X-Correlation-Id")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeUiHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{port}"
        client = LoopbackActionClient(base_url)
        assert client.trigger_call("start") is True
        assert recorded["state_cookie"] == "nova_ui_session=syn-sess-007"
        assert recorded["post_path"] == "/api/call/start"
        assert recorded["post_cookie"] == "nova_ui_session=syn-sess-007"
        assert recorded["post_csrf"] == "syn-csrf-token-007"
        assert recorded["post_origin"] == base_url
        assert isinstance(recorded["post_corr"], str) and recorded["post_corr"].startswith("shell-")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
