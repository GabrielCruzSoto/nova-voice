"""Native Linux desktop shell coordinator (`PyQt6` tray + frameless panel + orb IPC).

Coordinates three desktop surfaces without mixing `GTK4` and `PyQt6` in one process:
1. System tray icon (`QSystemTrayIcon`) with `Mostrar/Ocultar panel`, `Iniciar/Colgar llamada`
   and `Salir` actions, switching between muted and active `N` squircle icons in real time.
2. Frameless per-pixel translucent `QWebEngineView` conversation panel (`420x680`, hidden on
   startup unless `--show-panel` is supplied), loading `<loopback_url>/#desktop-shell`.
3. Isolated `nova.adapters.desktop_orb_overlay` child process speaking `nova.desktop.shell_ipc.v1`
   over stdout JSONL, with automatic fallback to a frameless translucent `PyQt6` orb widget when
   Wayland `zwlr_layer_shell_v1` is unavailable (`EXIT_LAYER_SHELL_UNSUPPORTED == 75`).
"""

from __future__ import annotations

import contextlib
import http.client
import importlib
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from nova.adapters.desktop_orb_overlay import (
    EXIT_INVALID_ARGS,
    EXIT_LAYER_SHELL_UNSUPPORTED,
    EXIT_OK,
    SHELL_IPC_SCHEMA,
    LoopbackOrbStateSubscriber,
    OrbVisualState,
    ShellIpcAction,
    build_orb_visual_state,
    compute_radial_equalizer_bars,
    validate_loopback_url,
)

__all__ = [
    "EXIT_INVALID_ARGS",
    "EXIT_LAYER_SHELL_UNSUPPORTED",
    "EXIT_OK",
    "DesktopShellController",
    "LoopbackActionClient",
    "WindowCliArgs",
    "build_panel_shell_url",
    "main",
    "parse_shell_ipc_line",
    "parse_window_args",
    "run_window",
    "should_hide_panel_for_title",
]

logger = logging.getLogger("nova.desktop_window")

_VALID_SHELL_IPC_ACTIONS: frozenset[str] = frozenset(
    {"ready", "toggle_panel", "call_start", "call_end", "quit"}
)
_MAX_IPC_LINE_BYTES = 512
_PANEL_HIDE_TITLE_PREFIX = "Nova::HIDE_PANEL"
_STATIC_DIR = Path(__file__).resolve().parent.parent / "ui" / "static"


@dataclass(frozen=True, slots=True)
class WindowCliArgs:
    """Validated CLI arguments for `python -m nova.adapters.desktop_window`."""

    base_url: str
    show_panel: bool


def parse_window_args(argv: Sequence[str]) -> WindowCliArgs:
    """Validate `<loopback_url> [--show-panel]` and return normalized arguments."""
    if not argv:
        raise ValueError("Missing required <loopback_url> argument.")

    url_arg: str | None = None
    show_panel = False
    for raw in argv:
        token = raw.strip()
        if token == "--show-panel":
            show_panel = True
        elif token.startswith("-"):
            raise ValueError(f"Unsupported CLI flag: {token}")
        elif url_arg is None:
            url_arg = token
        else:
            raise ValueError("Unexpected positional argument.")

    if url_arg is None:
        raise ValueError("Missing required <loopback_url> argument.")

    host, port = validate_loopback_url(url_arg)
    return WindowCliArgs(base_url=f"http://{host}:{port}", show_panel=show_panel)


def build_panel_shell_url(base_url: str) -> str:
    """Return validated `#desktop-shell` URL for the frameless `QWebEngineView` host."""
    host, port = validate_loopback_url(base_url)
    return f"http://{host}:{port}/#desktop-shell"


def should_hide_panel_for_title(title: str) -> bool:
    """Return True when `app.ts` signals the native host to hide the panel."""
    return title.strip().startswith(_PANEL_HIDE_TITLE_PREFIX)


