"""Pure application logic for Nova interactive whiteboard explanations (NOVA-019)."""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from typing import Any, cast

from nova.domain.errors import redact_sensitive_text
from nova.domain.models import (
    MAX_SPEECH_CHUNK_CHARS,
    MAX_WHITEBOARD_CALLOUTS,
    MAX_WHITEBOARD_EDGES,
    MAX_WHITEBOARD_NODES,
    MAX_WHITEBOARD_SLIDES,
    WhiteboardAccent,
    WhiteboardDeck,
    WhiteboardEdge,
    WhiteboardEdgeStyle,
    WhiteboardIcon,
    WhiteboardNode,
    WhiteboardNodeShape,
    WhiteboardSlide,
    WhiteboardVisualKind,
)

_VALID_VISUAL_KINDS: frozenset[str] = frozenset(
    ("flowchart", "mindmap", "architecture", "sketch_cards")
)
_VALID_SHAPES: frozenset[str] = frozenset(
    ("box", "pill", "decision", "database", "actor", "cloud", "note")
)
_VALID_ICONS: frozenset[str] = frozenset(
    (
        "spark",
        "brain",
        "gear",
        "database",
        "cloud",
        "shield",
        "user",
        "code",
        "network",
        "rocket",
        "bulb",
        "check",
        "warning",
        "layers",
        "flow",
        "book",
    )
)
_VALID_ACCENTS: frozenset[str] = frozenset(
    ("blue", "emerald", "amber", "violet", "rose", "slate")
)
_ACCENT_CYCLE: tuple[WhiteboardAccent, ...] = (
    "blue",
    "emerald",
    "amber",
    "violet",
    "rose",
    "slate",
)
_ICON_CYCLE: tuple[WhiteboardIcon, ...] = (
    "bulb",
    "layers",
    "flow",
    "gear",
    "network",
    "shield",
    "database",
    "rocket",
)

