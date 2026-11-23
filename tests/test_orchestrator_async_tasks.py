"""Tests for T4 (NOVA-020 AC-1, AC-2, AC-3): decoupled _turn_lock and Option A directives."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nova.application.action_directive import ActionDirectiveStreamFilter
from nova.application.orchestrator import NovaOrchestrator, OrchestratorSettings
from nova.application.task_manager import TaskManager
from nova.domain.events import (
    ChatStreamEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
    TaskCreatedEvent,
    VoiceSpeechChunkEvent,
)
from nova.domain.models import (
    CodingTaskRequest,
    ResolveWorkspaceRequest,
    RunnerProgressEvent,
    WorkspaceContext,
)


class _ScriptedTier1Client:
    """Fake Tier1ConversationPort yielding scripted deltas per turn."""

    def __init__(self, turns: list[list[str]]) -> None:
        self._turns = list(turns)
        self.prompts: list[str] = []
        self.switched_paths: list[str] = []
        self.aborted = 0

    async def switch_workspace(
        self,
        new_workspace_path: str,
        *,
        workspace_active: bool = True,
        allowed_project_paths: tuple[str, ...] = (),
        session_id: str | None = None,
    ) -> None:
        _ = (workspace_active, allowed_project_paths, session_id)
        self.switched_paths.append(new_workspace_path)

    async def barge_in_abort(self) -> None:
        self.aborted += 1

    async def stream_prompt(self, message: str) -> AsyncIterator[str]:
        self.prompts.append(message)
        chunks = self._turns.pop(0) if self._turns else ["Entendido."]
        for chunk in chunks:
            await asyncio.sleep(0)
            yield chunk


class _SlowTier2Runner:
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self.started = started
        self.release = release
        self.cancelled = False

    async def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
        yield RunnerProgressEvent(kind="init", conversation_id="conv-slow", step_type="init")
        self.started.set()
        try:
            await self.release.wait()
            yield RunnerProgressEvent(
                kind="completed",
                conversation_id="conv-slow",
                step_type="result",
                final_response="Refactor completed.",
            )
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _FakeResolver:
    def __init__(self, workspaces: dict[str, WorkspaceContext], active_key: str | None) -> None:
        self._workspaces = workspaces
        self._active = workspaces[active_key] if active_key is not None else None

    @property
    def active_context(self) -> WorkspaceContext | None:
        return self._active

    async def resolve(self, request: ResolveWorkspaceRequest) -> WorkspaceContext:
        for key, ctx in self._workspaces.items():
            if request.query in (key, ctx.name, ctx.canonical_path):
                self._active = ctx
                return ctx
        raise AssertionError(f"Unknown workspace query: {request.query}")


def _ws(tmp_path: Path, name: str) -> WorkspaceContext:
    p = tmp_path / name
    p.mkdir(parents=True, exist_ok=True)
    return WorkspaceContext(
        workspace_id=f"ws-{name}",
        name=name,
        canonical_path=str(p.resolve()),
        selected_via="exact_path",
    )


def test_nova020_op1_action_directive_filter_handles_single_char_streaming_and_malformed() -> None:
    flt = ActionDirectiveStreamFilter()
    raw = (
        "Claro, mando esa tarea ahora. "
        '[[NOVA_ACTION:{"action":"dispatch_task",'
        '"objective":"Fix [a]]b parser","provider_hint":"claude"}]]'
        " Mientras tanto podemos seguir hablando."
    )
    out_parts: list[str] = []
    for ch in raw:
        vis = flt.feed(ch)
        if vis:
            out_parts.append(vis)
    tail = flt.flush()
    if tail:
        out_parts.append(tail)

    visible = "".join(out_parts)
    assert "NOVA_ACTION" not in visible
    assert "dispatch_task" not in visible
    assert "Claro, mando esa tarea ahora." in visible
    assert "Mientras tanto podemos seguir hablando." in visible
    assert len(flt.directives) == 1
    assert flt.directives[0].action == "dispatch_task"
    assert flt.directives[0].objective == "Fix [a]]b parser"
    assert flt.directives[0].provider_hint == "claude"

    # Malformed directive is suppressed cleanly and never leaks JSON
    flt_bad = ActionDirectiveStreamFilter()
    bad_vis = flt_bad.feed('Ok [[NOVA_ACTION:{not valid json]] listo.') + flt_bad.flush()
    assert "NOVA_ACTION" not in bad_vis
    assert "not valid json" not in bad_vis
    assert flt_bad.directives == ()
    assert flt_bad.invalid_count == 1


@pytest.mark.asyncio
async def test_nova020_ac1_ac2_turn_lock_decoupled_and_structured_directives_flow(
    tmp_path: Path,
) -> None:
    ws_alpha = _ws(tmp_path, "alpha")
    ws_beta = _ws(tmp_path, "beta")
    resolver = _FakeResolver({"alpha": ws_alpha, "beta": ws_beta}, "alpha")
    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def _collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(_collect)

    started = asyncio.Event()
    release = asyncio.Event()
    slow_runner = _SlowTier2Runner(started, release)

    mgr = TaskManager(
        runner_factory=lambda provider, ws_path, task_id, model: slow_runner,
        event_bus=bus,
    )
    mgr.start()

    tier1 = _ScriptedTier1Client(
        turns=[
            [
                "Dale, la mando en segundo plano. ",
                '[[NOVA_ACTION:{"action":"dispatch_task",'
                '"objective":"Refactorizar parser","provider_hint":"claude"}]]',
            ],
            [
                "Claro, reviso el estado. ",
                '[[NOVA_ACTION:{"action":"list_tasks","status_filter":"active"}]]',
            ],
            [
                "Perfecto, la cancelo ahora. ",
                '[[NOVA_ACTION:{"action":"cancel_task"}]]',
            ],
        ]
    )
    orch = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=slow_runner,
        event_bus=bus,
        settings=OrchestratorSettings(tier1_engine="pi", tier2_engine="agy"),
        task_manager=mgr,
    )

    # Turn 1 dispatches slow Tier 2 task and releases _turn_lock in << 2s!
    t0 = time.monotonic()
    await orch.handle_user_utterance("Refactoriza el parser en claude", origin="voice")
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0

    await asyncio.wait_for(started.wait(), timeout=2.0)

    # Verify directive JSON NEVER leaked into ChatStreamEvent or VoiceSpeechChunkEvent
    chat_texts = [e.delta for e in events if isinstance(e, ChatStreamEvent)]
    speech_texts = [e.text for e in events if isinstance(e, VoiceSpeechChunkEvent)]
    assert not any("NOVA_ACTION" in txt for txt in chat_texts)
    assert not any("NOVA_ACTION" in txt for txt in speech_texts)

    created_events = [e for e in events if isinstance(e, TaskCreatedEvent)]
    assert len(created_events) == 1
    task_id = created_events[0].task.task_id
    assert created_events[0].task.provider == "claude"

    # AC-1: While slow_runner is STILL running, we can switch workspace AND converse with Tier 1!
    await asyncio.wait_for(orch.select_workspace("beta"), timeout=1.0)
    assert resolver.active_context is ws_beta

    # AC-1: barge_in() interrupts Tier 1 ONLY, NEVER cancels running Tier 2 worker
    await orch.barge_in()
    assert slow_runner.cancelled is False
    assert (await mgr.get_task(task_id)).status == "running"

    # Turn 2: list_tasks via structured directive while Tier 2 is still running
    await asyncio.wait_for(
        orch.handle_user_utterance("Como van las tareas?", origin="voice"),
        timeout=1.0,
    )
    assert any(task_id in e.delta for e in events if isinstance(e, ChatStreamEvent))

    # Turn 3: cancel_task via structured directive (auto-resolves single active task)
    await asyncio.wait_for(
        orch.handle_user_utterance("Cancela la tarea", origin="voice"),
        timeout=1.0,
    )
    assert (await mgr.get_task(task_id)).status == "cancelled"
    assert slow_runner.cancelled is True

    await mgr.close()


@pytest.mark.asyncio
async def test_nova020_ac3_no_workspace_rejects_dispatch_and_voice_confirmation_preserved(
    tmp_path: Path,
) -> None:
    ws_alpha = _ws(tmp_path, "alpha")
    resolver = _FakeResolver({"alpha": ws_alpha}, None)
    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def _collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(_collect)

    started = asyncio.Event()
    release = asyncio.Event()
    release.set()
    runner = _SlowTier2Runner(started, release)
    mgr = TaskManager(
        runner_factory=lambda provider, ws_path, task_id, model: runner,
        event_bus=bus,
    )
    mgr.start()

    tier1 = _ScriptedTier1Client(
        turns=[
            ['[[NOVA_ACTION:{"action":"dispatch_task","objective":"Edit code"}]]'],
            ['[[NOVA_ACTION:{"action":"dispatch_task","objective":"Edit code in alpha"}]]'],
        ]
    )
    orch = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=runner,
        event_bus=bus,
        settings=OrchestratorSettings(require_voice_confirmation=True),
        task_manager=mgr,
    )

    # Without workspace: dispatch_task directive must NOT create any task
    await orch.handle_user_utterance("Edita el codigo", origin="chat")
    assert await mgr.list_tasks() == []
    assert any("proyecto" in e.delta.lower() for e in events if isinstance(e, ChatStreamEvent))

    # With workspace + require_voice_confirmation=True on voice origin: holds PendingVoiceTask first
    await orch.select_workspace("alpha")
    await orch.handle_user_utterance("Edita el codigo en alpha", origin="voice")
    assert orch.pending_voice_task is not None
    assert await mgr.list_tasks() == []

    # After explicit confirmation, Tier 1 runs and enqueues task asynchronously
    await orch.handle_user_utterance("si", origin="voice")
    tasks = await mgr.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].workspace_name == "alpha"

    await mgr.wait_for_task(tasks[0].task_id)
    await mgr.close()
