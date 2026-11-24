"""Asynchronous background TaskManager with state machine and workspace locking (NOVA-020)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import random
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nova.domain.errors import (
    ApprovalNotFoundError,
    NovaDomainError,
    TaskManagerError,
    TaskNotFoundError,
    TaskQueueFullError,
    TaskStateError,
    redact_sensitive_text,
)
from nova.domain.events import (
    ChatStreamEvent,
    CommandApprovalRequestedEvent,
    CommandApprovalResolvedEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
    RunnerFailureEvent,
    TaskCreatedEvent,
    TaskProgressEvent,
    TaskStateChangedEvent,
    VoiceMilestoneEvent,
)
from nova.domain.models import (
    ALLOWED_TASK_TRANSITIONS,
    DEFAULT_PI_MODEL,
    MAX_TASK_ERROR_CHARS,
    MAX_TASK_LOG_CHARS,
    MAX_TASK_OBJECTIVE_CHARS,
    MAX_TASK_SUMMARY_CHARS,
    TERMINAL_TASK_STATUSES,
    AgentEngineId,
    AgentSessionPointer,
    AgentTaskRecord,
    CodingTaskRequest,
    TaskStatus,
    WorkspaceSessionPointer,
)
from nova.domain.ports import CodingRunnerPort, HistoryStorePort, TaskStorePort

logger = logging.getLogger("nova.task_manager")

TaskRunnerFactory = Callable[[AgentEngineId, str, str, str], CodingRunnerPort]

_SAFE_MODEL_TOKEN_RE = re.compile(r"^[^\s\x00-\x1f\x7f-][^\s\x00-\x1f\x7f]{0,199}$")
_PROVIDER_DEFAULT_TIER2_MODELS: dict[AgentEngineId, str] = {
    "claude": "opus",
    "opencode": "anthropic/claude-sonnet-4-5",
    "agy": "claude-opus-5-5-high",
    "pi": DEFAULT_PI_MODEL,
}


@contextlib.asynccontextmanager
async def _closing_async_iter[T](stream: AsyncIterator[T]) -> AsyncIterator[AsyncIterator[T]]:
    """Deterministically await stream.aclose() on exit or cancellation."""
    try:
        yield stream
    finally:
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            res = aclose()
            if inspect.isawaitable(res):
                await res


def _validate_and_resolve_workspace(
    raw_workspace: str,
    allowed_roots: tuple[str, ...],
) -> tuple[str, str | None, str | None]:
    """Resolve workspace off-loop and enforce non-$HOME, allowed_roots, and existing directory."""
    cleaned = raw_workspace.strip()
    home_dir = Path.home().resolve()
    if cleaned in {"~", "~/", "$HOME", "${HOME}"}:
        return (
            str(home_dir),
            "WORKSPACE_HOME_FORBIDDEN",
            "Home directory cannot be used as a task workspace.",
        )
    candidate = Path(cleaned).expanduser().resolve()
    resolved_str = str(candidate)
    if candidate == home_dir:
        return (
            resolved_str,
            "WORKSPACE_HOME_FORBIDDEN",
            "Home directory cannot be used as a task workspace.",
        )
    if allowed_roots:
        resolved_roots = [
            Path(root).expanduser().resolve()
            for root in allowed_roots
            if root and Path(root).expanduser().resolve() != home_dir
        ]
        if resolved_roots and not any(
            candidate.is_relative_to(root) and candidate != root for root in resolved_roots
        ):
            return (
                resolved_str,
                "WORKSPACE_OUTSIDE_ALLOWED_ROOTS",
                "Task workspace path is outside configured allowed_roots.",
            )
    if not candidate.exists() or not candidate.is_dir():
        return (
            resolved_str,
            "WORKSPACE_NOT_FOUND",
            "Task workspace path does not exist or is not a directory.",
        )
    return resolved_str, None, None


def _safe_milestone_text(raw: str, fallback: str, *, extra_secrets: tuple[str, ...] = ()) -> str:
    redacted = redact_sensitive_text(raw, extra_secrets=extra_secrets)
    words = [w for w in " ".join(redacted.split()).split(" ") if w]
    if not words:
        words = fallback.split()
    return " ".join(words[:24])


class TaskManagerSettings(BaseModel):
    """Concurrency, timeout, and bounded-cache limits for TaskManager (NOVA-020 NFR-2)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    max_concurrent_tasks: int = Field(default=4, ge=1, le=16)
    max_queued_tasks: int = Field(default=32, ge=1, le=128)
    max_tracked_tasks_in_ram: int = Field(default=100, ge=10, le=500)
    stdout_heartbeat_timeout_seconds: float = Field(default=90.0, gt=0.0, le=600.0)
    task_total_timeout_seconds: float = Field(default=600.0, gt=0.0, le=3600.0)
    terminal_cache_ttl_seconds: float = Field(default=3600.0, gt=0.0, le=86400.0)
    shutdown_cancel_grace_seconds: float = Field(default=5.0, gt=0.0, le=30.0)
    default_provider: AgentEngineId = "agy"
    default_model: str = Field(default="claude-opus-5-5-high", max_length=200)


