"""Native bottom-right transparent floating orb overlay (`gtk4-layer-shell` + Cairo).

Runs as an isolated child process of `nova.adapters.desktop_window` so GTK4/GLib and
PyQt6 event loops never collide in the same OS process. Subscribes strictly over
loopback (`127.0.0.1`) to `DesktopUiServer` (`/api/state` + `/api/events`) and emits
`nova.desktop.shell_ipc.v1` JSONL interaction frames on stdout.
"""

from __future__ import annotations

import ctypes
import http.client
import json
import math
import os
import re
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

EXIT_OK = 0
EXIT_INVALID_ARGS = 2
EXIT_LAYER_SHELL_UNSUPPORTED = 75

SHELL_IPC_SCHEMA = "nova.desktop.shell_ipc.v1"
ShellIpcAction = Literal["ready", "toggle_panel", "call_start", "call_end", "quit"]

_LOOPBACK_URL_RE = re.compile(r"^http://127\.0\.0\.1:([1-9][0-9]{0,4})/?$")

_ACTIVE_CALL_SUBTITLES: dict[str, str] = {
    "listening": "Escuchando...",
    "thinking": "Pensando...",
    "executing": "Ejecutando...",
    "speaking": "Hablando...",
}

_RING_PALETTE: tuple[tuple[float, float, float], ...] = (
    (0.00, 0.824, 1.00),   # #00d2ff electric cyan
    (0.220, 0.741, 0.973),  # #38bdf8 sky glow
    (0.388, 0.400, 0.945),  # #6366f1 cosmic indigo
    (0.659, 0.333, 0.969),  # #a855f7 nova violet
    (0.925, 0.282, 0.600),  # #ec4899 magenta accent
)


@dataclass(frozen=True, slots=True)
class OrbVisualState:
    """Pure immutable visual state for the floating desktop orb."""

    call_state: str
    voice_enabled: bool
    in_call: bool
    title: str
    subtitle: str
    requires_animation_timer: bool


@dataclass(frozen=True, slots=True)
class RadialBarSegment:
    """Single radial acoustic equalizer bar segment in Cairo device coordinates."""

    x1: float
    y1: float
    x2: float
    y2: float
    r: float
    g: float
    b: float
    a: float


def validate_loopback_url(url: str) -> tuple[str, int]:
    """Validate `http://127.0.0.1:<port>` strictly and return `(host, port)`."""
    cleaned = url.strip()
    match = _LOOPBACK_URL_RE.fullmatch(cleaned)
    if match is None:
        raise ValueError("Overlay refuses non-loopback or malformed URL.")
    port = int(match.group(1))
    if port < 1 or port > 65535:
        raise ValueError("Port out of valid TCP range.")
    return ("127.0.0.1", port)


def build_orb_visual_state(call_state: str, *, voice_enabled: bool = True) -> OrbVisualState:
    """Map `CallSessionState` into deterministic labels and 0%-GPU standby policy."""
    subtitle = _ACTIVE_CALL_SUBTITLES.get(call_state)
    if subtitle is not None:
        return OrbVisualState(
            call_state=call_state,
            voice_enabled=voice_enabled,
            in_call=True,
            title="¡Oye Nova!",
            subtitle=subtitle,
            requires_animation_timer=True,
        )
    return OrbVisualState(
        call_state=call_state if call_state in ("standby", "closed") else "standby",
        voice_enabled=voice_enabled,
        in_call=False,
        title="En reposo",
        subtitle="(Di 'Oye Nova')",
        requires_animation_timer=False,
    )


def _lerp_palette(t: float) -> tuple[float, float, float]:
    n = len(_RING_PALETTE)
    scaled = (t % 1.0) * n
    idx = int(scaled) % n
    nxt = (idx + 1) % n
    frac = scaled - int(scaled)
    c1 = _RING_PALETTE[idx]
    c2 = _RING_PALETTE[nxt]
    return (
        c1[0] + (c2[0] - c1[0]) * frac,
        c1[1] + (c2[1] - c1[1]) * frac,
        c1[2] + (c2[2] - c1[2]) * frac,
    )


def compute_radial_equalizer_bars(
    *,
    phase: float,
    bar_count: int = 40,
    cx: float = 238.0,
    cy: float = 77.0,
    inner_radius: float = 43.5,
) -> list[RadialBarSegment]:
    """Compute 360-degree radial acoustic equalizer bar segments around `(cx, cy)`."""
    count = max(8, bar_count)
    segments: list[RadialBarSegment] = []
    two_pi = 2.0 * math.pi
    for idx in range(count):
        ratio = idx / count
        angle = ratio * two_pi
        wave_a = 0.5 + 0.5 * math.sin(angle * 5.0 + phase * 3.4)
        wave_b = 0.5 + 0.5 * math.cos(angle * 3.0 - phase * 2.3)
        wave_c = 0.5 + 0.5 * math.sin(angle * 9.0 + phase * 5.1)
        amplitude = 5.5 + 13.5 * (0.45 * wave_a + 0.35 * wave_b + 0.20 * wave_c)
        outer_r = inner_radius + amplitude
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        r, g, b = _lerp_palette(ratio + phase * 0.05)
        alpha = min(1.0, 0.72 + 0.28 * wave_a)
        segments.append(
            RadialBarSegment(
                x1=cx + cos_a * inner_radius,
                y1=cy + sin_a * inner_radius,
                x2=cx + cos_a * outer_r,
                y2=cy + sin_a * outer_r,
                r=r,
                g=g,
                b=b,
                a=alpha,
            )
        )
    return segments


