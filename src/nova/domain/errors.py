"""Machine-readable domain exceptions matching contract.md error catalog."""

from __future__ import annotations


class NovaDomainError(Exception):
    """Base domain error with a machine-readable contract code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.sanitized_message = message


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
