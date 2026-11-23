"""Tests for T5 (NOVA-020 AC-10): UiStateHub task SSE projection and DesktopUiServer /api/tasks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from nova.adapters.desktop_ui_server import DesktopUiServer, DesktopUiServerSettings
from nova.adapters.sqlite_history_store import SqliteHistoryStore
from nova.application.orchestrator import NovaOrchestrator
from nova.application.task_manager import TaskManager
from nova.application.ui_state_hub import UiStateHub
from nova.domain.events import OrchestratorEventBus
from nova.domain.models import (
    AgentTaskRecord,
    CodingTaskRequest,
    ResolveWorkspaceRequest,
    RunnerProgressEvent,
    WorkspaceContext,
)


class _FakeTier1:
    async def switch_workspace(self, new_workspace_path: str) -> None:
        _ = new_workspace_path

    async def barge_in_abort(self) -> None:
        return None

    async def stream_prompt(self, message: str) -> AsyncIterator[str]:
        _ = message
        yield "Ok."


class _GateRunner:
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self.started = started
        self.release = release

    async def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
        yield RunnerProgressEvent(kind="init", conversation_id="conv-api", step_type="init")
        self.started.set()
        yield RunnerProgressEvent(
            kind="delta",
            conversation_id="conv-api",
            step_type="editing",
            text_delta="Working on patch",
        )
        await self.release.wait()
        yield RunnerProgressEvent(
            kind="completed",
            conversation_id="conv-api",
            step_type="result",
            final_response="Patch finished.",
        )


class _SimpleResolver:
    def __init__(self, ws: WorkspaceContext | None) -> None:
        self._ws = ws

    @property
    def active_context(self) -> WorkspaceContext | None:
        return self._ws

    async def resolve(self, request: ResolveWorkspaceRequest) -> WorkspaceContext:
        assert self._ws is not None
        return self._ws


async def _http_request(
    port: int,
    method: str,
    target: str,
    *,
    cookie: str | None = None,
    csrf: str | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    raw_body = json.dumps(body).encode("utf-8") if body is not None else b""
    hdrs = [
        f"{method} {target} HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Connection: close",
    ]
    if cookie:
        hdrs.append(f"Cookie: {cookie}")
    if csrf:
        hdrs.append(f"X-Nova-CSRF: {csrf}")
    if body is not None:
        hdrs.append("Content-Type: application/json")
        hdrs.append(f"Content-Length: {len(raw_body)}")
    req_bytes = ("\r\n".join(hdrs) + "\r\n\r\n").encode("latin-1") + raw_body
    writer.write(req_bytes)
    await writer.drain()
    raw_resp = await asyncio.wait_for(reader.read(), timeout=3.0)
    writer.close()
    await writer.wait_closed()
    head, _, payload = raw_resp.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status_code = int(lines[0].split(" ")[1])
    resp_headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            resp_headers[k.strip().lower()] = v.strip()
    return status_code, resp_headers, payload


@pytest.mark.asyncio
async def test_nova020_ac10_ui_state_hub_hydrates_and_streams_task_sse_events(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "demo"
    ws.mkdir()
    store = SqliteHistoryStore(tmp_path / "history.db")
    # Seed a completed historical task
    await store.upsert_task(
        AgentTaskRecord(
            task_id="tsk-hist-001",
            correlation_id="corr-hist",
            workspace_canonical_path=str(ws.resolve()),
            workspace_name="demo",
            provider="opencode",
            model="sonnet",
            objective="Historical job",
            status="completed",
            result_summary="Finished earlier.",
            created_at=500.0,
            updated_at=505.0,
            completed_at=505.0,
        )
    )

    bus = OrchestratorEventBus()
    started = asyncio.Event()
    release = asyncio.Event()
    mgr = TaskManager(
        runner_factory=lambda p, w, tid, m: _GateRunner(started, release),
        event_bus=bus,
        task_store=store,
    )
    mgr.start()

    hub = UiStateHub(
        event_bus=bus,
        history_store=store,
        task_store=store,
    )
    hub.start()
    await hub.hydrate_history()

    snap0 = await hub.build_snapshot(csrf_token="tok-1")
    assert snap0.schema_version == "1.3.0"
    assert len(snap0.tasks) == 1
    assert snap0.tasks[0].id == "tsk-hist-001"
    assert snap0.agents_overview is not None
    assert len(snap0.agents_overview.tasks) == 1

    sub = hub.subscribe_sse()
    created = await mgr.dispatch_task(
        workspace_canonical_path=str(ws.resolve()),
        workspace_name="demo",
        objective="Live SSE job",
        provider="claude",
    )
    await asyncio.wait_for(started.wait(), timeout=2.0)
    release.set()
    await asyncio.wait_for(mgr.wait_for_task(created.task_id), timeout=2.0)

    sse_types: list[str] = []
    while not sub.queue.empty():
        item = sub.queue.get_nowait()
        if item.envelope is not None:
            sse_types.append(item.envelope.event_type)

    assert "task_created" in sse_types
    assert "task_state_changed" in sse_types
    assert "task_progress" in sse_types

    snap1 = await hub.build_snapshot(csrf_token="tok-1")
    ids = [t.id for t in snap1.tasks]
    assert created.task_id in ids
    live_view = next(t for t in snap1.tasks if t.id == created.task_id)
    assert live_view.provider == "claude"
    assert live_view.status == "completed"

    hub.unsubscribe_sse(sub)
    hub.close()
    await mgr.close()
    await store.close()


@pytest.mark.asyncio
async def test_nova020_ac10_desktop_ui_server_tasks_rest_and_csrf_guards(
    tmp_path: Path,
) -> None:
    ws_dir = tmp_path / "proj"
    ws_dir.mkdir()
    ws = WorkspaceContext(
        workspace_id="ws-proj",
        name="proj",
        canonical_path=str(ws_dir.resolve()),
        selected_via="exact_path",
    )
    store = SqliteHistoryStore(tmp_path / "history.db")
    bus = OrchestratorEventBus()
    started = asyncio.Event()
    release = asyncio.Event()

    mgr = TaskManager(
        runner_factory=lambda p, w, tid, m: _GateRunner(started, release),
        event_bus=bus,
        task_store=store,
    )
    mgr.start()

    orch = NovaOrchestrator(
        workspace_resolver=_SimpleResolver(ws),
        tier1_client=_FakeTier1(),
        tier2_runner=_GateRunner(started, release),
        event_bus=bus,
        task_manager=mgr,
    )
    hub = UiStateHub(
        event_bus=bus,
        initial_workspace=ws,
        history_store=store,
        task_store=store,
    )

    async with DesktopUiServer(
        orchestrator=orch,
        state_hub=hub,
        event_bus=bus,
        task_manager=mgr,
        settings=DesktopUiServerSettings(host="127.0.0.1", port=0),
    ) as server:
        # Bootstrap session cookie via GET /, then fetch CSRF token via GET /api/state
        st_idx, hdrs_idx, _ = await _http_request(server.port, "GET", "/")
        assert st_idx == 200
        cookie = hdrs_idx["set-cookie"].split(";", 1)[0]
        st, _, raw = await _http_request(server.port, "GET", "/api/state", cookie=cookie)
        assert st == 200
        csrf = json.loads(raw.decode("utf-8"))["csrf_token"]

        # POST /api/tasks without CSRF -> 403
        st_no_csrf, _, _ = await _http_request(
            server.port,
            "POST",
            "/api/tasks",
            cookie=cookie,
            body={"objective": "Unauthorized create"},
        )
        assert st_no_csrf == 403

        # POST /api/tasks with $HOME -> 400 WORKSPACE_HOME_FORBIDDEN
        st_home, _, raw_home = await _http_request(
            server.port,
            "POST",
            "/api/tasks",
            cookie=cookie,
            csrf=csrf,
            body={
                "objective": "Bad workspace",
                "workspace_path": str(Path.home().resolve()),
            },
        )
        assert st_home == 400
        assert json.loads(raw_home.decode("utf-8"))["error_code"] == "WORKSPACE_HOME_FORBIDDEN"

        # POST /api/tasks valid -> 201
        st_create, _, raw_create = await _http_request(
            server.port,
            "POST",
            "/api/tasks",
            cookie=cookie,
            csrf=csrf,
            body={"objective": "Run full refactor", "provider": "claude"},
        )
        assert st_create == 201
        created_payload = json.loads(raw_create.decode("utf-8"))
        task_id = created_payload["task"]["task_id"]
        assert created_payload["task"]["provider"] == "claude"

        await asyncio.wait_for(started.wait(), timeout=2.0)

        # GET /api/tasks -> 200 with task + agents_overview
        st_list, _, raw_list = await _http_request(
            server.port,
            "GET",
            "/api/tasks?status=running&limit=10",
            cookie=cookie,
        )
        assert st_list == 200
        list_data = json.loads(raw_list.decode("utf-8"))
        assert any(t["task_id"] == task_id for t in list_data["tasks"])
        assert "agents_overview" in list_data

        # GET /api/tasks/{task_id} -> 200, missing -> 404
        st_get, _, raw_get = await _http_request(
            server.port,
            "GET",
            f"/api/tasks/{task_id}",
            cookie=cookie,
        )
        assert st_get == 200
        assert json.loads(raw_get.decode("utf-8"))["task"]["task_id"] == task_id

        st_404, _, raw_404 = await _http_request(
            server.port,
            "GET",
            "/api/tasks/tsk-notfound0000",
            cookie=cookie,
        )
        assert st_404 == 404
        assert json.loads(raw_404.decode("utf-8"))["error_code"] == "TASK_NOT_FOUND"

        # POST /api/tasks/{task_id}/cancel -> 200 and idempotent on second call
        st_cancel1, _, raw_cancel1 = await _http_request(
            server.port,
            "POST",
            f"/api/tasks/{task_id}/cancel",
            cookie=cookie,
            csrf=csrf,
            body={},
        )
        assert st_cancel1 == 200
        assert json.loads(raw_cancel1.decode("utf-8"))["task"]["status"] == "cancelled"

        st_cancel2, _, raw_cancel2 = await _http_request(
            server.port,
            "POST",
            f"/api/tasks/{task_id}/cancel",
            cookie=cookie,
            csrf=csrf,
            body={},
        )
        assert st_cancel2 == 200
        assert json.loads(raw_cancel2.decode("utf-8"))["task"]["status"] == "cancelled"

    await mgr.close()
    await store.close()
