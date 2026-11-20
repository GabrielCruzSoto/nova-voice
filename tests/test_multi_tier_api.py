"""TDD tests for NOVA-017 T5: /api/agents/catalog, /api/diagnostics/agent, and /api/credentials."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nova.adapters.agent_registry import AgentAdapterRegistry
from nova.adapters.credential_store import SystemCredentialStore
from nova.adapters.desktop_ui_server import DesktopUiServer, DesktopUiServerSettings
from nova.application.live_diagnostics import LiveDiagnosticsConfig, LiveDiagnosticsService
from nova.application.ui_state_hub import UiStateHub
from nova.domain.events import CommandApprovalResolvedEvent, OrchestratorEventBus
from nova.domain.models import ModelsSettingsSection, WorkspaceContext, WorkspaceQueryMode


class _FakeOrchestrator:
    async def select_workspace(
        self,
        query: str,
        mode: WorkspaceQueryMode = "ui_picker",
        correlation_id: str | None = None,
    ) -> WorkspaceContext:
        return WorkspaceContext(
            workspace_id="ws-1",
            name="demo",
            canonical_path=query,
            selected_via=mode,
        )

    async def handle_user_utterance(
        self,
        utterance: str,
        correlation_id: str | None = None,
    ) -> None:
        return None

    async def barge_in(self) -> None:
        return None

    async def resolve_command_approval(
        self,
        audit_id: str,
        *,
        approved: bool,
        resolved_by: str = "user_ui",
    ) -> CommandApprovalResolvedEvent:
        return CommandApprovalResolvedEvent(
            audit_id=audit_id,
            correlation_id="corr-1",
            approved=approved,
            verdict="approved_by_human" if approved else "rejected_by_human",
            resolved_by=resolved_by,
        )


async def _raw_http(
    port: int,
    method: str,
    path: str,
    *,
    cookie: str = "",
    csrf: str = "",
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
    headers = [
        f"{method} {path} HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Connection: close",
    ]
    if cookie:
        headers.append(f"Cookie: {cookie}")
    if csrf:
        headers.append(f"X-Nova-CSRF: {csrf}")
        headers.append(f"Origin: http://127.0.0.1:{port}")
    if body is not None:
        headers.append("Content-Type: application/json")
        headers.append(f"Content-Length: {len(payload_bytes)}")
    req = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + payload_bytes
    writer.write(req)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()

    head, _, body_part = raw.partition(b"\r\n\r\n")
    head_lines = head.decode("utf-8", errors="replace").split("\r\n")
    status_code = int(head_lines[0].split(" ")[1])
    resp_headers: dict[str, str] = {}
    for line in head_lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            resp_headers[k.strip().lower()] = v.strip()
    if resp_headers.get("content-type", "").startswith("application/json") and body_part.strip():
        parsed = json.loads(body_part.decode("utf-8"))
    else:
        parsed = {}
    return status_code, resp_headers, parsed


@pytest.mark.asyncio
async def test_agents_catalog_credentials_and_generic_agent_probe(tmp_path: Path) -> None:
    bus = OrchestratorEventBus()
    hub = UiStateHub(event_bus=bus)
    cred_store = SystemCredentialStore(
        fallback_path=tmp_path / "credentials.json",
        prefer_libsecret=False,
    )
    registry = AgentAdapterRegistry(
        which_resolver=lambda name: f"/usr/bin/{name}" if name in ("claude", "opencode") else None,
        credential_store=cred_store,
        include_fallback_paths=False,
    )

    async def fake_claude_probe(
        exe: str,
        extra: tuple[str, ...],
        model: str,
        probe_dir: str,
        ipc_env: dict[str, str] | None,
    ) -> None:
        _ = (exe, extra, model, probe_dir, ipc_env)

    diag = LiveDiagnosticsService(
        config=LiveDiagnosticsConfig(),
        which_resolver=lambda name: "/usr/bin/claude" if name == "claude" else None,
        claude_probe_runner=fake_claude_probe,
    )

    server = DesktopUiServer(
        orchestrator=_FakeOrchestrator(),
        state_hub=hub,
        event_bus=bus,
        diagnostics_service=diag,
        agent_catalog_provider=lambda: registry.build_catalog(ModelsSettingsSection()),
        credential_store=cred_store,
        settings=DesktopUiServerSettings(host="127.0.0.1", port=0),
    )

    async with server:
        # Establish session via GET / and fetch CSRF via GET /api/state
        idx_code, idx_headers, _ = await _raw_http(server.port, "GET", "/")
        assert idx_code == 200
        cookie_hdr = idx_headers["set-cookie"].split(";", 1)[0]
        st_code, _, st_body = await _raw_http(
            server.port, "GET", "/api/state", cookie=cookie_hdr
        )
        assert st_code == 200
        csrf_token = str(st_body["csrf_token"])

        # 1. PUT /api/credentials (never echoes raw secret)
        synth_secret = "sk-ant-ui-api-synthetic-9999"
        c_code, _, c_body = await _raw_http(
            server.port,
            "PUT",
            "/api/credentials",
            cookie=cookie_hdr,
            csrf=csrf_token,
            body={
                "provider": "anthropic",
                "env_var": "ANTHROPIC_API_KEY",
                "value": synth_secret,
            },
        )
        assert c_code == 200
        assert c_body["ok"] is True
        assert c_body["configured"] is True
        assert synth_secret not in json.dumps(c_body)

        # 2. GET /api/agents/catalog (never exposes raw secret)
        cat_code, _, cat_body = await _raw_http(
            server.port, "GET", "/api/agents/catalog", cookie=cookie_hdr
        )
        assert cat_code == 200
        assert synth_secret not in json.dumps(cat_body)
        assert cat_body["active_tier1"]["engine"] == "opencode"
        assert cat_body["active_tier2"]["engine"] == "claude"

        # 3. POST /api/diagnostics/agent
        p_code, _, p_body = await _raw_http(
            server.port,
            "POST",
            "/api/diagnostics/agent",
            cookie=cookie_hdr,
            csrf=csrf_token,
            body={"engine": "claude", "tier": "tier1", "model": "haiku"},
        )
        assert p_code == 200
        assert p_body["ok"] is True
        assert p_body["engine"] == "claude"
        assert p_body["status"] == "connected"

        # 4. DELETE /api/credentials
        d_code, _, d_body = await _raw_http(
            server.port,
            "DELETE",
            "/api/credentials",
            cookie=cookie_hdr,
            csrf=csrf_token,
            body={"provider": "anthropic", "env_var": "ANTHROPIC_API_KEY"},
        )
        assert d_code == 200
        assert d_body["configured"] is False
