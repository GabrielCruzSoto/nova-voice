"""Loopback 127.0.0.1 asyncio HTTP + SSE bridge for Nova Desktop UI (NOVA-004 OP-1..OP-6)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import inspect
import json
import os
import random
import re
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nova.adapters.workspace_resolver import LocalWorkspaceResolver
from nova.application.live_diagnostics import LiveDiagnosticsService
from nova.application.models_provisioning import VoiceModelsProvisioningService
from nova.application.settings_service import SettingsService
from nova.application.task_manager import (
    _SAFE_MODEL_TOKEN_RE,
    TaskManager,
    _validate_and_resolve_workspace,
)
from nova.application.ui_state_hub import UiStateHub
from nova.domain.errors import (
    ApprovalNotFoundError,
    DesktopUiError,
    NovaDomainError,
    TaskManagerError,
    TaskNotFoundError,
    TaskQueueFullError,
    TaskStateError,
    WorkspaceResolutionError,
    redact_sensitive_text,
)
from nova.domain.events import (
    CommandApprovalResolvedEvent,
    OrchestratorEventBus,
    RunnerFailureEvent,
)
from nova.domain.models import (
    MAX_TASK_OBJECTIVE_CHARS,
    AgentCatalogSnapshot,
    AgentEngineId,
    AgentTier,
    CallSession,
    InitializeWorkspaceRequest,
    TaskStatus,
    WorkspaceContext,
    WorkspaceQueryMode,
)
from nova.domain.ports import CredentialStorePort, FolderPickerPort

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
_TASK_ID_PATH_RE = re.compile(r"^/api/tasks/([A-Za-z0-9_-]{1,80})$")
_TASK_CANCEL_PATH_RE = re.compile(r"^/api/tasks/([A-Za-z0-9_-]{1,80})/cancel$")
_VALID_TASK_STATUSES: frozenset[str] = frozenset(
    {"queued", "running", "waiting_approval", "completed", "failed", "cancelled"}
)
_VALID_AGENT_PROVIDERS: frozenset[str] = frozenset({"pi", "agy", "claude", "opencode"})

_HTTP_STATUS_TEXT: dict[int, str] = {
    200: "OK",
    201: "Created",
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
        settings_service: SettingsService | None = None,
        workspace_resolver: LocalWorkspaceResolver | None = None,
        diagnostics_service: LiveDiagnosticsService | None = None,
        health_report_provider: Callable[[], Any] | None = None,
        agent_catalog_provider: Callable[[], AgentCatalogSnapshot] | None = None,
        credential_store: CredentialStorePort | None = None,
        health_recheck_callback: Callable[[], Awaitable[tuple[Any, bool]]] | None = None,
        models_provisioning_service: VoiceModelsProvisioningService | None = None,
        task_manager: TaskManager | None = None,
        shutdown_callback: Callable[[str], None] | None = None,
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
        self._settings_service = settings_service
        self._workspace_resolver = workspace_resolver
        self._diagnostics_service = diagnostics_service
        self._health_report_provider = health_report_provider
        self._agent_catalog_provider = agent_catalog_provider
        self._credential_store = credential_store
        self._health_recheck_callback = health_recheck_callback
        self._models_provisioning_service = models_provisioning_service
        self._task_manager = task_manager or getattr(orchestrator, "task_manager", None)
        self._shutdown_callback = shutdown_callback
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

    def set_voice_coordinator(self, voice_coordinator: VoiceControlUiPort | None) -> None:
        """Bind or replace the active voice coordinator after hot-reload."""
        self._voice_coordinator = voice_coordinator

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
                query=parsed_url.query,
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
        query: str = "",
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

        if method == "GET" and path == "/api/settings":
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
            if self._settings_service is None:
                await self._write_error(
                    writer,
                    503,
                    "UI_SETTINGS_UNAVAILABLE",
                    "Persistent settings service is not enabled in this session.",
                    corr_id,
                )
                return
            await self._write_json(
                writer,
                200,
                self._settings_service.get_settings_payload(),
                corr_id,
            )
            return

        if method == "GET" and path == "/api/audio/devices":
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
            if self._settings_service is None:
                await self._write_json(
                    writer,
                    200,
                    {"available": False, "backend": "none", "inputs": [], "outputs": []},
                    corr_id,
                )
                return
            catalog = await self._settings_service.list_audio_devices()
            await self._write_json(writer, 200, catalog.model_dump(), corr_id)
            return

        if method == "GET" and path == "/api/health":
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
            report = (
                self._health_report_provider()
                if self._health_report_provider is not None
                else None
            )
            if report is None:
                await self._write_json(
                    writer,
                    200,
                    {
                        "all_ok": True,
                        "missing_critical_count": 0,
                        "missing_optional_count": 0,
                        "components": [],
                        "alert_message": None,
                    },
                    corr_id,
                )
                return
            data = report.model_dump() if hasattr(report, "model_dump") else report
            await self._write_json(writer, 200, data, corr_id)
            return

        if method == "GET" and path == "/api/agents/catalog":
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
            if self._agent_catalog_provider is None:
                await self._write_json(
                    writer,
                    200,
                    AgentCatalogSnapshot().model_dump(),
                    corr_id,
                )
                return
            snapshot = self._agent_catalog_provider()
            await self._write_json(writer, 200, snapshot.model_dump(), corr_id)
            return

        if method == "GET" and path == "/api/models/status":
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
            if self._models_provisioning_service is None:
                await self._write_json(
                    writer,
                    200,
                    {
                        "status": "idle",
                        "all_installed": True,
                        "total_models": 0,
                        "completed_models": 0,
                        "current_model": None,
                        "bytes_downloaded": 0,
                        "total_bytes": 0,
                        "percent": 100,
                        "error": None,
                        "models": [],
                    },
                    corr_id,
                )
                return
            status_view = self._models_provisioning_service.get_status()
            await self._write_json(writer, 200, status_view.model_dump(), corr_id)
            return

        if method == "GET" and path == "/api/tasks":
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
            await self._handle_get_tasks(writer, query, corr_id)
            return

        if method == "GET":
            task_match = _TASK_ID_PATH_RE.fullmatch(path)
            if task_match is not None:
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
                await self._handle_get_task_by_id(writer, task_match.group(1), corr_id)
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

        if method in ("POST", "PUT", "DELETE") and path.startswith("/api/"):
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
            if method == "PUT":
                await self._dispatch_put_api(writer, path, body_bytes, corr_id)
            elif method == "DELETE":
                await self._dispatch_delete_api(writer, path, body_bytes, corr_id)
            else:
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

    async def _dispatch_put_api(
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

        if path == "/api/settings":
            if self._settings_service is None:
                await self._write_error(
                    writer,
                    503,
                    "UI_SETTINGS_UNAVAILABLE",
                    "Persistent settings service is not enabled in this session.",
                    corr_id,
                )
                return
            try:
                result = await self._settings_service.update_settings(
                    payload,
                    correlation_id=corr_id,
                )
            except DesktopUiError as exc:
                status_code = 503 if exc.code == "CONFIG_PERSIST_FAILED" else 422
                await self._write_error(
                    writer, status_code, exc.code, exc.sanitized_message, corr_id
                )
                return
            await self._write_json(writer, 200, result, corr_id)
            return

        if path == "/api/credentials":
            if self._credential_store is None:
                await self._write_error(
                    writer,
                    503,
                    "CREDENTIAL_STORE_UNAVAILABLE",
                    "Credential store is not enabled in this session.",
                    corr_id,
                )
                return
            provider = payload.get("provider")
            env_var = payload.get("env_var")
            value = payload.get("value")
            if (
                not isinstance(provider, str)
                or not isinstance(env_var, str)
                or not isinstance(value, str)
            ):
                await self._write_error(
                    writer,
                    400,
                    "INVALID_CREDENTIAL_REQUEST",
                    "Fields 'provider', 'env_var', and 'value' must be strings.",
                    corr_id,
                )
                return
            try:
                await asyncio.to_thread(
                    self._credential_store.set_secret, provider, env_var, value
                )
            except DesktopUiError as exc:
                status_code = 503 if exc.code == "CREDENTIAL_PERSIST_FAILED" else 400
                await self._write_error(
                    writer, status_code, exc.code, exc.sanitized_message, corr_id
                )
                return
            await self._write_json(
                writer,
                200,
                {
                    "ok": True,
                    "provider": provider.strip().lower(),
                    "env_var": env_var.strip(),
                    "configured": True,
                    "backend": self._credential_store.backend_name,
                },
                corr_id,
            )
            return

        await self._write_error(
            writer,
            404,
            "UI_ROUTE_NOT_FOUND",
            f"Unknown PUT route: {path}",
            corr_id,
        )

    async def _dispatch_delete_api(
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

        if path == "/api/credentials":
            if self._credential_store is None:
                await self._write_error(
                    writer,
                    503,
                    "CREDENTIAL_STORE_UNAVAILABLE",
                    "Credential store is not enabled in this session.",
                    corr_id,
                )
                return
            provider = payload.get("provider")
            env_var = payload.get("env_var")
            if not isinstance(provider, str) or not isinstance(env_var, str):
                await self._write_error(
                    writer,
                    400,
                    "INVALID_CREDENTIAL_REQUEST",
                    "Fields 'provider' and 'env_var' must be strings.",
                    corr_id,
                )
                return
            try:
                await asyncio.to_thread(
                    self._credential_store.delete_secret, provider, env_var
                )
            except DesktopUiError as exc:
                await self._write_error(writer, 400, exc.code, exc.sanitized_message, corr_id)
                return
            await self._write_json(
                writer,
                200,
                {
                    "ok": True,
                    "provider": provider.strip().lower(),
                    "env_var": env_var.strip(),
                    "configured": False,
                },
                corr_id,
            )
            return

        await self._write_error(
            writer,
            404,
            "UI_ROUTE_NOT_FOUND",
            f"Unknown DELETE route: {path}",
            corr_id,
        )

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

        if path == "/api/shutdown":
            if self._shutdown_callback is None:
                await self._write_error(
                    writer,
                    503,
                    "UI_SHUTDOWN_UNAVAILABLE",
                    "Runtime shutdown handler is not configured.",
                    corr_id,
                )
                return
            await self._write_json(
                writer,
                202,
                {"status": "shutting_down"},
                corr_id,
            )
            asyncio.get_running_loop().call_soon(self._shutdown_callback, "ui_http")
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

        if path == "/api/call/ptt":
            raw_action = payload.get("action")
            if raw_action not in ("start", "stop", "toggle"):
                await self._write_error(
                    writer,
                    422,
                    "INVALID_PTT_ACTION",
                    "Field 'action' must be one of: start, stop, toggle.",
                    corr_id,
                )
                return
            if self._voice_coordinator is None:
                await self._write_error(
                    writer,
                    503,
                    "VOICE_UNAVAILABLE",
                    "Local voice pipeline is not enabled in this session.",
                    corr_id,
                )
                return
            method_map = {
                "start": "start_ptt_hold",
                "stop": "stop_ptt_hold",
                "toggle": "trigger_ptt_toggle",
            }
            handler = getattr(self._voice_coordinator, method_map[raw_action], None)
            if handler is None:
                await self._write_error(
                    writer,
                    503,
                    "VOICE_UNAVAILABLE",
                    "Push-to-Talk control is not supported by the active voice coordinator.",
                    corr_id,
                )
                return
            await handler()
            await self._write_json(
                writer,
                200,
                {
                    "status": "accepted",
                    "ok": True,
                    "action": raw_action,
                    "call_state": self._voice_coordinator.session.state,
                },
                corr_id,
            )
            return

        if path == "/api/settings/shortcut":
            if self._settings_service is None:
                await self._write_error(
                    writer,
                    503,
                    "UI_SETTINGS_UNAVAILABLE",
                    "Persistent settings service is not enabled in this session.",
                    corr_id,
                )
                return
            raw_shortcut = payload.get("shortcut")
            if not isinstance(raw_shortcut, str) or not raw_shortcut.strip():
                await self._write_error(
                    writer,
                    400,
                    "UI_INVALID_JSON_OR_PAYLOAD",
                    "Field 'shortcut' must be a non-empty string.",
                    corr_id,
                )
                return
            try:
                reg_result = self._settings_service.register_shortcut(raw_shortcut.strip())
            except DesktopUiError as exc:
                await self._write_error(writer, 422, exc.code, exc.sanitized_message, corr_id)
                return
            await self._write_json(writer, 200, asdict(reg_result), corr_id)
            return

        if path == "/api/workspace/clear":
            clear_fn = getattr(self._orchestrator, "clear_workspace", None)
            if callable(clear_fn):
                res = clear_fn(correlation_id=corr_id)
                if inspect.isawaitable(res):
                    await res
            elif self._workspace_resolver is not None:
                await self._workspace_resolver.clear_active_workspace(corr_id)
            if self._settings_service is not None:
                await self._settings_service.clear_active_workspace()
            await self._write_json(
                writer,
                200,
                {
                    "cleared": True,
                    "active_workspace": None,
                    "process_cwd": str(Path.home().resolve()),
                },
                corr_id,
            )
            return

        if path == "/api/workspace/init":
            await self._handle_workspace_init(writer, payload, corr_id)
            return

        if path.startswith("/api/diagnostics/"):
            await self._handle_live_diagnostic(writer, path, payload, corr_id)
            return

        if path == "/api/models/provision":
            if self._models_provisioning_service is None:
                await self._write_error(
                    writer,
                    503,
                    "UI_MODELS_PROVISIONING_UNAVAILABLE",
                    "Models provisioning service is not configured.",
                    corr_id,
                )
                return
            state_view = await self._models_provisioning_service.start_provisioning()
            await self._write_json(writer, 202, state_view.model_dump(), corr_id)
            return

        if path == "/api/health/recheck":
            if self._health_recheck_callback is not None:
                report, voice_enabled = await self._health_recheck_callback()
                report_data = report.model_dump() if hasattr(report, "model_dump") else report
                await self._write_json(
                    writer,
                    200,
                    {"health": report_data, "voice_enabled": voice_enabled},
                    corr_id,
                )
                return
            report = (
                self._health_report_provider()
                if self._health_report_provider is not None
                else None
            )
            report_data = (
                report.model_dump()
                if (report is not None and hasattr(report, "model_dump"))
                else (
                    report
                    or {
                        "all_ok": True,
                        "missing_critical_count": 0,
                        "missing_optional_count": 0,
                        "components": [],
                        "alert_message": None,
                    }
                )
            )
            await self._write_json(
                writer,
                200,
                {"health": report_data, "voice_enabled": self._hub.voice_enabled},
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

        if path == "/api/tasks":
            await self._handle_post_task(writer, payload, corr_id)
            return

        task_cancel_match = _TASK_CANCEL_PATH_RE.fullmatch(path)
        if task_cancel_match is not None:
            await self._handle_cancel_task(writer, task_cancel_match.group(1), corr_id)
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

    async def _handle_get_tasks(
        self,
        writer: asyncio.StreamWriter,
        query: str,
        corr_id: str,
    ) -> None:
        if self._task_manager is None:
            await self._write_error(
                writer,
                503,
                "TASK_MANAGER_UNAVAILABLE",
                "TaskManager is not attached to DesktopUiServer.",
                corr_id,
            )
            return
        params = parse_qs(query, keep_blank_values=False)
        raw_status = params.get("status", [None])[0]
        status_filter: TaskStatus | None = None
        if raw_status is not None:
            if raw_status not in _VALID_TASK_STATUSES:
                await self._write_error(
                    writer,
                    400,
                    "UI_INVALID_QUERY",
                    f"Unsupported task status filter: '{raw_status}'.",
                    corr_id,
                )
                return
            status_filter = raw_status  # type: ignore[assignment]

        raw_limit = params.get("limit", ["50"])[0]
        try:
            limit_val = int(raw_limit)
        except ValueError:
            await self._write_error(
                writer,
                400,
                "UI_INVALID_QUERY",
                "Query parameter 'limit' must be an integer between 1 and 100.",
                corr_id,
            )
            return
        if limit_val < 1 or limit_val > 100:
            await self._write_error(
                writer,
                400,
                "UI_INVALID_QUERY",
                "Query parameter 'limit' must be between 1 and 100.",
                corr_id,
            )
            return

        ws_filter = params.get("workspace", [None])[0]
        tasks = await self._task_manager.list_tasks(
            limit=limit_val,
            status=status_filter,
            workspace_path=ws_filter,
        )
        projected = [self._hub.project_task(t).model_dump() for t in tasks]
        overview = self._hub.build_agents_overview().model_dump()
        await self._write_json(
            writer,
            200,
            {"tasks": projected, "agents_overview": overview},
            corr_id,
        )

    async def _handle_get_task_by_id(
        self,
        writer: asyncio.StreamWriter,
        task_id: str,
        corr_id: str,
    ) -> None:
        if self._task_manager is None:
            await self._write_error(
                writer,
                503,
                "TASK_MANAGER_UNAVAILABLE",
                "TaskManager is not attached to DesktopUiServer.",
                corr_id,
            )
            return
        try:
            rec = await self._task_manager.get_task(task_id)
        except TaskNotFoundError as exc:
            await self._write_error(writer, 404, exc.code, exc.sanitized_message, corr_id)
            return
        await self._write_json(
            writer,
            200,
            {"task": self._hub.project_task(rec).model_dump()},
            corr_id,
        )

    async def _handle_post_task(
        self,
        writer: asyncio.StreamWriter,
        payload: dict[str, Any],
        corr_id: str,
    ) -> None:
        if self._task_manager is None:
            await self._write_error(
                writer,
                503,
                "TASK_MANAGER_UNAVAILABLE",
                "TaskManager is not attached to DesktopUiServer.",
                corr_id,
            )
            return
        raw_obj = payload.get("objective")
        if not isinstance(raw_obj, str) or not raw_obj.strip():
            await self._write_error(
                writer,
                400,
                "TASK_INVALID_REQUEST",
                "Field 'objective' must be a non-empty string.",
                corr_id,
            )
            return
        if len(raw_obj.strip()) > MAX_TASK_OBJECTIVE_CHARS:
            await self._write_error(
                writer,
                400,
                "TASK_INVALID_REQUEST",
                f"Field 'objective' must not exceed {MAX_TASK_OBJECTIVE_CHARS} characters.",
                corr_id,
            )
            return

        raw_provider = payload.get("provider")
        provider: AgentEngineId | None = None
        if raw_provider is not None:
            if not isinstance(raw_provider, str) or raw_provider not in _VALID_AGENT_PROVIDERS:
                await self._write_error(
                    writer,
                    400,
                    "TASK_INVALID_REQUEST",
                    "Field 'provider' must be one of pi, agy, claude, opencode.",
                    corr_id,
                )
                return
            provider = raw_provider  # type: ignore[assignment]

        raw_model = payload.get("model")
        model: str | None = None
        if raw_model is not None:
            if not isinstance(raw_model, str):
                await self._write_error(
                    writer,
                    400,
                    "TASK_INVALID_REQUEST",
                    "Field 'model' must be a string.",
                    corr_id,
                )
                return
            cleaned_model = raw_model.strip()
            if cleaned_model and not _SAFE_MODEL_TOKEN_RE.fullmatch(cleaned_model):
                await self._write_error(
                    writer,
                    400,
                    "TASK_INVALID_REQUEST",
                    (
                        "Field 'model' must be <= 200 characters without whitespace, "
                        "control characters, or leading '-'."
                    ),
                    corr_id,
                )
                return
            model = cleaned_model or None

        raw_ws = payload.get("workspace_path")
        ws_path: str | None = None
        ws_name: str | None = None
        if raw_ws is not None:
            if not isinstance(raw_ws, str) or not raw_ws.strip():
                await self._write_error(
                    writer,
                    400,
                    "TASK_INVALID_REQUEST",
                    "Field 'workspace_path' must be a non-empty string when provided.",
                    corr_id,
                )
                return
            allowed_roots: tuple[str, ...] = ()
            if self._workspace_resolver is not None:
                raw_roots = getattr(self._workspace_resolver, "allowed_roots", ())
                if isinstance(raw_roots, tuple):
                    allowed_roots = raw_roots
            resolved_ws, ws_err_code, ws_err_msg = await asyncio.to_thread(
                _validate_and_resolve_workspace,
                raw_ws.strip(),
                allowed_roots,
            )
            if ws_err_code is not None:
                ws_status = 403 if ws_err_code == "WORKSPACE_OUTSIDE_ALLOWED_ROOTS" else 400
                await self._write_error(
                    writer,
                    ws_status,
                    ws_err_code,
                    ws_err_msg or "Invalid task workspace path.",
                    corr_id,
                )
                return
            ws_path = resolved_ws
            ws_name = Path(resolved_ws).name or resolved_ws
        elif self._workspace_resolver is not None and self._workspace_resolver.active_context:
            active = self._workspace_resolver.active_context
            ws_path = active.canonical_path
            ws_name = active.name
        elif self._hub.active_workspace is not None:
            ws_path = self._hub.active_workspace.canonical_path
            ws_name = self._hub.active_workspace.display_name

        if not ws_path:
            await self._write_error(
                writer,
                400,
                "WORKSPACE_REQUIRED",
                "Select an active workspace or pass 'workspace_path' to create a task.",
                corr_id,
            )
            return

        try:
            created = await self._task_manager.dispatch_task(
                workspace_canonical_path=ws_path,
                workspace_name=ws_name or Path(ws_path).name,
                objective=raw_obj.strip(),
                provider=provider,
                model=model,
                correlation_id=corr_id,
            )
        except TaskQueueFullError as exc:
            await self._write_error(writer, 429, exc.code, exc.sanitized_message, corr_id)
            return
        except TaskStateError as exc:
            await self._write_error(writer, 409, exc.code, exc.sanitized_message, corr_id)
            return
        except TaskManagerError as exc:
            if exc.code == "TASK_MANAGER_UNAVAILABLE":
                status = 503
            elif exc.code == "WORKSPACE_OUTSIDE_ALLOWED_ROOTS":
                status = 403
            else:
                status = 400
            await self._write_error(writer, status, exc.code, exc.sanitized_message, corr_id)
            return

        await self._write_json(
            writer,
            201,
            {"task": self._hub.project_task(created).model_dump()},
            corr_id,
        )

    async def _handle_cancel_task(
        self,
        writer: asyncio.StreamWriter,
        task_id: str,
        corr_id: str,
    ) -> None:
        if self._task_manager is None:
            await self._write_error(
                writer,
                503,
                "TASK_MANAGER_UNAVAILABLE",
                "TaskManager is not attached to DesktopUiServer.",
                corr_id,
            )
            return
        try:
            cancelled = await self._task_manager.cancel_task(task_id)
        except TaskNotFoundError as exc:
            await self._write_error(writer, 404, exc.code, exc.sanitized_message, corr_id)
            return
        except TaskManagerError as exc:
            await self._write_error(writer, 400, exc.code, exc.sanitized_message, corr_id)
            return
        await self._write_json(
            writer,
            200,
            {"task": self._hub.project_task(cancelled).model_dump()},
            corr_id,
        )

    async def _handle_workspace_init(
        self,
        writer: asyncio.StreamWriter,
        payload: dict[str, Any],
        corr_id: str,
    ) -> None:
        if self._workspace_resolver is None:
            await self._write_error(
                writer,
                503,
                "UI_WORKSPACE_INIT_UNAVAILABLE",
                "Workspace initializer is not configured.",
                corr_id,
            )
            return

        raw_parent = payload.get("parent_root")
        raw_slug = payload.get("workspace_slug")
        raw_display = payload.get("display_name")
        raw_projects = payload.get("projects", [])
        raw_activate = payload.get("activate", True)

        if (
            not isinstance(raw_parent, str)
            or not raw_parent.strip()
            or not isinstance(raw_slug, str)
            or (raw_display is not None and not isinstance(raw_display, str))
            or not isinstance(raw_projects, list)
            or not all(isinstance(x, str) for x in raw_projects)
            or not isinstance(raw_activate, bool)
        ):
            await self._write_error(
                writer,
                400,
                "UI_INVALID_JSON_OR_PAYLOAD",
                "Invalid fields in workspace initialization payload.",
                corr_id,
            )
            return

        try:
            init_req = InitializeWorkspaceRequest(
                correlation_id=corr_id,
                parent_root=raw_parent.strip(),
                workspace_slug=raw_slug.strip(),
                display_name=raw_display.strip() if isinstance(raw_display, str) else None,
                projects=tuple(x.strip() for x in raw_projects),
                activate=raw_activate,
            )
            ctx = await self._workspace_resolver.initialize_workspace(init_req)
            if raw_activate:
                ctx = await self._orchestrator.select_workspace(
                    ctx.canonical_path,
                    mode="ui_picker",
                    correlation_id=corr_id,
                )
                if self._settings_service is not None:
                    await self._settings_service.record_active_workspace(ctx.canonical_path)
        except WorkspaceResolutionError as exc:
            status_and_code = {
                "WORKSPACE_HOME_FORBIDDEN": (403, "WORKSPACE_HOME_FORBIDDEN"),
                "WORKSPACE_OUTSIDE_ALLOWED_ROOTS": (403, "WORKSPACE_OUTSIDE_ALLOWED_ROOTS"),
                "WORKSPACE_ALREADY_EXISTS": (409, "WORKSPACE_ALREADY_EXISTS"),
                "WORKSPACE_INVALID_MANIFEST": (422, "WORKSPACE_INVALID_MANIFEST"),
                "WORKSPACE_RESOLUTION_TIMEOUT": (504, "WORKSPACE_RESOLUTION_TIMEOUT"),
            }.get(exc.code, (422, "WORKSPACE_INVALID_MANIFEST"))
            await self._write_error(
                writer,
                status_and_code[0],
                status_and_code[1],
                exc.sanitized_message,
                corr_id,
            )
            return
        except Exception as exc:
            await self._write_error(
                writer,
                422,
                "WORKSPACE_INVALID_MANIFEST",
                str(exc),
                corr_id,
            )
            return

        await self._write_json(
            writer,
            201,
            {
                "created": True,
                "manifest_path": ctx.manifest_path,
                "workspace": {
                    "display_name": ctx.name,
                    "canonical_path": ctx.canonical_path,
                    "selected_via": ctx.selected_via,
                    "topology": ctx.topology,
                    "projects": [p.model_dump() for p in ctx.projects],
                },
            },
            corr_id,
        )

    async def _handle_live_diagnostic(
        self,
        writer: asyncio.StreamWriter,
        path: str,
        payload: dict[str, Any],
        corr_id: str,
    ) -> None:
        if self._diagnostics_service is None:
            await self._write_error(
                writer,
                503,
                "UI_DIAGNOSTICS_UNAVAILABLE",
                "Live diagnostics service is not enabled.",
                corr_id,
            )
            return

        try:
            if path == "/api/diagnostics/microphone":
                cap = payload.get("capture_target")
                dur = payload.get("duration_seconds", 2.0)
                if (cap is not None and not isinstance(cap, str)) or not isinstance(
                    dur, (int, float)
                ) or isinstance(dur, bool):
                    await self._write_error(
                        writer,
                        400,
                        "UI_INVALID_JSON_OR_PAYLOAD",
                        "Invalid microphone diagnostic parameters.",
                        corr_id,
                    )
                    return
                res_mic = await self._diagnostics_service.test_microphone(
                    capture_target=cap,
                    duration_seconds=float(dur),
                )
                await self._write_json(writer, 200, res_mic.model_dump(), corr_id)
                return

            if path == "/api/diagnostics/audio-output":
                play = payload.get("playback_target")
                if play is not None and not isinstance(play, str):
                    await self._write_error(
                        writer,
                        400,
                        "UI_INVALID_JSON_OR_PAYLOAD",
                        "Invalid audio-output diagnostic parameters.",
                        corr_id,
                    )
                    return
                res_out = await self._diagnostics_service.test_audio_output(playback_target=play)
                await self._write_json(writer, 200, res_out.model_dump(), corr_id)
                return

            if path == "/api/diagnostics/pi":
                prov = payload.get("provider")
                mod = payload.get("model")
                if (prov is not None and not isinstance(prov, str)) or (
                    mod is not None and not isinstance(mod, str)
                ):
                    await self._write_error(
                        writer,
                        400,
                        "UI_INVALID_JSON_OR_PAYLOAD",
                        "Invalid Pi diagnostic parameters.",
                        corr_id,
                    )
                    return
                res_pi = await self._diagnostics_service.test_pi_connection(
                    provider=prov,
                    model=mod,
                )
                await self._write_json(writer, 200, res_pi.model_dump(), corr_id)
                return

            if path == "/api/diagnostics/antigravity":
                agy_mod = payload.get("model")
                if agy_mod is not None and not isinstance(agy_mod, str):
                    await self._write_error(
                        writer,
                        400,
                        "UI_INVALID_JSON_OR_PAYLOAD",
                        "Invalid Antigravity diagnostic parameters.",
                        corr_id,
                    )
                    return
                res_agy = await self._diagnostics_service.test_antigravity_connection(
                    model=agy_mod
                )
                await self._write_json(writer, 200, res_agy.model_dump(), corr_id)
                return

            if path == "/api/diagnostics/agent":
                raw_engine = payload.get("engine")
                raw_tier = payload.get("tier", "tier1")
                raw_prov = payload.get("provider")
                raw_mod = payload.get("model")
                if (
                    raw_engine not in ("pi", "agy", "claude", "opencode")
                    or raw_tier not in ("tier1", "tier2")
                    or (raw_prov is not None and not isinstance(raw_prov, str))
                    or (raw_mod is not None and not isinstance(raw_mod, str))
                ):
                    await self._write_error(
                        writer,
                        400,
                        "DIAGNOSTIC_INVALID_PARAMS",
                        "Invalid agent diagnostic parameters.",
                        corr_id,
                    )
                    return
                eng: AgentEngineId = raw_engine
                tier_val: AgentTier = raw_tier
                res_agent = await self._diagnostics_service.test_agent_connection(
                    eng,
                    tier=tier_val,
                    provider=raw_prov,
                    model=raw_mod,
                )
                await self._write_json(writer, 200, res_agent.model_dump(), corr_id)
                return
        except DesktopUiError as exc:
            await self._write_error(writer, 422, exc.code, exc.sanitized_message, corr_id)
            return

        await self._write_error(
            writer,
            404,
            "UI_ROUTE_NOT_FOUND",
            f"Unknown diagnostic route: {path}",
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
                "WORKSPACE_HOME_FORBIDDEN": (403, "WORKSPACE_HOME_FORBIDDEN"),
                "WORKSPACE_OUTSIDE_ALLOWED_ROOTS": (403, "WORKSPACE_OUTSIDE_ALLOWED_ROOTS"),
                "WORKSPACE_NOT_FOUND": (404, "WORKSPACE_NOT_FOUND"),
                "WORKSPACE_AMBIGUOUS_MATCH": (409, "WORKSPACE_AMBIGUOUS"),
                "WORKSPACE_INVALID_MANIFEST": (422, "WORKSPACE_INVALID_MANIFEST"),
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

        if self._settings_service is not None:
            await self._settings_service.record_active_workspace(ctx.canonical_path)
        await self._write_json(
            writer,
            200,
            {
                "cancelled": False,
                "workspace": {
                    "display_name": ctx.name,
                    "canonical_path": ctx.canonical_path,
                    "selected_via": ctx.selected_via,
                    "topology": ctx.topology,
                    "projects": [p.model_dump() for p in ctx.projects],
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
