"""TDD unit and loopback integration tests for NOVA-007 T3: desktop_orb_overlay."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from nova.adapters.desktop_orb_overlay import (
    EXIT_INVALID_ARGS,
    EXIT_LAYER_SHELL_UNSUPPORTED,
    SHELL_IPC_SCHEMA,
    LoopbackOrbStateSubscriber,
    build_orb_visual_state,
    compute_radial_equalizer_bars,
    format_shell_ipc_message,
    parse_sse_frame,
    run_orb_overlay,
    validate_loopback_url,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = str(_REPO_ROOT / "src")


def test_ac11_validate_loopback_url_accepts_strict_loopback_and_rejects_others() -> None:
    host, port = validate_loopback_url("http://127.0.0.1:18742/")
    assert host == "127.0.0.1"
    assert port == 18742

    for invalid in (
        "",
        "https://127.0.0.1:8080",
        "http://localhost:8080",
        "http://0.0.0.0:8080",
        "http://192.168.1.10:8080",
        "http://127.0.0.1:0",
        "http://127.0.0.1:70000",
        "file:///etc/passwd",
    ):
        with pytest.raises(ValueError):
            validate_loopback_url(invalid)

    # CLI rejects non-loopback URL with exit code 2 without opening sockets
    proc = subprocess.run(
        [sys.executable, "-m", "nova.adapters.desktop_orb_overlay", "https://evil.example"],
        env={"PYTHONPATH": _SRC_DIR},
        capture_output=True,
        timeout=5.0,
        check=False,
    )
    assert proc.returncode == EXIT_INVALID_ARGS


def test_ac2_run_orb_overlay_returns_exit_75_when_layer_shell_unsupported() -> None:
    code = run_orb_overlay(
        "http://127.0.0.1:18742",
        layer_shell_probe=lambda: False,
    )
    assert code == EXIT_LAYER_SHELL_UNSUPPORTED


def test_ac3_ac4_orb_visual_state_and_radial_equalizer_geometry() -> None:
    # Standby & closed: 0% continuous animation timer, exact reference captions
    for inactive_state in ("standby", "closed", "unknown_future_state"):
        st = build_orb_visual_state(inactive_state, voice_enabled=True)
        assert st.in_call is False
        assert st.requires_animation_timer is False
        assert st.title == "En reposo"
        assert st.subtitle == "(Di 'Oye Nova')"

    # Active call states: animation timer enabled + exact speech-bubble titles/subtitles
    expected = {
        "listening": "Escuchando...",
        "thinking": "Pensando...",
        "executing": "Ejecutando...",
        "speaking": "Hablando...",
    }
    for state_name, subtitle in expected.items():
        st = build_orb_visual_state(state_name, voice_enabled=True)
        assert st.in_call is True
        assert st.requires_animation_timer is True
        assert st.title == "¡Oye Nova!"
        assert st.subtitle == subtitle

    # 360-degree radial acoustic equalizer bars
    bars = compute_radial_equalizer_bars(phase=1.25, bar_count=40)
    assert len(bars) == 40
    for seg in bars:
        assert 0.0 <= seg.r <= 1.0
        assert 0.0 <= seg.g <= 1.0
        assert 0.0 <= seg.b <= 1.0
        assert 0.0 < seg.a <= 1.0
        length = ((seg.x2 - seg.x1) ** 2 + (seg.y2 - seg.y1) ** 2) ** 0.5
        assert 4.0 <= length <= 26.0

    # Shell IPC JSONL message formatting
    line = format_shell_ipc_message("toggle_panel")
    parsed = json.loads(line)
    assert parsed == {"schema": SHELL_IPC_SCHEMA, "action": "toggle_panel"}

    # SSE parser handles nova_event and ignores heartbeat comments
    assert parse_sse_frame(": heartbeat") is None
    ev = parse_sse_frame(
        "event: nova_event\n"
        'data: {"event_type":"call_state_changed","payload":{"state":"listening"}}'
    )
    assert ev is not None
    assert ev[0] == "nova_event"
    assert ev[1]["event_type"] == "call_state_changed"


async def test_ac11_loopback_orb_subscriber_authenticates_cookie_and_tracks_sse_states() -> None:
    observed_cookies: list[str] = []
    observed_states: list[tuple[str, bool]] = []
    sse_ready = asyncio.Event()

    async def _handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw_head = await reader.readuntil(b"\r\n\r\n")
            lines = raw_head.decode("latin-1").split("\r\n")
            req_line = lines[0]
            headers: dict[str, str] = {}
            for hline in lines[1:]:
                if ":" in hline:
                    k, v = hline.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            if req_line.startswith("GET / "):
                resp = (
                    "HTTP/1.1 200 OK\r\n"
                    "Set-Cookie: nova_ui_session=tok-orb-test; "
                    "HttpOnly; SameSite=Strict; Path=/\r\n"
                    "Content-Length: 2\r\n"
                    "Connection: close\r\n\r\nOK"
                )
                writer.write(resp.encode("latin-1"))
                await writer.drain()
            elif req_line.startswith("GET /api/state "):
                observed_cookies.append(headers.get("cookie", ""))
                body = json.dumps(
                    {
                        "schema_version": "1.3.0",
                        "csrf_token": "csrf-orb-test",
                        "voice_enabled": True,
                        "call_session": {
                            "call_id": "c-1",
                            "state": "standby",
                            "turn_count": 0,
                            "active_correlation_id": None,
                        },
                    }
                ).encode("utf-8")
                head = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("latin-1")
                writer.write(head + body)
                await writer.drain()
            elif req_line.startswith("GET /api/events "):
                observed_cookies.append(headers.get("cookie", ""))
                head = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/event-stream\r\n"
                    "Cache-Control: no-cache\r\n\r\n"
                ).encode("latin-1")
                writer.write(head)
                await writer.drain()

                frame1 = (
                    b"event: nova_event\n"
                    b'data: {"seq":1,"event_type":"call_state_changed",'
                    b'"payload":{"state":"listening"}}\n\n'
                )
                writer.write(frame1)
                await writer.drain()
                await asyncio.sleep(0.05)

                frame2 = (
                    b"event: nova_event\n"
                    b'data: {"seq":2,"event_type":"call_state_changed",'
                    b'"payload":{"state":"standby"}}\n\n'
                )
                writer.write(frame2)
                await writer.drain()
                sse_ready.set()
                await asyncio.sleep(0.3)
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(_handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    stop_event = threading.Event()

    subscriber = LoopbackOrbStateSubscriber(
        base_url=f"http://127.0.0.1:{port}",
        stop_event=stop_event,
        on_state_change=lambda st, voice: observed_states.append((st, voice)),
    )
    subscriber.start()
    try:
        await asyncio.wait_for(sse_ready.wait(), timeout=3.0)
        for _ in range(30):
            if len(observed_states) >= 3:
                break
            await asyncio.sleep(0.02)
    finally:
        stop_event.set()
        subscriber.join(timeout=2.0)
        server.close()
        await server.wait_closed()

    assert not subscriber.is_alive(), "Subscriber thread must exit cleanly after stop_event"
    assert all("nova_ui_session=tok-orb-test" in c for c in observed_cookies)
    assert [st for st, _ in observed_states[:3]] == ["standby", "listening", "standby"]


def test_ac10_subscriber_invokes_on_parent_lost_when_reparented_after_disconnect() -> None:
    """Contract OP-8: If parent dies (PPID changes) and loopback closes, subscriber exits 0."""
    current_ppid = [4242]
    parent_lost = threading.Event()
    stop_event = threading.Event()

    def _ppid() -> int:
        curr = current_ppid[0]
        current_ppid[0] = 1
        return curr

    subscriber = LoopbackOrbStateSubscriber(
        base_url="http://127.0.0.1:65530",
        stop_event=stop_event,
        on_state_change=lambda _s, _v: None,
        on_parent_lost=parent_lost.set,
        ppid_provider=_ppid,
    )
    subscriber.start()
    assert parent_lost.wait(timeout=2.0) is True
    subscriber.join(timeout=1.0)
    assert not subscriber.is_alive()


async def test_f8_subscriber_detects_reparenting_while_sse_stream_stays_idle() -> None:
    """F8: Subscriber detects parent SIGKILL during SSE idle read timeout without disconnect."""
    current_ppid = [7777]
    parent_lost = threading.Event()
    sse_connected = asyncio.Event()
    sse_done = asyncio.Event()
    stop_event = threading.Event()

    async def _idle_sse_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            req = head.decode("latin-1").splitlines()[0]
            if req.startswith("GET / "):
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Set-Cookie: nova_ui_session=tok-idle; HttpOnly; Path=/\r\n"
                    b"Content-Length: 2\r\nConnection: close\r\n\r\nOK"
                )
                await writer.drain()
            elif req.startswith("GET /api/state "):
                body = b'{"voice_enabled":true,"call_session":{}}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("latin-1")
                    + body
                )
                await writer.drain()
            elif req.startswith("GET /api/events "):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n: ok\n\n"
                )
                await writer.drain()
                sse_connected.set()
                await sse_done.wait()
        finally:
            writer.close()

    server = await asyncio.start_server(_idle_sse_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    subscriber = LoopbackOrbStateSubscriber(
        base_url=f"http://127.0.0.1:{port}",
        stop_event=stop_event,
        on_state_change=lambda _s, _v: None,
        on_parent_lost=parent_lost.set,
        ppid_provider=lambda: current_ppid[0],
    )
    subscriber.start()
    try:
        await asyncio.wait_for(sse_connected.wait(), timeout=2.0)
        current_ppid[0] = 1
        assert await asyncio.to_thread(parent_lost.wait, 1.0) is True
    finally:
        stop_event.set()
        sse_done.set()
        subscriber.join(timeout=1.0)
        server.close()
        await server.wait_closed()