def parse_shell_ipc_line(line: str) -> ShellIpcAction | None:
    """Parse one `nova.desktop.shell_ipc.v1` JSONL frame; return None if invalid."""
    encoded = line.encode("utf-8", errors="replace")
    if len(encoded) > _MAX_IPC_LINE_BYTES:
        return None
    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != SHELL_IPC_SCHEMA:
        return None
    action = payload.get("action")
    if not isinstance(action, str) or action not in _VALID_SHELL_IPC_ACTIONS:
        return None
    if action == "ready":
        return "ready"
    if action == "toggle_panel":
        return "toggle_panel"
    if action == "call_start":
        return "call_start"
    if action == "call_end":
        return "call_end"
    return "quit"


def _extract_cookie_pair(set_cookie_header: str | None) -> str:
    if not set_cookie_header:
        return ""
    for part in set_cookie_header.split(","):
        pair = part.strip().split(";", 1)[0].strip()
        if pair.startswith("nova_ui_session="):
            return pair
    return ""


class LoopbackActionClient:
    """RAM-only authenticated client for `/api/call/start` and `/api/call/end`."""

    def __init__(self, base_url: str, *, timeout_sec: float = 3.0) -> None:
        host, port = validate_loopback_url(base_url)
        self._host = host
        self._port = port
        self._origin = f"http://{host}:{port}"
        self._timeout_sec = timeout_sec
        self._lock = threading.Lock()
        self._cookie = ""
        self._csrf_token = ""

    def _ensure_credentials(self) -> tuple[str, str]:
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout_sec)
        try:
            conn.request("GET", "/", headers={"Host": f"{self._host}:{self._port}"})
            resp = conn.getresponse()
            cookie = _extract_cookie_pair(resp.getheader("Set-Cookie"))
            resp.read()
        finally:
            conn.close()

        if not cookie:
            return ("", "")

        conn_state = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout_sec)
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
            if st_resp.status != 200:
                return ("", "")
            parsed = json.loads(raw.decode("utf-8"))
            csrf = str(parsed.get("csrf_token", "")) if isinstance(parsed, dict) else ""
        finally:
            conn_state.close()

        return (cookie, csrf)

    def _post_with_credentials(
        self,
        endpoint: str,
        cookie: str,
        csrf_token: str,
    ) -> int:
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout_sec)
        try:
            headers = {
                "Host": f"{self._host}:{self._port}",
                "Origin": self._origin,
                "Cookie": cookie,
                "X-Nova-CSRF": csrf_token,
                "X-Correlation-Id": f"shell-{uuid.uuid4().hex[:12]}",
                "Content-Type": "application/json",
                "Content-Length": "2",
            }
            conn.request("POST", endpoint, body=b"{}", headers=headers)
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    def trigger_call(self, action: Literal["start", "end"]) -> bool:
        """Send authenticated `POST /api/call/<action>` over loopback."""
        endpoint = "/api/call/start" if action == "start" else "/api/call/end"
        with self._lock:
            try:
                if not self._cookie or not self._csrf_token:
                    self._cookie, self._csrf_token = self._ensure_credentials()
                if not self._cookie or not self._csrf_token:
                    return False
                status = self._post_with_credentials(endpoint, self._cookie, self._csrf_token)
                if status == 403:
                    self._cookie, self._csrf_token = self._ensure_credentials()
                    if not self._cookie or not self._csrf_token:
                        return False
                    status = self._post_with_credentials(endpoint, self._cookie, self._csrf_token)
                return 200 <= status < 300
            except (OSError, http.client.HTTPException, ValueError):
                return False