_DELEGATE_MARKER_RE = re.compile(r"\[delegate_tier2\]", re.IGNORECASE)
_EXPLICIT_BOARD_RE = re.compile(
    r"\b("
    r"pizarra|tablero|presentacion|diapositiva|diapositivas|"
    r"mapa\s+mental|mapas\s+mentales|mind\s*map|"
    r"diagrama|diagramas|flowchart|flujograma|"
    r"dibujito|dibujitos|dibujame|dibuja|esquema\s+visual|whiteboard"
    r")\b",
    re.IGNORECASE,
)
_EXPLAIN_TRIGGER_RE = re.compile(
    r"\b("
    r"explicame|explica|explicar|ensename|ensenar|"
    r"como\s+funciona|como\s+funcionan|que\s+es\s+\w+|"
    r"explain|how\s+does\s+.+?\s+work|walk\s+me\s+through"
    r")\b",
    re.IGNORECASE,
)
_CODE_MUTATION_GUARD_RE = re.compile(
    r"\b("
    r"modifica|modificar|edita|editar|refactoriza|refactorizar|refactor|"
    r"implementa|implementar|implement|crea\s+(?:un|el)\s+archivo|"
    r"corrige\s+(?:el\s+)?(?:bug|error)|arregla\s+(?:el\s+)?(?:bug|error)|"
    r"ejecuta|ejecutar|corre\s+los\s+tests|run\s+tests|"
    r"haz\s+commit|borra|elimina|instala|despliega|delete\s+file"
    r")\b",
    re.IGNORECASE,
)
_TOPIC_LEAD_RE = re.compile(
    r"^(?:por\s+favor\s+)?(?:oye\s+nova[,\s]+)?"
    r"(?:explicame|explícame|explica|ensename|enséñame|"
    r"armame|ármame|hazme|creame|créame|muestrame|muéstrame|dibujame|dibújame|dibuja|"
    r"explain|walk\s+me\s+through)\s+",
    re.IGNORECASE,
)
_TOPIC_BOARD_FILLER_RE = re.compile(
    r"^(?:una\s+presentaci[oó]n(?:\s+con\s+(?:diagramas?|mapas?\s+mentales?|dibujitos?))?\s*"
    r"(?:sobre|de|acerca\s+de)?\s*|"
    r"(?:en\s+la\s+pizarra|en\s+el\s+tablero)\s*|"
    r"(?:un\s+)?(?:mapa\s+mental|diagrama|esquema(?:\s+visual)?|dibujo)\s*(?:sobre|de)?\s*)",
    re.IGNORECASE,
)
_FENCED_WHITEBOARD_RE = re.compile(
    r"```(?:nova-whiteboard|whiteboard|json)\s*\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)
_ANY_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_PSEUDO_XML_RE = re.compile(r"</?(?!REDACTED:)[a-zA-Z_][\w-]*\b[^>]*>")
_SAFE_NODE_ID_CLEAN_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def is_visual_explanation_request(utterance: str) -> bool:
    """Return True when the user asks Nova to explain a topic visually on the whiteboard."""
    trimmed = utterance.strip()
    if not trimmed:
        return False
    if _DELEGATE_MARKER_RE.search(trimmed) is not None:
        return False
    folded = _strip_accents(trimmed).lower()
    if _CODE_MUTATION_GUARD_RE.search(folded) is not None:
        return False
    if _EXPLICIT_BOARD_RE.search(folded) is not None:
        return True
    return _EXPLAIN_TRIGGER_RE.search(folded) is not None


def extract_explanation_topic(utterance: str) -> str:
    """Derive a clean <=120-char topic title from a visual explanation prompt."""
    cleaned = " ".join(utterance.strip().split())
    if not cleaned:
        return "Explicación visual"
    reduced = _TOPIC_LEAD_RE.sub("", cleaned).strip(" ¿?¡!.,:;")
    for _ in range(2):
        reduced = _TOPIC_BOARD_FILLER_RE.sub("", reduced).strip(" ¿?¡!.,:;")
    candidate = reduced or cleaned
    candidate = candidate[:120].strip()
    if not candidate:
        return "Explicación visual"
    return candidate[0].upper() + candidate[1:]


def build_whiteboard_prompt(utterance: str) -> str:
    """Wrap a user explanation request with strict structured JSON instructions for Tier 1."""
    clean_req = " ".join(utterance.strip().split())
    return (
        "El usuario solicita una explicación visual en la Pizarra interactiva de Nova.\n"
        f"Solicitud del usuario: {clean_req}\n\n"
        "Instrucciones estrictas de formato:\n"
        "1. Responde en español claro y didáctico.\n"
        "2. Incluye un único bloque cercado ```nova-whiteboard con un objeto JSON válido.\n"
        "3. NUNCA incluyas etiquetas HTML ni código SVG crudo.\n"
        "4. Esquema JSON obligatorio:\n"
        '   {"topic": "string <= 120", "summary": "string <= 400", "slides": [\n'
        '     {"title": "string <= 100", "subtitle": "string <= 160",\n'
        '      "visual_kind": "flowchart|mindmap|architecture|sketch_cards",\n'
        '      "nodes": [{"id": "n1", "label": "string <= 60", "detail": "string <= 120",\n'
        '        "shape": "box|pill|decision|database|actor|cloud|note",\n'
        '        "icon": "spark|brain|gear|database|cloud|shield|user|code|network|rocket|'
        'bulb|check|warning|layers|flow|book",\n'
        '        "accent": "blue|emerald|amber|violet|rose|slate", "group": "string <= 40"}],\n'
        '      "edges": [{"source_id": "n1", "target_id": "n2", "label": "string <= 40", '
        '"style": "solid|dashed"}],\n'
        '      "callouts": ["idea clave <= 140"],\n'
        '      "narration": "explicación hablada natural en español <= 550 caracteres"}\n'
        "   ]}\n"
        "5. Usa entre 2 y 5 láminas combinando diagramas (flowchart/architecture), mapas mentales "
        "(mindmap) y tarjetas ilustradas con dibujitos (sketch_cards). Máx 12 nodos y 18 aristas "
        "por lámina. Redacta 'narration' lista para ser leída en voz alta sin Markdown."
    )


def _clean_text(
    raw: Any,
    *,
    max_len: int,
    extra_secrets: tuple[str, ...],
    strip_markup: bool = False,
) -> str:
    if raw is None:
        return ""
    text = str(raw)
    redacted = redact_sensitive_text(text, extra_secrets=extra_secrets)
    if strip_markup:
        redacted = _ANY_FENCE_RE.sub(" ", redacted)
        redacted = _PSEUDO_XML_RE.sub(" ", redacted)
        redacted = re.sub(r"[`*_~|]+", " ", redacted)
    normalized = " ".join(redacted.strip().split())
    if len(normalized) > max_len:
        cut = normalized[: max_len - 1].rsplit(" ", 1)[0].strip()
        normalized = (cut if len(cut) >= max_len // 2 else normalized[: max_len - 1]).rstrip(
            " ,;:"
        ) + "…"
    return normalized


def _sanitize_node_id(raw: Any, fallback_idx: int) -> str:
    candidate = _SAFE_NODE_ID_CLEAN_RE.sub("_", str(raw or "").strip()).strip("_")
    if not candidate:
        return f"n{fallback_idx}"
    return candidate[:32]


def _extract_json_payload(raw_response: str) -> dict[str, Any] | None:
    for match in _FENCED_WHITEBOARD_RE.finditer(raw_response):
        candidate = match.group(1).strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    stripped = raw_response.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if 0 <= start < end:
        snippet = stripped[start : end + 1]
        if '"slides"' in snippet:
            try:
                parsed = json.loads(snippet)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
    return None


def _coerce_slide(
    raw_slide: dict[str, Any],
    *,
    slide_index: int,
    fallback_topic: str,
    extra_secrets: tuple[str, ...],
) -> WhiteboardSlide | None:
    title = _clean_text(
        raw_slide.get("title"),
        max_len=100,
        extra_secrets=extra_secrets,
    ) or f"{fallback_topic[:80]} ({slide_index + 1})"
    subtitle = _clean_text(
        raw_slide.get("subtitle"),
        max_len=160,
        extra_secrets=extra_secrets,
    )
    raw_kind = str(raw_slide.get("visual_kind", "")).strip().lower()
    visual_kind: WhiteboardVisualKind = (
        cast(WhiteboardVisualKind, raw_kind)
        if raw_kind in _VALID_VISUAL_KINDS
        else "sketch_cards"
    )

    raw_nodes = raw_slide.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        return None

    nodes: list[WhiteboardNode] = []
    seen_ids: set[str] = set()
    id_remap: dict[str, str] = {}

    for idx, item in enumerate(raw_nodes):
        if len(nodes) >= MAX_WHITEBOARD_NODES:
            break
        if not isinstance(item, dict):
            continue
        label = _clean_text(item.get("label"), max_len=60, extra_secrets=extra_secrets)
        if not label:
            continue
        raw_id = str(item.get("id") or f"n{idx + 1}").strip()
        node_id = _sanitize_node_id(raw_id, idx + 1)
        unique_id = node_id
        suffix = 2
        while unique_id in seen_ids:
            unique_id = f"{node_id[:28]}_{suffix}"
            suffix += 1
        seen_ids.add(unique_id)
        if raw_id:
            id_remap[raw_id] = unique_id
        id_remap[node_id] = unique_id

        raw_shape = str(item.get("shape", "box")).strip().lower()
        shape: WhiteboardNodeShape = (
            cast(WhiteboardNodeShape, raw_shape) if raw_shape in _VALID_SHAPES else "box"
        )
        raw_icon = str(item.get("icon", "spark")).strip().lower()
        icon: WhiteboardIcon = (
            cast(WhiteboardIcon, raw_icon) if raw_icon in _VALID_ICONS else "spark"
        )
        raw_accent = str(item.get("accent", "blue")).strip().lower()
        accent: WhiteboardAccent = (
            cast(WhiteboardAccent, raw_accent) if raw_accent in _VALID_ACCENTS else "blue"
        )
        detail = _clean_text(item.get("detail"), max_len=120, extra_secrets=extra_secrets)
        group = _clean_text(item.get("group"), max_len=40, extra_secrets=extra_secrets)

        nodes.append(
            WhiteboardNode(
                id=unique_id,
                label=label,
                detail=detail,
                shape=shape,
                icon=icon,
                accent=accent,
                group=group,
            )
        )

    if not nodes:
        return None

    edges: list[WhiteboardEdge] = []
    raw_edges = raw_slide.get("edges")
    if isinstance(raw_edges, list):
        for edge_item in raw_edges:
            if len(edges) >= MAX_WHITEBOARD_EDGES:
                break
            if not isinstance(edge_item, dict):
                continue
            src_raw = str(edge_item.get("source_id") or "").strip()
            tgt_raw = str(edge_item.get("target_id") or "").strip()
            src_id = id_remap.get(src_raw) or id_remap.get(_sanitize_node_id(src_raw, 0))
            tgt_id = id_remap.get(tgt_raw) or id_remap.get(_sanitize_node_id(tgt_raw, 0))
            if not src_id or not tgt_id or src_id not in seen_ids or tgt_id not in seen_ids:
                continue
            edge_label = _clean_text(
                edge_item.get("label"),
                max_len=40,
                extra_secrets=extra_secrets,
            )
            raw_style = str(edge_item.get("style", "solid")).strip().lower()
            style: WhiteboardEdgeStyle = "dashed" if raw_style == "dashed" else "solid"
            edges.append(
                WhiteboardEdge(
                    source_id=src_id,
                    target_id=tgt_id,
                    label=edge_label,
                    style=style,
                )
            )

    callouts: list[str] = []
    raw_callouts = raw_slide.get("callouts")
    if isinstance(raw_callouts, list):
        for c_item in raw_callouts:
            if len(callouts) >= MAX_WHITEBOARD_CALLOUTS:
                break
            cleaned_callout = _clean_text(c_item, max_len=140, extra_secrets=extra_secrets)
            if cleaned_callout:
                callouts.append(cleaned_callout)

    narration = _clean_text(
        raw_slide.get("narration"),
        max_len=MAX_SPEECH_CHUNK_CHARS,
        extra_secrets=extra_secrets,
        strip_markup=True,
    )
    if not narration:
        node_labels = ", ".join(n.label for n in nodes[:4])
        narration = _clean_text(
            f"En esta lámina sobre {title} vemos los elementos principales: {node_labels}.",
            max_len=MAX_SPEECH_CHUNK_CHARS,
            extra_secrets=extra_secrets,
            strip_markup=True,
        )

    return WhiteboardSlide(
        slide_index=slide_index,
        title=title,
        subtitle=subtitle,
        visual_kind=visual_kind,
        nodes=tuple(nodes),
        edges=tuple(edges),
        callouts=tuple(callouts),
        narration=narration,
    )


def build_fallback_whiteboard_deck(
    topic: str,
    raw_prose: str,
    *,
    correlation_id: str,
    extra_secrets: tuple[str, ...] = (),
) -> WhiteboardDeck:
    """Deterministically convert plain or degraded LLM prose into a valid WhiteboardDeck."""
    safe_topic = (
        _clean_text(topic, max_len=120, extra_secrets=extra_secrets) or "Explicación visual"
    )
    cleaned_prose = redact_sensitive_text(raw_prose, extra_secrets=extra_secrets)
    cleaned_prose = _ANY_FENCE_RE.sub(" ", cleaned_prose)
    cleaned_prose = _PSEUDO_XML_RE.sub(" ", cleaned_prose)

    # Extract meaningful chunks from paragraphs, bullets, or sentences
    raw_segments: list[str] = []
    for block in re.split(r"\n\s*\n+", cleaned_prose):
        lines = [
            re.sub(r"^\s*(?:#{1,6}\s+|[-*•]\s+|\d+\.\s+)", "", ln).strip()
            for ln in block.splitlines()
        ]
        joined = " ".join(ln for ln in lines if ln)
        joined = re.sub(r"[`*_~|]+", " ", joined)
        joined = " ".join(joined.split())
        if len(joined) >= 12:
            raw_segments.append(joined)

    if len(raw_segments) <= 1 and cleaned_prose.strip():
        sentences = [
            " ".join(re.sub(r"[`*_~|]+", " ", s).split())
            for s in re.split(r"(?<=[.!?…])\s+", cleaned_prose.strip())
            if len(s.strip()) >= 10
        ]
        if sentences:
            raw_segments = sentences

    if not raw_segments:
        raw_segments = [f"Resumen conceptual de {safe_topic} organizado visualmente en la pizarra."]

    # Slide 0: Mind map overview of the main concepts
    concept_segments = raw_segments[:6]
    mindmap_nodes: list[WhiteboardNode] = [
        WhiteboardNode(
            id="root",
            label=_clean_text(safe_topic, max_len=60, extra_secrets=extra_secrets) or "Tema",
            detail="Concepto central",
            shape="pill",
            icon="brain",
            accent="blue",
            group="Centro",
        )
    ]
    mindmap_edges: list[WhiteboardEdge] = []
    for idx, seg in enumerate(concept_segments, start=1):
        words = seg.split()
        short_label = _clean_text(" ".join(words[:5]), max_len=60, extra_secrets=extra_secrets)
        detail = _clean_text(seg, max_len=120, extra_secrets=extra_secrets)
        node_id = f"b{idx}"
        mindmap_nodes.append(
            WhiteboardNode(
                id=node_id,
                label=short_label or f"Punto {idx}",
                detail=detail,
                shape="box",
                icon=_ICON_CYCLE[(idx - 1) % len(_ICON_CYCLE)],
                accent=_ACCENT_CYCLE[idx % len(_ACCENT_CYCLE)],
                group=f"Rama {idx}",
            )
        )
        mindmap_edges.append(
            WhiteboardEdge(source_id="root", target_id=node_id, label="", style="solid")
        )

    slide0_narration = _clean_text(
        " ".join(concept_segments[:2]),
        max_len=MAX_SPEECH_CHUNK_CHARS,
        extra_secrets=extra_secrets,
        strip_markup=True,
    )
    slides: list[WhiteboardSlide] = [
        WhiteboardSlide(
            slide_index=0,
            title=_clean_text(
                f"Mapa general: {safe_topic}", max_len=100, extra_secrets=extra_secrets
            ),
            subtitle="Visión rápida de los conceptos clave",
            visual_kind="mindmap",
            nodes=tuple(mindmap_nodes),
            edges=tuple(mindmap_edges),
            callouts=tuple(
                _clean_text(seg, max_len=140, extra_secrets=extra_secrets)
                for seg in concept_segments[:2]
            ),
            narration=slide0_narration or f"Veamos los puntos principales de {safe_topic}.",
        )
    ]

    # Slide 1 (when >= 2 segments): Step-by-step sketch cards / flow
    if len(raw_segments) >= 2:
        step_segments = raw_segments[:4]
        step_nodes: list[WhiteboardNode] = []
        step_edges: list[WhiteboardEdge] = []
        for idx, seg in enumerate(step_segments, start=1):
            words = seg.split()
            short_label = _clean_text(" ".join(words[:5]), max_len=60, extra_secrets=extra_secrets)
            detail = _clean_text(seg, max_len=120, extra_secrets=extra_secrets)
            node_id = f"s{idx}"
            step_nodes.append(
                WhiteboardNode(
                    id=node_id,
                    label=short_label or f"Paso {idx}",
                    detail=detail,
                    shape="box",
                    icon=_ICON_CYCLE[(idx + 1) % len(_ICON_CYCLE)],
                    accent=_ACCENT_CYCLE[(idx - 1) % len(_ACCENT_CYCLE)],
                )
            )
            if idx > 1:
                step_edges.append(
                    WhiteboardEdge(source_id=f"s{idx - 1}", target_id=node_id, label="luego")
                )
        slide1_narration = _clean_text(
            " ".join(step_segments[1:] if len(step_segments) > 2 else step_segments),
            max_len=MAX_SPEECH_CHUNK_CHARS,
            extra_secrets=extra_secrets,
            strip_markup=True,
        )
        slides.append(
            WhiteboardSlide(
                slide_index=1,
                title=_clean_text(
                    f"Paso a paso: {safe_topic}", max_len=100, extra_secrets=extra_secrets
                ),
                subtitle="Secuencia explicativa en la pizarra",
                visual_kind="flowchart",
                nodes=tuple(step_nodes),
                edges=tuple(step_edges),
                callouts=(
                    _clean_text(step_segments[-1], max_len=140, extra_secrets=extra_secrets),
                ),
                narration=slide1_narration or f"Así se encadena el proceso de {safe_topic}.",
            )
        )

    summary = _clean_text(
        raw_segments[0],
        max_len=400,
        extra_secrets=extra_secrets,
        strip_markup=True,
    )
    return WhiteboardDeck(
        deck_id=f"deck-{uuid.uuid4()}",
        correlation_id=correlation_id,
        topic=safe_topic,
        summary=summary,
        slides=tuple(slides[:MAX_WHITEBOARD_SLIDES]),
        active_slide_index=0,
        source="deterministic_fallback",
    )


def parse_whiteboard_deck(
    raw_response: str,
    *,
    topic: str,
    correlation_id: str,
    extra_secrets: tuple[str, ...] = (),
) -> WhiteboardDeck:
    """Parse structured LLM output into a bounded, redacted WhiteboardDeck with safe fallback."""
    fallback_topic = (
        _clean_text(topic, max_len=120, extra_secrets=extra_secrets) or "Explicación visual"
    )
    payload = _extract_json_payload(raw_response)
    if payload is None:
        return build_fallback_whiteboard_deck(
            fallback_topic,
            raw_response,
            correlation_id=correlation_id,
            extra_secrets=extra_secrets,
        )

    raw_slides = payload.get("slides")
    if not isinstance(raw_slides, list) or not raw_slides:
        return build_fallback_whiteboard_deck(
            fallback_topic,
            raw_response,
            correlation_id=correlation_id,
            extra_secrets=extra_secrets,
        )

    deck_topic = (
        _clean_text(payload.get("topic"), max_len=120, extra_secrets=extra_secrets)
        or fallback_topic
    )
    summary = _clean_text(
        payload.get("summary"),
        max_len=400,
        extra_secrets=extra_secrets,
        strip_markup=True,
    )

    slides: list[WhiteboardSlide] = []
    for raw_slide in raw_slides:
        if len(slides) >= MAX_WHITEBOARD_SLIDES:
            break
        if not isinstance(raw_slide, dict):
            continue
        coerced = _coerce_slide(
            raw_slide,
            slide_index=len(slides),
            fallback_topic=deck_topic,
            extra_secrets=extra_secrets,
        )
        if coerced is not None:
            slides.append(coerced)

    if not slides:
        return build_fallback_whiteboard_deck(
            deck_topic,
            raw_response,
            correlation_id=correlation_id,
            extra_secrets=extra_secrets,
        )

    return WhiteboardDeck(
        deck_id=f"deck-{uuid.uuid4()}",
        correlation_id=correlation_id,
        topic=deck_topic,
        summary=summary,
        slides=tuple(slides),
        active_slide_index=0,
        source="llm_structured",
    )


def redact_whiteboard_deck(
    deck: WhiteboardDeck,
    *,
    extra_secrets: tuple[str, ...] = (),
) -> WhiteboardDeck:
    """Defense-in-depth re-redaction of every text field inside an existing WhiteboardDeck."""
    redacted_slides: list[WhiteboardSlide] = []
    for slide in deck.slides:
        redacted_nodes = tuple(
            node.model_copy(
                update={
                    "label": _clean_text(node.label, max_len=60, extra_secrets=extra_secrets)
                    or "Concepto",
                    "detail": _clean_text(node.detail, max_len=120, extra_secrets=extra_secrets),
                    "group": _clean_text(node.group, max_len=40, extra_secrets=extra_secrets),
                }
            )
            for node in slide.nodes
        )
        redacted_edges = tuple(
            edge.model_copy(
                update={
                    "label": _clean_text(edge.label, max_len=40, extra_secrets=extra_secrets),
                }
            )
            for edge in slide.edges
        )
        redacted_callouts = tuple(
            c
            for c in (
                _clean_text(item, max_len=140, extra_secrets=extra_secrets)
                for item in slide.callouts
            )
            if c
        )
        redacted_slides.append(
            slide.model_copy(
                update={
                    "title": _clean_text(slide.title, max_len=100, extra_secrets=extra_secrets)
                    or "Lámina",
                    "subtitle": _clean_text(
                        slide.subtitle, max_len=160, extra_secrets=extra_secrets
                    ),
                    "nodes": redacted_nodes,
                    "edges": redacted_edges,
                    "callouts": redacted_callouts,
                    "narration": _clean_text(
                        slide.narration,
                        max_len=MAX_SPEECH_CHUNK_CHARS,
                        extra_secrets=extra_secrets,
                        strip_markup=True,
                    )
                    or "Explicación disponible en la pizarra.",
                }
            )
        )
    return deck.model_copy(
        update={
            "topic": _clean_text(deck.topic, max_len=120, extra_secrets=extra_secrets)
            or "Pizarra",
            "summary": _clean_text(deck.summary, max_len=400, extra_secrets=extra_secrets),
            "slides": tuple(redacted_slides),
        }
    )


def format_whiteboard_chat_summary(deck: WhiteboardDeck) -> str:
    """Format a clean human-readable chat summary for a generated whiteboard (zero raw JSON)."""
    lines: list[str] = [f"Pizarra lista: {deck.topic} ({len(deck.slides)} láminas)."]
    if deck.summary:
        lines.append(deck.summary)
    for slide in deck.slides:
        lines.append(f"{slide.slide_index + 1}. {slide.title}")
    lines.append("Puedes explorar los diagramas en la pestaña Pizarra.")
    return "\n".join(lines)
