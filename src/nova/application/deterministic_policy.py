"""Layer 1 pure deterministic shell command policy (<5 ms, fail-closed positive allowlist)."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from nova.domain.models import Layer1Decision

_BIN_PREFIX = r"(?:(?:/usr(?:/local)?)?/s?bin/)?"
_PRIV_PREFIX = (
    rf"(?:{_BIN_PREFIX}(?:sudo|doas|pkexec|env)"
    r"(?:\s+(?:-[ugUpC]\s+\S+|-[^\s;&|]+|[A-Za-z_][A-Za-z0-9_]*=\S+))*\s+)*"
)

_HARD_DENY_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "CATASTROPHIC_RM_ROOT_OR_HOME",
        re.compile(
            rf"(?:^|[;&|]\s*){_PRIV_PREFIX}{_BIN_PREFIX}\brm\b"
            r"(?=[^\n;&|]*(?:\s-(?:[A-Za-z]*[rf][A-Za-z]*)|--recursive|--force))"
            r"[^\n;&|]*\s+['\"]?(?:/+|/+\*|~/?|\$HOME/?|\$\{HOME\}/?|\.\./*)['\"]?(?:\s|$|[;&|])"
        ),
        "Recursive or forced deletion targeting host root, home, or parent directory.",
    ),
    (
        "CATASTROPHIC_FILESYSTEM_FORMAT",
        re.compile(rf"(?:^|[;&|]\s*){_PRIV_PREFIX}{_BIN_PREFIX}\bmkfs(?:\.[A-Za-z0-9_-]+)?\b"),
        "Filesystem formatting command targeting block storage.",
    ),
    (
        "CATASTROPHIC_RAW_BLOCK_WRITE",
        re.compile(
            rf"(?:^|[;&|]\s*){_PRIV_PREFIX}{_BIN_PREFIX}"
            r"(?:\bdd\b[^\n;&|]*\bof=['\"]?/dev/|\bshred\b[^\n;&|]*['\"]?/dev/)"
        ),
        "Direct destructive write or shred targeting a raw /dev device.",
    ),
    (
        "CATASTROPHIC_CHMOD_ROOT",
        re.compile(
            rf"(?:^|[;&|]\s*){_PRIV_PREFIX}{_BIN_PREFIX}\b(?:chmod|chown)\b"
            r"(?=[^\n;&|]*(?:-R|--recursive))(?=[^\n;&|]*\b777\b)"
            r"[^\n;&|]*\s+['\"]?/+['\"]?(?:\s|$|[;&|])"
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

_SAFE_GIT_SUBCOMMAND_FLAGS: dict[str, frozenset[str]] = {
    "status": frozenset(
        {
            "-s",
            "-b",
            "-sb",
            "-bs",
            "--short",
            "--branch",
            "--porcelain",
            "--porcelain=v1",
            "--porcelain=v2",
            "-u",
            "-uno",
            "-unormal",
            "-uall",
        }
    ),
    "diff": frozenset(
        {
            "--stat",
            "--numstat",
            "--shortstat",
            "--name-only",
            "--name-status",
            "--staged",
            "--cached",
            "--no-color",
            "--check",
        }
    ),
    "log": frozenset(
        {
            "--oneline",
            "--graph",
            "--decorate",
            "--stat",
            "--name-only",
            "--name-status",
            "--no-color",
            "--abbrev-commit",
        }
    ),
    "show": frozenset(
        {
            "--stat",
            "--name-only",
            "--name-status",
            "--oneline",
            "--no-color",
            "-s",
            "--no-patch",
        }
    ),
    "rev-parse": frozenset(
        {
            "--abbrev-ref",
            "--short",
            "--verify",
            "--show-toplevel",
            "--is-inside-work-tree",
        }
    ),
}

_ALLOWED_PYTEST_BOOL_FLAGS: frozenset[str] = frozenset(
    {
        "--collect-only",
        "--co",
        "--lf",
        "--ff",
        "--nf",
        "--no-header",
        "--no-summary",
        "--showlocals",
        "--strict-markers",
        "--strict-config",
    }
)
_ALLOWED_TB_MODES: frozenset[str] = frozenset(
    {"auto", "long", "short", "line", "native", "no"}
)
_ALLOWED_COLOR_MODES: frozenset[str] = frozenset({"yes", "no", "auto"})

_ALLOWED_MYPY_FLAGS: frozenset[str] = frozenset(
    {
        "--strict",
        "--pretty",
        "--no-pretty",
        "--show-error-codes",
        "--hide-error-codes",
        "--show-column-numbers",
        "--no-error-summary",
        "--ignore-missing-imports",
        "--check-untyped-defs",
        "--warn-unused-ignores",
        "--warn-return-any",
        "--no-incremental",
        "--version",
        "-V",
    }
)

_ALLOWED_RUFF_CHECK_BOOL_FLAGS: frozenset[str] = frozenset(
    {"-q", "--quiet", "-v", "--verbose", "--statistics", "--no-cache", "--preview"}
)
_ALLOWED_RUFF_OUTPUT_FORMATS: frozenset[str] = frozenset(
    {"full", "concise", "json", "github", "gitlab", "junit", "pylint", "rdjson", "azure", "sarif"}
)

_LS_SHORT_FLAG_RE = re.compile(r"^-[1aAhltrFdsS]+$")
_WC_SHORT_FLAG_RE = re.compile(r"^-[lwcLm]+$")
_CAT_SHORT_FLAG_RE = re.compile(r"^-[AbeEnstTuv]+$")
_HEAD_TAIL_NUM_FLAG_RE = re.compile(r"^-(?:[0-9]+|[nc]\+?[0-9]+)$")
_PYTEST_SHORT_CLUSTER_RE = re.compile(r"^-[qvxsl]+$")
_RUFF_RULE_SELECTOR_RE = re.compile(r"^[A-Z0-9,]+$")
_DIGITS_RE = re.compile(r"^\+?[0-9]+$")
_BARE_EXE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_VENV_EXE_RE = re.compile(r"^(?:\./)?\.venv/bin/[A-Za-z0-9._-]+$")
_UNSAFE_GIT_CONFIG_TOKENS: tuple[str, ...] = (
    "[include",
    "[filter",
    "[alias",
    "[pager",
    "[diff",
    "[credential",
    "[submodule",
    "[guitool",
    "[difftool",
    "[mergetool",
    "fsmonitor",
    "worktree",
    "external",
    "textconv",
    "command",
    "clean",
    "smudge",
    "process",
    "pager",
    "sshcommand",
    "gitproxy",
    "hookspath",
    "alternaterefscommand",
    "askpass",
    "helper",
    "uploadpack",
    "receivepack",
    "attributesfile",
    "excludesfile",
    "[gpg",
    "showsignature",
    "defaultkeycommand",
    "allowedsignersfile",
)


def _has_control_characters(command: str) -> bool:
    """Detect ASCII control characters (except horizontal tab) or DEL."""
    for ch in command:
        code = ord(ch)
        if (code < 32 and ch != "\t") or code == 127:
            return True
    return False


def _has_shell_control_or_substitution(command: str) -> bool:
    """Detect unquoted operators/comments, backslashes outside single quotes, and $-expansion."""
    in_single = False
    in_double = False
    for ch in command:
        if in_single:
            if ch == "'":
                in_single = False
            continue
        if ch == "\\":
            return True
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
        elif ch in {
            "#",
            ";",
            "|",
            "&",
            "<",
            ">",
            "(",
            ")",
            "`",
            "$",
            "{",
            "}",
            "*",
            "?",
            "[",
            "]",
        }:
            return True
    return False


def _is_within_workspace(candidate: Path, workspace_root: Path) -> bool:
    resolved = Path(os.path.realpath(candidate))
    return resolved == workspace_root or resolved.is_relative_to(workspace_root)


def _append_path_and_node_prefix(candidates: list[str], raw_val: str) -> None:
    if not raw_val:
        return
    candidates.append(raw_val)
    if "::" in raw_val:
        prefix = raw_val.split("::", 1)[0]
        if prefix:
            candidates.append(prefix)
    if ":~" in raw_val:
        tilde_tail = "~" + raw_val.split(":~", 1)[1]
        candidates.append(tilde_tail)


def _extract_candidate_paths(tokens: list[str]) -> list[str]:
    """Extract all non-flag arguments plus paths embedded in flags or after '--'."""
    candidates: list[str] = []
    after_double_dash = False

    for token in tokens[1:]:
        if token == "--" and not after_double_dash:
            after_double_dash = True
            continue

        if after_double_dash and not token.startswith("@"):
            _append_path_and_node_prefix(candidates, token)

        if token.startswith("-"):
            if "=" in token:
                _, rhs = token.split("=", 1)
                if rhs and not rhs.startswith("@"):
                    _append_path_and_node_prefix(candidates, rhs)
                    if "=" in rhs:
                        _append_path_and_node_prefix(candidates, rhs.split("=", 1)[1])
            elif len(token) > 2 and token[1] != "-":
                body = token[1:]
                for idx, ch in enumerate(body):
                    if ch in {"/", "~", "."}:
                        _append_path_and_node_prefix(candidates, body[idx:])
                        break
                for split_idx in range(2, len(token)):
                    suffix = token[split_idx:]
                    if suffix:
                        _append_path_and_node_prefix(candidates, suffix)
            continue

        if not token.startswith("@"):
            if not after_double_dash:
                _append_path_and_node_prefix(candidates, token)
            if "=" in token:
                _, rhs = token.split("=", 1)
                if rhs and not rhs.startswith("@"):
                    _append_path_and_node_prefix(candidates, rhs)
    return candidates


def _check_response_file_tokens(
    tokens: list[str],
    resolved_cwd: Path,
    workspace_root: Path,
) -> Layer1Decision | None:
    """Detect argparse @response-file arguments anywhere in argv (even after '--')."""
    for token in tokens[1:]:
        if token == "--":
            continue
        at_payload: str | None = None
        if token.startswith("@"):
            at_payload = token.lstrip("@")
        elif token.startswith("-") and "=" in token:
            _, rhs = token.split("=", 1)
            if rhs.startswith("@"):
                at_payload = rhs.lstrip("@")

        if at_payload is None:
            continue

        if at_payload:
            expanded = os.path.expanduser(at_payload)
            candidate = Path(expanded) if os.path.isabs(expanded) else resolved_cwd / expanded
            if (
                at_payload.startswith("~")
                or expanded.startswith("~")
                or not _is_within_workspace(candidate, workspace_root)
            ):
                return Layer1Decision(
                    verdict="escalate",
                    rule_id="OUTSIDE_WORKSPACE_PATH",
                    risk_hint="high",
                    reason=(
                        f"Command references a response file outside workspace: '@{at_payload}'."
                    ),
                )
        return Layer1Decision(
            verdict="escalate",
            rule_id="RESPONSE_FILE_EXPANSION",
            risk_hint="high",
            reason="CLI @response-file argument expansion requires auditor inspection.",
        )
    return None


def _check_paths_confined(
    tokens: list[str],
    resolved_cwd: Path,
    workspace_root: Path,
) -> Layer1Decision | None:
    for raw_path in _extract_candidate_paths(tokens):
        expanded = os.path.expanduser(raw_path)
        if raw_path.startswith("~") or expanded.startswith("~"):
            return Layer1Decision(
                verdict="escalate",
                rule_id="OUTSIDE_WORKSPACE_PATH",
                risk_hint="high",
                reason=f"Shell tilde path '{raw_path}' requires auditor review.",
            )
        candidate = Path(expanded) if os.path.isabs(expanded) else resolved_cwd / expanded
        if not _is_within_workspace(candidate, workspace_root):
            return Layer1Decision(
                verdict="escalate",
                rule_id="OUTSIDE_WORKSPACE_PATH",
                risk_hint="high",
                reason=f"Command references a path outside active workspace: '{raw_path}'.",
            )
    return None


def _validate_git_subcommand_flags(subcmd: str, rest: list[str]) -> bool:
    if any(":" in tok for tok in rest):
        return False
    allowed_flags = _SAFE_GIT_SUBCOMMAND_FLAGS[subcmd]
    idx = 0
    while idx < len(rest):
        tok = rest[idx]
        if tok == "--":
            return False
        if tok.startswith("-"):
            if tok in allowed_flags:
                idx += 1
                continue
            if subcmd == "diff" and re.fullmatch(r"(?:-U\d+|--unified=\d+)", tok):
                idx += 1
                continue
            if subcmd == "log":
                if re.fullmatch(r"(?:-\d+|-n\d+|--max-count=\d+)", tok):
                    idx += 1
                    continue
                if tok in {"-n", "--max-count"} and idx + 1 < len(rest):
                    if _DIGITS_RE.fullmatch(rest[idx + 1]):
                        idx += 2
                        continue
            return False
        idx += 1
    return True


def _verify_confined_git_repository(
    resolved_cwd: Path,
    workspace_root: Path,
) -> Layer1Decision | None:
    """Ensure Git resolves a confined .git inside workspace_root without external pointers."""
    outside_decision = Layer1Decision(
        verdict="escalate",
        rule_id="OUTSIDE_WORKSPACE_PATH",
        risk_hint="high",
        reason="Git repository metadata resolves outside the active workspace.",
    )

    cur = resolved_cwd
    found_dot_git: Path | None = None
    found_parent: Path | None = None
    while _is_within_workspace(cur, workspace_root):
        candidate_git = cur / ".git"
        if os.path.lexists(candidate_git):
            found_dot_git = candidate_git
            found_parent = cur
            break
        if cur == workspace_root:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent

    if found_dot_git is None or found_parent is None:
        return outside_decision

    real_dot_git = Path(os.path.realpath(found_dot_git))
    if not _is_within_workspace(real_dot_git, workspace_root):
        return outside_decision

    if real_dot_git.is_file():
        try:
            raw_ptr = real_dot_git.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            return outside_decision
        first_line = raw_ptr.splitlines()[0].strip() if raw_ptr.splitlines() else ""
        if not first_line.startswith("gitdir:"):
            return outside_decision
        ptr_target = first_line.removeprefix("gitdir:").strip()
        if not ptr_target:
            return outside_decision
        git_dir = Path(os.path.realpath(found_parent / ptr_target))
        if not git_dir.is_dir() or not _is_within_workspace(git_dir, workspace_root):
            return outside_decision
    elif real_dot_git.is_dir():
        git_dir = real_dot_git
    else:
        return outside_decision

    effective_dirs = [git_dir]
    commondir_entry = git_dir / "commondir"
    if os.path.lexists(commondir_entry):
        real_common_file = Path(os.path.realpath(commondir_entry))
        if not _is_within_workspace(real_common_file, workspace_root):
            return outside_decision
        if real_common_file.is_file():
            try:
                raw_common = real_common_file.read_text(encoding="utf-8", errors="replace")[:4096]
            except OSError:
                return outside_decision
            common_rel = raw_common.strip()
            if not common_rel:
                return outside_decision
            common_dir = Path(os.path.realpath(git_dir / common_rel))
            if not common_dir.is_dir() or not _is_within_workspace(common_dir, workspace_root):
                return outside_decision
            effective_dirs.append(common_dir)

    for gdir in effective_dirs:
        for entry_name in ("HEAD", "objects", "refs", "config", "packed-refs"):
            sub_entry = gdir / entry_name
            if os.path.lexists(sub_entry):
                if not _is_within_workspace(Path(os.path.realpath(sub_entry)), workspace_root):
                    return outside_decision
        if os.path.lexists(gdir / "objects" / "info" / "alternates") or os.path.lexists(
            gdir / "config.worktree"
        ):
            return outside_decision
        cfg_path = gdir / "config"
        if os.path.lexists(cfg_path):
            if not cfg_path.is_file():
                return outside_decision
            try:
                if cfg_path.stat().st_size > 16384:
                    return Layer1Decision(
                        verdict="escalate",
                        rule_id="GIT_MUTATING_OR_UNSAFE_FLAG",
                        risk_hint="high",
                        reason="Git config exceeds 16 KiB Layer 1 fast-path limit.",
                    )
                cfg_raw = cfg_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return outside_decision
            active_lines = [
                line.strip().lower()
                for line in cfg_raw.splitlines()
                if line.strip() and not line.lstrip().startswith(("#", ";"))
            ]
            cfg_lower = "\n".join(active_lines)
            if any(directive in cfg_lower for directive in _UNSAFE_GIT_CONFIG_TOKENS):
                return Layer1Decision(
                    verdict="escalate",
                    rule_id="GIT_MUTATING_OR_UNSAFE_FLAG",
                    risk_hint="high",
                    reason="Git config defines external includes, filters, hooks, or drivers.",
                )

    return None


def _evaluate_git_tokens(args: list[str]) -> Layer1Decision:
    if not args:
        return Layer1Decision(
            verdict="escalate",
            rule_id="GIT_MUTATING_OR_UNSAFE",
            risk_hint="medium",
            reason="Bare git invocation without subcommand requires review.",
        )

    if args[0].startswith("-"):
        return Layer1Decision(
            verdict="escalate",
            rule_id="GIT_MUTATING_OR_UNSAFE_FLAG",
            risk_hint="high",
            reason=f"Git global option '{args[0]}' is not permitted in Layer 1 fast-path.",
        )

    subcmd = args[0]
    rest = args[1:]
    if subcmd == "branch":
        if rest == ["--show-current"]:
            return Layer1Decision(
                verdict="allow",
                rule_id="ALLOWLIST_GIT_BRANCH_CURRENT",
                risk_hint="low",
                reason="Read-only git branch inspection inside active workspace.",
            )
        return Layer1Decision(
            verdict="escalate",
            rule_id="GIT_MUTATING_OR_UNSAFE",
            risk_hint="medium",
            reason="Only 'git branch --show-current' is auto-allowed in Layer 1.",
        )

    if subcmd in _SAFE_GIT_SUBCOMMAND_FLAGS:
        if not _validate_git_subcommand_flags(subcmd, rest):
            return Layer1Decision(
                verdict="escalate",
                rule_id="GIT_MUTATING_OR_UNSAFE_FLAG",
                risk_hint="high",
                reason=f"Unapproved git '{subcmd}' option requires auditor review.",
            )
        return Layer1Decision(
            verdict="allow",
            rule_id="ALLOWLIST_GIT_READONLY",
            risk_hint="low",
            reason=f"Read-only git '{subcmd}' inside active workspace.",
        )

    return Layer1Decision(
        verdict="escalate",
        rule_id="GIT_MUTATING_OR_UNSAFE",
        risk_hint="high" if subcmd in {"push", "reset", "clean"} else "medium",
        reason=f"Git subcommand '{subcmd}' mutates repository or remote state.",
    )


def _evaluate_aigile_tokens(args: list[str]) -> Layer1Decision:
    exact_safe: set[tuple[str, ...]] = {
        ("--version",),
        ("help",),
        ("doctor",),
        ("harness", "check"),
        ("harness", "status"),
        ("session", "status"),
        ("scan",),
    }
    t_args = tuple(args)
    if t_args in exact_safe:
        return Layer1Decision(
            verdict="allow",
            rule_id="ALLOWLIST_AIGILE_READONLY",
            risk_hint="low",
            reason="Safe local aigile verification command.",
        )

    if len(args) == 3 and args[:2] == ["scan", "--path"] and not args[2].startswith("-"):
        return Layer1Decision(
            verdict="allow",
            rule_id="ALLOWLIST_AIGILE_READONLY",
            risk_hint="low",
            reason="Safe local aigile path scan.",
        )

    if len(args) in {3, 4} and args[:2] == ["receipt", "verify"]:
        rem = args[2:]
        non_flags = [x for x in rem if not x.startswith("-")]
        flags = [x for x in rem if x.startswith("-")]
        if len(non_flags) == 1 and set(flags).issubset({"--verify-head"}):
            return Layer1Decision(
                verdict="allow",
                rule_id="ALLOWLIST_AIGILE_READONLY",
                risk_hint="low",
                reason="Safe local aigile receipt verification.",
            )

    return Layer1Decision(
        verdict="escalate",
        rule_id="AIGILE_MUTATING_SUBCOMMAND",
        risk_hint="medium",
        reason="Mutating or unrecognized './aigile' invocation requires approval.",
    )


def _validate_pytest_args(args: list[str]) -> bool:
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok == "--":
            return False
        if tok.startswith("-"):
            if _PYTEST_SHORT_CLUSTER_RE.fullmatch(tok) or tok in _ALLOWED_PYTEST_BOOL_FLAGS:
                idx += 1
                continue
            if tok in {"-k", "-m"} and idx + 1 < len(args) and not args[idx + 1].startswith("-"):
                idx += 2
                continue
            if tok in {"--maxfail", "--durations"} and idx + 1 < len(args):
                if _DIGITS_RE.fullmatch(args[idx + 1]):
                    idx += 2
                    continue
                return False
            if re.fullmatch(r"--(?:maxfail|durations)=\d+", tok):
                idx += 1
                continue
            if tok == "--tb" and idx + 1 < len(args) and args[idx + 1] in _ALLOWED_TB_MODES:
                idx += 2
                continue
            if tok.startswith("--tb=") and tok.split("=", 1)[1] in _ALLOWED_TB_MODES:
                idx += 1
                continue
            if (
                tok == "--color"
                and idx + 1 < len(args)
                and args[idx + 1] in _ALLOWED_COLOR_MODES
            ):
                idx += 2
                continue
            if tok.startswith("--color=") and tok.split("=", 1)[1] in _ALLOWED_COLOR_MODES:
                idx += 1
                continue
            return False
        idx += 1
    return True


def _validate_mypy_args(args: list[str]) -> bool:
    if args in (["--version"], ["-V"]):
        return True
    has_target = False
    for tok in args:
        if tok == "--":
            return False
        if tok.startswith("-"):
            if tok not in _ALLOWED_MYPY_FLAGS:
                return False
        else:
            has_target = True
    return has_target


def _validate_ruff_args(args: list[str]) -> bool:
    if args in (["--version"], ["-V"], ["version"]):
        return True
    if not args or args[0] != "check":
        return False
    idx = 1
    while idx < len(args):
        tok = args[idx]
        if tok == "--":
            return False
        if tok.startswith("-"):
            if tok in _ALLOWED_RUFF_CHECK_BOOL_FLAGS:
                idx += 1
                continue
            if tok == "--output-format" and idx + 1 < len(args):
                if args[idx + 1] in _ALLOWED_RUFF_OUTPUT_FORMATS:
                    idx += 2
                    continue
                return False
            if (
                tok.startswith("--output-format=")
                and tok.split("=", 1)[1] in _ALLOWED_RUFF_OUTPUT_FORMATS
            ):
                idx += 1
                continue
            if tok in {"--select", "--ignore"} and idx + 1 < len(args):
                if _RUFF_RULE_SELECTOR_RE.fullmatch(args[idx + 1]):
                    idx += 2
                    continue
                return False
            if tok.startswith(("--select=", "--ignore=")):
                if _RUFF_RULE_SELECTOR_RE.fullmatch(tok.split("=", 1)[1]):
                    idx += 1
                    continue
                return False
            return False
        idx += 1
    return True


def _validate_coreutil_args(exe_name: str, args: list[str]) -> bool:
    if exe_name == "pwd":
        return args in ([], ["-L"], ["-P"])

    positional = 0
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok == "--":
            return False
        if tok.startswith("-"):
            if exe_name == "ls":
                if _LS_SHORT_FLAG_RE.fullmatch(tok) or tok in {
                    "--color=never",
                    "--color=auto",
                    "--color=always",
                }:
                    idx += 1
                    continue
                return False
            if exe_name == "stat":
                if tok in {"-t", "--terse"}:
                    idx += 1
                    continue
                return False
            if exe_name == "wc":
                if _WC_SHORT_FLAG_RE.fullmatch(tok):
                    idx += 1
                    continue
                return False
            if exe_name == "cat":
                if _CAT_SHORT_FLAG_RE.fullmatch(tok):
                    idx += 1
                    continue
                return False
            if exe_name in {"head", "tail"}:
                if tok in {"-q", "-v"} or _HEAD_TAIL_NUM_FLAG_RE.fullmatch(tok):
                    idx += 1
                    continue
                if tok in {"-n", "-c"} and idx + 1 < len(args):
                    if _DIGITS_RE.fullmatch(args[idx + 1]):
                        idx += 2
                        continue
                return False
            return False
        positional += 1
        idx += 1

    if exe_name == "ls":
        return True
    return positional >= 1


def _check_positive_allowlist_syntax(exe_name: str, args: list[str]) -> Layer1Decision | None:
    """Enforce closed-world flag and subcommand allowlist for every Layer 1 tool."""
    if exe_name == "pytest":
        ok = _validate_pytest_args(args)
    elif exe_name == "mypy":
        ok = _validate_mypy_args(args)
    elif exe_name == "ruff":
        ok = _validate_ruff_args(args)
    else:
        ok = _validate_coreutil_args(exe_name, args)

    if ok:
        return None
    return Layer1Decision(
        verdict="escalate",
        rule_id="UNSAFE_TOOL_CONFIG_FLAG",
        risk_hint="high",
        reason=f"Arguments or options for '{exe_name}' are outside Layer 1 positive allowlist.",
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
        stripped_for_deny = command.strip()
        if not stripped_for_deny:
            return Layer1Decision(
                verdict="escalate",
                rule_id="EMPTY_COMMAND",
                risk_hint="medium",
                reason="Empty command string cannot be auto-approved.",
            )

        for rule_id, pattern, reason in _HARD_DENY_PATTERNS:
            if pattern.search(stripped_for_deny) is not None:
                return Layer1Decision(
                    verdict="hard_deny",
                    rule_id=rule_id,
                    risk_hint="critical",
                    reason=reason,
                )

        if _has_control_characters(command):
            return Layer1Decision(
                verdict="escalate",
                rule_id="CONTROL_CHARACTER_DETECTED",
                risk_hint="high",
                reason="Multiline or control-character command requires auditor inspection.",
            )

        stripped = command.strip(" \t")
        if not stripped:
            return Layer1Decision(
                verdict="escalate",
                rule_id="EMPTY_COMMAND",
                risk_hint="medium",
                reason="Empty command string cannot be auto-approved.",
            )

        if not os.path.isabs(cwd) or not os.path.isabs(workspace_canonical_path):
            return Layer1Decision(
                verdict="escalate",
                rule_id="OUTSIDE_WORKSPACE_CWD",
                risk_hint="high",
                reason="Command working directory and workspace root must be absolute paths.",
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

        response_escape = _check_response_file_tokens(tokens, resolved_cwd, workspace_root)
        if response_escape is not None:
            return response_escape

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

        if "=" in head:
            return Layer1Decision(
                verdict="escalate",
                rule_id="UNTRUSTED_RELATIVE_EXECUTABLE",
                risk_hint="high",
                reason="Environment variable assignment or '=' in executable token requires audit.",
            )

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

        if _VENV_EXE_RE.fullmatch(head):
            venv_bin = Path(os.path.realpath(workspace_root / ".venv" / "bin"))
            resolved_exe = Path(os.path.realpath(resolved_cwd / head))
            if (
                not _is_within_workspace(resolved_exe, workspace_root)
                or resolved_exe.parent != venv_bin
            ):
                return Layer1Decision(
                    verdict="escalate",
                    rule_id="UNTRUSTED_RELATIVE_EXECUTABLE",
                    risk_hint="high",
                    reason=f"Executable path '{head}' is outside workspace .venv/bin.",
                )
        elif not _BARE_EXE_RE.fullmatch(head) or head in {".", ".."}:
            return Layer1Decision(
                verdict="escalate",
                rule_id="UNTRUSTED_RELATIVE_EXECUTABLE",
                risk_hint="high",
                reason=f"Executable path '{head}' is outside workspace .venv/bin.",
            )

        if exe_name == "git":
            git_decision = _evaluate_git_tokens(args)
            if git_decision.verdict == "allow":
                repo_escape = _verify_confined_git_repository(resolved_cwd, workspace_root)
                if repo_escape is not None:
                    return repo_escape
            return git_decision

        if exe_name in _SIMPLE_ALLOWLIST_BINARIES:
            unsafe_syntax = _check_positive_allowlist_syntax(exe_name, args)
            if unsafe_syntax is not None:
                return unsafe_syntax
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