def format_shell_ipc_message(action: ShellIpcAction, **extra: str) -> str:
    """Serialize one `nova.desktop.shell_ipc.v1` JSONL message."""
    payload: dict[str, str] = {"schema": SHELL_IPC_SCHEMA, "action": action}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_sse_frame(raw_frame: str) -> tuple[str, dict[str, Any]] | None:
    """Parse one raw SSE frame into `(event_name, json_dict)`, ignoring comments."""
    event_name = "message"
    data_lines: list[str] = []
    for line in raw_frame.splitlines():
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return None
    try:
        parsed = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return (event_name, parsed)


def _extract_session_cookie(set_cookie_header: str | None) -> str:
    if not set_cookie_header:
        return ""
    for part in set_cookie_header.split(","):
        first = part.strip().split(";", 1)[0].strip()
        if first.startswith("nova_ui_session="):
            return first
    return ""


class LoopbackOrbStateSubscriber(threading.Thread):
    """Daemon thread tracking `CallSession.state` from `127.0.0.1` via HTTP + SSE."""

    def __init__(
        self,
        *,
        base_url: str,
        stop_event: threading.Event,
        on_state_change: Callable[[str, bool], None],
        on_parent_lost: Callable[[], None] | None = None,
        ppid_provider: Callable[[], int] = os.getppid,
    ) -> None:
        super().__init__(name="nova-orb-sse", daemon=True)
        host, port = validate_loopback_url(base_url)
        self._host = host
        self._port = port
        self._stop_event = stop_event
        self._on_state_change = on_state_change
        self._on_parent_lost = on_parent_lost
        self._ppid_provider = ppid_provider
        self._initial_ppid = ppid_provider()
        self._voice_enabled = True

    def _fetch_bootstrap_cookie_and_state(self) -> str:
        conn = http.client.HTTPConnection(self._host, self._port, timeout=3.0)
        try:
            conn.request("GET", "/", headers={"Host": f"{self._host}:{self._port}"})
            resp = conn.getresponse()
            cookie = _extract_session_cookie(resp.getheader("Set-Cookie"))
            resp.read()
        finally:
            conn.close()

        if not cookie:
            return ""

        conn_state = http.client.HTTPConnection(self._host, self._port, timeout=3.0)
        try:
            conn_state.request(
                "GET",
                "/api/state",
                headers={
                    "Host": f"{self._host}:{self._port}",
                    "Cookie": cookie,
                },
            )
            st_resp = conn_state.getresponse()
            raw = st_resp.read()
            if st_resp.status == 200:
                snap = json.loads(raw.decode("utf-8"))
                self._voice_enabled = bool(snap.get("voice_enabled", True))
                call_session = snap.get("call_session") or {}
                initial_state = str(call_session.get("state", "standby"))
                self._on_state_change(initial_state, self._voice_enabled)
        finally:
            conn_state.close()

        return cookie

    def _consume_sse(self, cookie: str) -> None:
        with socket.create_connection((self._host, self._port), timeout=3.0) as sock:
            req = (
                f"GET /api/events HTTP/1.1\r\n"
                f"Host: {self._host}:{self._port}\r\n"
                f"Accept: text/event-stream\r\n"
                f"Cookie: {cookie}\r\n"
                f"Connection: keep-alive\r\n\r\n"
            ).encode("latin-1")
            sock.sendall(req)
            sock.settimeout(0.25)

            header_buf = b""
            while not self._stop_event.is_set() and b"\r\n\r\n" not in header_buf:
                try:
                    chunk = sock.recv(4096)
                except TimeoutError:
                    continue
                if not chunk:
                    return
                header_buf += chunk

            _, _, remainder = header_buf.partition(b"\r\n\r\n")
            text_buf = remainder.decode("utf-8", errors="replace")

            while not self._stop_event.is_set():
                while "\n\n" in text_buf:
                    raw_frame, text_buf = text_buf.split("\n\n", 1)
                    parsed = parse_sse_frame(raw_frame)
                    if parsed is None:
                        continue
                    ev_name, data = parsed
                    if ev_name == "snapshot":
                        self._voice_enabled = bool(data.get("voice_enabled", self._voice_enabled))
                        cs = data.get("call_session")
                        if isinstance(cs, dict):
                            self._on_state_change(
                                str(cs.get("state", "standby")),
                                self._voice_enabled,
                            )
                    elif ev_name == "nova_event" and data.get("event_type") == "call_state_changed":
                        payload = data.get("payload")
                        if isinstance(payload, dict):
                            next_st = str(payload.get("state", "standby"))
                            self._on_state_change(next_st, self._voice_enabled)

                try:
                    chunk = sock.recv(4096)
                except TimeoutError:
                    continue
                if not chunk:
                    return
                text_buf += chunk.decode("utf-8", errors="replace")

    def run(self) -> None:
        backoff = 0.25
        while not self._stop_event.is_set():
            try:
                cookie = self._fetch_bootstrap_cookie_and_state()
                if cookie and not self._stop_event.is_set():
                    backoff = 0.25
                    self._consume_sse(cookie)
            except Exception:
                pass
            if (
                self._on_parent_lost is not None
                and self._ppid_provider() != self._initial_ppid
            ):
                self._stop_event.set()
                self._on_parent_lost()
                break
            if self._stop_event.wait(timeout=backoff):
                break
            backoff = min(3.0, backoff * 2.0)