class TaskManager:
    """Schedules, isolates, persists, and supervises asynchronous Tier 2 agent tasks."""

    def __init__(
        self,
        *,
        runner_factory: TaskRunnerFactory,
        event_bus: OrchestratorEventBus,
        task_store: TaskStorePort | None = None,
        history_store: HistoryStorePort | None = None,
        security_gatekeeper: Any | None = None,
        allowed_roots_provider: Callable[[], tuple[str, ...]] | None = None,
        extra_secrets_provider: Callable[[], tuple[str, ...]] | None = None,
        settings: TaskManagerSettings | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._runner_factory = runner_factory
        self._event_bus = event_bus
        self._task_store = task_store
        self._history_store = history_store
        self._security_gatekeeper = security_gatekeeper
        self._allowed_roots_provider = allowed_roots_provider
        self._extra_secrets_provider = extra_secrets_provider
        self._settings = settings or TaskManagerSettings()
        self._clock = clock

        self._semaphore = asyncio.Semaphore(self._settings.max_concurrent_tasks)
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._tasks: OrderedDict[str, AgentTaskRecord] = OrderedDict()
        self._ram_expires_at: dict[str, float] = {}
        self._worker_tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_requested: set[str] = set()
        self._started = False
        self._closed = False

    def is_task_waiting_approval(self, task_id: str) -> bool:
        """Return True while the given task is blocked awaiting human Gatekeeper approval."""
        rec = self._tasks.get(task_id)
        return rec is not None and rec.status == "waiting_approval"

    @property
    def settings(self) -> TaskManagerSettings:
        return self._settings

    @property
    def ram_cache_size(self) -> int:
        return len(self._tasks)

    @property
    def active_count(self) -> int:
        return sum(
            1 for task in self._tasks.values() if task.status in ("running", "waiting_approval")
        )

    @property
    def queued_count(self) -> int:
        return sum(1 for task in self._tasks.values() if task.status == "queued")

    def update_defaults(
        self,
        *,
        default_provider: AgentEngineId | None = None,
        default_model: str | None = None,
    ) -> None:
        """Hot-update default provider/model for newly dispatched tasks."""
        updates: dict[str, object] = {}
        if default_provider is not None:
            updates["default_provider"] = default_provider
        if default_model is not None and default_model.strip():
            updates["default_model"] = default_model.strip()
        if updates:
            self._settings = self._settings.model_copy(update=updates)

    def start(self) -> None:
        """Idempotently subscribe to OrchestratorEventBus for Gatekeeper correlation."""
        if not self._started:
            self._started = True
            self._event_bus.subscribe(self._on_event)

    async def reconcile_on_startup(self) -> int:
        """Mark interrupted non-terminal persisted tasks as failed after daemon restart."""
        if self._task_store is None:
            return 0
        count = await self._task_store.reconcile_interrupted_tasks(
            updated_at=self._clock(),
            reason="DAEMON_RESTARTED",
        )
        if count > 0:
            self._log("tasks_reconciled_on_startup", count=str(count))
        return count

    @staticmethod
    def validate_transition(current: TaskStatus, target: TaskStatus) -> None:
        """Enforce closed state machine transitions (OP-2)."""
        allowed = ALLOWED_TASK_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise TaskStateError(current, target)

    def _extra_secrets(self) -> tuple[str, ...]:
        secrets: list[str] = []
        if self._security_gatekeeper is not None:
            gk_secrets = getattr(self._security_gatekeeper, "extra_secrets", ())
            if isinstance(gk_secrets, tuple):
                secrets.extend(gk_secrets)
        if self._extra_secrets_provider is not None:
            secrets.extend(self._extra_secrets_provider())
        return tuple(secrets)

    def _redact(self, text: str) -> str:
        return redact_sensitive_text(text, extra_secrets=self._extra_secrets())

    def _log(self, event_name: str, **fields: str | bool) -> None:
        payload: dict[str, str | bool] = {"event": event_name}
        for key, val in fields.items():
            payload[key] = self._redact(val) if isinstance(val, str) else val
        logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def _workspace_lock(self, workspace_canonical_path: str) -> asyncio.Lock:
        lock = self._workspace_locks.get(workspace_canonical_path)
        if lock is None:
            lock = asyncio.Lock()
            self._workspace_locks[workspace_canonical_path] = lock
        return lock

    def _cleanup_idle_workspace_lock(self, workspace_canonical_path: str) -> None:
        lock = self._workspace_locks.get(workspace_canonical_path)
        if lock is None or lock.locked():
            return
        has_pending = any(
            t.workspace_canonical_path == workspace_canonical_path
            and t.status not in TERMINAL_TASK_STATUSES
            for t in self._tasks.values()
        )
        if not has_pending:
            self._workspace_locks.pop(workspace_canonical_path, None)

    def _task_lock(self, task_id: str) -> asyncio.Lock:
        lock = self._task_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._task_locks[task_id] = lock
        return lock

    def _cleanup_idle_task_lock(self, task_id: str) -> None:
        lock = self._task_locks.get(task_id)
        if lock is not None and not lock.locked() and task_id not in self._tasks:
            self._task_locks.pop(task_id, None)

    def _record_ram_task(self, task: AgentTaskRecord) -> None:
        self._tasks[task.task_id] = task
        self._tasks.move_to_end(task.task_id)
        if task.status in TERMINAL_TASK_STATUSES:
            jitter = random.uniform(0.95, 1.05)
            self._ram_expires_at[task.task_id] = (
                self._clock() + self._settings.terminal_cache_ttl_seconds * jitter
            )
        else:
            self._ram_expires_at.pop(task.task_id, None)
        self.prune_expired_ram_cache()

    def prune_expired_ram_cache(self) -> None:
        """Evict expired terminal tasks and enforce bounded max_tracked_tasks_in_ram."""
        now = self._clock()
        expired_ids = [
            tid
            for tid, exp in self._ram_expires_at.items()
            if (
                exp <= now
                and tid in self._tasks
                and self._tasks[tid].status in TERMINAL_TASK_STATUSES
            )
        ]
        for tid in expired_ids:
            self._tasks.pop(tid, None)
            self._ram_expires_at.pop(tid, None)
            self._cleanup_idle_task_lock(tid)

        while len(self._tasks) > self._settings.max_tracked_tasks_in_ram:
            evict_id = next(
                (
                    tid
                    for tid, rec in self._tasks.items()
                    if rec.status in TERMINAL_TASK_STATUSES
                ),
                None,
            )
            if evict_id is None:
                break
            self._tasks.pop(evict_id, None)
            self._ram_expires_at.pop(evict_id, None)
            self._cleanup_idle_task_lock(evict_id)

    async def _persist_task(self, task: AgentTaskRecord) -> None:
        if self._task_store is not None:
            await self._task_store.upsert_task(task)

    async def _apply_transition_locked(
        self,
        current: AgentTaskRecord,
        target: TaskStatus,
        *,
        reason: str,
        last_log: str | None = None,
        result_summary: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        pending_audit_id: str | None = None,
        clear_pending_audit: bool = False,
        conversation_id: str | None = None,
    ) -> AgentTaskRecord:
        self.validate_transition(current.status, target)
        now = self._clock()
        updates: dict[str, Any] = {
            "status": target,
            "updated_at": now,
        }
        if target == "running" and current.started_at is None:
            updates["started_at"] = now
        if target in TERMINAL_TASK_STATUSES:
            updates["completed_at"] = now
            updates["pending_audit_id"] = None
        if last_log is not None:
            updates["last_log"] = self._redact(last_log)[:MAX_TASK_LOG_CHARS]
        if result_summary is not None:
            updates["result_summary"] = self._redact(result_summary)[:MAX_TASK_SUMMARY_CHARS]
        if error_code is not None:
            updates["error_code"] = error_code[:120]
        if error_message is not None:
            updates["error_message"] = self._redact(error_message)[:MAX_TASK_ERROR_CHARS]
        if clear_pending_audit:
            updates["pending_audit_id"] = None
        elif pending_audit_id is not None:
            updates["pending_audit_id"] = pending_audit_id
        if conversation_id is not None:
            updates["conversation_id"] = conversation_id

        updated = current.model_copy(update=updates)
        self._record_ram_task(updated)
        await self._persist_task(updated)
        self._log(
            "task_state_changed",
            task_id=current.task_id,
            correlation_id=updated.correlation_id,
            previous_status=current.status,
            status=target,
            reason=reason,
        )
        await self._event_bus.publish(
            TaskStateChangedEvent(
                task_id=current.task_id,
                correlation_id=updated.correlation_id,
                previous_status=current.status,
                status=target,
                task=updated,
                reason=reason,
            )
        )
        return updated

    async def _transition(
        self,
        task_id: str,
        target: TaskStatus,
        *,
        reason: str,
        last_log: str | None = None,
        result_summary: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        pending_audit_id: str | None = None,
        clear_pending_audit: bool = False,
        conversation_id: str | None = None,
    ) -> AgentTaskRecord:
        async with self._task_lock(task_id):
            current = self._tasks.get(task_id)
            if current is None:
                raise TaskNotFoundError(task_id)
            return await self._apply_transition_locked(
                current,
                target,
                reason=reason,
                last_log=last_log,
                result_summary=result_summary,
                error_code=error_code,
                error_message=error_message,
                pending_audit_id=pending_audit_id,
                clear_pending_audit=clear_pending_audit,
                conversation_id=conversation_id,
            )

    async def _transition_to_terminal(
        self,
        task_id: str,
        target: Literal["cancelled", "failed"],
        *,
        reason: str,
        last_log: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> tuple[AgentTaskRecord, str | None, bool]:
        """Atomically transition to terminal state; return (record, prev_audit_id, transitioned)."""
        async with self._task_lock(task_id):
            current = self._tasks.get(task_id)
            if current is None:
                raise TaskNotFoundError(task_id)
            if current.status in TERMINAL_TASK_STATUSES:
                return current, None, False
            prev_audit = current.pending_audit_id
            updated = await self._apply_transition_locked(
                current,
                target,
                reason=reason,
                last_log=last_log,
                error_code=error_code,
                error_message=error_message,
                clear_pending_audit=True,
            )
            return updated, prev_audit, True

    async def dispatch_task(
        self,
        *,
        workspace_canonical_path: str,
        workspace_name: str,
        objective: str,
        provider: AgentEngineId | None = None,
        model: str | None = None,
        correlation_id: str | None = None,
    ) -> AgentTaskRecord:
        """Validate, enqueue, persist, and launch an asynchronous Tier 2 background task."""
        if self._closed:
            raise TaskManagerError(
                code="TASK_MANAGER_UNAVAILABLE",
                message="TaskManager is shutting down and cannot accept new tasks.",
            )

        cleaned_ws = workspace_canonical_path.strip()
        if not cleaned_ws:
            raise TaskManagerError(
                code="WORKSPACE_REQUIRED",
                message="An active workspace is required to dispatch a background task.",
            )
        allowed_roots = (
            self._allowed_roots_provider() if self._allowed_roots_provider is not None else ()
        )
        resolved_ws, ws_err_code, ws_err_msg = await asyncio.to_thread(
            _validate_and_resolve_workspace,
            cleaned_ws,
            allowed_roots,
        )
        if ws_err_code is not None:
            raise TaskManagerError(
                code=ws_err_code,
                message=ws_err_msg or "Invalid task workspace path.",
            )

        stripped_obj = objective.strip()
        if not stripped_obj:
            raise TaskManagerError(
                code="TASK_INVALID_REQUEST",
                message="Task objective cannot be empty.",
            )
        if len(stripped_obj) > MAX_TASK_OBJECTIVE_CHARS:
            raise TaskManagerError(
                code="TASK_INVALID_REQUEST",
                message=(
                    f"Task objective exceeds maximum length of {MAX_TASK_OBJECTIVE_CHARS} chars."
                ),
            )
        redacted_objective = self._redact(stripped_obj)
        if len(redacted_objective) > MAX_TASK_OBJECTIVE_CHARS:
            redacted_objective = redacted_objective[:MAX_TASK_OBJECTIVE_CHARS]

        effective_provider: AgentEngineId = provider or self._settings.default_provider
        if model is not None and model.strip():
            cleaned_model = model.strip()
            if not _SAFE_MODEL_TOKEN_RE.fullmatch(cleaned_model):
                raise TaskManagerError(
                    code="TASK_INVALID_REQUEST",
                    message=(
                        "Task model must be <= 200 characters with no whitespace, "
                        "control characters, or leading '-'."
                    ),
                )
            effective_model = cleaned_model
        elif effective_provider == self._settings.default_provider:
            effective_model = self._settings.default_model.strip() or (
                _PROVIDER_DEFAULT_TIER2_MODELS[effective_provider]
            )
        else:
            effective_model = _PROVIDER_DEFAULT_TIER2_MODELS[effective_provider]

        if self.queued_count >= self._settings.max_queued_tasks:
            raise TaskQueueFullError(self._settings.max_queued_tasks)

        corr_id = correlation_id or f"corr-{uuid.uuid4()}"
        task_id = f"tsk-{uuid.uuid4().hex[:12]}"
        now = self._clock()

        record = AgentTaskRecord(
            task_id=task_id,
            correlation_id=corr_id,
            workspace_canonical_path=resolved_ws,
            workspace_name=self._redact(workspace_name.strip() or Path(resolved_ws).name)[:200],
            provider=effective_provider,
            model=effective_model,
            tier="tier2",
            objective=redacted_objective,
            status="queued",
            last_log="En cola para ejecución.",
            created_at=now,
            updated_at=now,
        )
        self._record_ram_task(record)
        await self._persist_task(record)
        self._log(
            "task_created",
            task_id=task_id,
            correlation_id=corr_id,
            provider=effective_provider,
            workspace_name=record.workspace_name,
        )
        await self._event_bus.publish(TaskCreatedEvent(correlation_id=corr_id, task=record))

        worker = asyncio.create_task(self._execute_task_worker(task_id))
        self._worker_tasks[task_id] = worker

        def _cleanup_worker(_done: asyncio.Task[None]) -> None:
            self._worker_tasks.pop(task_id, None)

        worker.add_done_callback(_cleanup_worker)
        return record

    async def get_task(self, task_id: str) -> AgentTaskRecord:
        """Return latest task snapshot from RAM cache or persistent SQLite store."""
        self.prune_expired_ram_cache()
        cached = self._tasks.get(task_id)
        if cached is not None:
            return cached
        if self._task_store is not None:
            persisted = await self._task_store.get_task(task_id)
            if persisted is not None:
                return persisted
        raise TaskNotFoundError(task_id)

    async def list_tasks(
        self,
        *,
        limit: int = 50,
        status: TaskStatus | None = None,
        workspace_path: str | None = None,
    ) -> list[AgentTaskRecord]:
        """Return bounded tasks newest-first merging live RAM state and SQLite history."""
        self.prune_expired_ram_cache()
        bounded_limit = min(max(1, limit), 200)
        merged: dict[str, AgentTaskRecord] = {}
        if self._task_store is not None:
            stored = await self._task_store.list_tasks(
                limit=bounded_limit,
                status=status,
                workspace_path=workspace_path,
            )
            for item in stored:
                merged[item.task_id] = item
        for item in self._tasks.values():
            if status is not None and item.status != status:
                merged.pop(item.task_id, None)
                continue
            if workspace_path is not None and item.workspace_canonical_path != workspace_path:
                continue
            merged[item.task_id] = item

        ordered = sorted(merged.values(), key=lambda t: (t.created_at, t.updated_at), reverse=True)
        return ordered[:bounded_limit]

    async def _abort_pending_approval(self, audit_id: str | None, *, resolved_by: str) -> None:
        if not audit_id or self._security_gatekeeper is None:
            return
        resolve_fn = getattr(self._security_gatekeeper, "resolve_command_approval", None)
        if callable(resolve_fn):
            with contextlib.suppress(ApprovalNotFoundError, Exception):
                await resolve_fn(audit_id, approved=False, resolved_by=resolved_by)

    async def cancel_task(self, task_id: str) -> AgentTaskRecord:
        """Idempotently cancel a queued, running, or waiting_approval task (OP-2, AC-7)."""
        task = await self.get_task(task_id)
        if task.status in TERMINAL_TASK_STATUSES:
            return task

        self._cancel_requested.add(task_id)
        updated, pending_audit, transitioned = await self._transition_to_terminal(
            task_id,
            "cancelled",
            reason="cancelled_by_user",
            last_log="Cancelada por el usuario.",
        )
        if not transitioned:
            self._cancel_requested.discard(task_id)
            return updated
        await self._abort_pending_approval(pending_audit, resolved_by="task_cancelled")
        worker = self._worker_tasks.get(task_id)
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(worker),
                    timeout=self._settings.shutdown_cancel_grace_seconds,
                )
        self._cancel_requested.discard(task_id)
        return updated

    async def wait_for_task(self, task_id: str) -> AgentTaskRecord:
        """Await completion of an in-flight task (callers may wrap with asyncio.wait_for)."""
        worker = self._worker_tasks.get(task_id)
        if worker is not None and not worker.done():
            await asyncio.shield(worker)
        return await self.get_task(task_id)

    async def close(self) -> None:
        """Cancel all active background tasks cleanly and unsubscribe from the event bus."""
        self._closed = True
        if self._started:
            self._started = False
            self._event_bus.unsubscribe(self._on_event)

        pending_audits = [
            rec.pending_audit_id
            for rec in self._tasks.values()
            if rec.pending_audit_id and rec.status not in TERMINAL_TASK_STATUSES
        ]
        for audit_id in pending_audits:
            await self._abort_pending_approval(audit_id, resolved_by="daemon_shutdown")

        pending_workers = [w for w in self._worker_tasks.values() if not w.done()]
        for tid in list(self._worker_tasks.keys()):
            self._cancel_requested.add(tid)
        for worker in pending_workers:
            worker.cancel()
        if pending_workers:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*pending_workers, return_exceptions=True),
                    timeout=self._settings.shutdown_cancel_grace_seconds,
                )
        self._worker_tasks.clear()

    async def _on_event(self, event: OrchestratorEvent) -> None:
        if isinstance(event, CommandApprovalRequestedEvent) and event.task_id:
            task = self._tasks.get(event.task_id)
            if task is not None and task.status == "running":
                with contextlib.suppress(TaskNotFoundError, TaskStateError):
                    await self._transition(
                        event.task_id,
                        "waiting_approval",
                        reason="gatekeeper_approval_requested",
                        last_log=f"Esperando aprobación de comando: {event.sanitized_command}",
                        pending_audit_id=event.audit_id,
                    )
            return

        if isinstance(event, CommandApprovalResolvedEvent) and event.task_id:
            task = self._tasks.get(event.task_id)
            if task is not None and task.status == "waiting_approval":
                with contextlib.suppress(TaskNotFoundError, TaskStateError):
                    await self._transition(
                        event.task_id,
                        "running",
                        reason=f"gatekeeper_approval_{event.verdict}",
                        last_log=(
                            "Comando aprobado; reanudando ejecución."
                            if event.approved
                            else "Comando rechazado por el usuario; continuando."
                        ),
                        clear_pending_audit=True,
                    )

    async def _lookup_prior_conversation(
        self,
        workspace_path: str,
        provider: AgentEngineId,
    ) -> str | None:
        if self._history_store is None:
            return None
        get_agent = getattr(self._history_store, "get_agent_session", None)
        if callable(get_agent):
            ptr = await get_agent(workspace_path, "tier2", provider)
            if ptr is not None and getattr(ptr, "session_id", None):
                return str(ptr.session_id)
        if provider == "agy":
            legacy = await self._history_store.get_session_pointer(workspace_path)
            if legacy is not None and legacy.agy_conversation_id:
                return legacy.agy_conversation_id
        return None

    async def _save_conversation_pointer(
        self,
        workspace_path: str,
        provider: AgentEngineId,
        conversation_id: str,
    ) -> None:
        if self._history_store is None or not conversation_id or conversation_id == "unknown":
            return
        now_ts = self._clock()
        save_agent = getattr(self._history_store, "save_agent_session", None)
        if callable(save_agent):
            await save_agent(
                AgentSessionPointer(
                    workspace_canonical_path=workspace_path,
                    tier="tier2",
                    agent_provider=provider,
                    session_id=conversation_id,
                    updated_at=now_ts,
                )
            )
        if provider == "agy":
            await self._history_store.save_session_pointer(
                WorkspaceSessionPointer(
                    workspace_canonical_path=workspace_path,
                    agy_conversation_id=conversation_id,
                    updated_at=now_ts,
                )
            )

    def _register_gatekeeper_workspace(self, task_id: str, workspace_path: str) -> None:
        if self._security_gatekeeper is None:
            return
        reg = getattr(self._security_gatekeeper, "register_task_workspace", None)
        if callable(reg):
            reg(task_id, workspace_path)

    def _unregister_gatekeeper_workspace(self, task_id: str) -> None:
        if self._security_gatekeeper is None:
            return
        unreg = getattr(self._security_gatekeeper, "unregister_task_workspace", None)
        if callable(unreg):
            unreg(task_id)

    async def _execute_task_worker(self, task_id: str) -> None:
        initial = self._tasks.get(task_id)
        if initial is None or initial.status in TERMINAL_TASK_STATUSES:
            return
        ws_path = initial.workspace_canonical_path
        ws_lock = self._workspace_lock(ws_path)
        registered_gk = False

        try:
            async with ws_lock:
                current = self._tasks.get(task_id)
                if (
                    current is None
                    or current.status in TERMINAL_TASK_STATUSES
                    or task_id in self._cancel_requested
                ):
                    return

                async with self._semaphore:
                    current = self._tasks.get(task_id)
                    if (
                        current is None
                        or current.status in TERMINAL_TASK_STATUSES
                        or task_id in self._cancel_requested
                    ):
                        return

                    self._register_gatekeeper_workspace(task_id, ws_path)
                    registered_gk = True
                    running = await self._transition(
                        task_id,
                        "running",
                        reason="worker_started",
                        last_log=f"Ejecutando en {current.provider} ({current.workspace_name})...",
                    )
                    prior_conv = await self._lookup_prior_conversation(ws_path, running.provider)
                    runner = self._runner_factory(
                        running.provider,
                        ws_path,
                        task_id,
                        running.model,
                    )
                    request = CodingTaskRequest(
                        correlation_id=running.correlation_id,
                        workspace_path=ws_path,
                        prompt=running.objective,
                        model=running.model or _PROVIDER_DEFAULT_TIER2_MODELS[running.provider],
                        conversation_id=prior_conv,
                    )
                    collected_deltas: list[str] = []
                    final_response = ""
                    latest_conv = prior_conv

                    try:
                        async with asyncio.timeout(self._settings.task_total_timeout_seconds):
                            async with _closing_async_iter(runner.execute(request)) as stream:
                                async for progress in stream:
                                    if (
                                        progress.conversation_id
                                        and progress.conversation_id != "unknown"
                                    ):
                                        latest_conv = progress.conversation_id
                                        await self._save_conversation_pointer(
                                            ws_path,
                                            running.provider,
                                            progress.conversation_id,
                                        )

                                    if progress.kind == "delta" and progress.text_delta:
                                        redacted_delta = self._redact(progress.text_delta)
                                        collected_deltas.append(redacted_delta)
                                        snippet = " ".join(redacted_delta.split())[
                                            :MAX_TASK_LOG_CHARS
                                        ]
                                        if snippet:
                                            await self._record_progress(
                                                task_id=task_id,
                                                step_type=progress.step_type or "delta",
                                                last_log=snippet,
                                            )
                                    elif progress.kind == "milestone":
                                        snippet = (
                                            f"Etapa: {progress.step_type}"[:MAX_TASK_LOG_CHARS]
                                        )
                                        await self._record_progress(
                                            task_id=task_id,
                                            step_type=progress.step_type or "milestone",
                                            last_log=snippet,
                                        )
                                    elif progress.kind == "completed" and progress.final_response:
                                        final_response = self._redact(progress.final_response)
                    except TimeoutError:
                        await self._fail_task(
                            task_id,
                            error_code="RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH",
                            message=(
                                f"Task exceeded total execution timeout "
                                f"({self._settings.task_total_timeout_seconds:.0f}s)."
                            ),
                        )
                        return

                    summary = final_response or "".join(collected_deltas).strip()
                    if not summary:
                        summary = f"Tarea {task_id} completada en {running.workspace_name}."

                    if task_id in self._cancel_requested:
                        return
                    current_now = self._tasks.get(task_id)
                    if current_now is None or current_now.status in TERMINAL_TASK_STATUSES:
                        return

                    try:
                        done = await self._transition(
                            task_id,
                            "completed",
                            reason="worker_completed",
                            last_log=summary[:MAX_TASK_LOG_CHARS],
                            result_summary=summary,
                            conversation_id=latest_conv,
                        )
                    except (TaskNotFoundError, TaskStateError):
                        return
                    await self._event_bus.publish(
                        ChatStreamEvent(
                            correlation_id=done.correlation_id,
                            delta=f"[Tarea {done.task_id} · {done.provider}] {summary}",
                            source="tier2_runner",
                            step_type="result",
                        )
                    )
                    await self._event_bus.publish(
                        VoiceMilestoneEvent(
                            correlation_id=done.correlation_id,
                            summary_text=_safe_milestone_text(
                                f"Terminó la tarea en {done.workspace_name}.",
                                fallback="Tarea en segundo plano completada.",
                                extra_secrets=self._extra_secrets(),
                            ),
                            priority="normal",
                        )
                    )

        except asyncio.CancelledError:
            current_now = self._tasks.get(task_id)
            if current_now is not None and current_now.status not in TERMINAL_TASK_STATUSES:
                with contextlib.suppress(Exception):
                    await self._transition(
                        task_id,
                        "cancelled",
                        reason="worker_cancelled",
                        last_log="Cancelada.",
                        clear_pending_audit=True,
                    )
            raise
        except NovaDomainError as exc:
            await self._fail_task(task_id, error_code=exc.code, message=exc.sanitized_message)
        except Exception as exc:
            await self._fail_task(
                task_id,
                error_code="RUNNER_EXECUTION_FAILED",
                message=str(exc) or "Unexpected task runner failure.",
            )
        finally:
            if registered_gk:
                self._unregister_gatekeeper_workspace(task_id)
            self._cleanup_idle_workspace_lock(ws_path)

    async def _record_progress(self, *, task_id: str, step_type: str, last_log: str) -> None:
        current = self._tasks.get(task_id)
        if current is None or current.status in TERMINAL_TASK_STATUSES:
            return
        safe_log = self._redact(last_log)[:MAX_TASK_LOG_CHARS]
        if not safe_log:
            return
        updated = current.model_copy(update={"last_log": safe_log, "updated_at": self._clock()})
        self._record_ram_task(updated)
        await self._event_bus.publish(
            TaskProgressEvent(
                task_id=task_id,
                correlation_id=updated.correlation_id,
                step_type=step_type,
                last_log=safe_log,
                provider=updated.provider,
                workspace_canonical_path=updated.workspace_canonical_path,
            )
        )

    async def _fail_task(self, task_id: str, *, error_code: str, message: str) -> None:
        current = self._tasks.get(task_id)
        if current is None or current.status in TERMINAL_TASK_STATUSES:
            return
        redacted_msg = self._redact(message)[:MAX_TASK_ERROR_CHARS]
        try:
            failed, pending_audit, transitioned = await self._transition_to_terminal(
                task_id,
                "failed",
                reason=f"worker_failed:{error_code}",
                last_log=redacted_msg[:MAX_TASK_LOG_CHARS],
                error_code=error_code,
                error_message=redacted_msg,
            )
        except TaskNotFoundError:
            return
        if not transitioned:
            return
        await self._abort_pending_approval(pending_audit, resolved_by=f"task_failed:{error_code}")
        await self._event_bus.publish(
            RunnerFailureEvent(
                correlation_id=failed.correlation_id,
                error_code=error_code,
                sanitized_message=redacted_msg,
                recoverable=True,
            )
        )
        await self._event_bus.publish(
            VoiceMilestoneEvent(
                correlation_id=failed.correlation_id,
                summary_text=_safe_milestone_text(
                    f"Falló la tarea en {failed.workspace_name}. Revisa el detalle en el chat.",
                    fallback="Falló una tarea en segundo plano.",
                    extra_secrets=self._extra_secrets(),
                ),
                priority="interrupt",
            )
        )
