#!/usr/bin/env python3
"""Claude Code PreToolUse hook client for Nova SecurityGatekeeper (NOVA-017)."""

from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from typing import Any

IPC_TIMEOUT_SECONDS = 280.0
MAX_READ_BYTES = 65536


def _emit(decision: str, reason: str | None = None) -> None:
    specific: dict[str, str] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
    }
    if reason:
        specific["permissionDecisionReason"] = reason
    sys.stdout.write(json.dumps({"hookSpecificOutput": specific}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _extract_cwd(hook_input: dict[str, Any], tool_input: dict[str, Any]) -> str:
    for container in (tool_input, hook_input):
        for key in ("cwd", "Cwd", "workingDirectory"):
            raw = container.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw
    return os.getcwd()


def main() -> int:
    socket_path = os.environ.get("NOVA_GATEKEEPER_SOCKET", "").strip()
    if not socket_path:
        # Standalone Claude Code session outside Nova: immediate pass-through
        _emit("allow")
        return 0

    try:
        raw_stdin = sys.stdin.read()
        parsed_stdin: Any = json.loads(raw_stdin) if raw_stdin.strip() else {}
    except json.JSONDecodeError:
        _emit("deny", "GATEKEEPER_INVALID_HOOK_INPUT: Malformed PreToolUse JSON on stdin.")
        return 0

    if not isinstance(parsed_stdin, dict):
        _emit("deny", "GATEKEEPER_INVALID_HOOK_INPUT: Expected JSON object on stdin.")
        return 0

    tool_name = str(parsed_stdin.get("tool_name") or "Bash")
    if tool_name not in ("Bash", "bash", "shell", "run_command"):
        _emit("allow")
        return 0

    raw_tool_input = parsed_stdin.get("tool_input")
    tool_input: dict[str, Any] = raw_tool_input if isinstance(raw_tool_input, dict) else {}
    command_line = str(tool_input.get("command") or "")
    effective_cwd = _extract_cwd(parsed_stdin, tool_input)

    source = os.environ.get("NOVA_GATEKEEPER_SOURCE", "tier2_claude").strip() or "tier2_claude"
    task_id = os.environ.get("NOVA_TASK_ID", "").strip() or None
    ipc_request: dict[str, Any] = {
        "protocol_version": "1.0",
        "token": os.environ.get("NOVA_GATEKEEPER_TOKEN", ""),
        "correlation_id": f"corr-{uuid.uuid4()}",
        "source": source,
        "tool_name": "Bash",
        "command": command_line,
        "cwd": effective_cwd,
    }
    if task_id:
        ipc_request["task_id"] = task_id

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(IPC_TIMEOUT_SECONDS)
            client.connect(socket_path)
            client.sendall((json.dumps(ipc_request, ensure_ascii=False) + "\n").encode("utf-8"))
            chunks: list[bytes] = []
            received = 0
            while received < MAX_READ_BYTES:
                data = client.recv(4096)
                if not data:
                    break
                chunks.append(data)
                received += len(data)
                if b"\n" in data:
                    break
    except OSError as exc:
        _emit("deny", f"GATEKEEPER_IPC_UNREACHABLE: {exc} (fail-closed).")
        return 0

    joined_bytes = b"".join(chunks)
    if b"\n" not in joined_bytes:
        _emit(
            "deny",
            "GATEKEEPER_INCOMPLETE_IPC_RESPONSE: Missing newline delimiter (fail-closed).",
        )
        return 0

    raw_resp = joined_bytes.split(b"\n", 1)[0].strip()
    try:
        parsed_any: Any = json.loads(raw_resp.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _emit("deny", "GATEKEEPER_INVALID_IPC_RESPONSE: Malformed decision (fail-closed).")
        return 0

    if not isinstance(parsed_any, dict):
        _emit("deny", "GATEKEEPER_INVALID_IPC_RESPONSE: Expected JSON object (fail-closed).")
        return 0

    if parsed_any.get("allowed") is True:
        _emit("allow")
    else:
        reason = str(parsed_any.get("reason") or "Blocked by Nova SecurityGatekeeper.")
        _emit("deny", reason)
    return 0


def _entrypoint() -> int:
    try:
        return main()
    except Exception:
        _emit(
            "deny",
            "GATEKEEPER_HOOK_INTERNAL_ERROR: Unexpected hook failure (fail-closed).",
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
