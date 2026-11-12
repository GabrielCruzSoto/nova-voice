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


def _extract_effective_cwd(
    hook_input: dict[str, Any],
    tool_args: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Resolve effective execution directory preserving exact POSIX characters (F2, F3)."""
    for key in ("Cwd", "cwd", "workingDirectory", "DirectoryPath"):
        if key in tool_args and tool_args[key] is not None:
            raw = tool_args[key]
            if not isinstance(raw, str) or not raw.strip():
                return (
                    None,
                    f"GATEKEEPER_INVALID_CWD: Explicit tool argument '{key}' is empty or invalid.",
                )
            return raw, None

    for key in ("cwd", "Cwd"):
        if key in hook_input and hook_input[key] is not None:
            raw_top = hook_input[key]
            if not isinstance(raw_top, str) or not raw_top.strip():
                return (
                    None,
                    f"GATEKEEPER_INVALID_CWD: Hook field '{key}' is empty or invalid.",
                )
            return raw_top, None

    workspace_paths = hook_input.get("workspacePaths")
    if isinstance(workspace_paths, list) and workspace_paths:
        first_ws = workspace_paths[0]
        if isinstance(first_ws, str) and first_ws.strip():
            return first_ws, None
        return None, "GATEKEEPER_INVALID_CWD: workspacePaths[0] is empty or invalid."

    return None, "GATEKEEPER_MISSING_CWD: Unable to determine target working directory."


def main() -> int:
    socket_path = os.environ.get("NOVA_GATEKEEPER_SOCKET", "").strip()
    if not socket_path:
        # Standalone agy session outside Nova: immediate zero-friction pass-through
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

    hook_input: dict[str, Any] = parsed_stdin
    tool_call = hook_input.get("toolCall")
    raw_args = tool_call.get("args") if isinstance(tool_call, dict) else None
    tool_args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
    command_line = tool_args.get("CommandLine", "")

    effective_cwd, cwd_err = _extract_effective_cwd(hook_input, tool_args)
    if effective_cwd is None:
        _emit("deny", cwd_err or "GATEKEEPER_MISSING_CWD: Missing working directory.")
        return 0

    ipc_request = {
        "protocol_version": "1.0",
        "token": os.environ.get("NOVA_GATEKEEPER_TOKEN", ""),
        "correlation_id": f"corr-{uuid.uuid4()}",
        "source": "tier2_agy",
        "tool_name": "run_command",
        "command": str(command_line),
        "cwd": effective_cwd,
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

    joined_bytes = b"".join(chunks)
    if b"\n" not in joined_bytes:
        _emit(
            "deny",
            "GATEKEEPER_INCOMPLETE_IPC_RESPONSE: Missing newline frame delimiter (fail-closed).",
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

    parsed_resp: dict[str, Any] = parsed_any
    if parsed_resp.get("allowed") is True:
        _emit("allow")
    else:
        reason = str(parsed_resp.get("reason") or "Blocked by Nova SecurityGatekeeper.")
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
