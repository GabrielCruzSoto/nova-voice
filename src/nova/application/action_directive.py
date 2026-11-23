"""Incremental stream filter and validator for Tier 1 structured directives (NOVA-020 OP-1)."""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from nova.domain.errors import redact_sensitive_text
from nova.domain.models import Tier1ActionDirective

logger = logging.getLogger("nova.action_directive")

DIRECTIVE_OPEN_PREFIX = "[[NOVA_ACTION:"
DIRECTIVE_CLOSE_SUFFIX = "]]"
MAX_DIRECTIVE_CHARS = 8_192
MAX_DIRECTIVES_PER_TURN = 4


class ActionDirectiveStreamFilter:
    """Incrementally strips `[[NOVA_ACTION:{...}]]` blocks from deltas and validates JSON."""

    def __init__(self, *, extra_secrets: tuple[str, ...] = ()) -> None:
        self._extra_secrets = extra_secrets
        self._buf = ""
        self._in_directive = False
        self._directive_buf = ""
        self._in_json_string = False
        self._json_escape = False
        self._directives: list[Tier1ActionDirective] = []
        self._invalid_count = 0

    @property
    def directives(self) -> tuple[Tier1ActionDirective, ...]:
        """Validated directives extracted so far (bounded to MAX_DIRECTIVES_PER_TURN)."""
        return tuple(self._directives)

    @property
    def invalid_count(self) -> int:
        """Count of malformed or oversized directive blocks safely suppressed."""
        return self._invalid_count

    def feed(self, delta: str) -> str:
        """Feed a streamed LLM delta and return only safe visible text for chat and TTS."""
        if not delta:
            return ""
        self._buf += delta
        visible_parts: list[str] = []

        while self._buf:
            if not self._in_directive:
                open_idx = self._buf.find(DIRECTIVE_OPEN_PREFIX)
                if open_idx >= 0:
                    if open_idx > 0:
                        visible_parts.append(self._buf[:open_idx])
                    self._buf = self._buf[open_idx + len(DIRECTIVE_OPEN_PREFIX) :]
                    self._in_directive = True
                    self._directive_buf = ""
                    self._in_json_string = False
                    self._json_escape = False
                    continue

                hold = self._trailing_prefix_hold_len(self._buf)
                if hold > 0:
                    emit_upto = len(self._buf) - hold
                    if emit_upto > 0:
                        visible_parts.append(self._buf[:emit_upto])
                        self._buf = self._buf[emit_upto:]
                    break

                visible_parts.append(self._buf)
                self._buf = ""
                break

            # Inside an open [[NOVA_ACTION:... directive: scan respecting JSON strings.
            closed = False
            consumed = 0
            idx = 0
            buf_len = len(self._buf)
            while idx < buf_len:
                ch = self._buf[idx]
                if self._in_json_string:
                    self._directive_buf += ch
                    if self._json_escape:
                        self._json_escape = False
                    elif ch == "\\":
                        self._json_escape = True
                    elif ch == '"':
                        self._in_json_string = False
                    idx += 1
                else:
                    if ch == '"':
                        self._in_json_string = True
                        self._directive_buf += ch
                        idx += 1
                    elif ch == "]" and idx + 1 < buf_len and self._buf[idx + 1] == "]":
                        consumed = idx + 2
                        closed = True
                        break
                    elif ch == "]" and idx + 1 == buf_len:
                        # Hold lone trailing ']' in case next delta begins with ']'
                        consumed = idx
                        break
                    else:
                        self._directive_buf += ch
                        idx += 1

                if len(self._directive_buf) > MAX_DIRECTIVE_CHARS:
                    self._record_invalid("directive_exceeded_max_chars")
                    self._in_directive = False
                    self._directive_buf = ""
                    self._in_json_string = False
                    self._json_escape = False
                    consumed = idx
                    break
            else:
                consumed = buf_len

            self._buf = self._buf[consumed:]
            if closed:
                raw_json = self._directive_buf.strip()
                self._in_directive = False
                self._directive_buf = ""
                self._in_json_string = False
                self._json_escape = False
                self._parse_and_store(raw_json)
            elif self._in_directive:
                # Waiting for more deltas (possibly holding lone trailing ']')
                break

        return "".join(visible_parts)

    def flush(self) -> str:
        """Drain remaining safe visible text at end of stream; discard unclosed directives."""
        if self._in_directive:
            self._record_invalid("unclosed_directive_at_eof")
            self._in_directive = False
            self._directive_buf = ""
            self._in_json_string = False
            self._json_escape = False
            self._buf = ""
            return ""
        leftover = self._buf
        self._buf = ""
        return leftover

    @staticmethod
    def _trailing_prefix_hold_len(text: str) -> int:
        max_check = min(len(text), len(DIRECTIVE_OPEN_PREFIX) - 1)
        for length in range(max_check, 0, -1):
            if DIRECTIVE_OPEN_PREFIX.startswith(text[-length:]):
                return length
        return 0

    def _parse_and_store(self, raw_json: str) -> None:
        if len(self._directives) >= MAX_DIRECTIVES_PER_TURN:
            self._record_invalid("max_directives_per_turn_exceeded")
            return
        try:
            parsed: Any = json.loads(raw_json)
        except json.JSONDecodeError:
            self._record_invalid("invalid_json")
            return
        if not isinstance(parsed, dict):
            self._record_invalid("non_object_json")
            return
        try:
            directive = Tier1ActionDirective.model_validate(parsed)
        except ValidationError:
            self._record_invalid("schema_validation_failed")
            return
        if directive.action == "dispatch_task" and not (directive.objective or "").strip():
            self._record_invalid("dispatch_missing_objective")
            return
        self._directives.append(directive)

    def _record_invalid(self, reason: str) -> None:
        self._invalid_count += 1
        logger.warning(
            json.dumps(
                {
                    "event": "tier1_action_directive_invalid",
                    "reason": redact_sensitive_text(reason, extra_secrets=self._extra_secrets),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
