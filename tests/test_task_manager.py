"""Unit and concurrency tests for TaskManager (NOVA-020 AC-4, AC-5, AC-7, AC-8, AC-9)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nova.adapters.sqlite_history_store import SqliteHistoryStore
from nova.application.security_gatekeeper import GatekeeperSettings, SecurityGatekeeper
from nova.application.task_manager import TaskManager, TaskManagerSettings
from nova.application.ui_state_hub import UiStateHub
from nova.domain.errors import (
    CodingRunnerError,
    TaskManagerError,
    TaskNotFoundError,
    TaskQueueFullError,
    TaskStateError,
)
from nova.domain.events import (
    CommandApprovalRequestedEvent,
    CommandApprovalResolvedEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
    TaskCreatedEvent,
    TaskProgressEvent,
    TaskStateChangedEvent,
    VoiceMilestoneEvent,
)
from nova.domain.models import (
    AgentEngineId,
    CodingTaskRequest,
    CommandAuditInput,
    CommandAuditResult,
    CommandInterceptRequest,
    ResolveWorkspaceRequest,
    RunnerProgressEvent,
    WorkspaceContext,
)


class _ControlledRunner:
    """Deterministic fake CodingRunnerPort controlled by asyncio.Event gates."""

    def __init__(
        self,
        *,
        started_event: asyncio.Event | None = None,
        release_event: asyncio.Event | None = None,
        fail_code: str | None = None,
        final_text: str = "Done cleanly.",
        conversation_id: str = "conv-1",
    ) -> None:
        self.started_event = started_event
        self.release_event = release_event
        self.fail_code = fail_code
        self.final_text = final_text
        self.conversation_id = conversation_id
        self.cancelled = False
        self.received_requests: list[CodingTaskRequest] = []

    async def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
        self.received_requests.append(request)
        yield RunnerProgressEvent(
            kind="init",
            conversation_id=self.conversation_id,
            step_type="init",
        )
        if self.started_event is not None:
            self.started_event.set()
        try:
            yield RunnerProgressEvent(
                kind="delta",
                conversation_id=self.conversation_id,
                step_type="editing",
                text_delta="Applying patch with nvapi-secret-999999",
            )
            if self.release_event is not None:
                await self.release_event.wait()
            if self.fail_code is not None:
                raise CodingRunnerError(
                    code=self.fail_code,
                    message=f"Simulated failure {self.fail_code}",
                )
            yield RunnerProgressEvent(
                kind="completed",
                conversation_id=self.conversation_id,
                step_type="result",
                final_response=self.final_text,
            )
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _FakeGatekeeperRegistry:
    def __init__(self) -> None:
        self.registered: dict[str, str] = {}
        self.unregistered: list[str] = []
        self.extra_secrets: tuple[str, ...] = ("ephemeral-secret-xyz",)

    def register_task_workspace(self, task_id: str, workspace_canonical_path: str) -> None:
        self.registered[task_id] = workspace_canonical_path

    def unregister_task_workspace(self, task_id: str) -> None:
        self.registered.pop(task_id, None)
        self.unregistered.append(task_id)


@pytest.mark.asyncio
async def test_nova020_ac4_ac9_task_lifecycle_redaction_and_gatekeeper_waiting(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "alpha"
    ws.mkdir()
    store = SqliteHistoryStore(tmp_path / "history.db")
    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def _on_event(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(_on_event)
    gk = _FakeGatekeeperRegistry()

    started = asyncio.Event()
    release = asyncio.Event()
    runner = _ControlledRunner(
        started_event=started,
        release_event=release,
        final_text="Updated parser; token=ephemeral-secret-xyz removed.",
    )

    mgr = TaskManager(
        runner_factory=lambda provider, ws_path, task_id, model: runner,
        event_bus=bus,
        task_store=store,
        security_gatekeeper=gk,
    )
    mgr.start()

    created = await mgr.dispatch_task(
        workspace_canonical_path=str(ws.resolve()),
        workspace_name="alpha",
        objective="Fix bug using nvapi-secret-123456",
        provider="claude",
        model="opus",
        correlation_id="corr-life-1",
    )
    assert created.status == "queued"
    assert "nvapi-secret-123456" not in created.objective
    assert "<REDACTED:secret>" in created.objective

    await asyncio.wait_for(started.wait(), timeout=2.0)
    await asyncio.sleep(0.02)
    running_rec = await mgr.get_task(created.task_id)
    assert running_rec.status == "running"
    assert gk.registered.get(created.task_id) == str(ws.resolve())

    # Simulate Gatekeeper requesting human approval for this task
    await bus.publish(
        CommandApprovalRequestedEvent(
            audit_id="aud-42",
            correlation_id="corr-gk-1",
            source="tier2_claude",
            sanitized_command="rm -rf build",
            cwd=str(ws.resolve()),
            risk_level="medium",
            spoken_summary="Aprobar limpieza de carpeta build.",
            technical_explanation="Removes build directory.",
            expires_in_seconds=60.0,
            task_id=created.task_id,
        )
    )
    waiting_rec = await mgr.get_task(created.task_id)
    assert waiting_rec.status == "waiting_approval"
    assert waiting_rec.pending_audit_id == "aud-42"

    # Simulate human approving command
    await bus.publish(
        CommandApprovalResolvedEvent(
            audit_id="aud-42",
            correlation_id="corr-gk-1",
            approved=True,
            verdict="approved_by_human",
            resolved_by="user_voice",
            task_id=created.task_id,
        )
    )
    resumed_rec = await mgr.get_task(created.task_id)
    assert resumed_rec.status == "running"
    assert resumed_rec.pending_audit_id is None

    # Allow worker to complete
    release.set()
    await asyncio.wait_for(mgr.wait_for_task(created.task_id), timeout=2.0)

    done_rec = await mgr.get_task(created.task_id)
    assert done_rec.status == "completed"
    assert done_rec.result_summary is not None
    assert "ephemeral-secret-xyz" not in done_rec.result_summary
    assert "<REDACTED:secret>" in done_rec.result_summary
    assert created.task_id in gk.unregistered

    # Persisted cleanly in SQLite with redaction
    persisted = await store.get_task(created.task_id)
    assert persisted is not None
    assert persisted.status == "completed"
    assert "nvapi-secret-123456" not in persisted.objective
    assert persisted.result_summary is not None
    assert "ephemeral-secret-xyz" not in persisted.result_summary

    # Verify events emitted
    assert any(isinstance(e, TaskCreatedEvent) for e in events)
    state_seq = [
        (e.previous_status, e.status)
        for e in events
        if isinstance(e, TaskStateChangedEvent) and e.task_id == created.task_id
    ]
    assert state_seq == [
        ("queued", "running"),
        ("running", "waiting_approval"),
        ("waiting_approval", "running"),
        ("running", "completed"),
    ]
    assert any(isinstance(e, TaskProgressEvent) for e in events)
    assert any(isinstance(e, VoiceMilestoneEvent) for e in events)

    # Illegal transition from terminal raises TaskStateError
    with pytest.raises(TaskStateError):
        mgr.validate_transition("completed", "running")

    await mgr.close()
    await store.close()


@pytest.mark.asyncio
async def test_nova020_ac5_per_workspace_fifo_lock_and_cross_workspace_parallelism(
    tmp_path: Path,
) -> None:
    ws_alpha = tmp_path / "alpha"
    ws_beta = tmp_path / "beta"
    ws_alpha.mkdir()
    ws_beta.mkdir()

    bus = OrchestratorEventBus()
    runners: dict[str, _ControlledRunner] = {}
    started_order: list[str] = []

    e_start_1 = asyncio.Event()
    e_rel_1 = asyncio.Event()
    e_start_2 = asyncio.Event()
    e_rel_2 = asyncio.Event()
    e_start_3 = asyncio.Event()
    e_rel_3 = asyncio.Event()

    specs: dict[AgentEngineId, tuple[asyncio.Event, asyncio.Event]] = {
        "claude": (e_start_1, e_rel_1),
        "opencode": (e_start_2, e_rel_2),
        "agy": (e_start_3, e_rel_3),
    }

    def _factory(
        provider: AgentEngineId,
        ws_path: str,
        task_id: str,
        model: str,
    ) -> _ControlledRunner:
        s_ev, r_ev = specs[provider]
        started_order.append(task_id)
        r = _ControlledRunner(started_event=s_ev, release_event=r_ev)
        runners[task_id] = r
        return r

    mgr = TaskManager(
        runner_factory=_factory,
        event_bus=bus,
        settings=TaskManagerSettings(max_concurrent_tasks=4),
    )
    mgr.start()

    t1 = await mgr.dispatch_task(
        workspace_canonical_path=str(ws_alpha.resolve()),
        workspace_name="alpha",
        objective="Alpha job 1",
        provider="claude",
    )
    t2 = await mgr.dispatch_task(
        workspace_canonical_path=str(ws_alpha.resolve()),
        workspace_name="alpha",
        objective="Alpha job 2",
        provider="opencode",
    )
    t3 = await mgr.dispatch_task(
        workspace_canonical_path=str(ws_beta.resolve()),
        workspace_name="beta",
        objective="Beta job 1",
        provider="agy",
    )

    # T1 (alpha) and T3 (beta) MUST run in parallel!
    await asyncio.wait_for(e_start_1.wait(), timeout=2.0)
    await asyncio.wait_for(e_start_3.wait(), timeout=2.0)
    await asyncio.sleep(0.05)

    assert (await mgr.get_task(t1.task_id)).status == "running"
    assert (await mgr.get_task(t3.task_id)).status == "running"
    # T2 targets alpha, so it MUST still be queued waiting for T1's workspace lock!
    assert (await mgr.get_task(t2.task_id)).status == "queued"
    assert not e_start_2.is_set()

    # Release T1 -> T2 now starts in FIFO order
    e_rel_1.set()
    await asyncio.wait_for(e_start_2.wait(), timeout=2.0)
    assert (await mgr.get_task(t2.task_id)).status == "running"

    e_rel_2.set()
    e_rel_3.set()
    await asyncio.wait_for(mgr.wait_for_task(t2.task_id), timeout=2.0)
    await asyncio.wait_for(mgr.wait_for_task(t3.task_id), timeout=2.0)
    assert len(set(id(r) for r in runners.values())) == 3

    await mgr.close()


@pytest.mark.asyncio
async def test_nova020_ac7_ac8_cancel_idempotency_validation_and_heartbeat_failure(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "repo"
    ws.mkdir()
    store = SqliteHistoryStore(tmp_path / "h.db")
    bus = OrchestratorEventBus()

    started = asyncio.Event()
    never_release = asyncio.Event()
    runners_created: list[_ControlledRunner] = []

    def _factory(
        provider: AgentEngineId,
        ws_path: str,
        task_id: str,
        model: str,
    ) -> _ControlledRunner:
        if len(runners_created) == 0:
            r = _ControlledRunner(started_event=started, release_event=never_release)
        else:
            r = _ControlledRunner(fail_code="RUNNER_STDOUT_HEARTBEAT_TIMEOUT")
        runners_created.append(r)
        return r

    mgr = TaskManager(
        runner_factory=_factory,
        event_bus=bus,
        task_store=store,
        settings=TaskManagerSettings(max_concurrent_tasks=2, max_queued_tasks=2),
    )
    mgr.start()

    # Reject $HOME and empty objective
    with pytest.raises(TaskManagerError) as exc_home:
        await mgr.dispatch_task(
            workspace_canonical_path=str(Path.home().resolve()),
            workspace_name="home",
            objective="Do not allow home",
            provider="claude",
        )
    assert exc_home.value.code == "WORKSPACE_HOME_FORBIDDEN"

    with pytest.raises(TaskManagerError) as exc_empty:
        await mgr.dispatch_task(
            workspace_canonical_path=str(ws.resolve()),
            workspace_name="repo",
            objective="   ",
            provider="claude",
        )
    assert exc_empty.value.code == "TASK_INVALID_REQUEST"

    # Unknown task_id raises TaskNotFoundError
    with pytest.raises(TaskNotFoundError):
        await mgr.cancel_task("tsk-does-not-exist")

    # Running task cancellation + idempotency
    t_run = await mgr.dispatch_task(
        workspace_canonical_path=str(ws.resolve()),
        workspace_name="repo",
        objective="Long running task",
        provider="claude",
    )
    await asyncio.wait_for(started.wait(), timeout=2.0)

    cancelled = await mgr.cancel_task(t_run.task_id)
    assert cancelled.status == "cancelled"
    assert runners_created[0].cancelled is True

    # Idempotent cancel on already cancelled task
    cancelled_again = await mgr.cancel_task(t_run.task_id)
    assert cancelled_again.status == "cancelled"

    # Runner heartbeat timeout error transitions task to failed with exact code
    t_fail = await mgr.dispatch_task(
        workspace_canonical_path=str(ws.resolve()),
        workspace_name="repo",
        objective="Will hang",
        provider="agy",
    )
    await asyncio.wait_for(mgr.wait_for_task(t_fail.task_id), timeout=2.0)
    failed_rec = await mgr.get_task(t_fail.task_id)
    assert failed_rec.status == "failed"
    assert failed_rec.error_code == "RUNNER_STDOUT_HEARTBEAT_TIMEOUT"

    await mgr.close()
    await store.close()


@pytest.mark.asyncio
async def test_nova020_nfr2_queue_limit_and_ram_ttl_eviction_falls_back_to_sqlite(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    store = SqliteHistoryStore(tmp_path / "h.db")
    bus = OrchestratorEventBus()
    now_holder = [1000.0]

    started = asyncio.Event()
    release = asyncio.Event()

    mgr = TaskManager(
        runner_factory=lambda p, w, tid, m: _ControlledRunner(
            started_event=started if not started.is_set() else None,
            release_event=release,
        ),
        event_bus=bus,
        task_store=store,
        settings=TaskManagerSettings(
            max_concurrent_tasks=1,
            max_queued_tasks=1,
            max_tracked_tasks_in_ram=10,
            terminal_cache_ttl_seconds=60.0,
        ),
        clock=lambda: now_holder[0],
    )
    mgr.start()

    t1 = await mgr.dispatch_task(
        workspace_canonical_path=str(ws.resolve()),
        workspace_name="ws",
        objective="First",
        provider="claude",
    )
    await asyncio.wait_for(started.wait(), timeout=2.0)

    t2 = await mgr.dispatch_task(
        workspace_canonical_path=str(ws.resolve()),
        workspace_name="ws",
        objective="Queued second",
        provider="claude",
    )
    assert t2.status == "queued"

    # Third task exceeds max_queued_tasks=1
    with pytest.raises(TaskQueueFullError):
        await mgr.dispatch_task(
            workspace_canonical_path=str(ws.resolve()),
            workspace_name="ws",
            objective="Overflow third",
            provider="claude",
        )

    # Cancel queued task cleanly
    c2 = await mgr.cancel_task(t2.task_id)
    assert c2.status == "cancelled"

    release.set()
    await asyncio.wait_for(mgr.wait_for_task(t1.task_id), timeout=2.0)

    # Advance clock past TTL (+5% max jitter = 63s) and verify RAM eviction + SQLite fallback
    now_holder[0] = 1100.0
    mgr.prune_expired_ram_cache()
    assert mgr.ram_cache_size == 0

    from_db = await mgr.get_task(t1.task_id)
    assert from_db.task_id == t1.task_id
    assert from_db.status == "completed"

    await mgr.close()
    await store.close()


@pytest.mark.asyncio
async def test_nova020_f3_f8_cancel_during_waiting_approval_unblocks_gatekeeper_and_ui(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "secure_repo"
    ws.mkdir()
    ws_ctx = WorkspaceContext(
        workspace_id="ws-sec",
        name="secure_repo",
        canonical_path=str(ws.resolve()),
        selected_via="exact_path",
    )

    class _Resolver:
        @property
        def active_context(self) -> WorkspaceContext | None:
            return ws_ctx

        async def resolve(self, request: ResolveWorkspaceRequest) -> WorkspaceContext:
            return ws_ctx

    class _Auditor:
        async def audit(self, request: CommandAuditInput) -> CommandAuditResult:
            return CommandAuditResult(
                risk_level="high",
                spoken_summary="Comando sensible.",
                technical_explanation="Requires approval.",
                audited_by="test",
            )

    store = SqliteHistoryStore(tmp_path / "h.db")
    bus = OrchestratorEventBus()
    resolved_events: list[CommandApprovalResolvedEvent] = []

    async def _on_ev(ev: OrchestratorEvent) -> None:
        if isinstance(ev, CommandApprovalResolvedEvent):
            resolved_events.append(ev)

    bus.subscribe(_on_ev)
    gk = SecurityGatekeeper(
        workspace_resolver=_Resolver(),
        auditor=_Auditor(),
        event_bus=bus,
        settings=GatekeeperSettings(approval_timeout_seconds=30.0),
    )
    hub = UiStateHub(event_bus=bus, initial_workspace=ws_ctx)
    hub.start()

    started = asyncio.Event()
    started2 = asyncio.Event()
    never_release = asyncio.Event()
    spawn_count = [0]

    def _runner_factory(p: AgentEngineId, w: str, tid: str, m: str) -> _ControlledRunner:
        spawn_count[0] += 1
        s_ev = started if spawn_count[0] == 1 else started2
        return _ControlledRunner(started_event=s_ev, release_event=never_release)

    mgr = TaskManager(
        runner_factory=_runner_factory,
        event_bus=bus,
        task_store=store,
        security_gatekeeper=gk,
    )
    mgr.start()

    created = await mgr.dispatch_task(
        workspace_canonical_path=str(ws.resolve()),
        workspace_name="secure_repo",
        objective="Run guarded command",
        provider="claude",
    )
    await asyncio.wait_for(started.wait(), timeout=2.0)

    # Gatekeeper requests human approval for this task
    eval_task = asyncio.create_task(
        gk.evaluate_and_authorize(
            CommandInterceptRequest(
                correlation_id="corr-f3",
                source="tier2_claude",
                tool_name="Bash",
                command="sudo rm -rf /tmp/x",
                cwd=str(ws.resolve()),
                task_id=created.task_id,
            )
        )
    )
    await asyncio.sleep(0.05)
    assert (await mgr.get_task(created.task_id)).status == "waiting_approval"
    assert mgr.is_task_waiting_approval(created.task_id) is True
    snap_wait = await hub.build_snapshot(csrf_token="t")
    assert len(snap_wait.pending_approvals) == 1

    # F3: Cancelling task aborts pending approval, unblocks Gatekeeper, clears UI card
    cancelled = await mgr.cancel_task(created.task_id)
    assert cancelled.status == "cancelled"
    decision = await asyncio.wait_for(eval_task, timeout=2.0)
    assert decision.allowed is False
    assert any(e.task_id == created.task_id and e.approved is False for e in resolved_events)
    snap_after = await hub.build_snapshot(csrf_token="t")
    assert len(snap_after.pending_approvals) == 0

    # F3 (direct IPC disconnect / CancelledError in SecurityGatekeeper): publishes resolved event
    gk.register_task_workspace("tsk-disc", str(ws.resolve()))
    eval_disc = asyncio.create_task(
        gk.evaluate_and_authorize(
            CommandInterceptRequest(
                correlation_id="corr-disc",
                source="tier2_claude",
                tool_name="Bash",
                command="sudo id",
                cwd=str(ws.resolve()),
                task_id="tsk-disc",
            )
        )
    )
    await asyncio.sleep(0.05)
    eval_disc.cancel()
    with pytest.raises(asyncio.CancelledError):
        await eval_disc
    assert any(
        e.task_id == "tsk-disc" and e.resolved_by == "task_cancelled_or_disconnected"
        for e in resolved_events
    )
    gk.unregister_task_workspace("tsk-disc")

    # F8: Concurrent cancel_task + CommandApprovalRequestedEvent keeps cancelled in RAM & SQLite
    t2 = await mgr.dispatch_task(
        workspace_canonical_path=str(ws.resolve()),
        workspace_name="secure_repo",
        objective="Race test",
        provider="opencode",
    )
    await asyncio.wait_for(started2.wait(), timeout=2.0)
    await asyncio.gather(
        mgr.cancel_task(t2.task_id),
        bus.publish(
            CommandApprovalRequestedEvent(
                audit_id="aud-race",
                correlation_id="corr-race",
                source="tier2_opencode",
                sanitized_command="rm -rf build",
                cwd=str(ws.resolve()),
                risk_level="medium",
                spoken_summary="Aprobar",
                technical_explanation="Race",
                expires_in_seconds=60.0,
                task_id=t2.task_id,
            )
        ),
    )
    assert (await mgr.get_task(t2.task_id)).status == "cancelled"
    persisted_t2 = await store.get_task(t2.task_id)
    assert persisted_t2 is not None
    assert persisted_t2.status == "cancelled"

    hub.close()
    await mgr.close()
    await store.close()


@pytest.mark.asyncio
async def test_nova020_f1_f5_f7_f12_task_manager_bounds_and_total_timeout(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    ws = allowed_root / "repo"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    bus = OrchestratorEventBus()
    never_release = asyncio.Event()
    mgr = TaskManager(
        runner_factory=lambda p, w, tid, m: _ControlledRunner(release_event=never_release),
        event_bus=bus,
        allowed_roots_provider=lambda: (str(allowed_root),),
        settings=TaskManagerSettings(
            default_provider="agy",
            default_model="claude-opus-5-5-high",
            task_total_timeout_seconds=0.25,
        ),
    )
    mgr.start()

    # F1: allowed_roots enforcement
    with pytest.raises(TaskManagerError) as exc_out:
        await mgr.dispatch_task(
            workspace_canonical_path=str(outside),
            workspace_name="outside",
            objective="Escape",
            provider="claude",
        )
    assert exc_out.value.code == "WORKSPACE_OUTSIDE_ALLOWED_ROOTS"

    # F12: objective > 4000 chars and unsafe model rejected
    with pytest.raises(TaskManagerError) as exc_len:
        await mgr.dispatch_task(
            workspace_canonical_path=str(ws),
            workspace_name="repo",
            objective="a" * 4001,
            provider="claude",
        )
    assert exc_len.value.code == "TASK_INVALID_REQUEST"

    with pytest.raises(TaskManagerError) as exc_mod:
        await mgr.dispatch_task(
            workspace_canonical_path=str(ws),
            workspace_name="repo",
            objective="Valid objective",
            provider="claude",
            model="--bad-flag",
        )
    assert exc_mod.value.code == "TASK_INVALID_REQUEST"

    # F5 & F7: cross-provider default model uses claude's "opus" and total timeout fires cleanly
    task = await mgr.dispatch_task(
        workspace_canonical_path=str(ws),
        workspace_name="repo",
        objective="Hang past total timeout",
        provider="claude",
    )
    assert task.model == "opus"
    await asyncio.wait_for(mgr.wait_for_task(task.task_id), timeout=2.0)
    final_rec = await mgr.get_task(task.task_id)
    assert final_rec.status == "failed"
    assert final_rec.error_code == "RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH"

    await mgr.close()