class DesktopShellController:
    """Pure hexagonal coordinator for shell IPC frames, fallback orb and child reaping."""

    def __init__(
        self,
        *,
        is_panel_visible: Callable[[], bool],
        set_panel_visible: Callable[[bool], None],
        trigger_call_action: Callable[[Literal["start", "end"]], None],
        activate_fallback_orb: Callable[[], None],
        quit_shell: Callable[[], None],
    ) -> None:
        self._is_panel_visible = is_panel_visible
        self._set_panel_visible = set_panel_visible
        self._trigger_call_action = trigger_call_action
        self._activate_fallback_orb = activate_fallback_orb
        self._quit_shell = quit_shell
        self.overlay_ready: bool = False
        self.fallback_orb_active: bool = False

    def toggle_panel(self) -> None:
        """Flip conversation panel visibility without mutating call state (`AC-6`)."""
        self._set_panel_visible(not self._is_panel_visible())

    def handle_ipc_line(self, line: str) -> bool:
        """Dispatch one `shell_ipc.v1` line; return False if ignored."""
        action = parse_shell_ipc_line(line)
        if action is None:
            return False
        if action == "ready":
            self.overlay_ready = True
            return True
        if action == "toggle_panel":
            self.toggle_panel()
            return True
        if action == "call_start":
            self._trigger_call_action("start")
            return True
        if action == "call_end":
            self._trigger_call_action("end")
            return True
        self._quit_shell()
        return True

    def handle_overlay_exit(self, exit_code: int, *, shutting_down: bool) -> None:
        """Activate `PyQt6` fallback orb if `desktop_orb_overlay` exited with `75`."""
        if shutting_down or self.fallback_orb_active:
            return
        if exit_code == EXIT_LAYER_SHELL_UNSUPPORTED:
            self.fallback_orb_active = True
            self._activate_fallback_orb()

    @staticmethod
    def terminate_overlay_process(
        proc: subprocess.Popen[str] | None,
        *,
        timeout_sec: float = 3.0,
    ) -> None:
        """Deterministically terminate and reap the `desktop_orb_overlay` child (`AC-10`)."""
        if proc is None:
            return
        try:
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.terminate()
                try:
                    proc.wait(timeout=timeout_sec)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(OSError):
                        proc.kill()
                    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                        proc.wait(timeout=1.0)
        finally:
            if proc.stdout is not None:
                with contextlib.suppress(OSError):
                    proc.stdout.close()


@dataclass(frozen=True, slots=True)
class _QtModules:
    core: Any
    gui: Any
    widgets: Any
    webengine: Any
    svg: Any | None


def _load_pyqt6_modules() -> _QtModules:
    core = importlib.import_module("PyQt6.QtCore")
    gui = importlib.import_module("PyQt6.QtGui")
    widgets = importlib.import_module("PyQt6.QtWidgets")
    webengine = importlib.import_module("PyQt6.QtWebEngineWidgets")
    svg: Any | None = None
    with contextlib.suppress(ImportError):
        svg = importlib.import_module("PyQt6.QtSvg")
    return _QtModules(core=core, gui=gui, widgets=widgets, webengine=webengine, svg=svg)


def _render_tray_icon(qt: _QtModules, *, active: bool) -> Any:
    pixmap = qt.gui.QPixmap(64, 64)
    pixmap.fill(qt.core.Qt.GlobalColor.transparent)
    svg_name = "nova-squircle-icon.svg" if active else "nova-squircle-muted.svg"
    svg_path = _STATIC_DIR / svg_name

    painter = qt.gui.QPainter(pixmap)
    painter.setRenderHint(qt.gui.QPainter.RenderHint.Antialiasing, True)
    rendered = False
    if qt.svg is not None and svg_path.is_file():
        renderer = qt.svg.QSvgRenderer(str(svg_path))
        if renderer.isValid():
            renderer.render(painter)
            rendered = True

    if not rendered:
        bg_color = qt.gui.QColor(22, 30, 48, 240) if active else qt.gui.QColor(30, 36, 48, 235)
        border_color = (
            qt.gui.QColor(0, 210, 255, 230) if active else qt.gui.QColor(82, 96, 113, 180)
        )
        painter.setBrush(bg_color)
        pen = qt.gui.QPen(border_color)
        pen.setWidth(3)
        painter.setPen(pen)
        painter.drawRoundedRect(4, 4, 56, 56, 16, 16)
        fg = qt.gui.QColor(0, 210, 255) if active else qt.gui.QColor(124, 139, 158)
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(34)
        painter.setFont(font)
        painter.setPen(fg)
        painter.drawText(pixmap.rect(), int(qt.core.Qt.AlignmentFlag.AlignCenter), "N")

    painter.end()
    return qt.gui.QIcon(pixmap)


