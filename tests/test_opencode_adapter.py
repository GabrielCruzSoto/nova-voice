"""TDD tests for NOVA-017 T3: OpenCodeAdapter (Tier 1 & Tier 2) and Gatekeeper plugin injection."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nova.adapters.opencode_adapter import OpenCodeAdapter, OpenCodeConfig
from nova.domain.models import CodingTaskRequest


def _write_fake_opencode_cli(script_path: Path) -> None:
    script_path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
assert args[0] == "run"
assert "--format" in args and args[args.index("--format") + 1] == "json"

if os.environ.get("NOVA_GATEKEEPER_SOCKET"):
    cfg_raw = os.environ.get("OPENCODE_CONFIG_CONTENT", "")
    assert cfg_raw, "OPENCODE_CONFIG_CONTENT must be injected when Gatekeeper is active"
    cfg = json.loads(cfg_raw)
    assert "plugin" in cfg and len(cfg["plugin"]) >= 1

sess_id = args[args.index("--session") + 1] if "--session" in args else "ses_oc_new_77"
ev1 = {"type": "text", "sessionID": sess_id, "part": {"text": "Hola desde OpenCode"}}
ev2 = {"type": "step_finish", "sessionID": sess_id, "part": {"reason": "stop"}}
print(json.dumps(ev1), flush=True)
print(json.dumps(ev2), flush=True)
""",
        encoding="utf-8",
    )
    os.chmod(script_path, 0o755)


@pytest.mark.asyncio
async def test_opencode_adapter_tier1_and_tier2_stream_and_session(tmp_path: Path) -> None:
    fake_oc = tmp_path / "fake_opencode.py"
    _write_fake_opencode_cli(fake_oc)

    adapter = OpenCodeAdapter(
        OpenCodeConfig(
            executable=sys.executable,
            extra_leading_args=(str(fake_oc),),
            model="anthropic/claude-haiku-4-5",
            workspace_path=str(tmp_path),
            gatekeeper_ipc_env={
                "NOVA_GATEKEEPER_SOCKET": str(tmp_path / "gk.sock"),
                "NOVA_GATEKEEPER_TOKEN": "tok-oc",
            },
        )
    )

    chunks = [delta async for delta in adapter.stream_prompt("Explica el repo")]
    assert "".join(chunks) == "Hola desde OpenCode"
    assert adapter.active_session_id == "ses_oc_new_77"

    events = [
        ev
        async for ev in adapter.execute(
            CodingTaskRequest(
                correlation_id="corr-oc-t2",
                workspace_path=str(tmp_path),
                prompt="Implementa endpoint",
                model="anthropic/claude-sonnet-4-5",
                conversation_id="ses_oc_existing_99",
            )
        )
    ]
    assert any(ev.kind == "init" and ev.conversation_id == "ses_oc_existing_99" for ev in events)
    assert any(ev.kind == "delta" and "Hola desde OpenCode" in ev.text_delta for ev in events)
    assert any(
        ev.kind == "completed" and ev.conversation_id == "ses_oc_existing_99" for ev in events
    )
