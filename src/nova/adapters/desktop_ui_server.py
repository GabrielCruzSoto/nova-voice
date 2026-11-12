"""Loopback 127.0.0.1 asyncio HTTP + SSE bridge for Nova Desktop UI (NOVA-004 OP-1..OP-6)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import random
import re
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nova.application.ui_state_hub import UiStateHub
from nova.domain.errors import (
    ApprovalNotFoundError,
    DesktopUiError,
    NovaDomainError,
    WorkspaceResolutionError,
    redact_sensitive_text,
)
from nova.domain.events import (
    CommandApprovalResolvedEvent,
    OrchestratorEventBus,
    RunnerFailureEvent,
)
from nova.domain.models import CallSession, WorkspaceContext, WorkspaceQueryMode
from nova.domain.ports import FolderPickerPort

MAX_HTTP_BODY_BYTES = 65_536
MAX_HEADER_LINE_BYTES = 8_192
MAX_HEADER_LINES = 64
MAX_UTTERANCE_CHARS = 16_000
SESSION_COOKIE_NAME = "nova_ui_session"
CSRF_HEADER_NAME = "x-nova-csrf"
CSP_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)

_SAFE_CORR_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_SAFE_AUDIT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SAFE_ASSET_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\.(js|css|svg)$")
_APPROVAL_PATH_RE = re.compile(r"^/api/approvals/([A-Za-z0-9_-]{1,80})/resolve$")

_HTTP_STATUS_TEXT: dict[int, str] = {
    200: "OK",
    202: "Accepted",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    413: "Payload Too Large",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


class OrchestratorUiTargetPort(Protocol):
    """Application interface invoked by DesktopUiServer (satisfied by NovaOrchestrator)."""

    async def select_workspace(
        self,
        query: str,
        mode: WorkspaceQueryMode = "ui_picker",
        correlation_id: str | None = None,
    ) -> WorkspaceContext: ...

    async def handle_user_utterance(
        self,
        utterance: str,
        correlation_id: str | None = None,
    ) -> None: ...

    async def barge_in(self) -> None: ...

    async def resolve_command_approval(
        self,
        audit_id: str,
        *,
        approved: bool,
        resolved_by: str = "user_ui",
    ) -> CommandApprovalResolvedEvent: ...


class VoiceControlUiPort(Protocol):
    """Optional voice coordinator control port invoked by DesktopUiServer (OP-3)."""

    @property
    def session(self) -> CallSession: ...

    async def start_call(self, reason: str = "ui_call_start") -> None: ...

    async def end_call(self, reason: str = "ui_call_end") -> None: ...

    async def barge_in(self) -> None: ...


@dataclass(slots=True, frozen=True)
class _UiBridgeSession:
    cache_key: str
    session_id: str
    session_secret: str
    csrf_token: str
    expires_at_monotonic: float


class DesktopUiServerSettings(BaseModel):
    """Configuration boundary for DesktopUiServer enforcing 127.0.0.1 loopback only."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    host: str = "127.0.0.1"
    port: int = Field(default=0, ge=0, le=65535)
    request_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)
    heartbeat_interval_seconds: float = Field(default=15.0, gt=0.2, le=120.0)
    session_ttl_seconds: float = Field(default=28_800.0, gt=60.0, le=86_400.0)
    session_jitter_ratio: float = Field(default=0.05, ge=0.0, le=0.25)
    max_sessions: int = Field(default=8, ge=1, le=32)

    @field_validator("host")
    @classmethod
    def _validate_loopback_only(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned != "127.0.0.1":
            raise ValueError("DesktopUiServer must bind strictly to '127.0.0.1' loopback")
        return cleaned


def _default_static_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "ui" / "static"


class DesktopUiServer:
    """Confined 127.0.0.1 HTTP + SSE server implementing OP-1 through OP-6."""

    def __init__(
        self,
        *,
        orchestrator: OrchestratorUiTargetPort,
        state_hub: UiStateHub,
        event_bus: OrchestratorEventBus | None = None,
        voice_coordinator: VoiceControlUiPort | None = None,
        folder_picker: FolderPickerPort | None = None,
        static_dir: Path | None = None,
        settings: DesktopUiServerSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._orchestrator = orchestrator
        self._hub = state_hub
        self._event_bus = event_bus
        self._voice_coordinator = voice_coordinator
        self._folder_picker = folder_picker
        self._static_dir = static_dir or _default_static_dir()
        self._settings = settings or DesktopUiServerSettings()
        self._clock = clock
        self._rng = rng

        self._server: asyncio.Server | None = None
        self._bound_port = 0
        self._sessions: OrderedDict[str, _UiBridgeSession] = OrderedDict()
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._chat_turn_tasks: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int:
        return self._bound_port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._bound_port}"

    @property
    def extra_secrets(self) -> tuple[str, ...]:
        """Expose ephemeral session secrets and CSRF tokens for universal redaction."""
        items: list[str] = []
        for sess in self._sessions.values():
            items.append(sess.session_secret)
            items.append(sess.csrf_token)
        return tuple(items)

    async def __aenter__(self) -> DesktopUiServer:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Bind TCP socket strictly on 127.0.0.1 and accept local UI connections."""
        if self._server is not None:
            return
        self._hub.start()
        server = await asyncio.start_server(
            self._on_client_connected,
            host="127.0.0.1",
            port=self._settings.port,
        )
        self._server = server
        sockets = server.sockets or ()
        if sockets:
            sockname = sockets[0].getsockname()
            self._bound_port = int(sockname[1])

    async def close(self) -> None:
        """Close listening socket and cancel active SSE streams and background turns."""
        srv = self._server
        self._server = None
        if srv is not None:
            srv.close()

        for task in list(self._chat_turn_tasks):
            task.cancel()
        if self._chat_turn_tasks:
            await asyncio.gather(*self._chat_turn_tasks, return_exceptions=True)
        self._chat_turn_tasks.clear()

        for task in list(self._client_tasks):
            task.cancel()
        if self._client_tasks:
            await asyncio.gather(*self._client_tasks, return_exceptions=True)
        self._client_tasks.clear()

        if srv is not None:
            with contextlib.suppress(Exception):
                await srv.wait_closed()

    def _on_client_connected(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.create_task(self._handle_connection(reader, writer))
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    def _prune_sessions(self) -> None:
        now = self._clock()
        expired = [sid for sid, s in self._sessions.items() if s.expires_at_monotonic <= now]
        for sid in expired:
            self._sessions.pop(sid, None)

    def _get_or_create_session(self, cookie_header: str | None) -> tuple[_UiBridgeSession, bool]:
        existing = self._validate_session_cookie(cookie_header)
        if existing is not None:
            return existing, False

        self._prune_sessions()
        if len(self._sessions) >= self._settings.max_sessions:
            self._sessions.popitem(last=False)

        session_id = secrets.token_urlsafe(18)
        session_secret = secrets.token_urlsafe(24)
        csrf_token = secrets.token_urlsafe(24)
        jitter = 1.0 + self._rng(
            -self._settings.session_jitter_ratio,
            self._settings.session_jitter_ratio,
        )
        ttl = self._settings.session_ttl_seconds * jitter
        sess = _UiBridgeSession(
            cache_key=f"nova:ui:bridge:session:{session_id}",
            session_id=session_id,
            session_secret=session_secret,
            csrf_token=csrf_token,
            expires_at_monotonic=self._clock() + ttl,
        )
        self._sessions[session_id] = sess
        return sess, True

    def _validate_session_cookie(self, cookie_header: str | None) -> _UiBridgeSession | None:
        if not cookie_header or len(cookie_header) > 4096:
            return None
        self._prune_sessions()
        jar = SimpleCookie()
        try:
            jar.load(cookie_header)
        except Exception:
            return None
        morsel = jar.get(SESSION_COOKIE_NAME)
        if morsel is None:
            return None
        sid, dot, provided_secret = morsel.value.partition(".")
        if not sid or not dot or not provided_secret:
            return None
        sess = self._sessions.get(sid)
        if sess is None:
            return None
        if not hmac.compare_digest(sess.session_secret, provided_secret):
            return None
        return sess

    def _validate_host_and_origin(
        self,
        headers_multi: dict[str, list[str]],
        *,
        is_mutating: bool,
    ) -> bool:
        hosts = headers_multi.get("host", [])
        if len(hosts) != 1:
            return False
        host_val = hosts[0].strip()
        if host_val not in (f"127.0.0.1:{self._bound_port}", "127.0.0.1"):
            return False

        sec_site = headers_multi.get("sec-fetch-site", [])
        if any(v.strip().lower() == "cross-site" for v in sec_site):
            return False

        origins = headers_multi.get("origin", [])
        if origins:
            if len(origins) != 1:
                return False
            if origins[0].strip() != f"http://127.0.0.1:{self._bound_port}":
                return False
        elif is_mutating:
            # Allow CLI/same-origin loopback requests without Origin header,
            # while still requiring valid HttpOnly session cookie + X-Nova-CSRF.
            pass
        return True

    @staticmethod
    def _resolve_correlation_id(headers: dict[str, str]) -> str:
        candidate = headers.get("x-correlation-id", "").strip()
        if candidate and _SAFE_CORR_RE.fullmatch(candidate):
            return candidate
        return f"corr-{uuid.uuid4()}"

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        corr_id = f"corr-{uuid.uuid4()}"
        try:
            req_line = await asyncio.wait_for(
                reader.readline(),
                timeout=self._settings.request_timeout_seconds,
            )
            if not req_line:
                return
            if len(req_line) > MAX_HEADER_LINE_BYTES:
                await self._write_error(
                    writer,
                    400,
                    "UI_INVALID_JSON_OR_PAYLOAD",
                    "Request line exceeds maximum length.",
                    corr_id,
                )
                return

            parts = req_line.decode("latin-1").rstrip("\r\n").split(" ")
            if len(parts) != 3:
                await self._write_error(
                    writer,
                    400,
                    "UI_INVALID_JSON_OR_PAYLOAD",
                    "Malformed HTTP request line.",
                    corr_id,
                )
                return
            method, raw_target, _ = parts
            parsed_url = urlsplit(raw_target)
            path = parsed_url.path or "/"

            headers_multi: dict[str, list[str]] = {}
            headers: dict[str, str] = {}
            for _ in range(MAX_HEADER_LINES + 1):
                hline = await asyncio.wait_for(
                    reader.readline(),
                    timeout=self._settings.request_timeout_seconds,
                )
                if len(hline) > MAX_HEADER_LINE_BYTES:
                    await self._write_error(
                        writer,
                        400,
                        "UI_INVALID_JSON_OR_PAYLOAD",
                        "Header line too long.",
                        corr_id,
                    )
                    return
                if hline in (b"\r\n", b"\n", b""):
                    break
                decoded = hline.decode("latin-1").rstrip("\r\n")
                key, colon, val = decoded.partition(":")
                if not colon:
                    await self._write_error(
                        writer,
                        400,
                        "UI_INVALID_JSON_OR_PAYLOAD",
                        "Malformed HTTP header.",
                        corr_id,
                    )
                    return
                k_low = key.strip().lower()
                v_clean = val.strip()
                headers_multi.setdefault(k_low, []).append(v_clean)
                headers[k_low] = v_clean
            else:
                await self._write_error(
                    writer,
                    400,
                    "UI_INVALID_JSON_OR_PAYLOAD",
                    "Too many HTTP headers.",
                    corr_id,
                )
                return

            corr_id = self._resolve_correlation_id(headers)

            if "transfer-encoding" in headers_multi:
                await self._write_error(
                    writer,
                    400,
                    "UI_INVALID_JSON_OR_PAYLOAD",
                    "Transfer-Encoding is not supported on loopback UI bridge.",
                    corr_id,
                )
                return

            is_mutating = method not in ("GET", "HEAD")
            if not self._validate_host_and_origin(headers_multi, is_mutating=is_mutating):
                await self._write_error(
                    writer,
                    403,
                    "UI_FORBIDDEN_ORIGIN_OR_CSRF",
                    "Invalid Host or Origin header for loopback UI bridge.",
                    corr_id,
                )
                return

            content_length = 0
            if "content-length" in headers_multi:
                cl_vals = headers_multi["content-length"]
                if len(cl_vals) != 1 or not cl_vals[0].isdigit():
                    await self._write_error(
                        writer,
                        400,
                        "UI_INVALID_JSON_OR_PAYLOAD",
                        "Invalid Content-Length header.",
                        corr_id,
                    )
                    return
                content_length = int(cl_vals[0])
                if content_length > MAX_HTTP_BODY_BYTES:
                    await self._write_error(
                        writer,
                        413,
                        "UI_PAYLOAD_TOO_LARGE",
                        f"HTTP payload exceeds {MAX_HTTP_BODY_BYTES} bytes.",
                        corr_id,
                    )
                    return

            body_bytes = b""
            if content_length > 0:
                body_bytes = await asyncio.wait_for(
                    reader.readexactly(content_length),
                    timeout=self._settings.request_timeout_seconds,
                )

            await self._dispatch_request(
                reader=reader,
                writer=writer,
                method=method,
                path=path,
                headers=headers,
                body_bytes=body_bytes,
                corr_id=corr_id,
            )
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            safe_msg = redact_sensitive_text(str(exc), extra_secrets=self.extra_secrets)
            with contextlib.suppress(Exception):
                await self._write_error(
                    writer,
                    400,
                    "UI_INVALID_JSON_OR_PAYLOAD",
                    f"Unhandled bridge request error: {safe_msg}",
                    corr_id,
                )
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _dispatch_request(
        self,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
        body_bytes: bytes,
        corr_id: str,
    ) -> None:
        if method == "GET" and path == "/":
            await self._handle_index(writer, headers.get("cookie"), corr_id)
            return

        if method == "GET" and path.startswith("/static/"):
            asset_name = path.removeprefix("/static/")
            await self._handle_static_asset(writer, asset_name, corr_id)
            return

        if method == "GET" and path == "/api/state":
            sess = self._validate_session_cookie(headers.get("cookie"))
            if sess is None:
                await self._write_error(
                    writer,
                    403,
                    "UI_FORBIDDEN_ORIGIN_OR_CSRF",
                    "Missing or invalid UI session cookie.",
                    corr_id,
                )
                return
            snap = await self._hub.build_snapshot(csrf_token=sess.csrf_token)
            await self._write_json(writer, 200, snap.model_dump(), corr_id)
            return

        if method == "GET" and path == "/api/events":
            sess = self._validate_session_cookie(headers.get("cookie"))
            if sess is None:
                await self._write_error(
                    writer,
                    403,
                    "UI_FORBIDDEN_ORIGIN_OR_CSRF",
                    "Missing or invalid UI session cookie.",
                    corr_id,
                )
                return
            await self._handle_sse_stream(reader, writer, sess, corr_id)
            return

        if method == "POST" and path.startswith("/api/"):
            sess = self._validate_session_cookie(headers.get("cookie"))
            provided_csrf = headers.get(CSRF_HEADER_NAME, "")
            if (
                sess is None
                or not provided_csrf
                or not hmac.compare_digest(sess.csrf_token, provided_csrf)
            ):
                await self._write_error(
                    writer,
                    403,
                    "UI_FORBIDDEN_ORIGIN_OR_CSRF",
                    "Invalid session cookie or X-Nova-CSRF header.",
                    corr_id,
                )
                return
            await self._dispatch_post_api(writer, path, body_bytes, corr_id)
            return

        await self._write_error(
            writer,
            404,
            "UI_ROUTE_NOT_FOUND",
            f"Unknown UI route: {method} {path}",
            corr_id,
        )

    async def _handle_index(
        self,
        writer: asyncio.StreamWriter,
        cookie_header: str | None,
        corr_id: str,
    ) -> None:
        sess, is_new = self._get_or_create_session(cookie_header)
        html_bytes = await asyncio.to_thread(self._read_static_file_sync, "index.html")
        if html_bytes is None:
            await self._write_error(
                writer,
                404,
                "UI_ROUTE_NOT_FOUND",
                "UI index.html asset not found.",
                corr_id,
            )
            return

        extra_headers: dict[str, str] = {}
        if is_new:
            cookie_val = f"{sess.session_id}.{sess.session_secret}"
            extra_headers["Set-Cookie"] = (
                f"{SESSION_COOKIE_NAME}={cookie_val}; HttpOnly; SameSite=Strict; Path=/"
            )
        await self._write_bytes(
            writer,
            200,
            "text/html; charset=utf-8",
            html_bytes,
            corr_id,
            extra_headers=extra_headers,
        )

    async def _handle_static_asset(
        self,
        writer: asyncio.StreamWriter,
        asset_name: str,
        corr_id: str,
    ) -> None:
        if not _SAFE_ASSET_RE.fullmatch(asset_name):
            await self._write_error(
                writer,
                404,
                "UI_ROUTE_NOT_FOUND",
                "Static asset not found.",
                corr_id,
            )
            return
        content = await asyncio.to_thread(self._read_static_file_sync, asset_name)
        if content is None:
            await self._write_error(
                writer,
                404,
                "UI_ROUTE_NOT_FOUND",
                "Static asset not found.",
                corr_id,
            )
            return
        if asset_name.endswith(".css"):
            mime = "text/css; charset=utf-8"
        elif asset_name.endswith(".svg"):
            mime = "image/svg+xml"
        else:
            mime = "text/javascript; charset=utf-8"
        await self._write_bytes(writer, 200, mime, content, corr_id)

    def _read_static_file_sync(self, filename: str) -> bytes | None:
        base_real = Path(os.path.realpath(str(self._static_dir)))
        candidate = Path(os.path.realpath(str(base_real / filename)))
        if not candidate.is_relative_to(base_real) or candidate == base_real:
            return None
        if not candidate.is_file():
            return None
        return candidate.read_bytes()

    async def _handle_sse_stream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        sess: _UiBridgeSession,
        corr_id: str,
    ) -> None:
        try:
            sub = self._hub.subscribe_sse()
        except DesktopUiError as exc:
            await self._write_error(writer, 429, exc.code, exc.sanitized_message, corr_id)
            return

        eof_task = asyncio.create_task(reader.read(1))
        queue_task: asyncio.Task[Any] | None = None
        try:
            hdr = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/event-stream; charset=utf-8\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n"
                "X-Content-Type-Options: nosniff\r\n"
                f"X-Correlation-Id: {corr_id}\r\n"
                f"Content-Security-Policy: {CSP_POLICY}\r\n\r\n"
            )
            writer.write(hdr.encode("latin-1"))

            init_snap = await self._hub.build_snapshot(csrf_token=sess.csrf_token)
            init_frame = f"event: snapshot\ndata: {init_snap.model_dump_json()}\n\n"
            writer.write(init_frame.encode("utf-8"))
            await writer.drain()

            while self._server is not None:
                if queue_task is None:
                    queue_task = asyncio.create_task(sub.queue.get())
                done, _ = await asyncio.wait(
                    {queue_task, eof_task},
                    timeout=self._settings.heartbeat_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if eof_task in done:
                    break
                if not done:
                    writer.write(b": heartbeat\n\n")
                    await writer.drain()
                    continue

                item = queue_task.result()
                queue_task = None
                if item.kind == "resync_snapshot":
                    snap = await self._hub.build_snapshot(csrf_token=sess.csrf_token)
                    frame = f"event: snapshot\ndata: {snap.model_dump_json()}\n\n"
                    writer.write(frame.encode("utf-8"))
                elif item.envelope is not None:
                    frame = f"event: nova_event\ndata: {item.envelope.model_dump_json()}\n\n"
                    writer.write(frame.encode("utf-8"))
                await writer.drain()
        except (ConnectionError, BrokenPipeError, OSError):
            return
        finally:
            for pending_t in (queue_task, eof_task):
                if pending_t is not None and not pending_t.done():
                    pending_t.cancel()
                    with contextlib.suppress(asyncio.CancelledError, OSError):
                        await pending_t
            self._hub.unsubscribe_sse(sub)

    def _parse_json_object(self, body_bytes: bytes) -> dict[str, Any]:
        if not body_bytes:
            return {}
        try:
            parsed = json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DesktopUiError(
                code="UI_INVALID_JSON_OR_PAYLOAD",
                message="Malformed JSON request body.",
            ) from exc
        if not isinstance(parsed, dict):
            raise DesktopUiError(
                code="UI_INVALID_JSON_OR_PAYLOAD",
                message="JSON request body must be an object.",
            )
        return parsed

    async def _dispatch_post_api(
        self,
        writer: asyncio.StreamWriter,
        path: str,
        body_bytes: bytes,
        corr_id: str,
    ) -> None:
        try:
            payload = self._parse_json_object(body_bytes)
        except DesktopUiError as exc:
            await self._write_error(writer, 400, exc.code, exc.sanitized_message, corr_id)
            return

        if path == "/api/chat":
            raw_utterance = payload.get("utterance")
            if not isinstance(raw_utterance, str):
                await self._write_error(
                    writer,
                    400,
                    "UI_INVALID_JSON_OR_PAYLOAD",
                    "Field 'utterance' must be a string.",
                    corr_id,
                )
                return
            trimmed = raw_utterance.strip()
            if not trimmed or len(trimmed) > MAX_UTTERANCE_CHARS:
                await self._write_error(
                    writer,
                    422,
                    "UI_EMPTY_UTTERANCE",
                    f"Utterance must contain between 1 and {MAX_UTTERANCE_CHARS} characters.",
                    corr_id,
                )
                return
            await self._hub.record_user_utterance(trimmed, correlation_id=corr_id)
            self._spawn_chat_turn(trimmed, corr_id)
            await self._write_json(
                writer,
                202,
                {"accepted": True, "correlation_id": corr_id},
                corr_id,
            )
            return

        if path in ("/api/call/start", "/api/call/end", "/api/call/barge-in"):
            if self._voice_coordinator is None:
                if path == "/api/call/barge-in":
                    await self._orchestrator.barge_in()
                await self._write_error(
                    writer,
                    503,
                    "UI_VOICE_UNAVAILABLE",
                    "Local voice pipeline is not enabled in this session.",
                    corr_id,
                )
                return
            action = path.rsplit("/", 1)[1]
            if action == "start":
                await self._voice_coordinator.start_call(reason="ui_call_start")
            elif action == "end":
                await self._voice_coordinator.end_call(reason="ui_call_end")
            else:
                await self._voice_coordinator.barge_in()
            await self._write_json(
                writer,
                200,
                {"ok": True, "call_state": self._voice_coordinator.session.state},
                corr_id,
            )
            return

        if path == "/api/workspace/select":
            raw_query = payload.get("path_or_query")
            if not isinstance(raw_query, str) or not raw_query.strip():
                await self._write_error(
                    writer,
                    400,
                    "UI_INVALID_JSON_OR_PAYLOAD",
                    "Field 'path_or_query' must be a non-empty string.",
                    corr_id,
                )
                return
            await self._select_workspace_and_respond(writer, raw_query.strip(), corr_id)
            return

        if path == "/api/workspace/pick-native":
            if self._folder_picker is None:
                await self._write_error(
                    writer,
                    503,
                    "UI_NATIVE_PICKER_UNAVAILABLE",
                    "Native GTK3 folder picker is not configured.",
                    corr_id,
                )
                return
            try:
                chosen = await self._folder_picker.pick_directory()
            except DesktopUiError as exc:
                await self._write_error(writer, 503, exc.code, exc.sanitized_message, corr_id)
                return
            if chosen is None:
                await self._write_json(
                    writer,
                    200,
                    {"cancelled": True, "workspace": None},
                    corr_id,
                )
                return
            await self._select_workspace_and_respond(writer, str(chosen), corr_id)
            return

        approval_match = _APPROVAL_PATH_RE.fullmatch(path)
        if approval_match is not None:
            audit_id = approval_match.group(1)
            approved = payload.get("approved")
            if not isinstance(approved, bool) or not _SAFE_AUDIT_ID_RE.fullmatch(audit_id):
                await self._write_error(
                    writer,
                    400,
                    "UI_INVALID_JSON_OR_PAYLOAD",
                    "Field 'approved' must be a boolean.",
                    corr_id,
                )
                return
            try:
                await self._orchestrator.resolve_command_approval(
                    audit_id,
                    approved=approved,
                    resolved_by="user_ui",
                )
            except ApprovalNotFoundError:
                await self._write_error(
                    writer,
                    404,
                    "GATEKEEPER_AUDIT_NOT_FOUND",
                    f"Command approval '{audit_id}' was already resolved or expired.",
                    corr_id,
                )
                return
            except NovaDomainError as exc:
                await self._write_error(writer, 400, exc.code, exc.sanitized_message, corr_id)
                return
            await self._write_json(
                writer,
                200,
                {"audit_id": audit_id, "approved": approved, "resolved_by": "user_ui"},
                corr_id,
            )
            return

        await self._write_error(
            writer,
            404,
            "UI_ROUTE_NOT_FOUND",
            f"Unknown POST route: {path}",
            corr_id,
        )

    async def _select_workspace_and_respond(
        self,
        writer: asyncio.StreamWriter,
        query: str,
        corr_id: str,
    ) -> None:
        try:
            ctx = await self._orchestrator.select_workspace(
                query,
                mode="ui_picker",
                correlation_id=corr_id,
            )
        except WorkspaceResolutionError as exc:
            status_and_code = {
                "WORKSPACE_OUTSIDE_ALLOWED_ROOTS": (403, "WORKSPACE_OUTSIDE_ALLOWED_ROOTS"),
                "WORKSPACE_NOT_FOUND": (404, "WORKSPACE_NOT_FOUND"),
                "WORKSPACE_AMBIGUOUS_MATCH": (409, "WORKSPACE_AMBIGUOUS"),
                "WORKSPACE_RESOLUTION_TIMEOUT": (504, "WORKSPACE_RESOLUTION_TIMEOUT"),
            }.get(exc.code, (400, "UI_INVALID_JSON_OR_PAYLOAD"))
            await self._write_error(
                writer,
                status_and_code[0],
                status_and_code[1],
                exc.sanitized_message,
                corr_id,
            )
            return

        await self._write_json(
            writer,
            200,
            {
                "cancelled": False,
                "workspace": {
                    "display_name": ctx.name,
                    "canonical_path": ctx.canonical_path,
                    "selected_via": ctx.selected_via,
                },
            },
            corr_id,
        )

    def _spawn_chat_turn(self, utterance: str, corr_id: str) -> None:
        async def _runner() -> None:
            try:
                await self._orchestrator.handle_user_utterance(utterance, correlation_id=corr_id)
            except asyncio.CancelledError:
                raise
            except NovaDomainError as exc:
                if self._event_bus is not None:
                    await self._event_bus.publish(
                        RunnerFailureEvent(
                            correlation_id=corr_id,
                            error_code=exc.code,
                            sanitized_message=exc.sanitized_message,
                            recoverable=True,
                        )
                    )
            except Exception as exc:
                if self._event_bus is not None:
                    safe = redact_sensitive_text(str(exc), extra_secrets=self.extra_secrets)
                    await self._event_bus.publish(
                        RunnerFailureEvent(
                            correlation_id=corr_id,
                            error_code="ORCHESTRATOR_UNEXPECTED_FAILURE",
                            sanitized_message=safe,
                            recoverable=True,
                        )
                    )

        task = asyncio.create_task(_runner())
        self._chat_turn_tasks.add(task)
        task.add_done_callback(self._chat_turn_tasks.discard)

    async def _write_error(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        error_code: str,
        message: str,
        corr_id: str,
    ) -> None:
        safe_msg = redact_sensitive_text(message, extra_secrets=self.extra_secrets)
        await self._write_json(
            writer,
            status,
            {
                "error_code": error_code,
                "message": safe_msg,
                "correlation_id": corr_id,
            },
            corr_id,
        )

    async def _write_json(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict[str, Any],
        corr_id: str,
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await self._write_bytes(writer, status, "application/json; charset=utf-8", raw, corr_id)

    async def _write_bytes(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        content_type: str,
        body: bytes,
        corr_id: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        reason = _HTTP_STATUS_TEXT.get(status, "OK")
        lines = [
            f"HTTP/1.1 {status} {reason}\r\n",
            f"Content-Type: {content_type}\r\n",
            f"Content-Length: {len(body)}\r\n",
            "Cache-Control: no-store\r\n",
            "Connection: close\r\n",
            "X-Content-Type-Options: nosniff\r\n",
            f"X-Correlation-Id: {corr_id}\r\n",
            f"Content-Security-Policy: {CSP_POLICY}\r\n",
        ]
        if extra_headers:
            for k, v in extra_headers.items():
                lines.append(f"{k}: {v}\r\n")
        lines.append("\r\n")
        writer.write("".join(lines).encode("latin-1") + body)
        await writer.drain()
