"""Machine-readable domain exceptions matching contract.md error catalog."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

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

    def __init__(
        self,
        code: str,
        message: str,
        *,
        extra_secrets: tuple[str, ...] = (),
    ) -> None:
        redacted = redact_sensitive_text(message, extra_secrets=extra_secrets)
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


class VoicePipelineError(NovaDomainError):
    """Raised when an audio stream, wake-word, VAD, STT, or TTS operation fails (NOVA-003)."""


class DesktopUiError(NovaDomainError):
    """Raised when the loopback Desktop UI bridge or native picker encounters a domain failure."""


_CONFIG_SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|_|-)(?:api[_-]?key|token|secret|password|passwd|credential|private[_-]?key|auth)$"
)


def reject_secret_settings_keys(data: Any) -> None:
    """Reject credential-like keys in persisted config payloads before saving or applying."""
    if isinstance(data, Mapping):
        for key, value in data.items():
            key_str = str(key)
            if _CONFIG_SECRET_KEY_RE.search(key_str):
                raise DesktopUiError(
                    code="CONFIG_SECRET_FORBIDDEN",
                    message=(
                        f"Forbidden credential key '{key_str}' in config.toml; "
                        "store secrets in environment variables only."
                    ),
                )
            reject_secret_settings_keys(value)
    elif isinstance(data, list):
        for item in data:
            reject_secret_settings_keys(item)