def _probe_gtk4_layer_shell() -> bool:
    """Return True only when GTK4, Cairo, and `zwlr_layer_shell_v1` are supported."""
    try:
        layer = ctypes.CDLL("/lib/libgtk4-layer-shell.so", mode=ctypes.RTLD_GLOBAL)
        gtk = ctypes.CDLL("libgtk-4.so.1", mode=ctypes.RTLD_GLOBAL)
        ctypes.CDLL("libcairo.so.2", mode=ctypes.RTLD_GLOBAL)
        gtk.gtk_init_check.restype = ctypes.c_int
        if gtk.gtk_init_check() == 0:
            return False
        layer.gtk_layer_is_supported.restype = ctypes.c_int
        return bool(layer.gtk_layer_is_supported() == 1)
    except OSError:
        return False


def _bind_cairo_signatures(cairo: ctypes.CDLL) -> None:
    d = ctypes.c_double
    vp = ctypes.c_void_p
    ci = ctypes.c_int

    cairo.cairo_save.argtypes = [vp]
    cairo.cairo_restore.argtypes = [vp]
    cairo.cairo_set_operator.argtypes = [vp, ci]
    cairo.cairo_paint.argtypes = [vp]
    cairo.cairo_new_path.argtypes = [vp]
    cairo.cairo_close_path.argtypes = [vp]
    cairo.cairo_move_to.argtypes = [vp, d, d]
    cairo.cairo_line_to.argtypes = [vp, d, d]
    cairo.cairo_curve_to.argtypes = [vp, d, d, d, d, d, d]
    cairo.cairo_arc.argtypes = [vp, d, d, d, d, d]
    cairo.cairo_set_source_rgba.argtypes = [vp, d, d, d, d]
    cairo.cairo_set_source.argtypes = [vp, vp]
    cairo.cairo_set_line_width.argtypes = [vp, d]
    cairo.cairo_set_line_cap.argtypes = [vp, ci]
    cairo.cairo_set_line_join.argtypes = [vp, ci]
    cairo.cairo_fill.argtypes = [vp]
    cairo.cairo_fill_preserve.argtypes = [vp]
    cairo.cairo_stroke.argtypes = [vp]

    cairo.cairo_pattern_create_linear.argtypes = [d, d, d, d]
    cairo.cairo_pattern_create_linear.restype = vp
    cairo.cairo_pattern_create_radial.argtypes = [d, d, d, d, d, d]
    cairo.cairo_pattern_create_radial.restype = vp
    cairo.cairo_pattern_add_color_stop_rgba.argtypes = [vp, d, d, d, d, d]
    cairo.cairo_pattern_destroy.argtypes = [vp]

    cairo.cairo_select_font_face.argtypes = [vp, ctypes.c_char_p, ci, ci]
    cairo.cairo_set_font_size.argtypes = [vp, d]
    cairo.cairo_show_text.argtypes = [vp, ctypes.c_char_p]

    cairo.cairo_region_create_rectangle.argtypes = [vp]
    cairo.cairo_region_create_rectangle.restype = vp
    cairo.cairo_region_destroy.argtypes = [vp]


class _CairoRectInt(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
    ]


class _CairoTextExtents(ctypes.Structure):
    _fields_ = [
        ("x_bearing", ctypes.c_double),
        ("y_bearing", ctypes.c_double),
        ("width", ctypes.c_double),
        ("height", ctypes.c_double),
        ("x_advance", ctypes.c_double),
        ("y_advance", ctypes.c_double),
    ]


def _rounded_rect_path(
    cairo: ctypes.CDLL,
    cr: int,
    x: float,
    y: float,
    w: float,
    h: float,
    r: float,
) -> None:
    half_pi = math.pi * 0.5
    cairo.cairo_new_path(cr)
    cairo.cairo_arc(cr, x + w - r, y + r, r, -half_pi, 0.0)
    cairo.cairo_arc(cr, x + w - r, y + h - r, r, 0.0, half_pi)
    cairo.cairo_arc(cr, x + r, y + h - r, r, half_pi, math.pi)
    cairo.cairo_arc(cr, x + r, y + r, r, math.pi, 1.5 * math.pi)
    cairo.cairo_close_path(cr)


