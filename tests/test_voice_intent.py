"""TDD tests for T4: Deterministic spoken intent router (OP-4 & AC-3)."""

from __future__ import annotations

import pytest

from nova.application.voice_intent import (
    classify_confirmation_response,
    classify_voice_control_intent,
)


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


@pytest.mark.parametrize(
    "phrase",
    [
        "sí",
        "Sí.",
        "Oye Nova, sí",
        "confirmar",
        "¡Confirmado!",
        "adelante",
        "dale",
        "ejecútalo",
        "correcto",
        "eso mismo",
        "Sí, eso es lo que entiendo.",
        "Oye Nova, sí, ejecútalo",
    ],
)
def test_confirmation_classifier_recognizes_closed_confirm_phrases(phrase: str) -> None:
    result = classify_confirmation_response(phrase)
    assert result.action == "confirm"


@pytest.mark.parametrize(
    "phrase",
    [
        "no",
        "No.",
        "Oye Nova, cancelar",
        "cancelar",
        "descartar",
        "déjalo",
        "olvídalo",
        "detener",
        "no gracias",
        "mejor no",
    ],
)
def test_confirmation_classifier_recognizes_closed_cancel_phrases(phrase: str) -> None:
    result = classify_confirmation_response(phrase)
    assert result.action == "cancel"


@pytest.mark.parametrize(
    ("phrase", "expected_clean"),
    [
        (
            "Oye Nova, no, mejor revisa el archivo orchestrator",
            "no, mejor revisa el archivo orchestrator",
        ),
        (
            "cambia las pruebas unitarias por documentación",
            "cambia las pruebas unitarias por documentación",
        ),
        (
            "sí pero solo en el módulo de audio",
            "sí pero solo en el módulo de audio",
        ),
    ],
)
def test_confirmation_classifier_treats_other_utterances_as_correction(
    phrase: str,
    expected_clean: str,
) -> None:
    result = classify_confirmation_response(phrase)
    assert result.action == "correct"
    assert result.cleaned_text == expected_clean


def test_confirmation_classifier_ignores_empty_or_bare_wake_word() -> None:
    assert classify_confirmation_response("   ...  ").action == "ignore_empty"
    assert classify_confirmation_response("Oye Nova!").action == "ignore_empty"
