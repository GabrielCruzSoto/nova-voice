"""Deterministic integration tests for T4: NovaOrchestrator (AC-2, AC-3, AC-4, AC-6, NFR-1)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

from nova.adapters.workspace_resolver import LocalWorkspaceResolver
from nova.application.orchestrator import NovaOrchestrator, OrchestratorSettings
from nova.domain.errors import CodingRunnerError
from nova.domain.events import (
    ChatStreamEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
    RunnerFailureEvent,
    VoiceMilestoneEvent,
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
    bare_ephemeral = "bare_ipc_token_value_88776655"
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