def _draw_four_point_star(
    cairo: ctypes.CDLL,
    cr: int,
    cx: float,
    cy: float,
    arm: float,
    waist: float,
    rgba: tuple[float, float, float, float],
) -> None:
    cairo.cairo_new_path(cr)
    cairo.cairo_move_to(cr, cx, cy - arm)
    cairo.cairo_curve_to(cr, cx + waist, cy - waist, cx + waist, cy - waist, cx + arm, cy)
    cairo.cairo_curve_to(cr, cx + waist, cy + waist, cx + waist, cy + waist, cx, cy + arm)
    cairo.cairo_curve_to(cr, cx - waist, cy + waist, cx - waist, cy + waist, cx - arm, cy)
    cairo.cairo_curve_to(cr, cx - waist, cy - waist, cx - waist, cy - waist, cx, cy - arm)
    cairo.cairo_close_path(cr)
    cairo.cairo_set_source_rgba(cr, *rgba)
    cairo.cairo_fill(cr)


def _draw_monogram_n(
    cairo: ctypes.CDLL,
    cr: int,
    cx: float,
    cy: float,
    scale: float,
    *,
    active: bool,
) -> None:
    # Normalize original 216x216 coordinates centered around (108, 108)
    pts = (
        (64.0, 156.0),
        (64.0, 60.0),
        (86.0, 60.0),
        (132.0, 126.0),
        (132.0, 60.0),
        (152.0, 60.0),
        (152.0, 156.0),
        (130.0, 156.0),
        (84.0, 90.0),
        (84.0, 156.0),
    )
    cairo.cairo_new_path(cr)
    for idx, (px, py) in enumerate(pts):
        tx = cx + (px - 108.0) * scale
        ty = cy + (py - 108.0) * scale
        if idx == 0:
            cairo.cairo_move_to(cr, tx, ty)
        else:
            cairo.cairo_line_to(cr, tx, ty)
    cairo.cairo_close_path(cr)

    span = 48.0 * scale
    grad = cairo.cairo_pattern_create_linear(cx - span, cy + span, cx + span, cy - span)
    if active:
        cairo.cairo_pattern_add_color_stop_rgba(grad, 0.0, 0.00, 0.824, 1.00, 1.0)
        cairo.cairo_pattern_add_color_stop_rgba(grad, 0.48, 0.388, 0.400, 0.945, 1.0)
        cairo.cairo_pattern_add_color_stop_rgba(grad, 1.0, 0.659, 0.333, 0.969, 1.0)
    else:
        cairo.cairo_pattern_add_color_stop_rgba(grad, 0.0, 0.310, 0.365, 0.435, 1.0)
        cairo.cairo_pattern_add_color_stop_rgba(grad, 0.55, 0.455, 0.525, 0.610, 1.0)
        cairo.cairo_pattern_add_color_stop_rgba(grad, 1.0, 0.600, 0.675, 0.760, 1.0)

    cairo.cairo_set_source(cr, grad)
    cairo.cairo_fill_preserve(cr)
    cairo.cairo_pattern_destroy(grad)

    cairo.cairo_set_line_join(cr, 1)  # CAIRO_LINE_JOIN_ROUND
    cairo.cairo_set_line_width(cr, 1.1)
    if active:
        cairo.cairo_set_source_rgba(cr, 0.78, 0.93, 1.0, 0.68)
    else:
        cairo.cairo_set_source_rgba(cr, 0.78, 0.84, 0.91, 0.38)
    cairo.cairo_stroke(cr)

    # Starburst glint on top-right vertex of N
    sx = cx + (146.0 - 108.0) * scale
    sy = cy + (64.0 - 108.0) * scale
    if active:
        halo = cairo.cairo_pattern_create_radial(sx, sy, 0.5, sx, sy, 9.0)
        cairo.cairo_pattern_add_color_stop_rgba(halo, 0.0, 1.0, 0.984, 0.922, 0.88)
        cairo.cairo_pattern_add_color_stop_rgba(halo, 0.45, 0.992, 0.902, 0.541, 0.55)
        cairo.cairo_pattern_add_color_stop_rgba(halo, 1.0, 0.961, 0.620, 0.043, 0.0)
        cairo.cairo_arc(cr, sx, sy, 9.0, 0.0, 2.0 * math.pi)
        cairo.cairo_set_source(cr, halo)
        cairo.cairo_fill(cr)
        cairo.cairo_pattern_destroy(halo)
        _draw_four_point_star(cairo, cr, sx, sy, 9.0, 1.4, (1.0, 0.984, 0.922, 0.98))
    else:
        _draw_four_point_star(cairo, cr, sx, sy, 4.5, 0.9, (0.85, 0.90, 0.96, 0.78))


