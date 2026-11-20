"""Deterministic integration tests for T4: NovaOrchestrator (AC-2, AC-3, AC-4, AC-6, NFR-1)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nova.adapters.workspace_resolver import LocalWorkspaceResolver
from nova.application.orchestrator import (
    NovaOrchestrator,
    OrchestratorSettings,
    VoiceSentenceStreamSplitter,
    sanitize_spoken_chunk,
)
from nova.domain.errors import CodingRunnerError
from nova.domain.events import (
    ChatStreamEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
    RunnerFailureEvent,
    VoiceMilestoneEvent,
    VoiceSpeechChunkEvent,
    WorkspaceChangedEvent,
)
from nova.domain.models import CodingTaskRequest, RunnerProgressEvent


class _FakeTier1Client:
    def __init__(self, chunks: list[str] | None = None) -> None:
        self.chunks = chunks or ["Listo, revise el archivo de configuracion."]
        self.prompts: list[str] = []
        self.switched_paths: list[str] = []
        self.aborted = False

    async def switch_workspace(self, new_workspace_path: str) -> None:
        self.switched_paths.append(new_workspace_path)

    async def stream_prompt(self, message: str) -> AsyncIterator[str]:
        self.prompts.append(message)
        for chunk in self.chunks:
            yield chunk

    async def barge_in_abort(self) -> None:
        self.aborted = True


class _FakeTier2Runner:
    def __init__(self, fail_code: str | None = None) -> None:
        self.requests: list[CodingTaskRequest] = []
        self.fail_code = fail_code

    async def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
        self.requests.append(request)
        if self.fail_code is not None:
            raise CodingRunnerError(code=self.fail_code, message="Simulated runner timeout.")

        conv_id = request.conversation_id or "conv-tier2-777"
        yield RunnerProgressEvent(kind="init", conversation_id=conv_id, step_type="init")
        yield RunnerProgressEvent(
            kind="delta",
            conversation_id=conv_id,
            step_type="agent_response",
            text_delta="```python\ndef build(): pass\n```\nEditando /home/dev/repo/src/main.py",
        )
        yield RunnerProgressEvent(
            kind="completed",
            conversation_id=conv_id,
            step_type="result",
            final_response="Arquitectura hexagonal implementada con todas las pruebas aprobadas.",
            duration_seconds=2.1,
        )


async def test_ac2_routes_simple_task_to_tier1_pi_without_calling_tier2(tmp_path: Path) -> None:
    root = tmp_path / "Proyectos"
    repo = root / "demo-app"
    repo.mkdir(parents=True)

    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    tier1 = _FakeTier1Client()
    tier2 = _FakeTier2Runner()

    orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=tier2,
        event_bus=bus,
    )

    await orchestrator.select_workspace("demo app", mode="voice_fuzzy", correlation_id="c-ws")
    await orchestrator.handle_user_utterance(
        "Lee el README y decime de que trata",
        correlation_id="c-t1",
    )

    assert len(tier1.prompts) == 1
    assert len(tier2.requests) == 0
    assert any(isinstance(e, WorkspaceChangedEvent) for e in events)
    assert any(isinstance(e, ChatStreamEvent) and e.source == "tier1_pi" for e in events)
    milestones = [e for e in events if isinstance(e, VoiceMilestoneEvent)]
    assert len(milestones) >= 1
    assert all(len(m.summary_text.split()) <= 25 for m in milestones)


async def test_ac3_ac4_routes_complex_task_to_tier2_preserves_conversation_and_sanitizes_voice(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Proyectos"
    repo = root / "pagos-core"
    repo.mkdir(parents=True)

    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    tier1 = _FakeTier1Client()
    tier2 = _FakeTier2Runner()

    orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=tier2,
        event_bus=bus,
        settings=OrchestratorSettings(default_tier2_model="gemini-4-carbon-high"),
    )

    await orchestrator.select_workspace(str(repo), mode="exact_path", correlation_id="c-ws2")

    complex_prompt = (
        "Diseña y refactoriza toda la arquitectura multi-archivo del motor de pagos "
        "agregando contratos, migraciones e integracion completa con pruebas."
    )
    await orchestrator.handle_user_utterance(complex_prompt, correlation_id="c-complex-1")
    await orchestrator.handle_user_utterance(complex_prompt, correlation_id="c-complex-2")

    assert len(tier2.requests) == 2
    assert tier2.requests[0].model == "gemini-4-carbon-high"
    assert tier2.requests[0].conversation_id is None
    assert tier2.requests[1].conversation_id == "conv-tier2-777"

    chat_events = [e for e in events if isinstance(e, ChatStreamEvent)]
    voice_events = [e for e in events if isinstance(e, VoiceMilestoneEvent)]
    assert any(e.source == "tier2_runner" and "def build" in e.delta for e in chat_events)
    assert len(voice_events) >= 2
    for v_ev in voice_events:
        assert len(v_ev.summary_text.split()) <= 25
        assert "```" not in v_ev.summary_text
        assert "/home/" not in v_ev.summary_text


async def test_ac6_publishes_runner_failure_event_and_interrupt_voice_alert(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Proyectos"
    repo = root / "sistema-critico"
    repo.mkdir(parents=True)

    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    tier1 = _FakeTier1Client()
    tier2 = _FakeTier2Runner(fail_code="RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH")

    orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=tier2,
        event_bus=bus,
    )
    await orchestrator.select_workspace(str(repo), mode="exact_path", correlation_id="c-ws3")
    await orchestrator.handle_user_utterance(
        "[DELEGATE_TIER2] Refactoriza toda la arquitectura de microservicios y base de datos",
        correlation_id="c-err",
    )

    failures = [e for e in events if isinstance(e, RunnerFailureEvent)]
    assert len(failures) == 1
    assert failures[0].error_code == "RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH"
    interrupts = [
        e for e in events if isinstance(e, VoiceMilestoneEvent) and e.priority == "interrupt"
    ]
    assert len(interrupts) == 1

    await orchestrator.barge_in()
    assert tier1.aborted is True


async def test_nfr1_event_dispatch_p95_overhead_is_well_under_50ms(tmp_path: Path) -> None:
    root = tmp_path / "Proyectos"
    repo = root / "perf-repo"
    repo.mkdir(parents=True)

    bus = OrchestratorEventBus()
    latencies_ms: list[float] = []
    t0 = 0.0

    async def measure(ev: OrchestratorEvent) -> None:
        if isinstance(ev, ChatStreamEvent):
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)

    bus.subscribe(measure)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    tier1 = _FakeTier1Client(chunks=[f"chunk-{i}" for i in range(40)])
    tier2 = _FakeTier2Runner()

    orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=tier2,
        event_bus=bus,
    )
    await orchestrator.select_workspace(str(repo), mode="exact_path", correlation_id="c-perf")

    t0 = time.perf_counter()
    await orchestrator.handle_user_utterance("Explica una linea", correlation_id="c-p95")

    assert len(latencies_ms) == 40
    sorted_lat = sorted(latencies_ms)
    p95 = sorted_lat[int(len(sorted_lat) * 0.95) - 1]
    assert p95 < 50.0


async def test_f4_f6_f8_redacts_secrets_tracks_stage_changes_and_strips_lowercase_marker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Proyectos"
    repo = root / "seguro-app"
    repo.mkdir(parents=True)

    class _MultiStageRunner:
        def __init__(self) -> None:
            self.seen_prompts: list[str] = []

        async def execute(
            self,
            request: CodingTaskRequest,
        ) -> AsyncIterator[RunnerProgressEvent]:
            self.seen_prompts.append(request.prompt)
            if "fallar" in request.prompt:
                raise CodingRunnerError(
                    code="RUNNER_EXECUTION_FAILED",
                    message="Auth failed for dummy key nvapi-sample123456 and Bearer dummy_tok",
                )
            yield RunnerProgressEvent(
                kind="init",
                conversation_id="conv-ms",
                step_type="init",
            )
            yield RunnerProgressEvent(
                kind="delta",
                conversation_id="conv-ms",
                step_type="analysis",
                text_delta="Analizando contratos existentes.",
            )
            yield RunnerProgressEvent(
                kind="delta",
                conversation_id="conv-ms",
                step_type="tool_call",
                text_delta="Ejecutando suite de pruebas unitarias.",
            )
            yield RunnerProgressEvent(
                kind="completed",
                conversation_id="conv-ms",
                step_type="result",
                final_response="Cambios validados.",
            )

    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    tier1 = _FakeTier1Client()
    runner = _MultiStageRunner()

    orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=runner,
        event_bus=bus,
    )
    await orchestrator.select_workspace(str(repo), mode="exact_path", correlation_id="c-s0")
    events.clear()

    await orchestrator.handle_user_utterance(
        "[delegate_tier2] Audita modulos y pruebas",
        correlation_id="c-s1",
    )
    assert runner.seen_prompts[0] == "Audita modulos y pruebas"
    voice_milestones = [e for e in events if isinstance(e, VoiceMilestoneEvent)]
    # Start + stage 'analysis' + stage 'tool_call' + completion = 4 spoken milestones
    assert len(voice_milestones) == 4

    events.clear()
    await orchestrator.handle_user_utterance(
        "[delegate_tier2] Hacer fallar con token",
        correlation_id="c-s2",
    )
    failures = [e for e in events if isinstance(e, RunnerFailureEvent)]
    assert len(failures) == 1
    assert "nvapi-" not in failures[0].sanitized_message
    assert "dummy_tok" not in failures[0].sanitized_message
    assert "<REDACTED:secret>" in failures[0].sanitized_message


async def test_fu2_fu3_chat_stream_secret_redaction_and_full_uuid4_correlation_id(
    tmp_path: Path,
) -> None:
    import uuid as uuid_mod

    root = tmp_path / "Proyectos"
    repo = root / "sec-app"
    repo.mkdir(parents=True)

    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    tier1 = _FakeTier1Client(
        chunks=["Usando token sk-synt9988776655 y Bearer my_secret_bearer_token en respuesta."]
    )
    tier2 = _FakeTier2Runner()

    orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=tier2,
        event_bus=bus,
    )
    await orchestrator.select_workspace(str(repo), mode="exact_path")
    events.clear()

    await orchestrator.handle_user_utterance("Revisa estado rapido")
    chat_events = [e for e in events if isinstance(e, ChatStreamEvent)]
    assert len(chat_events) == 1
    assert "sk-synt9988776655" not in chat_events[0].delta
    assert "my_secret_bearer_token" not in chat_events[0].delta
    assert "<REDACTED:secret>" in chat_events[0].delta

    corr_id = chat_events[0].correlation_id
    assert corr_id.startswith("corr-")
    parsed_uuid = uuid_mod.UUID(corr_id.removeprefix("corr-"))
    assert parsed_uuid.version == 4


async def test_f7_orchestrator_redacts_gatekeeper_ephemeral_secrets_in_chat_stream(
    tmp_path: Path,
) -> None:
    from nova.application.security_gatekeeper import SecurityGatekeeper
    from nova.domain.models import CommandAuditInput, CommandAuditResult

    class _NoopAuditor:
        async def audit(self, request: CommandAuditInput) -> CommandAuditResult:
            return CommandAuditResult(
                risk_level="medium",
                spoken_summary="Necesito aprobacion para este comando.",
                technical_explanation=request.rule_id,
                audited_by="deterministic_fallback",
            )

    root = tmp_path / "Proyectos"
    repo = root / "f7-app"
    repo.mkdir(parents=True)

    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    gatekeeper = SecurityGatekeeper(
        workspace_resolver=resolver,
        auditor=_NoopAuditor(),
        event_bus=bus,
    )
    bare_ephemeral = "ephemeralSecret99887766"
    gatekeeper.register_ephemeral_secret(bare_ephemeral)

    tier1 = _FakeTier1Client(chunks=[f"Salida accidental filtrando {bare_ephemeral} en texto."])
    orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=_FakeTier2Runner(),
        event_bus=bus,
        security_gatekeeper=gatekeeper,
    )
    await orchestrator.select_workspace(str(repo), mode="exact_path")
    events.clear()

    await orchestrator.handle_user_utterance("Consulta simple")
    chat_events = [e for e in events if isinstance(e, ChatStreamEvent)]
    assert len(chat_events) == 1
    assert bare_ephemeral not in chat_events[0].delta
    assert "<REDACTED:secret>" in chat_events[0].delta

    class _LeakingFailRunner:
        async def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
            raise CodingRunnerError(
                code="RUNNER_EXECUTION_FAILED",
                message=f"Subprocess failed with {bare_ephemeral} in stderr",
            )
            yield  # pragma: no cover

    events.clear()
    fail_orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=_LeakingFailRunner(),
        event_bus=bus,
        security_gatekeeper=gatekeeper,
    )
    await fail_orchestrator.handle_user_utterance("[delegate_tier2] Forzar fallo con secreto")
    failures = [e for e in events if isinstance(e, RunnerFailureEvent)]
    assert len(failures) == 1
    assert bare_ephemeral not in failures[0].sanitized_message
    assert "<REDACTED:secret>" in failures[0].sanitized_message

    class _MilestoneSecretRunner:
        async def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
            yield RunnerProgressEvent(
                kind="milestone",
                conversation_id="c-ms-sec",
                step_type=f"stage_{bare_ephemeral}",
            )
            yield RunnerProgressEvent(
                kind="completed",
                conversation_id="c-ms-sec",
                step_type="result",
                final_response="ok",
            )

    events.clear()
    ms_orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=_MilestoneSecretRunner(),
        event_bus=bus,
        security_gatekeeper=gatekeeper,
    )
    await ms_orchestrator.handle_user_utterance("[delegate_tier2] Probar hito con secreto")
    voice_evs = [e for e in events if isinstance(e, VoiceMilestoneEvent)]
    assert all(bare_ephemeral not in v.summary_text for v in voice_evs)


async def test_conversational_utterance_no_workspace_routes_to_tier1(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Proyectos"
    root.mkdir(parents=True)
    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect1(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect1)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    assert resolver.active_context is None

    tier1 = _FakeTier1Client(chunks=["Hoy es sábado 14 de noviembre."])
    tier2 = _FakeTier2Runner()

    orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=tier2,
        event_bus=bus,
    )

    await orchestrator.handle_user_utterance("¿En qué día estamos hoy?")
    assert tier1.prompts == ["¿En qué día estamos hoy?"]
    assert len(tier2.requests) == 0

    voice_evs = [e for e in events if isinstance(e, VoiceMilestoneEvent)]
    assert len(voice_evs) == 1
    assert "Hoy es sábado" in voice_evs[0].summary_text

    chat_evs = [e for e in events if isinstance(e, ChatStreamEvent)]
    assert len(chat_evs) == 1
    assert chat_evs[0].delta == "Hoy es sábado 14 de noviembre."


async def test_complex_code_utterance_no_workspace_prompts_to_select_project(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Proyectos"
    root.mkdir(parents=True)
    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect1(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect1)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    assert resolver.active_context is None

    tier1 = _FakeTier1Client()
    tier2 = _FakeTier2Runner()

    orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=tier2,
        event_bus=bus,
    )

    await orchestrator.handle_user_utterance("[delegate_tier2] Modifica el archivo main.py")
    assert len(tier1.prompts) == 0
    assert len(tier2.requests) == 0

    voice_evs = [e for e in events if isinstance(e, VoiceMilestoneEvent)]
    assert len(voice_evs) == 1
    assert "por favor seleccioná un proyecto" in voice_evs[0].summary_text


async def test_nova013_ac9_clear_workspace_returns_tier1_to_conversational_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nova.domain.events import WorkspaceClearedEvent

    fake_home = tmp_path / "home_operador"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    root = tmp_path / "Workspaces"
    ws = root / "cliente-alfa"
    (ws / "repositories" / "svc-a").mkdir(parents=True)
    (ws / "nova-workspace.toml").write_text(
        'schema_version = "1.0"\n[workspace]\nid = "alfa"\nname = "Alfa"\n'
        'projects = ["repositories/svc-a"]\n',
        encoding="utf-8",
    )

    class _AwareTier1(_FakeTier1Client):
        def __init__(self) -> None:
            super().__init__()
            self.transitions: list[tuple[str, bool, tuple[str, ...]]] = []

        async def switch_workspace(
            self,
            new_workspace_path: str,
            *,
            workspace_active: bool = True,
            allowed_project_paths: tuple[str, ...] = (),
        ) -> None:
            self.transitions.append((new_workspace_path, workspace_active, allowed_project_paths))

    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect_ev(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect_ev)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus, home_dir=fake_home)
    tier1 = _AwareTier1()
    orch = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=_FakeTier2Runner(),
        event_bus=bus,
    )

    ctx = await orch.select_workspace(str(ws), mode="exact_path")
    assert ctx.topology == "multi-project"
    assert tier1.transitions[-1] == (
        str(ws.resolve()),
        True,
        (str((ws / "repositories" / "svc-a").resolve()),),
    )

    await orch.clear_workspace(correlation_id="corr-clear-orch")
    assert resolver.active_context is None
    last_path, last_active, last_projects = tier1.transitions[-1]
    assert last_path == str(fake_home.resolve())
    assert last_active is False
    assert len(last_projects) == 0
    assert any(isinstance(e, WorkspaceClearedEvent) for e in events)


async def test_orchestrator_session_continuity_with_sqlite_history_store(tmp_path: Path) -> None:
    from nova.adapters.sqlite_history_store import SqliteHistoryStore

    db_path = tmp_path / "continuity.db"
    store = SqliteHistoryStore(db_path)

    ws_dir = tmp_path / "project-cont"
    ws_dir.mkdir(parents=True)
    (ws_dir / "aff.yaml").write_text(
        'schema_version: "1.0"\nproject:\n  id: "cont"\n  name: "Cont"\n'
        'repositories: ["sub-a"]\n',
        encoding="utf-8",
    )
    (ws_dir / "repositories" / "sub-a").mkdir(parents=True)

    bus = OrchestratorEventBus()
    resolver = LocalWorkspaceResolver(allowed_roots=[tmp_path], event_bus=bus)
    tier1 = _FakeTier1Client()

    class _ContTier2(_FakeTier2Runner):
        async def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
            self.requests.append(request)
            yield RunnerProgressEvent(
                kind="delta",
                conversation_id="conv-preserved-777",
                step_type="agent_response",
                text_delta="Ejecutando arquitectura...",
            )
            yield RunnerProgressEvent(
                kind="completed",
                conversation_id="conv-preserved-777",
                step_type="result",
                final_response="Completado!",
            )

    tier2 = _ContTier2()
    orch1 = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=tier2,
        event_bus=bus,
        history_store=store,
    )

    # 1. Select workspace and run heavy architecture turn in Orch 1
    ctx = await orch1.select_workspace(str(ws_dir), mode="exact_path")
    await orch1.handle_user_utterance("Refactoriza la arquitectura del microservicio")

    # Verify Orch 1 saved session pointer to SQLite
    pointer = await store.get_session_pointer(ctx.canonical_path)
    assert pointer is not None
    assert pointer.agy_conversation_id == "conv-preserved-777"

    # 2. Simulate complete Nova restart by instantiating Orch 2 with clean in-memory state
    tier2_restart = _ContTier2()
    orch2 = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=tier2_restart,
        event_bus=bus,
        history_store=store,
    )
    # At start, in-memory tier2_conversations is empty
    assert len(orch2._tier2_conversations) == 0

    # Select workspace in Orch 2 -> loads session pointer from SQLite
    await orch2.select_workspace(str(ws_dir), mode="exact_path")
    assert orch2._tier2_conversations.get(ctx.canonical_path) == "conv-preserved-777"

    # Execute heavy turn -> requests has prior conversation_id!
    await orch2.handle_user_utterance("Continuar con la refactorizacion de arquitectura")
    assert len(tier2_restart.requests) == 1
    assert tier2_restart.requests[0].conversation_id == "conv-preserved-777"

    await store.close()


def test_nova018_sanitize_spoken_chunk_and_sentence_splitter_streams_cleanly() -> None:
    raw = (
        "### Resumen\n"
        "Este es el **proyecto Nova**, un asistente local de voz para Linux. "
        "<search>\nfind /home/dev -name '*.py'\n</search>\n"
        "```python\nprint('secret')\n```\n"
        "Además utiliza /home/dev/Proyectos/nova/src/main.py con token nvapi-secret99999999. "
        "¿Quieres profundizar en algún módulo?"
    )
    cleaned = sanitize_spoken_chunk(raw, extra_secrets=("nvapi-secret99999999",))
    assert "###" not in cleaned
    assert "**" not in cleaned
    assert "<search>" not in cleaned
    assert "find" not in cleaned
    assert "print" not in cleaned
    assert "/home/dev" not in cleaned
    assert "nvapi-secret99999999" not in cleaned
    assert "proyecto Nova" in cleaned
    assert "profundizar" in cleaned

    splitter = VoiceSentenceStreamSplitter(extra_secrets=("myTopSecret42",))
    emitted_mid: list[str] = []
    for delta in [
        "Primera oración completa del proyecto Nova. ",
        "Segunda oración hablando sobre la arquitectura hexagonal. ",
        "Tercera oración con secreto myTopSecret42 para cerrar.",
    ]:
        emitted_mid.extend(splitter.feed(delta))
    emitted_final = splitter.flush()

    assert len(emitted_mid) >= 1, "Must emit first sentence before stream ends (NFR-1)"
    all_chunks = emitted_mid + emitted_final
    joined = " ".join(all_chunks)
    assert "Primera oración completa" in joined
    assert "Segunda oración" in joined
    assert "Tercera oración" in joined
    assert "myTopSecret42" not in joined
    assert all(len(c) <= 600 for c in all_chunks)


async def test_nova018_ac2_ac3_ac6_voice_confirmation_then_full_spoken_stream(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Proyectos"
    repo = root / "nova-demo"
    repo.mkdir(parents=True)

    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    long_chunks = [
        "Este es el proyecto Nova, un asistente local de ingenieria por voz diseñado para Linux. ",
        "Combina reconocimiento local en GPU con un orquestador en dos niveles de agentes. ",
        "El primer nivel conversa de manera fluida y el segundo ejecuta cambios profundos. ",
        "Ademas integra un filtro de seguridad de dos capas antes de correr comandos peligrosos.",
    ]
    tier1 = _FakeTier1Client(chunks=long_chunks)
    tier2 = _FakeTier2Runner()
    now = [100.0]

    orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=tier2,
        event_bus=bus,
        clock=lambda: now[0],
    )
    await orchestrator.select_workspace(str(repo), mode="exact_path", correlation_id="c-ws")
    events.clear()

    # Phase 1: voice utterance must NOT invoke Tier 1 or Tier 2; must ask confirmation
    await orchestrator.handle_user_utterance(
        "Explícame de qué trata este proyecto",
        correlation_id="c-voice-1",
        origin="voice",
    )
    assert len(tier1.prompts) == 0
    assert len(tier2.requests) == 0
    assert orchestrator.pending_voice_task is not None
    assert orchestrator.pending_voice_task.revision == 1
    phase1_speech = [e for e in events if isinstance(e, VoiceSpeechChunkEvent)]
    assert len(phase1_speech) >= 1
    assert phase1_speech[-1].is_final is True
    assert "nova-demo" in " ".join(e.text for e in phase1_speech)

    events.clear()
    # Phase 2: explicit voice confirmation executes and streams full spoken chunks (> 25 words)
    await orchestrator.handle_user_utterance(
        "Sí, eso es lo que entiendo",
        correlation_id="c-voice-2",
        origin="voice",
    )
    assert orchestrator.pending_voice_task is None
    assert len(tier1.prompts) == 1
    assert tier1.prompts[0] == "Explícame de qué trata este proyecto"
    phase2_speech = [e for e in events if isinstance(e, VoiceSpeechChunkEvent)]
    assert len(phase2_speech) >= 2
    assert [c.chunk_index for c in phase2_speech] == list(range(len(phase2_speech)))
    assert all(not c.is_final for c in phase2_speech[:-1])
    assert phase2_speech[-1].is_final is True
    full_spoken = " ".join(c.text for c in phase2_speech)
    assert len(full_spoken.split()) > 35
    assert "comandos peligrosos" in full_spoken

    # Idempotency (NFR-2): a duplicate "sí" does NOT re-execute
    events.clear()
    await orchestrator.handle_user_utterance("Sí", correlation_id="c-voice-dup", origin="voice")
    assert len(tier1.prompts) == 1
    dup_speech = [e for e in events if isinstance(e, VoiceSpeechChunkEvent)]
    assert len(dup_speech) >= 1
    assert "pendiente" in " ".join(c.text.lower() for c in dup_speech)


async def test_nova018_ac4_ac5_cancel_correct_ttl_expiry_and_chat_direct(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Proyectos"
    repo = root / "app-uno"
    repo.mkdir(parents=True)

    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    tier1 = _FakeTier1Client(chunks=["Respuesta completa tras corrección."])
    tier2 = _FakeTier2Runner()
    now = [200.0]

    orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=tier2,
        event_bus=bus,
        settings=OrchestratorSettings(voice_confirmation_ttl_seconds=60.0),
        clock=lambda: now[0],
    )
    await orchestrator.select_workspace(str(repo), mode="exact_path")
    events.clear()

    # 1. Request -> Cancel
    await orchestrator.handle_user_utterance(
        "Revisa el módulo de pagos", correlation_id="c-1", origin="voice"
    )
    assert orchestrator.pending_voice_task is not None
    await orchestrator.handle_user_utterance("cancelar", correlation_id="c-2", origin="voice")
    assert orchestrator.pending_voice_task is None
    assert len(tier1.prompts) == 0

    # 2. Request -> Correction -> Confirm
    await orchestrator.handle_user_utterance(
        "Revisa el módulo de audio", correlation_id="c-3", origin="voice"
    )
    await orchestrator.handle_user_utterance(
        "no, mejor revisa el archivo orchestrator", correlation_id="c-4", origin="voice"
    )
    pending = orchestrator.pending_voice_task
    assert pending is not None
    assert pending.revision == 2
    assert "orchestrator" in pending.utterance
    assert len(tier1.prompts) == 0

    await orchestrator.handle_user_utterance("adelante", correlation_id="c-5", origin="voice")
    assert len(tier1.prompts) == 1
    assert "orchestrator" in tier1.prompts[0]

    # 3. Request -> TTL expires -> "sí" must NOT execute
    await orchestrator.handle_user_utterance(
        "Borra archivos temporales", correlation_id="c-6", origin="voice"
    )
    now[0] += 61.0
    assert orchestrator.pending_voice_task is None
    await orchestrator.handle_user_utterance("sí", correlation_id="c-7", origin="voice")
    assert len(tier1.prompts) == 1

    # 4. Typed chat (origin="chat") executes directly without confirmation gate (AC-8)
    await orchestrator.handle_user_utterance(
        "Consulta directa por chat", correlation_id="c-chat", origin="chat"
    )
    assert len(tier1.prompts) == 2
    assert tier1.prompts[1] == "Consulta directa por chat"

    # 5. Workspace change invalidates pending voice task (fail-safe)
    repo2 = root / "app-dos"
    repo2.mkdir(parents=True)
    await orchestrator.handle_user_utterance(
        "Modifica el README", correlation_id="c-ws-old", origin="voice"
    )
    assert orchestrator.pending_voice_task is not None
    await orchestrator.select_workspace(str(repo2), mode="exact_path")
    assert orchestrator.pending_voice_task is None

    # 6. Complex Tier 2 voice task also requires confirmation and streams spoken chunks
    events.clear()
    await orchestrator.handle_user_utterance(
        "[delegate_tier2] Refactoriza toda la arquitectura multi-archivo",
        correlation_id="c-t2-1",
        origin="voice",
    )
    assert len(tier2.requests) == 0
    assert orchestrator.pending_voice_task is not None
    await orchestrator.handle_user_utterance("confirmar", correlation_id="c-t2-2", origin="voice")
    assert len(tier2.requests) == 1
    t2_chunks = [e for e in events if isinstance(e, VoiceSpeechChunkEvent)]
    assert len(t2_chunks) >= 2
    assert t2_chunks[-1].is_final is True


