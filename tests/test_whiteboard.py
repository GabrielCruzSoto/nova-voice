"""Unit tests for NOVA-019 whiteboard domain models, events, intent detection, and parser."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from nova.application.whiteboard import (
    build_fallback_whiteboard_deck,
    build_whiteboard_prompt,
    format_whiteboard_chat_summary,
    is_visual_explanation_request,
    parse_whiteboard_deck,
    redact_whiteboard_deck,
)
from nova.domain.events import (
    VoiceSpeechChunkEvent,
    WhiteboardDeckCreatedEvent,
    WhiteboardSlideChangedEvent,
)
from nova.domain.models import (
    MAX_WHITEBOARD_EDGES,
    MAX_WHITEBOARD_NODES,
    MAX_WHITEBOARD_SLIDES,
    WhiteboardDeck,
    WhiteboardEdge,
    WhiteboardNode,
    WhiteboardSlide,
)


def test_ac1_visual_explanation_intent_detection_and_negative_guards() -> None:
    # Positive visual explanation requests (ES & EN)
    assert is_visual_explanation_request("Explícame cómo funciona OAuth2") is True
    assert is_visual_explanation_request("explicame la arquitectura de microservicios") is True
    assert (
        is_visual_explanation_request("Ármame una presentación con mapa mental sobre CQRS") is True
    )
    assert is_visual_explanation_request("Dibuja en la pizarra el flujo de autenticación") is True
    assert is_visual_explanation_request("Cómo funciona el event loop de Python") is True
    assert is_visual_explanation_request("Explain Kafka partitioning with a diagram") is True

    # Negative guards: editing/execution commands and explicit Tier 2 delegation
    assert is_visual_explanation_request("") is False
    assert is_visual_explanation_request("   ") is False
    assert is_visual_explanation_request("Refactoriza el módulo de pagos") is False
    assert is_visual_explanation_request("Explícame y modifica el archivo main.py") is False
    assert is_visual_explanation_request("Ejecuta pytest en el repositorio") is False
    assert is_visual_explanation_request("[delegate_tier2] Explícame la arquitectura") is False
    # Local repo/file inspection stays fast prose unless whiteboard/diagram is explicitly asked
    assert is_visual_explanation_request("Explícame de qué trata este proyecto") is False
    assert is_visual_explanation_request("Explica una linea") is False
    assert (
        is_visual_explanation_request("Explícame en la pizarra de qué trata este proyecto") is True
    )
    # F1 regression: English & Spanish code-mutation prompts and filenames must NOT hijack to board
    assert (
        is_visual_explanation_request("Crea una migración de contratos y explica los cambios")
        is False
    )
    assert is_visual_explanation_request("Edit whiteboard.ts to fix the layout") is False
    assert (
        is_visual_explanation_request(
            "Fix the architecture bug in microservices and explain the root cause"
        )
        is False
    )
    assert is_visual_explanation_request("Actualiza el archivo whiteboard.py") is False
    assert is_visual_explanation_request("Agrega tests para el tablero de pizarra") is False
    assert is_visual_explanation_request("Corrige el layout en whiteboard.ts") is False
    # Creating a presentation/diagram itself MUST still work
    assert is_visual_explanation_request("Crea una presentación sobre OAuth2") is True
    assert is_visual_explanation_request("Crea un mapa mental sobre Redis") is True


def test_ac2_domain_models_enforce_hard_bounds_and_referential_integrity() -> None:
    nodes = tuple(
        WhiteboardNode(id=f"n{i}", label=f"Nodo {i}", icon="brain", shape="box", accent="blue")
        for i in range(MAX_WHITEBOARD_NODES)
    )
    slide = WhiteboardSlide(
        slide_index=0,
        title="Mapa base",
        visual_kind="mindmap",
        nodes=nodes,
        edges=(WhiteboardEdge(source_id="n0", target_id="n1", label="conecta"),),
        callouts=("Idea clave",),
        narration="Aquí vemos cómo se conectan los conceptos centrales.",
    )
    deck = WhiteboardDeck(
        deck_id="deck-valid-1",
        correlation_id="corr-1",
        topic="Arquitectura Hexagonal",
        slides=(slide,),
    )
    assert deck.active_slide_index == 0
    assert len(deck.slides) == 1

    # Exceeding MAX_WHITEBOARD_NODES must fail
    too_many_nodes = tuple(
        WhiteboardNode(id=f"n{i}", label=f"Nodo {i}") for i in range(MAX_WHITEBOARD_NODES + 1)
    )
    with pytest.raises(ValidationError):
        WhiteboardSlide(
            slide_index=0,
            title="Demasiados nodos",
            visual_kind="flowchart",
            nodes=too_many_nodes,
            narration="Narración válida.",
        )

    # Dangling edge target must fail
    with pytest.raises(ValidationError):
        WhiteboardSlide(
            slide_index=0,
            title="Arista rota",
            visual_kind="flowchart",
            nodes=(WhiteboardNode(id="n1", label="Inicio"),),
            edges=(WhiteboardEdge(source_id="n1", target_id="missing"),),
            narration="Narración válida.",
        )

    # Exceeding MAX_WHITEBOARD_SLIDES must fail at slide_index and deck slides boundary
    with pytest.raises(ValidationError):
        WhiteboardSlide(
            slide_index=MAX_WHITEBOARD_SLIDES,
            title="Fuera de cota",
            visual_kind="sketch_cards",
            nodes=(WhiteboardNode(id="n1", label="Paso"),),
            narration="Explicación breve.",
        )

    too_many_slides = tuple(
        WhiteboardSlide(
            slide_index=min(i, MAX_WHITEBOARD_SLIDES - 1),
            title=f"Lámina {i}",
            visual_kind="sketch_cards",
            nodes=(WhiteboardNode(id="n1", label="Paso"),),
            narration="Explicación breve.",
        )
        for i in range(MAX_WHITEBOARD_SLIDES + 1)
    )
    with pytest.raises(ValidationError):
        WhiteboardDeck(
            deck_id="deck-too-big",
            correlation_id="corr-1",
            topic="Tema",
            slides=too_many_slides,
        )

    # Out-of-bounds active_slide_index must fail
    with pytest.raises(ValidationError):
        WhiteboardDeck(
            deck_id="deck-oob",
            correlation_id="corr-1",
            topic="Tema",
            slides=(slide,),
            active_slide_index=2,
        )


def test_ac2_ac9_parse_structured_deck_redacts_secrets_clamps_bounds_and_handles_enums() -> None:
    secret_val = "sk-testsecret1234567890"
    payload = {
        "topic": f"Flujo OAuth2 con token {secret_val}",
        "summary": "Resumen claro del protocolo.",
        "slides": [
            {
                "title": "Paso 1: Autorización",
                "subtitle": "Cliente hacia servidor",
                "visual_kind": "flowchart",
                "nodes": [
                    {
                        "id": f"node_{i}",
                        "label": f"Paso {i} {secret_val}" if i == 0 else f"Paso {i}",
                        "detail": "Detalle útil",
                        "shape": "unknown_shape" if i == 0 else "box",
                        "icon": "nonexistent_icon" if i == 0 else "shield",
                        "accent": "neon_purple" if i == 0 else "emerald",
                    }
                    for i in range(MAX_WHITEBOARD_NODES + 4)
                ],
                "edges": [
                    {
                        "source_id": f"node_{i}",
                        "target_id": f"node_{i + 1}",
                        "label": f"flujo {i}",
                    }
                    for i in range(MAX_WHITEBOARD_EDGES + 3)
                ]
                + [{"source_id": "node_0", "target_id": "ghost_node", "label": "invalida"}],
                "callouts": [f"Nota {i}" for i in range(6)],
                "narration": (
                    f"Primero el cliente solicita acceso usando {secret_val} de forma segura."
                ),
            }
        ],
    }
    raw = (
        f"Te preparé esta pizarra:\n```nova-whiteboard\n{json.dumps(payload)}\n```\n"
        "Espero te sirva."
    )

    deck = parse_whiteboard_deck(
        raw,
        topic="OAuth2",
        correlation_id="corr-parse-1",
        extra_secrets=(secret_val,),
    )

    assert deck.source == "llm_structured"
    assert len(deck.slides) == 1
    s0 = deck.slides[0]
    assert len(s0.nodes) == MAX_WHITEBOARD_NODES
    assert len(s0.edges) <= MAX_WHITEBOARD_EDGES
    assert all(e.target_id != "ghost_node" for e in s0.edges)
    assert len(s0.callouts) == 4
    # Unknown enums degrade safely
    assert s0.nodes[0].shape == "box"
    assert s0.nodes[0].icon == "spark"
    assert s0.nodes[0].accent == "blue"
    # Universal redaction across every field
    dumped = deck.model_dump_json()
    assert secret_val not in dumped
    assert "<REDACTED:" in dumped

    # Chat summary never leaks raw JSON schema
    chat_summary = format_whiteboard_chat_summary(deck)
    assert "```nova-whiteboard" not in chat_summary
    assert "Paso 1: Autorización" in chat_summary


def test_ac2_parse_fallback_on_malformed_json_or_plain_prose() -> None:
    # Broken JSON fence
    broken = (
        "```nova-whiteboard\n{invalid json,,,\n```\n"
        "El event loop coordina corrutinas en un hilo.\n"
        "Usa colas de tareas y callbacks sin bloquear."
    )
    deck_broken = parse_whiteboard_deck(
        broken,
        topic="Event Loop",
        correlation_id="corr-fallback-1",
    )
    assert deck_broken.source == "deterministic_fallback"
    assert 1 <= len(deck_broken.slides) <= MAX_WHITEBOARD_SLIDES
    assert deck_broken.slides[0].nodes
    assert deck_broken.slides[0].narration

    # Plain prose without any JSON
    prose = (
        "Kafka divide los tópicos en particiones ordenadas.\n\n"
        "Los productores escriben eventos con clave de partición.\n\n"
        "Los grupos de consumidores escalan leyendo particiones en paralelo."
    )
    deck_prose = build_fallback_whiteboard_deck(
        "Apache Kafka",
        prose,
        correlation_id="corr-fallback-2",
    )
    assert deck_prose.source == "deterministic_fallback"
    assert len(deck_prose.slides) >= 1
    assert "Kafka" in deck_prose.topic


def test_ac4_ac10_additive_events_and_prompt_builder() -> None:
    prompt = build_whiteboard_prompt("Explícame cómo funciona DNS")
    assert "nova-whiteboard" in prompt
    assert "flowchart" in prompt
    assert "mindmap" in prompt
    assert "sketch_cards" in prompt
    assert "SVG" in prompt

    # Backward-compatible VoiceSpeechChunkEvent without slide metadata
    legacy_chunk = VoiceSpeechChunkEvent(
        correlation_id="corr-legacy",
        text="Hola desde llamada.",
        chunk_index=0,
        is_final=True,
    )
    assert legacy_chunk.deck_id is None
    assert legacy_chunk.slide_index is None

    # Tagged VoiceSpeechChunkEvent with slide metadata
    tagged_chunk = VoiceSpeechChunkEvent(
        correlation_id="corr-sync",
        text="En esta primera lámina vemos la arquitectura.",
        chunk_index=0,
        is_final=False,
        deck_id="deck-1",
        slide_index=0,
    )
    assert tagged_chunk.deck_id == "deck-1"
    assert tagged_chunk.slide_index == 0

    deck = build_fallback_whiteboard_deck(
        "DNS",
        "El cliente consulta al resolver recursivo.",
        correlation_id="corr-sync",
    )
    created_ev = WhiteboardDeckCreatedEvent(correlation_id="corr-sync", deck=deck, auto_open=True)
    changed_ev = WhiteboardSlideChangedEvent(
        correlation_id="corr-sync",
        deck_id=deck.deck_id,
        slide_index=0,
        reason="narration_sync",
    )
    assert created_ev.auto_open is True
    assert changed_ev.reason == "narration_sync"

    # Defense-in-depth deck redaction (including node.id & edge endpoints, F5)
    secret_id = "sk-nodeidsecret987654"
    secret_node_payload = {
        "topic": "Seguridad",
        "slides": [
            {
                "title": "Nodos",
                "visual_kind": "flowchart",
                "nodes": [
                    {"id": secret_id, "label": "Origen"},
                    {"id": "n_ok", "label": "Destino"},
                ],
                "edges": [{"source_id": secret_id, "target_id": "n_ok", "label": "envia"}],
                "narration": "Flujo seguro.",
            }
        ],
    }
    parsed_sec = parse_whiteboard_deck(
        f"```nova-whiteboard\n{json.dumps(secret_node_payload)}\n```",
        topic="Seguridad",
        correlation_id="corr-f5-parse",
        extra_secrets=(secret_id,),
    )
    assert secret_id not in parsed_sec.model_dump_json()
    assert len(parsed_sec.slides[0].edges) == 1
    assert parsed_sec.slides[0].edges[0].source_id == parsed_sec.slides[0].nodes[0].id

    raw_deck_with_secret_id = WhiteboardDeck(
        deck_id="deck-f5",
        correlation_id="corr-f5",
        topic="Seguridad",
        summary="Token interno my-csrf-secret-999",
        slides=(
            WhiteboardSlide(
                slide_index=0,
                title="Mapa",
                visual_kind="flowchart",
                nodes=(
                    WhiteboardNode(id="tok_csrf_secret_999", label="Nodo A"),
                    WhiteboardNode(id="n2", label="Nodo B"),
                ),
                edges=(WhiteboardEdge(source_id="tok_csrf_secret_999", target_id="n2"),),
                narration="Flujo protegido.",
            ),
        ),
    )
    redacted = redact_whiteboard_deck(
        raw_deck_with_secret_id,
        extra_secrets=("my-csrf-secret-999", "tok_csrf_secret_999"),
    )
    assert "my-csrf-secret-999" not in redacted.summary
    assert "tok_csrf_secret_999" not in redacted.model_dump_json()
    assert "<REDACTED:" in redacted.summary
    assert len(redacted.slides[0].edges) == 1
    assert redacted.slides[0].edges[0].source_id == redacted.slides[0].nodes[0].id