def _draw_orb_scene(
    cairo: ctypes.CDLL,
    cr: int,
    state: OrbVisualState,
    phase: float,
) -> None:
    # 1. Clear surface to 100% transparent alpha
    cairo.cairo_save(cr)
    cairo.cairo_set_operator(cr, 1)  # CAIRO_OPERATOR_SOURCE
    cairo.cairo_set_source_rgba(cr, 0.0, 0.0, 0.0, 0.0)
    cairo.cairo_paint(cr)
    cairo.cairo_restore(cr)
    cairo.cairo_set_operator(cr, 2)  # CAIRO_OPERATOR_OVER

    cx = 238.0
    cy = 77.0

    if state.in_call:
        # Outer ambient neon bloom
        bloom = cairo.cairo_pattern_create_radial(cx, cy, 24.0, cx, cy, 70.0)
        cairo.cairo_pattern_add_color_stop_rgba(bloom, 0.0, 0.0, 0.824, 1.0, 0.36)
        cairo.cairo_pattern_add_color_stop_rgba(bloom, 0.52, 0.45, 0.36, 0.96, 0.22)
        cairo.cairo_pattern_add_color_stop_rgba(bloom, 1.0, 0.659, 0.333, 0.969, 0.0)
        cairo.cairo_arc(cr, cx, cy, 70.0, 0.0, 2.0 * math.pi)
        cairo.cairo_set_source(cr, bloom)
        cairo.cairo_fill(cr)
        cairo.cairo_pattern_destroy(bloom)

        # 360-degree radial acoustic equalizer bars
        bars = compute_radial_equalizer_bars(phase=phase, bar_count=40, cx=cx, cy=cy)
        cairo.cairo_set_line_cap(cr, 1)  # CAIRO_LINE_CAP_ROUND
        for seg in bars:
            # Soft neon halo stroke
            cairo.cairo_new_path(cr)
            cairo.cairo_move_to(cr, seg.x1, seg.y1)
            cairo.cairo_line_to(cr, seg.x2, seg.y2)
            cairo.cairo_set_line_width(cr, 4.4)
            cairo.cairo_set_source_rgba(cr, seg.r, seg.g, seg.b, seg.a * 0.28)
            cairo.cairo_stroke(cr)
            # Crisp core bar
            cairo.cairo_new_path(cr)
            cairo.cairo_move_to(cr, seg.x1, seg.y1)
            cairo.cairo_line_to(cr, seg.x2, seg.y2)
            cairo.cairo_set_line_width(cr, 2.2)
            cairo.cairo_set_source_rgba(cr, seg.r, seg.g, seg.b, seg.a)
            cairo.cairo_stroke(cr)

        # Luminous neon sphere + ring
        sphere = cairo.cairo_pattern_create_radial(cx - 10.0, cy - 12.0, 4.0, cx, cy, 41.0)
        cairo.cairo_pattern_add_color_stop_rgba(sphere, 0.0, 0.14, 0.20, 0.36, 0.94)
        cairo.cairo_pattern_add_color_stop_rgba(sphere, 1.0, 0.05, 0.08, 0.16, 0.96)
        cairo.cairo_arc(cr, cx, cy, 40.5, 0.0, 2.0 * math.pi)
        cairo.cairo_set_source(cr, sphere)
        cairo.cairo_fill_preserve(cr)
        cairo.cairo_pattern_destroy(sphere)

        ring = cairo.cairo_pattern_create_linear(cx - 40.0, cy - 40.0, cx + 40.0, cy + 40.0)
        cairo.cairo_pattern_add_color_stop_rgba(ring, 0.0, 0.00, 0.824, 1.00, 1.0)
        cairo.cairo_pattern_add_color_stop_rgba(ring, 0.5, 0.388, 0.400, 0.945, 0.95)
        cairo.cairo_pattern_add_color_stop_rgba(ring, 1.0, 0.659, 0.333, 0.969, 1.0)
        cairo.cairo_set_line_width(cr, 2.8)
        cairo.cairo_set_source(cr, ring)
        cairo.cairo_stroke(cr)
        cairo.cairo_pattern_destroy(ring)

        # Sparkling 4-point star flares along the ring
        pulse = 0.78 + 0.22 * math.sin(phase * 3.0)
        _draw_four_point_star(
            cairo, cr, cx - 37.0, cy - 16.0, 6.5 * pulse, 1.1, (1.0, 0.984, 0.922, 0.95)
        )
        _draw_four_point_star(
            cairo, cr, cx + 36.0, cy + 18.0, 5.5 * pulse, 1.0, (0.992, 0.902, 0.541, 0.9)
        )

        # Inner dark cosmic squircle
        sq_size = 52.0
        _rounded_rect_path(
            cairo, cr, cx - sq_size / 2.0, cy - sq_size / 2.0, sq_size, sq_size, 14.0
        )
        sq_bg = cairo.cairo_pattern_create_linear(cx - 26.0, cy - 26.0, cx + 26.0, cy + 26.0)
        cairo.cairo_pattern_add_color_stop_rgba(sq_bg, 0.0, 0.11, 0.15, 0.25, 0.98)
        cairo.cairo_pattern_add_color_stop_rgba(sq_bg, 1.0, 0.04, 0.06, 0.12, 0.98)
        cairo.cairo_set_source(cr, sq_bg)
        cairo.cairo_fill_preserve(cr)
        cairo.cairo_pattern_destroy(sq_bg)
        cairo.cairo_set_line_width(cr, 1.4)
        cairo.cairo_set_source_rgba(cr, 0.49, 0.83, 0.99, 0.62)
        cairo.cairo_stroke(cr)

        _draw_monogram_n(cairo, cr, cx, cy, scale=52.0 / 216.0, active=True)

        # Speech-bubble tooltip with right-pointing arrow ("¡Oye Nova!" / "Escuchando...")
        bx, by, bw, bh, br = 10.0, 47.0, 160.0, 60.0, 13.0
        half_pi = math.pi * 0.5
        cairo.cairo_new_path(cr)
        cairo.cairo_arc(cr, bx + bw - br, by + br, br, -half_pi, 0.0)
        cairo.cairo_line_to(cr, bx + bw, cy - 7.0)
        cairo.cairo_line_to(cr, bx + bw + 9.0, cy)
        cairo.cairo_line_to(cr, bx + bw, cy + 7.0)
        cairo.cairo_arc(cr, bx + bw - br, by + bh - br, br, 0.0, half_pi)
        cairo.cairo_arc(cr, bx + br, by + bh - br, br, half_pi, math.pi)
        cairo.cairo_arc(cr, bx + br, by + br, br, math.pi, 1.5 * math.pi)
        cairo.cairo_close_path(cr)

        cairo.cairo_set_source_rgba(cr, 0.059, 0.090, 0.165, 0.92)
        cairo.cairo_fill_preserve(cr)
        b_border = cairo.cairo_pattern_create_linear(bx, by, bx + bw, by + bh)
        cairo.cairo_pattern_add_color_stop_rgba(b_border, 0.0, 0.220, 0.741, 0.973, 0.55)
        cairo.cairo_pattern_add_color_stop_rgba(b_border, 1.0, 0.659, 0.333, 0.969, 0.55)
        cairo.cairo_set_line_width(cr, 1.4)
        cairo.cairo_set_source(cr, b_border)
        cairo.cairo_stroke(cr)
        cairo.cairo_pattern_destroy(b_border)

        _draw_centered_text(
            cairo, cr, state.title, bx + bw / 2.0, by + 25.0, 14.5, bold=True,
            rgba=(0.973, 0.980, 0.988, 1.0),
        )
        _draw_centered_text(
            cairo, cr, state.subtitle, bx + bw / 2.0, by + 45.0, 12.2, bold=False,
            rgba=(0.796, 0.835, 0.882, 0.95),
        )
        return

    # Standby / Closed: frosted translucent glass sphere (0% continuous animation)
    glass = cairo.cairo_pattern_create_radial(cx - 12.0, cy - 14.0, 4.0, cx, cy, 39.0)
    cairo.cairo_pattern_add_color_stop_rgba(glass, 0.0, 0.44, 0.51, 0.61, 0.36)
    cairo.cairo_pattern_add_color_stop_rgba(glass, 0.68, 0.13, 0.16, 0.22, 0.68)
    cairo.cairo_pattern_add_color_stop_rgba(glass, 1.0, 0.10, 0.13, 0.18, 0.78)
    cairo.cairo_arc(cr, cx, cy, 38.5, 0.0, 2.0 * math.pi)
    cairo.cairo_set_source(cr, glass)
    cairo.cairo_fill_preserve(cr)
    cairo.cairo_pattern_destroy(glass)

    cairo.cairo_set_line_width(cr, 1.5)
    cairo.cairo_set_source_rgba(cr, 0.78, 0.84, 0.91, 0.42)
    cairo.cairo_stroke(cr)

    # Subtle glass specular crescent highlight
    cairo.cairo_new_path(cr)
    cairo.cairo_arc(cr, cx, cy, 36.2, math.pi * 1.12, math.pi * 1.72)
    cairo.cairo_set_line_width(cr, 1.4)
    cairo.cairo_set_source_rgba(cr, 1.0, 1.0, 1.0, 0.25)
    cairo.cairo_stroke(cr)

    # Muted bevelled squircle inside glass sphere
    sq_idle = 48.0
    _rounded_rect_path(
        cairo, cr, cx - sq_idle / 2.0, cy - sq_idle / 2.0, sq_idle, sq_idle, 13.0
    )
    idle_bg = cairo.cairo_pattern_create_linear(cx - 24.0, cy - 24.0, cx + 24.0, cy + 24.0)
    cairo.cairo_pattern_add_color_stop_rgba(idle_bg, 0.0, 0.14, 0.17, 0.23, 0.94)
    cairo.cairo_pattern_add_color_stop_rgba(idle_bg, 1.0, 0.07, 0.09, 0.14, 0.96)
    cairo.cairo_set_source(cr, idle_bg)
    cairo.cairo_fill_preserve(cr)
    cairo.cairo_pattern_destroy(idle_bg)
    cairo.cairo_set_line_width(cr, 1.2)
    cairo.cairo_set_source_rgba(cr, 0.322, 0.376, 0.443, 0.65)
    cairo.cairo_stroke(cr)

    _draw_monogram_n(cairo, cr, cx, cy, scale=48.0 / 216.0, active=False)

    # Frosted standby caption pill on the left ("En reposo" / "(Di 'Oye Nova')")
    px, py, pw, ph = 28.0, 50.0, 154.0, 54.0
    _rounded_rect_path(cairo, cr, px, py, pw, ph, 12.0)
    cairo.cairo_set_source_rgba(cr, 0.04, 0.06, 0.11, 0.56)
    cairo.cairo_fill_preserve(cr)
    cairo.cairo_set_line_width(cr, 1.0)
    cairo.cairo_set_source_rgba(cr, 0.58, 0.64, 0.72, 0.22)
    cairo.cairo_stroke(cr)

    _draw_centered_text(
        cairo, cr, state.title, px + pw / 2.0, py + 23.0, 14.0, bold=True,
        rgba=(0.973, 0.980, 0.988, 0.96),
    )
    _draw_centered_text(
        cairo, cr, state.subtitle, px + pw / 2.0, py + 41.5, 11.8, bold=False,
        rgba=(0.796, 0.835, 0.882, 0.88),
    )


