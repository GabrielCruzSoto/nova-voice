"""Deterministic tests for T3: AntigravityCLIAdapter over CodingRunnerPort (AC-3, AC-6, OP-3)."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from nova.adapters.antigravity_adapter import AntigravityCLIAdapter, AntigravityRunnerConfig
from nova.domain.errors import CodingRunnerError
from nova.domain.models import CodingTaskRequest, RunnerProgressEvent
from nova.domain.ports import CodingRunnerPort


def _write_fake_agy(script_path: Path, body: str) -> list[str]:
    script_path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, "-u", str(script_path)]


async def test_ac3_executes_stream_json_captures_conversation_id_and_resumes_session(
    tmp_path: Path,
) -> None:
    fake_agy = tmp_path / "fake_agy.py"
    argv = _write_fake_agy(
        fake_agy,
        """
        import json, sys

        args = sys.argv[1:]
        assert "--output-format" in args and "stream-json" in args
        assert "--mode" in args and "accept-edits" in args
        assert "--disable-slash-commands" in args

        conv_id = "conv-first-001"
        if "--conversation" in args:
            idx = args.index("--conversation")
            conv_id = args[idx + 1]

        events = [
            {
                "event": "init",
                "conversation_id": conv_id,
                "init": {"model": "claude-opus-5-5-high", "cwd": "."},
            },
            {
                "event": "unknown_future_event",
                "payload": {"ignored": True},
            },
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conv_id,
                    "step_index": 1,
                    "state": "ACTIVE",
                    "step_type": "agent_response",
                    "text_delta": "Refactorizando arquitectura...",
                },
            },
            {
                "event": "result",
                "result": {
                    "conversation_id": conv_id,
                    "status": "SUCCESS",
                    "response": "Refactor completado.",
                    "duration_seconds": 1.25,
                },
            },
        ]
        for ev in events:
            sys.stdout.write(json.dumps(ev) + "\\n")
        sys.stdout.flush()
        """,
    )

    adapter: CodingRunnerPort = AntigravityCLIAdapter(
        AntigravityRunnerConfig(
            executable=argv[0],
            extra_leading_args=tuple(argv[1:]),
        )
    )

    events_turn1: list[RunnerProgressEvent] = [
        ev
        async for ev in adapter.execute(
            CodingTaskRequest(
                correlation_id="corr-t3-1",
                workspace_path=str(tmp_path),
                prompt="Implementa el modulo",
                model="claude-opus-5-5-high",
            )
        )
    ]

    assert events_turn1[0].kind == "init"
    assert events_turn1[0].conversation_id == "conv-first-001"
    assert any(ev.kind == "delta" and "Refactorizando" in ev.text_delta for ev in events_turn1)
    assert events_turn1[-1].kind == "completed"
    assert events_turn1[-1].final_response == "Refactor completado."

    # Turn 2 resumes the exact conversation_id
    events_turn2: list[RunnerProgressEvent] = [
        ev
        async for ev in adapter.execute(
            CodingTaskRequest(
                correlation_id="corr-t3-2",
                workspace_path=str(tmp_path),
                prompt="Agrega pruebas adicionales",
                model="gemini-4-carbon-high",
                conversation_id="conv-first-001",
            )
        )
    ]
    assert events_turn2[0].conversation_id == "conv-first-001"


@pytest.mark.parametrize(
    ("prompt", "model"),
    [
        ("   ", "claude-opus-5-5-high"),
        ("Implementa X", "unapproved-model-v0"),
    ],
)
async def test_op3_rejects_invalid_model_or_empty_prompt(
    tmp_path: Path,
    prompt: str,
    model: str,
) -> None:
    adapter = AntigravityCLIAdapter()
    with pytest.raises(CodingRunnerError) as exc_info:
        async for _ in adapter.execute(
            CodingTaskRequest(
                correlation_id="corr-inv",
                workspace_path=str(tmp_path),
                prompt=prompt,
                model=model,
            )
        ):
            pass
    assert exc_info.value.code == "RUNNER_INVALID_REQUEST"


async def test_op3_surfaces_runner_execution_failed_on_error_result(tmp_path: Path) -> None:
    fake_agy = tmp_path / "fake_agy_fail.py"
    argv = _write_fake_agy(
        fake_agy,
        """
        import json, sys
        sys.stdout.write(json.dumps({
            "event": "result",
            "result": {
                "conversation_id": "conv-err",
                "status": "ERROR",
                "response": "Compilation failed",
            }
        }) + "\\n")
        sys.stdout.flush()
        """,
    )
    adapter = AntigravityCLIAdapter(
        AntigravityRunnerConfig(executable=argv[0], extra_leading_args=tuple(argv[1:]))
    )

    with pytest.raises(CodingRunnerError) as exc_info:
        async for _ in adapter.execute(
            CodingTaskRequest(
                correlation_id="corr-fail",
                workspace_path=str(tmp_path),
                prompt="Rompe algo",
            )
        ):
            pass
    assert exc_info.value.code == "RUNNER_EXECUTION_FAILED"


async def test_ac6_terminates_hanging_agy_subprocess_on_timeout(tmp_path: Path) -> None:
    fake_agy = tmp_path / "fake_agy_hang.py"
    argv = _write_fake_agy(
        fake_agy,
        """
        import time
        time.sleep(30)
        """,
    )
    adapter = AntigravityCLIAdapter(
        AntigravityRunnerConfig(
            executable=argv[0],
            extra_leading_args=tuple(argv[1:]),
            timeout_seconds=0.25,
        )
    )

    with pytest.raises(CodingRunnerError) as exc_info:
        async for _ in adapter.execute(
            CodingTaskRequest(
                correlation_id="corr-timeout",
                workspace_path=str(tmp_path),
                prompt="Tarea infinita",
            )
        ):
            pass
    assert exc_info.value.code == "RUNNER_SUBPROCESS_TIMEOUT_OR_CRASH"
