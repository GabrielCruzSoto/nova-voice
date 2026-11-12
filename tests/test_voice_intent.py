"""TDD tests for T4: Deterministic spoken intent router (OP-4 & AC-3)."""

from __future__ import annotations

import pytest

from nova.application.voice_intent import classify_voice_control_intent


@pytest.mark.parametrize(
    "phrase",
    [
        "sí",
        "¡Sí, aprobar comando!",
        "aprobado.",
        "aprobar",
        "aprobar comando",
        "si aprobar",
        "adelante",
        "ejecútalo",
    ],
)
def test_pending_approval_accepts_exact_closed_approval_phrases(phrase: str) -> None:
    intent = classify_voice_control_intent(phrase, has_pending_approval=True)
    assert intent.kind == "approve_command"


@pytest.mark.parametrize(
    "phrase",
    [
        "no",
        "¡No, rechazar comando!",
        "rechazado",
        "rechazar",
        "no rechazar",
        "cancelar comando",
        "detener.",
    ],
)
def test_pending_approval_accepts_exact_closed_rejection_phrases(phrase: str) -> None:
    intent = classify_voice_control_intent(phrase, has_pending_approval=True)
    assert intent.kind == "reject_command"


@pytest.mark.parametrize(
    "ambiguous_phrase",
    [
        "qué hora es",
        "no estoy seguro si aprobar comando",
        "sí pero espera un segundo",
        "cambia al proyecto otro-repo",
        "ejecuta las pruebas primero",
    ],
)
def test_pending_approval_fails_closed_on_any_ambiguous_or_other_utterance(
    ambiguous_phrase: str,
) -> None:
    intent = classify_voice_control_intent(ambiguous_phrase, has_pending_approval=True)
    assert intent.kind == "hold_pending_approval"


@pytest.mark.parametrize(
    ("utterance", "expected_query"),
    [
        ("cambia al proyecto nova-workspace", "nova-workspace"),
        ("Oye Nova, abre el repositorio Pagos Core.", "Pagos Core"),
        ("seleccionar workspace motor_voz!", "motor_voz"),
    ],
)
def test_workspace_switch_intent_extracts_target_query(
    utterance: str,
    expected_query: str,
) -> None:
    intent = classify_voice_control_intent(utterance, has_pending_approval=False)
    assert intent.kind == "select_workspace"
    assert intent.workspace_query == expected_query


@pytest.mark.parametrize(
    "hangup_phrase",
    [
        "terminar llamada",
        "Colgar llamada.",
        "cerrar llamada",
        "Descansa Nova",
        "Oye Nova, hasta luego nova",
    ],
)
def test_end_call_phrases_transition_back_to_standby(hangup_phrase: str) -> None:
    intent = classify_voice_control_intent(hangup_phrase, has_pending_approval=False)
    assert intent.kind == "end_call"


def test_general_utterance_and_empty_or_bare_wake_word_handling() -> None:
    empty = classify_voice_control_intent("   ... ¡! ", has_pending_approval=True)
    assert empty.kind == "ignore_empty"

    bare_wake = classify_voice_control_intent("¡Oye Nova!", has_pending_approval=False)
    assert bare_wake.kind == "ignore_empty"

    prompt = classify_voice_control_intent(
        "Oye Nova, ejecuta pytest en modo silencioso.",
        has_pending_approval=False,
    )
    assert prompt.kind == "user_utterance"
    assert prompt.utterance_text == "ejecuta pytest en modo silencioso."