def _draw_centered_text(
    cairo: ctypes.CDLL,
    cr: int,
    text: str,
    center_x: float,
    baseline_y: float,
    font_size: float,
    *,
    bold: bool,
    rgba: tuple[float, float, float, float],
) -> None:
    encoded = text.encode("utf-8")
    cairo.cairo_select_font_face(cr, b"Sans", 0, 1 if bold else 0)
    cairo.cairo_set_font_size(cr, font_size)
    ext = _CairoTextExtents()
    cairo.cairo_text_extents.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.POINTER(_CairoTextExtents),
    ]
    cairo.cairo_text_extents(cr, encoded, ctypes.byref(ext))
    x = center_x - (ext.width / 2.0 + ext.x_bearing)
    cairo.cairo_move_to(cr, x, baseline_y)
    cairo.cairo_set_source_rgba(cr, *rgba)
    cairo.cairo_show_text(cr, encoded)


def _emit_ipc(action: ShellIpcAction, **extra: str) -> None:
    try:
        sys.stdout.write(format_shell_ipc_message(action, **extra) + "\n")
        sys.stdout.flush()
    except OSError:
        pass


def run_orb_overlay(
    url: str,
    *,
    layer_shell_probe: Callable[[], bool] = _probe_gtk4_layer_shell,
) -> int:
    """Run the Wayland layer-shell transparent orb overlay for `url`."""
    try:
        validate_loopback_url(url)
    except ValueError:
        return EXIT_INVALID_ARGS

    if not layer_shell_probe():
        return EXIT_LAYER_SHELL_UNSUPPORTED

    layer = ctypes.CDLL("/lib/libgtk4-layer-shell.so", mode=ctypes.RTLD_GLOBAL)
    gtk = ctypes.CDLL("libgtk-4.so.1", mode=ctypes.RTLD_GLOBAL)
    gobj = ctypes.CDLL("libgobject-2.0.so.0", mode=ctypes.RTLD_GLOBAL)
    glib = ctypes.CDLL("libglib-2.0.so.0", mode=ctypes.RTLD_GLOBAL)
    cairo = ctypes.CDLL("libcairo.so.2", mode=ctypes.RTLD_GLOBAL)
    _bind_cairo_signatures(cairo)

    vp = ctypes.c_void_p
    ci = ctypes.c_int

    gtk.gtk_window_new.restype = vp
    gtk.gtk_window_set_decorated.argtypes = [vp, ci]
    gtk.gtk_window_set_default_size.argtypes = [vp, ci, ci]
    gtk.gtk_window_set_child.argtypes = [vp, vp]
    gtk.gtk_widget_set_visible.argtypes = [vp, ci]
    gtk.gtk_widget_queue_draw.argtypes = [vp]
    gtk.gtk_widget_add_controller.argtypes = [vp, vp]
    gtk.gtk_native_get_surface.argtypes = [vp]
    gtk.gtk_native_get_surface.restype = vp
    gtk.gdk_surface_set_input_region.argtypes = [vp, vp]

    gtk.gtk_css_provider_new.restype = vp
    gtk.gtk_css_provider_load_from_string.argtypes = [vp, ctypes.c_char_p]
    gtk.gdk_display_get_default.restype = vp
    gtk.gtk_style_context_add_provider_for_display.argtypes = [vp, vp, ctypes.c_uint]

    gtk.gtk_drawing_area_new.restype = vp
    gtk.gtk_drawing_area_set_content_width.argtypes = [vp, ci]
    gtk.gtk_drawing_area_set_content_height.argtypes = [vp, ci]

    gtk.gtk_gesture_click_new.restype = vp
    gtk.gtk_gesture_single_set_button.argtypes = [vp, ctypes.c_uint]
    gtk.gtk_gesture_single_get_current_button.argtypes = [vp]
    gtk.gtk_gesture_single_get_current_button.restype = ctypes.c_uint

    layer.gtk_layer_init_for_window.argtypes = [vp]
    layer.gtk_layer_set_layer.argtypes = [vp, ci]
    layer.gtk_layer_set_anchor.argtypes = [vp, ci, ci]
    layer.gtk_layer_set_margin.argtypes = [vp, ci, ci]
    layer.gtk_layer_set_keyboard_mode.argtypes = [vp, ci]

    glib.g_main_context_default.restype = vp
    glib.g_main_context_iteration.argtypes = [vp, ci]

    win = gtk.gtk_window_new()
    gtk.gtk_window_set_decorated(win, 0)
    gtk.gtk_window_set_default_size(win, 316, 154)

    # LAYER_TOP = 2; RIGHT = 1, BOTTOM = 3; KEYBOARD_MODE_NONE = 0
    layer.gtk_layer_init_for_window(win)
    layer.gtk_layer_set_layer(win, 2)
    layer.gtk_layer_set_anchor(win, 1, 1)
    layer.gtk_layer_set_anchor(win, 3, 1)
    layer.gtk_layer_set_margin(win, 1, 20)
    layer.gtk_layer_set_margin(win, 3, 20)
    layer.gtk_layer_set_keyboard_mode(win, 0)

    prov = gtk.gtk_css_provider_new()
    gtk.gtk_css_provider_load_from_string(
        prov, b"window { background: transparent; box-shadow: none; border: none; }"
    )
    disp = gtk.gdk_display_get_default()
    if disp:
        gtk.gtk_style_context_add_provider_for_display(disp, prov, 800)

    area = gtk.gtk_drawing_area_new()
    gtk.gtk_drawing_area_set_content_width(area, 316)
    gtk.gtk_drawing_area_set_content_height(area, 154)

    state_lock = threading.Lock()
    current_visual = build_orb_visual_state("standby", voice_enabled=True)
    dirty_flag = True
    start_monotonic = time.monotonic()
    stop_event = threading.Event()

    def _on_sig(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_sig)
    signal.signal(signal.SIGINT, _on_sig)

    draw_cb_type = ctypes.CFUNCTYPE(None, vp, vp, ci, ci, vp)

    def _on_draw(_widget: int, cr: int, _w: int, _h: int, _data: int) -> None:
        with state_lock:
            snap = current_visual
        phase = (time.monotonic() - start_monotonic) if snap.requires_animation_timer else 0.0
        _draw_orb_scene(cairo, cr, snap, phase)

    c_on_draw = draw_cb_type(_on_draw)
    gtk.gtk_drawing_area_set_draw_func.argtypes = [vp, draw_cb_type, vp, vp]
    gtk.gtk_drawing_area_set_draw_func(area, c_on_draw, None, None)

    click_cb_type = ctypes.CFUNCTYPE(None, vp, ci, ctypes.c_double, ctypes.c_double, vp)

    def _on_pressed(gesture: int, _n_press: int, x: float, y: float, _data: int) -> None:
        dist_sq = (x - 238.0) ** 2 + (y - 77.0) ** 2
        if dist_sq > 54.0**2:
            return
        btn = int(gtk.gtk_gesture_single_get_current_button(gesture))
        if btn == 1:
            _emit_ipc("toggle_panel")
        elif btn == 3:
            with state_lock:
                in_call = current_visual.in_call
            _emit_ipc("call_end" if in_call else "call_start")

    c_on_pressed = click_cb_type(_on_pressed)
    gesture = gtk.gtk_gesture_click_new()
    gtk.gtk_gesture_single_set_button(gesture, 0)
    gobj.g_signal_connect_data.argtypes = [
        vp,
        ctypes.c_char_p,
        click_cb_type,
        vp,
        vp,
        ci,
    ]
    gobj.g_signal_connect_data(gesture, b"pressed", c_on_pressed, None, None, 0)
    gtk.gtk_widget_add_controller(area, gesture)

    gtk.gtk_window_set_child(win, area)
    gtk.gtk_widget_set_visible(win, 1)

    def _on_state_change(next_state: str, voice_enabled: bool) -> None:
        nonlocal current_visual, dirty_flag
        updated = build_orb_visual_state(next_state, voice_enabled=voice_enabled)
        with state_lock:
            if updated != current_visual:
                current_visual = updated
                dirty_flag = True

    subscriber = LoopbackOrbStateSubscriber(
        base_url=url,
        stop_event=stop_event,
        on_state_change=_on_state_change,
        on_parent_lost=stop_event.set,
    )
    subscriber.start()
    _emit_ipc("ready", backend="gtk4-layer-shell")

    ctx = glib.g_main_context_default()
    input_region_applied = False

    try:
        while not stop_event.is_set():
            glib.g_main_context_iteration(ctx, 0)
            if not input_region_applied:
                surf = gtk.gtk_native_get_surface(win)
                if surf:
                    hit_rect = _CairoRectInt(x=184, y=23, width=108, height=108)
                    region = cairo.cairo_region_create_rectangle(ctypes.byref(hit_rect))
                    if region:
                        gtk.gdk_surface_set_input_region(surf, region)
                        cairo.cairo_region_destroy(region)
                        input_region_applied = True

            with state_lock:
                animating = current_visual.requires_animation_timer
                should_draw = animating or dirty_flag
                dirty_flag = False

            if should_draw:
                gtk.gtk_widget_queue_draw(area)
                glib.g_main_context_iteration(ctx, 0)

            time.sleep(0.033 if animating else 0.045)
    finally:
        stop_event.set()
        gtk.gtk_widget_set_visible(win, 0)
        glib.g_main_context_iteration(ctx, 0)
        subscriber.join(timeout=1.5)

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(
            "Usage: python -m nova.adapters.desktop_orb_overlay <loopback_url>",
            file=sys.stderr,
        )
        return EXIT_INVALID_ARGS
    return run_orb_overlay(args[0])


if __name__ == "__main__":
    sys.exit(main())
