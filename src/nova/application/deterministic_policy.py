"""Layer 1 pure deterministic shell command policy (<5 ms, fail-closed precedence)."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from nova.domain.models import Layer1Decision

_PRIV_PREFIX = r"(?:(?:sudo|doas|pkexec|env)(?:\s+-[^\s;&|]+)*\s+)*"

_HARD_DENY_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "CATASTROPHIC_RM_ROOT_OR_HOME",
        re.compile(
            rf"(?:^|[;&|]\s*){_PRIV_PREFIX}\brm\b"
            r"(?=[^\n;&|]*(?:\s-(?:[A-Za-z]*[rf][A-Za-z]*)|--recursive|--force))"
            r"[^\n;&|]*\s+['\"]?(?:/|/\*|~/?|\$HOME/?|\$\{HOME\}/?|\.\./?)['\"]?(?:\s|$|[;&|])"
        ),
        "Recursive or forced deletion targeting host root, home, or parent directory.",
    ),
    (
        "CATASTROPHIC_FILESYSTEM_FORMAT",
        re.compile(rf"(?:^|[;&|]\s*){_PRIV_PREFIX}\bmkfs(?:\.[A-Za-z0-9_-]+)?\b"),
        "Filesystem formatting command targeting block storage.",
    ),
    (
        "CATASTROPHIC_RAW_BLOCK_WRITE",
        re.compile(
            rf"(?:^|[;&|]\s*){_PRIV_PREFIX}"
            r"(?:\bdd\b[^\n;&|]*\bof=/dev/|\bshred\b[^\n;&|]*/dev/)"
        ),
        "Direct destructive write or shred targeting a raw /dev device.",
    ),
    (
        "CATASTROPHIC_CHMOD_ROOT",
        re.compile(
            rf"(?:^|[;&|]\s*){_PRIV_PREFIX}\b(?:chmod|chown)\b"
            r"(?=[^\n;&|]*(?:-R|--recursive))(?=[^\n;&|]*\b777\b)"
            r"[^\n;&|]*\s+['\"]?/(?:['\"]|\s|$|[;&|])"
        ),
        "Recursive world-writable permission change targeting filesystem root.",
    ),
    (
        "CATASTROPHIC_FORK_BOMB",
        re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
        "Shell fork bomb pattern detected.",
    ),
)

_SENSITIVE_BINARIES: frozenset[str] = frozenset(
    {
        "sudo",
        "su",
        "pkexec",
        "doas",
        "curl",
        "wget",
        "ssh",
        "scp",
        "rsync",
        "nc",
        "ncat",
        "pip",
        "pip3",
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "chmod",
        "chown",
        "kill",
        "pkill",
        "systemctl",
        "service",
        "docker",
        "podman",
        "env",
        "printenv",
        "export",
    }
)

_SIMPLE_ALLOWLIST_BINARIES: frozenset[str] = frozenset(
    {"pwd", "ls", "stat", "wc", "head", "tail", "cat", "pytest", "mypy", "ruff"}
)
_SAFE_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {"status", "diff", "log", "show", "rev-parse"}
)
_UNSAFE_GIT_GLOBAL_FLAGS: frozenset[str] = frozenset(
    {"-c", "--config-env", "--exec-path", "--git-dir", "--work-tree"}
)
_SAFE_AIGILE_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("scan",),
    ("harness", "check"),
    ("harness", "status"),
    ("doctor",),
    ("session", "status"),
    ("receipt", "verify"),
    ("--version",),
    ("help",),
)


def _has_shell_control_or_substitution(command: str) -> bool:
    """Detect unquoted shell operators, globs, brace expansion, and any active $-expansion."""
    in_single = False
    in_double = False
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
            continue
        if in_single:
            if ch == "'":
                in_single = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_double:
            if ch == '"':
                in_double = False
            elif ch in {"`", "$"}:
                return True
            continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch in {";", "|", "&", "<", ">", "(", ")", "`", "$", "{", "}", "*", "?", "["}:
            return True
    return False


def _is_within_workspace(candidate: Path, workspace_root: Path) -> bool:
    resolved = Path(os.path.realpath(candidate))
    return resolved == workspace_root or resolved.is_relative_to(workspace_root)


def _extract_candidate_paths(tokens: list[str]) -> list[str]:
    """Extract all non-flag arguments plus paths embedded in flags or key=value pairs."""
    candidates: list[str] = []
    for token in tokens[1:]:
        if token.startswith("-"):
            if "=" in token:
                _, rhs = token.split("=", 1)
                if rhs:
                    candidates.append(rhs)
                    if "=" in rhs:
                        candidates.append(rhs.split("=", 1)[1])
            elif len(token) > 2 and token[1] != "-":
                attached = token[2:]
                if attached.startswith(("/", "~", ".")) or "/" in attached:
                    candidates.append(attached)
            continue

        candidates.append(token)
        if "=" in token:
            _, rhs = token.split("=", 1)
            if rhs:
                candidates.append(rhs)
    return candidates


def _check_paths_confined(
    tokens: list[str],
    resolved_cwd: Path,
    workspace_root: Path,
) -> Layer1Decision | None:
    for raw_path in _extract_candidate_paths(tokens):
        expanded = os.path.expanduser(raw_path)
        candidate = Path(expanded) if os.path.isabs(expanded) else resolved_cwd / expanded
        if not _is_within_workspace(candidate, workspace_root):
            return Layer1Decision(
                verdict="escalate",
                rule_id="OUTSIDE_WORKSPACE_PATH",
                risk_hint="high",
                reason=f"Command references a path outside active workspace: '{raw_path}'.",
            )
    return None


def _evaluate_git_tokens(args: list[str]) -> Layer1Decision:
    idx = 0
    while idx < len(args) and args[idx].startswith("-"):
        flag = args[idx].split("=", 1)[0]
        if flag in _UNSAFE_GIT_GLOBAL_FLAGS or flag.startswith("-c"):
            return Layer1Decision(
                verdict="escalate",
                rule_id="GIT_MUTATING_OR_UNSAFE_FLAG",
                risk_hint="high",
                reason=f"Git global flag '{flag}' requires auditor review.",
            )
        idx += 1

    if idx >= len(args):
        return Layer1Decision(
            verdict="escalate",
            rule_id="GIT_MUTATING_OR_UNSAFE",
            risk_hint="medium",
            reason="Bare git invocation without subcommand requires review.",
        )

    subcmd = args[idx]
    rest = args[idx + 1 :]
    if subcmd in _SAFE_GIT_SUBCOMMANDS:
        return Layer1Decision(
            verdict="allow",
            rule_id="ALLOWLIST_GIT_READONLY",
            risk_hint="low",
            reason=f"Read-only git '{subcmd}' inside active workspace.",
        )
    if subcmd == "branch" and rest == ["--show-current"]:
        return Layer1Decision(
            verdict="allow",
            rule_id="ALLOWLIST_GIT_BRANCH_CURRENT",
            risk_hint="low",
            reason="Read-only git branch inspection inside active workspace.",
        )
    return Layer1Decision(
        verdict="escalate",
        rule_id="GIT_MUTATING_OR_UNSAFE",
        risk_hint="high" if subcmd in {"push", "reset", "clean"} else "medium",
        reason=f"Git subcommand '{subcmd}' mutates repository or remote state.",
    )


def _evaluate_aigile_tokens(args: list[str]) -> Layer1Decision:
    for prefix in _SAFE_AIGILE_PREFIXES:
        if tuple(args[: len(prefix)]) == prefix:
            if prefix == ("scan",) and "--install-hook" in args:
                break
            return Layer1Decision(
                verdict="allow",
                rule_id="ALLOWLIST_AIGILE_READONLY",
                risk_hint="low",
                reason="Safe local aigile verification command.",
            )
    return Layer1Decision(
        verdict="escalate",
        rule_id="AIGILE_MUTATING_SUBCOMMAND",
        risk_hint="medium",
        reason="Mutating or unrecognized './aigile' subcommand requires approval.",
    )


class DeterministicCommandPolicy:
    """Layer 1 deterministic pre-execution filter (OP-2)."""

    def evaluate(
        self,
        command: str,
        cwd: str,
        workspace_canonical_path: str,
    ) -> Layer1Decision:
        """Evaluate command under strict fail-closed precedence (<5 ms p95)."""
        stripped = command.strip()
        if not stripped:
            return Layer1Decision(
                verdict="escalate",
                rule_id="EMPTY_COMMAND",
                risk_hint="medium",
                reason="Empty command string cannot be auto-approved.",
            )

        for rule_id, pattern, reason in _HARD_DENY_PATTERNS:
            if pattern.search(stripped) is not None:
                return Layer1Decision(
                    verdict="hard_deny",
                    rule_id=rule_id,
                    risk_hint="critical",
                    reason=reason,
                )

        if "\n" in command or "\r" in command or "\x00" in command:
            return Layer1Decision(
                verdict="escalate",
                rule_id="CONTROL_CHARACTER_DETECTED",
                risk_hint="high",
                reason="Multiline or null-byte command requires auditor inspection.",
            )

        workspace_root = Path(os.path.realpath(workspace_canonical_path))
        resolved_cwd = Path(os.path.realpath(cwd))
        if not _is_within_workspace(resolved_cwd, workspace_root):
            return Layer1Decision(
                verdict="escalate",
                rule_id="OUTSIDE_WORKSPACE_CWD",
                risk_hint="high",
                reason="Command working directory lies outside the active workspace.",
            )

        if _has_shell_control_or_substitution(stripped):
            return Layer1Decision(
                verdict="escalate",
                rule_id="COMPOUND_OR_SUBSTITUTION_OPERATOR",
                risk_hint="high",
                reason="Shell chaining, expansion, redirection, or substitution requires audit.",
            )

        try:
            tokens = shlex.split(stripped, posix=True)
        except ValueError as exc:
            return Layer1Decision(
                verdict="escalate",
                rule_id="SHLEX_PARSE_ERROR",
                risk_hint="high",
                reason=f"Unbalanced quotes or shell syntax error: {exc}",
            )

        if not tokens:
            return Layer1Decision(
                verdict="escalate",
                rule_id="EMPTY_COMMAND",
                risk_hint="medium",
                reason="No executable token found.",
            )

        path_escape = _check_paths_confined(tokens, resolved_cwd, workspace_root)
        if path_escape is not None:
            return path_escape

        return self._evaluate_executable(tokens, resolved_cwd, workspace_root)

    def _evaluate_executable(
        self,
        tokens: list[str],
        resolved_cwd: Path,
        workspace_root: Path,
    ) -> Layer1Decision:
        head = tokens[0]
        args = tokens[1:]
        exe_name = Path(head).name

        if exe_name in _SENSITIVE_BINARIES:
            return Layer1Decision(
                verdict="escalate",
                rule_id=f"SENSITIVE_BINARY_{exe_name.upper()}",
                risk_hint="high",
                reason=f"Executable '{exe_name}' modifies privileges, network, or permissions.",
            )

        if head == "./aigile":
            if not _is_within_workspace(resolved_cwd / head, workspace_root):
                return Layer1Decision(
                    verdict="escalate",
                    rule_id="OUTSIDE_WORKSPACE_PATH",
                    risk_hint="high",
                    reason="'./aigile' resolves outside active workspace.",
                )
            return _evaluate_aigile_tokens(args)

        if "/" in head or head.startswith((".", "~")):
            venv_bin = workspace_root / ".venv" / "bin"
            resolved_exe = Path(os.path.realpath(resolved_cwd / os.path.expanduser(head)))
            if resolved_exe.parent != venv_bin:
                return Layer1Decision(
                    verdict="escalate",
                    rule_id="UNTRUSTED_RELATIVE_EXECUTABLE",
                    risk_hint="medium",
                    reason=f"Executable path '{head}' is outside workspace .venv/bin.",
                )

        if exe_name == "git":
            return _evaluate_git_tokens(args)

        if exe_name in _SIMPLE_ALLOWLIST_BINARIES:
            return Layer1Decision(
                verdict="allow",
                rule_id=f"ALLOWLIST_{exe_name.upper()}",
                risk_hint="low",
                reason=f"Allowlisted local tool '{exe_name}' confined to workspace.",
            )

        return Layer1Decision(
            verdict="escalate",
            rule_id="UNLISTED_COMMAND",
            risk_hint="medium",
            reason=f"Executable '{exe_name}' is not in Layer 1 allowlist.",
        )