def _create_fallback_orb_widget(
    qt: _QtModules,
    *,
    on_left_click: Callable[[], None],
    on_right_click: Callable[[], None],
) -> Any:
    """Create frameless translucent `PyQt6` fallback orb when `zwlr_layer_shell_v1` is absent."""
    visual_ref: list[OrbVisualState] = [build_orb_visual_state("standby")]
    phase_ref: list[float] = [0.0]

    def _mouse_press(_self: Any, event: Any) -> None:
        btn = event.button()
        if btn == qt.core.Qt.MouseButton.LeftButton:
            on_left_click()
        elif btn == qt.core.Qt.MouseButton.RightButton:
            on_right_click()

    def _paint_orb(self_widget: Any, _event: Any) -> None:
        visual = visual_ref[0]
        painter = qt.gui.QPainter(self_widget)
        painter.setRenderHint(qt.gui.QPainter.RenderHint.Antialiasing, True)
        cx, cy = 228.0, 66.0

        painter.setBrush(qt.gui.QColor(15, 23, 42, 224 if visual.in_call else 188))
        border = (
            qt.gui.QColor(56, 189, 248, 130)
            if visual.in_call
            else qt.gui.QColor(148, 163, 184, 70)
        )
        painter.setPen(qt.gui.QPen(border, 1.2))
        painter.drawRoundedRect(8, 36, 152, 58, 14, 14)

        font_title = painter.font()
        font_title.setBold(True)
        font_title.setPixelSize(14)
        painter.setFont(font_title)
        painter.setPen(qt.gui.QColor(248, 250, 252))
        painter.drawText(20, 60, visual.title)

        font_sub = painter.font()
        font_sub.setBold(False)
        font_sub.setPixelSize(12)
        painter.setFont(font_sub)
        sub_col = (
            qt.gui.QColor(125, 211, 252)
            if visual.in_call
            else qt.gui.QColor(203, 213, 225)
        )
        painter.setPen(sub_col)
        painter.drawText(20, 80, visual.subtitle)

        if visual.in_call:
            bars = compute_radial_equalizer_bars(
                phase=phase_ref[0],
                bar_count=36,
                cx=cx,
                cy=cy,
                inner_radius=41.0,
            )
            for seg in bars:
                c = qt.gui.QColor.fromRgbF(seg.r, seg.g, seg.b, seg.a)
                pen = qt.gui.QPen(c, 2.4)
                pen.setCapStyle(qt.core.Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawLine(
                    qt.core.QPointF(seg.x1, seg.y1),
                    qt.core.QPointF(seg.x2, seg.y2),
                )

        sphere_col = (
            qt.gui.QColor(20, 28, 48, 232)
            if visual.in_call
            else qt.gui.QColor(34, 40, 52, 180)
        )
        ring_col = (
            qt.gui.QColor(0, 210, 255, 235)
            if visual.in_call
            else qt.gui.QColor(148, 163, 184, 95)
        )
        painter.setBrush(sphere_col)
        painter.setPen(qt.gui.QPen(ring_col, 2.2))
        painter.drawEllipse(qt.core.QPointF(cx, cy), 38.0, 38.0)

        icon = _render_tray_icon(qt, active=visual.in_call)
        icon.paint(painter, int(cx - 22), int(cy - 22), 44, 44)
        painter.end()

    orb_cls: Any = type(
        "_FallbackOrb",
        (qt.widgets.QWidget,),
        {"mousePressEvent": _mouse_press, "paintEvent": _paint_orb},
    )
    widget = orb_cls()
    flags = (
        qt.core.Qt.WindowType.FramelessWindowHint
        | qt.core.Qt.WindowType.WindowStaysOnTopHint
        | qt.core.Qt.WindowType.Tool
    )
    widget.setWindowFlags(flags)
    widget.setAttribute(qt.core.Qt.WidgetAttribute.WA_TranslucentBackground, True)
    widget.setFixedSize(296, 132)
    widget.setWindowTitle("Nova Orb")

    anim_timer = qt.core.QTimer(widget)
    anim_timer.setInterval(33)

    def _advance_frame() -> None:
        phase_ref[0] += 0.09
        widget.update()

    anim_timer.timeout.connect(_advance_frame)

    def _set_visual_state(state: OrbVisualState) -> None:
        visual_ref[0] = state
        if state.requires_animation_timer:
            if not anim_timer.isActive():
                anim_timer.start()
        elif anim_timer.isActive():
            anim_timer.stop()
        widget.update()

    widget.set_visual_state = _set_visual_state
    screen = qt.widgets.QApplication.primaryScreen()
    if screen is not None:
        geom = screen.availableGeometry()
        widget.move(geom.right() - widget.width() - 24, geom.bottom() - widget.height() - 24)
    return widget


def _run_pyqt6_shell(
    qt: _QtModules,
    *,
    base_url: str,
    show_panel: bool,
    frameless: bool,
    on_top: bool,
    transparent: bool,
    width: int,
    height: int,
) -> int:
    app = qt.widgets.QApplication.instance() or qt.widgets.QApplication([sys.argv[0]])
    app.setApplicationName("Nova")
    app.setQuitOnLastWindowClosed(False)

    # Frameless translucent QWebEngineView conversation panel
    panel = qt.widgets.QWidget()
    panel.setWindowTitle("Nova")
    win_flags = qt.core.Qt.WindowType.Window
    if frameless:
        win_flags |= qt.core.Qt.WindowType.FramelessWindowHint
    if on_top:
        win_flags |= qt.core.Qt.WindowType.WindowStaysOnTopHint
    panel.setWindowFlags(win_flags)
    if transparent:
        panel.setAttribute(qt.core.Qt.WidgetAttribute.WA_TranslucentBackground, True)
    panel.resize(width, height)

    layout = qt.widgets.QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    web_view = qt.webengine.QWebEngineView(panel)
    if transparent:
        web_view.setAttribute(qt.core.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        web_view.setStyleSheet("background: transparent;")
        page = web_view.page()
        if page is not None:
            page.setBackgroundColor(qt.core.Qt.GlobalColor.transparent)
    layout.addWidget(web_view)

    screen = app.primaryScreen()
    if screen is not None:
        avail = screen.availableGeometry()
        panel.move(max(16, avail.right() - width - 24), max(16, avail.bottom() - height - 96))

    action_client = LoopbackActionClient(base_url)
    event_queue: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)

    def _notify_wakeup() -> None:
        with contextlib.suppress(BlockingIOError, OSError):
            os.write(wake_w, b"\x01")

    def _enqueue(kind: str, payload: Any = None) -> None:
        event_queue.put((kind, payload))
        _notify_wakeup()

    muted_icon = _render_tray_icon(qt, active=False)
    active_icon = _render_tray_icon(qt, active=True)
    panel.setWindowIcon(active_icon)

    tray: Any | None = None
    toggle_menu_action: Any | None = None
    if qt.widgets.QSystemTrayIcon.isSystemTrayAvailable():
        tray = qt.widgets.QSystemTrayIcon(muted_icon, app)
        tray.setToolTip("Nova — En reposo (Di 'Oye Nova')")
        menu = qt.widgets.QMenu()
        toggle_menu_action = qt.gui.QAction("Mostrar panel", menu)
        start_call_action = qt.gui.QAction("Iniciar llamada", menu)
        end_call_action = qt.gui.QAction("Colgar llamada", menu)
        quit_action = qt.gui.QAction("Salir", menu)

        menu.addAction(toggle_menu_action)
        menu.addSeparator()
        menu.addAction(start_call_action)
        menu.addAction(end_call_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)

    current_orb_state: OrbVisualState = build_orb_visual_state("standby")
    fallback_orb: Any | None = None
    shutting_down = False

    def _sync_panel_menu_label() -> None:
        if toggle_menu_action is not None:
            label = "Ocultar panel" if panel.isVisible() else "Mostrar panel"
            toggle_menu_action.setText(label)

    def _set_panel_visible(visible: bool) -> None:
        if visible:
            panel.show()
            panel.raise_()
            panel.activateWindow()
        else:
            panel.hide()
        _sync_panel_menu_label()

    def _run_call_async(action: Literal["start", "end"]) -> None:
        threading.Thread(
            target=action_client.trigger_call,
            args=(action,),
            name=f"nova-shell-call-{action}",
            daemon=True,
        ).start()

    def _activate_fallback_orb() -> None:
        nonlocal fallback_orb
        if fallback_orb is not None:
            return
        fallback_orb = _create_fallback_orb_widget(
            qt,
            on_left_click=controller.toggle_panel,
            on_right_click=lambda: _run_call_async(
                "end" if current_orb_state.in_call else "start"
            ),
        )
        fallback_orb.set_visual_state(current_orb_state)
        fallback_orb.show()

    def _quit_shell() -> None:
        nonlocal shutting_down
        shutting_down = True
        app.quit()

    controller = DesktopShellController(
        is_panel_visible=panel.isVisible,
        set_panel_visible=_set_panel_visible,
        trigger_call_action=_run_call_async,
        activate_fallback_orb=_activate_fallback_orb,
        quit_shell=_quit_shell,
    )

    def _on_title_changed(title: str) -> None:
        if should_hide_panel_for_title(title):
            _set_panel_visible(False)

    web_view.titleChanged.connect(_on_title_changed)
    web_view.setUrl(qt.core.QUrl(build_panel_shell_url(base_url)))

    if tray is not None and toggle_menu_action is not None:
        toggle_menu_action.triggered.connect(controller.toggle_panel)
        start_call_action.triggered.connect(lambda: _run_call_async("start"))
        end_call_action.triggered.connect(lambda: _run_call_async("end"))
        quit_action.triggered.connect(_quit_shell)

        def _on_tray_activated(reason: Any) -> None:
            if reason == qt.widgets.QSystemTrayIcon.ActivationReason.Trigger:
                controller.toggle_panel()

        tray.activated.connect(_on_tray_activated)
        tray.show()

    # Spawn isolated Wayland Layer-Shell + Cairo orb process (`R1` / `OP-8`)
    overlay_proc: subprocess.Popen[str] | None = None
    try:
        overlay_proc = subprocess.Popen(
            [sys.executable, "-m", "nova.adapters.desktop_orb_overlay", base_url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            shell=False,
        )
    except OSError as exc:
        logger.warning("Unable to spawn desktop_orb_overlay (%s); using PyQt6 fallback", exc)
        controller.handle_overlay_exit(EXIT_LAYER_SHELL_UNSUPPORTED, shutting_down=False)

    def _read_overlay_stdout(proc: subprocess.Popen[str]) -> None:
        stream = proc.stdout
        if stream is not None:
            for raw_line in stream:
                _enqueue("ipc", raw_line)
        rc = proc.wait()
        _enqueue("overlay_exit", rc)

    if overlay_proc is not None:
        threading.Thread(
            target=_read_overlay_stdout,
            args=(overlay_proc,),
            name="nova-overlay-ipc-reader",
            daemon=True,
        ).start()

    sse_stop = threading.Event()
    sse_subscriber = LoopbackOrbStateSubscriber(
        base_url=base_url,
        stop_event=sse_stop,
        on_state_change=lambda st, ve: _enqueue("orb_state", (st, ve)),
        on_panel_toggle_requested=lambda: _enqueue("panel_toggle", None),
        on_parent_lost=lambda: _enqueue("signal", None),
    )
    sse_subscriber.start()

    def _drain_wakeup() -> None:
        with contextlib.suppress(BlockingIOError, OSError):
            while os.read(wake_r, 64):
                pass
        if shutting_down:
            app.quit()
            return
        while True:
            try:
                kind, payload = event_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "signal":
                _quit_shell()
                return
            if kind == "ipc" and isinstance(payload, str):
                controller.handle_ipc_line(payload)
            elif kind == "overlay_exit" and isinstance(payload, int):
                controller.handle_overlay_exit(payload, shutting_down=shutting_down)
            elif kind == "panel_toggle":
                controller.toggle_panel()
            elif kind == "orb_state" and isinstance(payload, tuple) and len(payload) == 2:
                nonlocal current_orb_state
                st_name, v_enabled = payload
                current_orb_state = build_orb_visual_state(
                    str(st_name),
                    voice_enabled=bool(v_enabled),
                )
                if tray is not None:
                    tray.setIcon(active_icon if current_orb_state.in_call else muted_icon)
                    tray.setToolTip(
                        f"Nova — {current_orb_state.title} ({current_orb_state.subtitle})"
                    )
                if fallback_orb is not None:
                    fallback_orb.set_visual_state(current_orb_state)

    notifier = qt.core.QSocketNotifier(wake_r, qt.core.QSocketNotifier.Type.Read, app)
    notifier.activated.connect(lambda _fd: _drain_wakeup())

    prev_sigterm: Any = None
    prev_sigint: Any = None
    prev_wakeup_fd = -1
    is_main_thread = threading.current_thread() is threading.main_thread()
    if is_main_thread:
        def _handle_posix_signal(_signum: int, _frame: Any) -> None:
            event_queue.put(("signal", None))

        prev_sigterm = signal.signal(signal.SIGTERM, _handle_posix_signal)
        prev_sigint = signal.signal(signal.SIGINT, _handle_posix_signal)
        prev_wakeup_fd = signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)

    if show_panel:
        _set_panel_visible(True)
    else:
        _sync_panel_menu_label()

    try:
        return int(app.exec())
    finally:
        shutting_down = True
        sse_stop.set()
        notifier.setEnabled(False)
        if is_main_thread:
            with contextlib.suppress(ValueError, OSError):
                signal.set_wakeup_fd(prev_wakeup_fd)
            if prev_sigterm is not None:
                signal.signal(signal.SIGTERM, prev_sigterm)
            if prev_sigint is not None:
                signal.signal(signal.SIGINT, prev_sigint)
        if tray is not None:
            tray.hide()
        if fallback_orb is not None:
            fallback_orb.close()
        panel.close()
        DesktopShellController.terminate_overlay_process(overlay_proc, timeout_sec=3.0)
        with contextlib.suppress(OSError):
            os.close(wake_r)
        with contextlib.suppress(OSError):
            os.close(wake_w)


def _run_pywebview_fallback(
    url: str,
    *,
    frameless: bool,
    on_top: bool,
    transparent: bool,
    width: int,
    height: int,
) -> int:
    try:
        webview = importlib.import_module("webview")
        webview.create_window(
            "Nova",
            url=url,
            width=width,
            height=height,
            frameless=frameless,
            on_top=on_top,
            transparent=transparent,
            easy_drag=True,
        )
        webview.start(gui="qt", debug=False)
        return EXIT_OK
    except Exception as exc:
        logger.warning("Native desktop shell unavailable (%s)", exc)
        return 1


def run_window(
    url: str,
    *,
    show_panel: bool = False,
    frameless: bool = True,
    on_top: bool = True,
    transparent: bool = True,
    width: int = 420,
    height: int = 680,
) -> int:
    """Launch native desktop shell (tray + floating orb + on-demand frameless panel)."""
    try:
        host, port = validate_loopback_url(url)
    except ValueError as exc:
        logger.error("Refusing invalid loopback URL: %s", exc)
        return EXIT_INVALID_ARGS

    normalized_url = f"http://{host}:{port}"
    try:
        qt = _load_pyqt6_modules()
    except ImportError:
        return _run_pywebview_fallback(
            normalized_url,
            frameless=frameless,
            on_top=on_top,
            transparent=transparent,
            width=width,
            height=height,
        )

    return _run_pyqt6_shell(
        qt,
        base_url=normalized_url,
        show_panel=show_panel,
        frameless=frameless,
        on_top=on_top,
        transparent=transparent,
        width=width,
        height=height,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for `python -m nova.adapters.desktop_window <loopback_url> [--show-panel]`."""
    raw_args = list(sys.argv[1:] if argv is None else argv)
    try:
        parsed = parse_window_args(raw_args)
    except ValueError as exc:
        print(
            f"Invalid arguments ({exc}). "
            "Usage: python -m nova.adapters.desktop_window <loopback_url> [--show-panel]",
            file=sys.stderr,
        )
        return EXIT_INVALID_ARGS

    return run_window(parsed.base_url, show_panel=parsed.show_panel)


if __name__ == "__main__":
    sys.exit(main())
