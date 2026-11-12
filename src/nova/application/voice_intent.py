"""Deterministic spoken intent classifier for active Nova voice calls (NOVA-003 OP-4)."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict

VoiceControlIntentKind = Literal[
    "ignore_empty",
    "approve_command",
    "reject_command",
    "hold_pending_approval",
    "select_workspace",
    "end_call",
    "user_utterance",
]

_APPROVAL_PHRASES: frozenset[str] = frozenset(
    {
        "si",
        "aprobado",
        "aprobar",
        "aprobar comando",
        "si aprobar",
        "si aprobar comando",
        "adelante",
        "ejecutalo",
    }
)

_REJECTION_PHRASES: frozenset[str] = frozenset(
    {
        "no",
        "rechazado",
        "rechazar",
        "rechazar comando",
        "no rechazar",
        "no rechazar comando",
        "cancelar",
        "cancelar comando",
        "detener",
    }
)

_END_CALL_PHRASES: frozenset[str] = frozenset(
    {
        "terminar llamada",
        "colgar llamada",
        "cerrar llamada",
        "descansa nova",
        "hasta luego nova",
    }
)

_PUNCT_STRIP_RE = re.compile(r"[^\w\s-]+", re.UNICODE)
_WAKE_PREFIX_NORM_RE = re.compile(r"^(?:oye\s+nova|hey\s+nova)\b\s*")
_WAKE_PREFIX_RAW_RE = re.compile(
    r"^\s*[¡!¿?.,;:\s]*(?:oye\s+nova|hey\s+nova)\b[\s,!.:;-]*",
    re.IGNORECASE,
)
_WORKSPACE_SWITCH_RE = re.compile(
    r"^\s*[¡!¿?.,;:\s]*(?:(?:oye\s+nova|hey\s+nova)\b[\s,!.:;-]*)?"
    r"(?:cambia|cambiar|abre|abrir|selecciona|seleccionar)\s+"
    r"(?:al\s+|el\s+)?(?:proyecto|repositorio|workspace)\s+(.+?)\s*[.!?;,¡¿]*\s*$",
    re.IGNORECASE,
)


def normalize_spoken_phrase(raw_text: str) -> str:
    """Fold accents, strip punctuation, and collapse whitespace for closed-set matching."""
    decomposed = unicodedata.normalize("NFKD", raw_text)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    without_punct = _PUNCT_STRIP_RE.sub(" ", without_accents.lower())
    return " ".join(without_punct.split())


class VoiceControlIntent(BaseModel):
    """Classified outcome for a single transcribed utterance inside an active call."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: VoiceControlIntentKind
    workspace_query: str | None = None
    utterance_text: str | None = None


def classify_voice_control_intent(
    transcript: str,
    *,
    has_pending_approval: bool,
) -> VoiceControlIntent:
    """Classify a spoken Spanish utterance deterministically and fail-closed on held commands."""
    normalized = normalize_spoken_phrase(transcript)
    if not normalized:
        return VoiceControlIntent(kind="ignore_empty")

    # Priority 1: Fail-closed SecurityGatekeeper resolution when a command is held.
    if has_pending_approval:
        candidate = _WAKE_PREFIX_NORM_RE.sub("", normalized).strip() or normalized
        if candidate in _APPROVAL_PHRASES:
            return VoiceControlIntent(kind="approve_command")
        if candidate in _REJECTION_PHRASES:
            return VoiceControlIntent(kind="reject_command")
        return VoiceControlIntent(kind="hold_pending_approval")

    # Priority 2: Spoken workspace / project switching.
    ws_match = _WORKSPACE_SWITCH_RE.match(transcript)
    if ws_match is not None:
        query = " ".join(ws_match.group(1).strip().split())
        if query and normalize_spoken_phrase(query):
            return VoiceControlIntent(kind="select_workspace", workspace_query=query)

    # Priority 3: Explicit call hangup returning to standby.
    stripped_norm = _WAKE_PREFIX_NORM_RE.sub("", normalized).strip()
    if normalized in _END_CALL_PHRASES or stripped_norm in _END_CALL_PHRASES:
        return VoiceControlIntent(kind="end_call")

    if not stripped_norm:
        return VoiceControlIntent(kind="ignore_empty")

    # Priority 4: General technical or conversational prompt routed to NovaOrchestrator.
    cleaned_utterance = _WAKE_PREFIX_RAW_RE.sub("", transcript).strip()
    if not cleaned_utterance:
        return VoiceControlIntent(kind="ignore_empty")
    return VoiceControlIntent(kind="user_utterance", utterance_text=cleaned_utterance)
