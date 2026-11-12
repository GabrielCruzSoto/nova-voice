"""Deterministic TDD tests for NOVA-004 T3: DesktopUiServer loopback security and OP-1..OP-6."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from nova.adapters.desktop_ui_server import DesktopUiServer, DesktopUiServerSettings
from nova.adapters.workspace_resolver import LocalWorkspaceResolver
from nova.application.orchestrator import NovaOrchestrator
from nova.application.security_gatekeeper import GatekeeperSettings, SecurityGatekeeper
from nova.application.ui_state_hub import UiStateHub
from nova.domain.errors import DesktopUiError
from nova.domain.events import (
    CommandApprovalRequestedEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
)
from nova.domain.models import (
    CallSession,
    CallSessionState,
    CodingTaskRequest,
    CommandAuditInput,
    CommandAuditResult,
    CommandInterceptRequest,
    ResolveWorkspaceRequest,
    RunnerProgressEvent,
)


class _StubTier1:
    def __init__(self) -> None:
        self.switched: list[str] = []
        self.aborted = 0

    async def switch_workspace(self, new_workspace_path: str) -> None:
        self.switched.append(new_workspace_path)

    async def stream_prompt(self, message: str) -> AsyncIterator[str]:
        yield f"Respuesta a: {message}"

    async def barge_in_abort(self) -> None:
        self.aborted += 1


class _StubTier2:
    async def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
        _ = request
        if False:
            yield RunnerProgressEvent(kind="init", conversation_id="c1")


class _StubAuditor:
    async def audit(self, request: CommandAuditInput) -> CommandAuditResult:
        return CommandAuditResult(
            risk_level="high",
            spoken_summary="Confirma ejecucion del comando retenido.",
            technical_explanation=f"Regla {request.rule_id}",
            audited_by="deterministic_fallback",
        )


class _StubVoiceCoordinator:
    def __init__(self, bus: OrchestratorEventBus) -> None:
        self._bus = bus
        self._state: CallSessionState = "standby"
        self.actions: list[str] = []

    @property
    def session(self) -> CallSession:
        return CallSession(
            call_id="call-ui-test",
            state=self._state,
            last_activity_monotonic=10.0,
            turn_count=1,
        )

    async def start_call(self, reason: str = "manual_call_start") -> None:
        _ = reason
        self.actions.append("start")
        self._state = "listening"

    async def end_call(self, reason: str = "manual_call_end") -> None:
        _ = reason
        self.actions.append("end")
        self._state = "standby"

    async def barge_in(self) -> None:
        self.actions.append("barge_in")
        self._state = "listening"


class _ProgrammableFolderPicker:
    def __init__(self, result: Path | Exception | None) -> None:
        self._result = result

    async def pick_directory(self, initial_dir: Path | None = None) -> Path | None:
        _ = initial_dir
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


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
        hdrs = {"Host": f"127.0.0.1:{port}", "Connection": "close"}
        if body:
            hdrs["Content-Length"] = str(len(body))
        if headers:
            hdrs.update(headers)
        lines = [f"{method} {path} HTTP/1.1\r\n"]
        for k, v in hdrs.items():
            lines.append(f"{k}: {v}\r\n")
        lines.append("\r\n")
        writer.write("".join(lines).encode("utf-8") + body)
        await writer.drain()

        status_line = (await asyncio.wait_for(reader.readline(), timeout=3.0)).decode("latin-1")
        status_code = int(status_line.split(" ")[1])
        resp_headers: dict[str, str] = {}
        while True:
            line = (await asyncio.wait_for(reader.readline(), timeout=3.0)).decode("latin-1")
            if line in ("\r\n", "\n", ""):
                break
            k, _, v = line.partition(":")
            resp_headers[k.strip().lower()] = v.strip()
        resp_body = await asyncio.wait_for(reader.read(), timeout=3.0)
        return status_code, resp_headers, resp_body
    finally:
        writer.close()
        await writer.wait_closed()


def _extract_cookie_pair(set_cookie_header: str) -> str:
    return set_cookie_header.split(";", 1)[0].strip()


def test_ac1_desktop_ui_server_rejects_non_loopback_bind_host() -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        DesktopUiServerSettings(host="0.0.0.0", port=0)


async def test_ac1_security_headers_cookie_csrf_host_origin_and_payload_limits(
    tmp_path: Path,
) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>Nova</title>", encoding="utf-8")
    (static_dir / "app.js").write_text("console.log('nova');", encoding="utf-8")

    root = tmp_path / "Proyectos"
    ws = root / "demo-app"
    ws.mkdir(parents=True)

    bus = OrchestratorEventBus()
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    await resolver.resolve(
        ResolveWorkspaceRequest(correlation_id="c-init", query=str(ws), mode="exact_path")
    )
    orch = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=_StubTier1(),
        tier2_runner=_StubTier2(),
        event_bus=bus,
    )
    hub = UiStateHub(
        event_bus=bus,
        available_workspaces_provider=resolver.list_available_workspaces,
        initial_workspace=resolver.active_context,
    )
    hub.start()

    async with DesktopUiServer(
        orchestrator=orch,
        state_hub=hub,
        static_dir=static_dir,
        settings=DesktopUiServerSettings(port=0),
    ) as server:
        port = server.port

        # 1) DNS Rebinding check: invalid Host rejected with 403
        st_bad_host, _, body_bad_host = await _raw_http_request(
            port,
            "GET",
            "/",
            headers={"Host": "evil.attacker.example"},
        )
        assert st_bad_host == 403
        assert json.loads(body_bad_host)["error_code"] == "UI_FORBIDDEN_ORIGIN_OR_CSRF"

        # 2) Valid GET / issues HttpOnly; SameSite=Strict cookie + strict CSP
        st_root, hdrs_root, html_bytes = await _raw_http_request(port, "GET", "/")
        assert st_root == 200
        assert b"<title>Nova</title>" in html_bytes
        set_cookie = hdrs_root["set-cookie"]
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie
        assert "script-src 'self'" in hdrs_root["content-security-policy"]
        cookie_hdr = _extract_cookie_pair(set_cookie)

        # 3) Path traversal on /static/ rejected with 404
        st_trav, _, _ = await _raw_http_request(port, "GET", "/static/../index.html")
        assert st_trav == 404

        # 4) GET /api/state without cookie -> 403; with cookie -> 200 + csrf_token
        st_unauth, _, _ = await _raw_http_request(port, "GET", "/api/state")
        assert st_unauth == 403

        st_state, _, state_bytes = await _raw_http_request(
            port,
            "GET",
            "/api/state",
            headers={"Cookie": cookie_hdr},
        )
        assert st_state == 200
        snap = json.loads(state_bytes)
        csrf_token = str(snap["csrf_token"])
        assert len(csrf_token) >= 20

        # 5) POST /api/chat without CSRF -> 403; with evil Origin -> 403
        chat_payload = json.dumps({"utterance": "hola"}).encode("utf-8")
        st_no_csrf, _, _ = await _raw_http_request(
            port,
            "POST",
            "/api/chat",
            headers={"Cookie": cookie_hdr, "Content-Type": "application/json"},
            body=chat_payload,
        )
        assert st_no_csrf == 403

        st_bad_origin, _, _ = await _raw_http_request(
            port,
            "POST",
            "/api/chat",
            headers={
                "Cookie": cookie_hdr,
                "X-Nova-CSRF": csrf_token,
                "Origin": "https://evil.example",
                "Content-Type": "application/json",
            },
            body=chat_payload,
        )
        assert st_bad_origin == 403

        # 6) Payload > 65536 bytes -> 413 UI_PAYLOAD_TOO_LARGE
        oversized = b"x" * 66_000
        st_413, _, body_413 = await _raw_http_request(
            port,
            "POST",
            "/api/chat",
            headers={"Cookie": cookie_hdr, "X-Nova-CSRF": csrf_token},
            body=oversized,
        )
        assert st_413 == 413
        assert json.loads(body_413)["error_code"] == "UI_PAYLOAD_TOO_LARGE"

        # 7) Empty utterance -> 422 UI_EMPTY_UTTERANCE
        st_422, _, body_422 = await _raw_http_request(
            port,
            "POST",
            "/api/chat",
            headers={"Cookie": cookie_hdr, "X-Nova-CSRF": csrf_token},
            body=json.dumps({"utterance": "   "}).encode("utf-8"),
        )
        assert st_422 == 422
        assert json.loads(body_422)["error_code"] == "UI_EMPTY_UTTERANCE"

    hub.close()


async def test_nfr1_sse_latency_under_50ms_and_full_op3_op4_op5_roundtrip(
    tmp_path: Path,
) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")

    root = tmp_path / "Proyectos"
    ws_a = root / "nova-alpha"
    ws_b = root / "nova-beta"
    outside = tmp_path / "outside-secret"
    ws_a.mkdir(parents=True)
    ws_b.mkdir(parents=True)
    outside.mkdir(parents=True)

    bus = OrchestratorEventBus()
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    await resolver.resolve(
        ResolveWorkspaceRequest(correlation_id="c-a", query=str(ws_a), mode="exact_path")
    )
    gatekeeper = SecurityGatekeeper(
        workspace_resolver=resolver,
        auditor=_StubAuditor(),
        event_bus=bus,
        settings=GatekeeperSettings(approval_timeout_seconds=10.0),
    )
    orch = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=_StubTier1(),
        tier2_runner=_StubTier2(),
        event_bus=bus,
        security_gatekeeper=gatekeeper,
    )
    voice = _StubVoiceCoordinator(bus)
    hub = UiStateHub(
        event_bus=bus,
        extra_secrets_provider=lambda: gatekeeper.extra_secrets,
        available_workspaces_provider=resolver.list_available_workspaces,
        call_session_provider=lambda: voice.session,
        initial_workspace=resolver.active_context,
        voice_enabled=True,
    )
    hub.start()
    picker = _ProgrammableFolderPicker(ws_b)

    async with DesktopUiServer(
        orchestrator=orch,
        state_hub=hub,
        voice_coordinator=voice,
        folder_picker=picker,
        static_dir=static_dir,
        settings=DesktopUiServerSettings(port=0, heartbeat_interval_seconds=5.0),
    ) as server:
        port = server.port
        _, hdrs_root, _ = await _raw_http_request(port, "GET", "/")
        cookie_hdr = _extract_cookie_pair(hdrs_root["set-cookie"])
        _, _, state_bytes = await _raw_http_request(
            port,
            "GET",
            "/api/state",
            headers={"Cookie": cookie_hdr},
        )
        csrf_token = str(json.loads(state_bytes)["csrf_token"])
        mut_headers = {
            "Cookie": cookie_hdr,
            "X-Nova-CSRF": csrf_token,
            "Origin": f"http://127.0.0.1:{port}",
            "Content-Type": "application/json",
        }

        # Open live SSE connection and verify initial snapshot + <50ms p95 event delivery
        sse_reader, sse_writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            req = (
                f"GET /api/events HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Cookie: {cookie_hdr}\r\n\r\n"
            )
            sse_writer.write(req.encode("utf-8"))
            await sse_writer.drain()

            status_line = (await asyncio.wait_for(sse_reader.readline(), timeout=2.0)).decode(
                "latin-1"
            )
            assert "200" in status_line
            while True:
                hline = (await asyncio.wait_for(sse_reader.readline(), timeout=2.0)).decode(
                    "latin-1"
                )
                if hline in ("\r\n", "\n", ""):
                    break

            async def _read_sse_frame() -> tuple[str, dict[str, Any]]:
                ev_name = ""
                data_str = ""
                while True:
                    raw = (await asyncio.wait_for(sse_reader.readline(), timeout=2.0)).decode(
                        "utf-8"
                    )
                    if raw in ("\r\n", "\n", ""):
                        if ev_name or data_str:
                            return ev_name, json.loads(data_str)
                        continue
                    if raw.startswith(":"):
                        continue
                    if raw.startswith("event:"):
                        ev_name = raw.partition(":")[2].strip()
                    elif raw.startswith("data:"):
                        data_str += raw.partition(":")[2].strip()

            init_ev, init_data = await _read_sse_frame()
            assert init_ev == "snapshot"
            assert init_data["active_workspace"]["display_name"] == "nova-alpha"

            # Measure NFR-1 bus -> SSE p95 latency across 10 events (< 50 ms)
            latencies_ms: list[float] = []
            for idx in range(10):
                t0 = time.perf_counter()
                await bus.publish(
                    CommandApprovalRequestedEvent(
                        audit_id=f"aud-lat-{idx}",
                        correlation_id=f"corr-lat-{idx}",
                        source="tier1_pi",
                        sanitized_command="echo test",
                        cwd=str(ws_a),
                        risk_level="medium",
                        spoken_summary="Prueba de latencia SSE local.",
                        technical_explanation="Test",
                        expires_in_seconds=30.0,
                    )
                )
                ev_type, ev_data = await _read_sse_frame()
                latencies_ms.append((time.perf_counter() - t0) * 1000.0)
                assert ev_type == "nova_event"
                assert ev_data["event_type"] == "command_approval_requested"

            p95_ms = sorted(latencies_ms)[int(len(latencies_ms) * 0.95) - 1]
            assert p95_ms < 50.0

            # OP-3: POST /api/call/start, /barge-in, /end
            for action, expected_state in (
                ("start", "listening"),
                ("barge-in", "listening"),
                ("end", "standby"),
            ):
                st_call, _, call_body = await _raw_http_request(
                    port,
                    "POST",
                    f"/api/call/{action}",
                    headers=mut_headers,
                    body=b"{}",
                )
                assert st_call == 200
                assert json.loads(call_body)["call_state"] == expected_state

            # OP-3: POST /api/chat -> 202 Accepted
            st_chat, _, chat_body = await _raw_http_request(
                port,
                "POST",
                "/api/chat",
                headers=mut_headers,
                body=json.dumps({"utterance": "Hola Nova"}).encode("utf-8"),
            )
            assert st_chat == 202
            assert json.loads(chat_body)["accepted"] is True
            await asyncio.sleep(0.05)

            # OP-4: POST /api/workspace/pick-native -> selects ws_b confined in allowed_roots
            st_pick, _, pick_body = await _raw_http_request(
                port,
                "POST",
                "/api/workspace/pick-native",
                headers=mut_headers,
                body=b"{}",
            )
            assert st_pick == 200
            picked_json = json.loads(pick_body)
            assert picked_json["cancelled"] is False
            assert picked_json["workspace"]["display_name"] == "nova-beta"

            # OP-4: Ambiguous query -> 409 WORKSPACE_AMBIGUOUS; outside -> 403
            st_amb, _, amb_body = await _raw_http_request(
                port,
                "POST",
                "/api/workspace/select",
                headers=mut_headers,
                body=json.dumps({"path_or_query": "nova"}).encode("utf-8"),
            )
            assert st_amb == 409
            assert json.loads(amb_body)["error_code"] == "WORKSPACE_AMBIGUOUS"

            st_out, _, out_body = await _raw_http_request(
                port,
                "POST",
                "/api/workspace/select",
                headers=mut_headers,
                body=json.dumps({"path_or_query": str(outside)}).encode("utf-8"),
            )
            assert st_out == 403
            assert json.loads(out_body)["error_code"] == "WORKSPACE_OUTSIDE_ALLOWED_ROOTS"

            # OP-4: Headless native picker -> 503 UI_NATIVE_PICKER_UNAVAILABLE
            picker._result = DesktopUiError(
                code="UI_NATIVE_PICKER_UNAVAILABLE",
                message="No DISPLAY",
            )
            st_503, _, body_503 = await _raw_http_request(
                port,
                "POST",
                "/api/workspace/pick-native",
                headers=mut_headers,
                body=b"{}",
            )
            assert st_503 == 503
            assert json.loads(body_503)["error_code"] == "UI_NATIVE_PICKER_UNAVAILABLE"

            # OP-5: Real SecurityGatekeeper hold + UI resolution + 404 on duplicate resolution
            req_Events: list[CommandApprovalRequestedEvent] = []

            async def _capture_req(ev: OrchestratorEvent) -> None:
                if isinstance(ev, CommandApprovalRequestedEvent):
                    req_Events.append(ev)

            bus.subscribe(_capture_req)
            eval_task = asyncio.create_task(
                gatekeeper.evaluate_and_authorize(
                    CommandInterceptRequest(
                        correlation_id="corr-gk-ui",
                        source="tier1_pi",
                        tool_name="bash",
                        command="npm install lodash",
                        cwd=str(ws_b),
                    )
                )
            )
            await asyncio.sleep(0.05)
            assert len(req_Events) == 1
            real_audit_id = req_Events[0].audit_id

            st_res, _, res_body = await _raw_http_request(
                port,
                "POST",
                f"/api/approvals/{real_audit_id}/resolve",
                headers=mut_headers,
                body=json.dumps({"approved": True}).encode("utf-8"),
            )
            assert st_res == 200
            assert json.loads(res_body)["resolved_by"] == "user_ui"
            decision = await eval_task
            assert decision.allowed is True

            # Duplicate resolution (e.g. race with voice) -> 404 GATEKEEPER_AUDIT_NOT_FOUND
            st_dup, _, dup_body = await _raw_http_request(
                port,
                "POST",
                f"/api/approvals/{real_audit_id}/resolve",
                headers=mut_headers,
                body=json.dumps({"approved": False}).encode("utf-8"),
            )
            assert st_dup == 404
            assert json.loads(dup_body)["error_code"] == "GATEKEEPER_AUDIT_NOT_FOUND"
        finally:
            sse_writer.close()
            await sse_writer.wait_closed()

        # NFR-3: Disconnecting SSE client cleans up subscriber immediately
        await asyncio.sleep(0.05)
        assert hub.subscriber_count == 0

    hub.close()
