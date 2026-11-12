#!/usr/bin/env python3
"""Antigravity PreToolUse hook client for Nova SecurityGatekeeper (NOVA-002 OP-5)."""

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
    payload: dict[str, str] = {"decision": decision}
    if reason:
        payload["reason"] = reason
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    socket_path = os.environ.get("NOVA_GATEKEEPER_SOCKET", "").strip()
    if not socket_path:
        # Standalone agy session outside Nova: immediate zero-friction pass-through
        _emit("allow")
        return 0

    try:
        raw_stdin = sys.stdin.read()
        hook_input: dict[str, Any] = json.loads(raw_stdin) if raw_stdin.strip() else {}
    except json.JSONDecodeError:
        _emit("deny", "GATEKEEPER_INVALID_HOOK_INPUT: Malformed PreToolUse JSON on stdin.")
        return 0

    tool_call = hook_input.get("toolCall")
    tool_args = tool_call.get("args") if isinstance(tool_call, dict) else {}
    command_line = (
        tool_args.get("CommandLine", "") if isinstance(tool_args, dict) else ""
    )
    workspace_paths = hook_input.get("workspacePaths")
    cwd = (
        str(workspace_paths[0])
        if isinstance(workspace_paths, list) and workspace_paths
        else os.getcwd()
    )

    ipc_request = {
        "protocol_version": "1.0",
        "token": os.environ.get("NOVA_GATEKEEPER_TOKEN", ""),
        "correlation_id": f"corr-{uuid.uuid4()}",
        "source": "tier2_agy",
        "tool_name": "run_command",
        "command": str(command_line),
        "cwd": cwd,
    }

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(IPC_TIMEOUT_SECONDS)
            client.connect(socket_path)
            client.sendall((json.dumps(ipc_request, ensure_ascii=False) + "\n").encode())
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

    raw_resp = b"".join(chunks).split(b"\n", 1)[0].strip()
    try:
        parsed_resp: dict[str, Any] = json.loads(raw_resp.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _emit("deny", "GATEKEEPER_INVALID_IPC_RESPONSE: Malformed decision (fail-closed).")
        return 0

    if parsed_resp.get("allowed") is True:
        _emit("allow")
    else:
        reason = str(parsed_resp.get("reason") or "Blocked by Nova SecurityGatekeeper.")
        _emit("deny", reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
