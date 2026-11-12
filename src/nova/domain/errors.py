"""Machine-readable domain exceptions matching contract.md error catalog."""

from __future__ import annotations

import re

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]+"),
    re.compile(r"\b(?:nvapi|sk|rk|pk|ghp|gho|xoxb|AIza)-[A-Za-z0-9_\-]{6,}"),
    re.compile(
        r"(?i)\b(?:nova_gatekeeper_token|api[_-]?key|token|secret|password|authorization)"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9._\-+/=]{4,}['\"]?"
    ),
)


def redact_sensitive_text(message: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Mask bearer tokens, vendor API keys, credential assignments, and dynamic session secrets."""
    sanitized = message
    for secret in extra_secrets:
        if secret and len(secret) >= 4:
            sanitized = sanitized.replace(secret, "<REDACTED:secret>")
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("<REDACTED:secret>", sanitized)
    return sanitized


class NovaDomainError(Exception):
    """Base domain error with a machine-readable contract code and redacted message."""

    def __init__(self, code: str, message: str) -> None:
        redacted = redact_sensitive_text(message)
        super().__init__(redacted)
        self.code = code
        self.sanitized_message = redacted


class WorkspaceResolutionError(NovaDomainError):
    """Raised when workspace resolution fails validation, confinement, or disambiguation."""

    def __init__(
        self,
        code: str,
        message: str,
        candidates: tuple[str, ...] = (),
    ) -> None:
        super().__init__(code=code, message=message)
        self.candidates = candidates


class PiRpcError(NovaDomainError):
    """Raised when Tier 1 Pi RPC rejects a command or fails at the subprocess boundary."""


class CodingRunnerError(NovaDomainError):
    """Raised when a Tier 2 CodingRunnerPort adapter rejects or fails execution."""


class GatekeeperError(NovaDomainError):
    """Raised when SecurityGatekeeper rejects an operation or encounters a queue/IPC error."""


class ApprovalNotFoundError(GatekeeperError):
    """Raised when resolving a non-existent or already-expired command approval request."""

    def __init__(self, audit_id: str) -> None:
        super().__init__(
            code="GATEKEEPER_APPROVAL_NOT_FOUND",
            message=f"Pending command approval '{audit_id}' was not found or already expired.",
        )
