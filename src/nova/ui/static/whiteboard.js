/**
 * Pure Deterministic SVG Whiteboard Engine & HTML Exporter (NOVA-019 AC-3, AC-7, AC-9, NFR-1).
 * Zero runtime dependencies, strict CSP 'self', universal XSS escaping, 16 local vector drawings.
 */
const HTML_ESCAPE_MAP = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
};
function esc(raw) {
    return raw.replace(/[&<>"']/g, (ch) => HTML_ESCAPE_MAP[ch] ?? ch);
}
const ACCENT_THEMES = {
    blue: {
        fill: "#0f233c",
        stroke: "#38bdf8",
        badgeBg: "#0c3456",
        text: "#f0f9ff",
        subtext: "#bae6fd",
    },
    emerald: {
        fill: "#0d2b24",
        stroke: "#34d399",
        badgeBg: "#0f3d32",
        text: "#ecfdf5",
        subtext: "#a7f3d0",
    },
    amber: {
        fill: "#2e210a",
        stroke: "#fbbf24",
        badgeBg: "#45300d",
        text: "#fffbeb",
        subtext: "#fde68a",
    },
    violet: {
        fill: "#211638",
        stroke: "#a78bfa",
        badgeBg: "#312054",
        text: "#f5f3ff",
        subtext: "#ddd6fe",
    },
    rose: {
        fill: "#311420",
        stroke: "#fb7185",
        badgeBg: "#491c2f",
        text: "#fff1f2",
        subtext: "#fecdd3",
    },
    slate: {
        fill: "#1a2232",
        stroke: "#94a3b8",
        badgeBg: "#263247",
        text: "#f8fafc",
        subtext: "#cbd5e1",
    },
};
const VISUAL_KIND_LABELS = {
    flowchart: "Diagrama de flujo",
    mindmap: "Mapa mental",
    architecture: "Arquitectura",
    sketch_cards: "Pizarra ilustrada",
};
function resolveAccent(accent) {
    if (accent in ACCENT_THEMES) {
        return ACCENT_THEMES[accent];
    }
    return ACCENT_THEMES.blue;
}
function resolveVisualKind(kind) {
    if (kind === "flowchart" ||
        kind === "mindmap" ||
        kind === "architecture" ||
        kind === "sketch_cards") {
        return kind;
    }
    return "sketch_cards";
}
function resolveShape(shape) {
    if (shape === "box" ||
        shape === "pill" ||
        shape === "decision" ||
        shape === "database" ||
        shape === "actor" ||
        shape === "cloud" ||
        shape === "note") {
        return shape;
    }
    return "box";
}
/**
 * Curated 16 local vector illustrations ("dibujitos") drawn in a 24x24 viewBox.
 * Pure static SVG paths — zero untrusted markup.
 */
export function renderWhiteboardIconPaths(icon, stroke) {
    const s = esc(stroke);
    const safeIcon = icon === "spark" ||
        icon === "brain" ||
        icon === "gear" ||
        icon === "database" ||
        icon === "cloud" ||
        icon === "shield" ||
        icon === "user" ||
        icon === "code" ||
        icon === "network" ||
        icon === "rocket" ||
        icon === "bulb" ||
        icon === "check" ||
        icon === "warning" ||
        icon === "layers" ||
        icon === "flow" ||
        icon === "book"
        ? icon
        : "spark";
    switch (safeIcon) {
        case "brain":
            return (`<path d="M9 5a3 3 0 0 0-3 3v.5A2.5 2.5 0 0 0 5 13a2.5 2.5 0 0 0 1.5 4.2A2.8 2.8 0 0 0 11 19V5a2 2 0 0 0-2 0Z" fill="none" stroke="${s}" stroke-width="1.8" stroke-linecap="round"/>` +
                `<path d="M15 5a3 3 0 0 1 3 3v.5A2.5 2.5 0 0 1 19 13a2.5 2.5 0 0 1-1.5 4.2A2.8 2.8 0 0 1 13 19V5a2 2 0 0 1 2 0Z" fill="none" stroke="${s}" stroke-width="1.8" stroke-linecap="round"/>` +
                `<path d="M8.5 10h2M13.5 10h2M8.5 14h2M13.5 14h2" stroke="${s}" stroke-width="1.4" stroke-linecap="round"/>`);
        case "gear":
            return (`<circle cx="12" cy="12" r="3.4" fill="none" stroke="${s}" stroke-width="1.8"/>` +
                `<path d="M12 3v2.4M12 18.6V21M3 12h2.4M18.6 12H21M5.6 5.6l1.7 1.7M16.7 16.7l1.7 1.7M18.4 5.6l-1.7 1.7M7.3 16.7l-1.7 1.7" stroke="${s}" stroke-width="1.8" stroke-linecap="round"/>` +
                `<circle cx="12" cy="12" r="6.5" fill="none" stroke="${s}" stroke-width="1.3" stroke-dasharray="2.5 2.5"/>`);
        case "database":
            return (`<ellipse cx="12" cy="6.2" rx="6.8" ry="2.6" fill="none" stroke="${s}" stroke-width="1.8"/>` +
                `<path d="M5.2 6.2v11.6c0 1.4 3 2.6 6.8 2.6s6.8-1.2 6.8-2.6V6.2" fill="none" stroke="${s}" stroke-width="1.8"/>` +
                `<path d="M5.2 12c0 1.4 3 2.6 6.8 2.6s6.8-1.2 6.8-2.6" fill="none" stroke="${s}" stroke-width="1.5"/>`);
        case "cloud":
            return (`<path d="M6.8 18h10.8a3.6 3.6 0 0 0 .5-7.15A5.2 5.2 0 0 0 8.1 8.9 4.2 4.2 0 0 0 6.8 18Z" fill="none" stroke="${s}" stroke-width="1.8" stroke-linejoin="round"/>`);
        case "shield":
            return (`<path d="M12 3.2 5.2 6v5.4c0 4.5 2.9 8.1 6.8 9.6 3.9-1.5 6.8-5.1 6.8-9.6V6L12 3.2Z" fill="none" stroke="${s}" stroke-width="1.8" stroke-linejoin="round"/>` +
                `<path d="m9.2 12.1 2 2 3.8-4.1" fill="none" stroke="${s}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`);
        case "user":
            return (`<circle cx="12" cy="8.2" r="3.4" fill="none" stroke="${s}" stroke-width="1.8"/>` +
                `<path d="M5.2 19.2c.8-3.4 3.4-5.2 6.8-5.2s6 1.8 6.8 5.2" fill="none" stroke="${s}" stroke-width="1.8" stroke-linecap="round"/>`);
        case "code":
            return (`<path d="m8.5 7.5-4.2 4.5 4.2 4.5M15.5 7.5l4.2 4.5-4.2 4.5M13.4 5.8l-2.8 12.4" fill="none" stroke="${s}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`);
        case "network":
            return (`<circle cx="12" cy="5.5" r="2.2" fill="none" stroke="${s}" stroke-width="1.7"/>` +
                `<circle cx="5.8" cy="17.5" r="2.2" fill="none" stroke="${s}" stroke-width="1.7"/>` +
                `<circle cx="18.2" cy="17.5" r="2.2" fill="none" stroke="${s}" stroke-width="1.7"/>` +
                `<path d="M10.9 7.4 7 15.5M13.1 7.4l3.9 8.1M8.1 17.5h7.8" stroke="${s}" stroke-width="1.6" stroke-linecap="round"/>`);
        case "rocket":
            return (`<path d="M14.5 4.2c2.8-.4 4.8.2 5.3.7.5.5 1.1 2.5.7 5.3l-5.6 5.6-6-6 5.6-5.6Z" fill="none" stroke="${s}" stroke-width="1.8" stroke-linejoin="round"/>` +
                `<circle cx="15" cy="9" r="1.4" fill="${s}"/>` +
                `<path d="M8.9 9.8 5.5 11l3 3M14.9 15.8l-1.2 3.4-3-3M5 19l2.4-2.4" fill="none" stroke="${s}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`);
        case "bulb":
            return (`<path d="M9.2 15.6c-1.8-1.1-2.8-2.9-2.8-5a5.6 5.6 0 1 1 11.2 0c0 2.1-1 3.9-2.8 5-.5.4-.8 1-.8 1.6H10c0-.6-.3-1.2-.8-1.6Z" fill="none" stroke="${s}" stroke-width="1.8" stroke-linejoin="round"/>` +
                `<path d="M9.8 19.6h4.4M10.5 21.5h3" stroke="${s}" stroke-width="1.7" stroke-linecap="round"/>`);
        case "check":
            return (`<circle cx="12" cy="12" r="8.2" fill="none" stroke="${s}" stroke-width="1.8"/>` +
                `<path d="m8.2 12.2 2.6 2.7 5.2-5.4" fill="none" stroke="${s}" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>`);
        case "warning":
            return (`<path d="M12 4.2 3.8 18.8h16.4L12 4.2Z" fill="none" stroke="${s}" stroke-width="1.8" stroke-linejoin="round"/>` +
                `<path d="M12 9.5v4.3" stroke="${s}" stroke-width="1.9" stroke-linecap="round"/>` +
                `<circle cx="12" cy="16.4" r="1" fill="${s}"/>`);
        case "layers":
            return (`<path d="M12 4.5 3.8 8.8 12 13l8.2-4.2L12 4.5Z" fill="none" stroke="${s}" stroke-width="1.8" stroke-linejoin="round"/>` +
                `<path d="M3.8 13l8.2 4.2 8.2-4.2M3.8 16.8 12 21l8.2-4.2" fill="none" stroke="${s}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>`);
        case "flow":
            return (`<rect x="3.5" y="4.5" width="6.5" height="5" rx="1.4" fill="none" stroke="${s}" stroke-width="1.7"/>` +
                `<rect x="14" y="14.5" width="6.5" height="5" rx="1.4" fill="none" stroke="${s}" stroke-width="1.7"/>` +
                `<path d="M10 7h4a3 3 0 0 1 3 3v4.5M14.8 12.4 17 14.6l2.2-2.2" fill="none" stroke="${s}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>`);
        case "book":
            return (`<path d="M4.5 5.8c2.4-.8 5.2-.5 7.5 1 2.3-1.5 5.1-1.8 7.5-1v12.4c-2.4-.8-5.2-.5-7.5 1-2.3-1.5-5.1-1.8-7.5-1V5.8Z" fill="none" stroke="${s}" stroke-width="1.8" stroke-linejoin="round"/>` +
                `<path d="M12 6.8v12.4" stroke="${s}" stroke-width="1.6"/>`);
        case "spark":
        default:
            return (`<path d="M12 3.2 14 9.2 20 11.2 14 13.2 12 19.2 10 13.2 4 11.2 10 9.2 12 3.2Z" fill="none" stroke="${s}" stroke-width="1.8" stroke-linejoin="round"/>` +
                `<circle cx="18.2" cy="5.6" r="1.1" fill="${s}"/>`);
    }
}
export function wrapSvgLabel(text, maxChars, maxLines) {
    const clean = text.trim().replace(/\s+/g, " ");
    if (!clean) {
        return [];
    }
    const words = clean.split(" ");
    const lines = [];
    let current = "";
    for (const word of words) {
        const piece = word.length > maxChars ? `${word.slice(0, Math.max(2, maxChars - 1))}…` : word;
        const candidate = current ? `${current} ${piece}` : piece;
        if (candidate.length <= maxChars) {
            current = candidate;
        }
        else {
            if (current) {
                lines.push(current);
            }
            current = piece;
            if (lines.length === maxLines - 1) {
                break;
            }
        }
    }
    if (current && lines.length < maxLines) {
        lines.push(current);
    }
    // Add ellipsis if content was truncated before the end
    const consumed = lines.join(" ");
    if (lines.length === maxLines && consumed.length < clean.length) {
        const lastIdx = lines.length - 1;
        const last = lines[lastIdx] ?? "";
        if (!last.endsWith("…")) {
            lines[lastIdx] =
                last.length >= maxChars
                    ? `${last.slice(0, Math.max(1, maxChars - 1))}…`
                    : `${last}…`;
        }
    }
    return lines;
}
function renderNodeShape(pos, theme) {
    const shape = resolveShape(pos.node.shape);
    const x = pos.cx - pos.w / 2;
    const y = pos.cy - pos.h / 2;
    const fill = esc(theme.fill);
    const stroke = esc(theme.stroke);
    if (shape === "pill") {
        const r = Math.min(pos.h / 2, 26);
        return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${pos.w.toFixed(1)}" height="${pos.h.toFixed(1)}" rx="${r.toFixed(1)}" fill="${fill}" stroke="${stroke}" stroke-width="2" />`;
    }
    if (shape === "decision") {
        const pts = [
            `${pos.cx.toFixed(1)},${y.toFixed(1)}`,
            `${(x + pos.w).toFixed(1)},${pos.cy.toFixed(1)}`,
            `${pos.cx.toFixed(1)},${(y + pos.h).toFixed(1)}`,
            `${x.toFixed(1)},${pos.cy.toFixed(1)}`,
        ].join(" ");
        return `<polygon points="${pts}" fill="${fill}" stroke="${stroke}" stroke-width="2" stroke-linejoin="round" />`;
    }
    if (shape === "database") {
        const rx = pos.w / 2;
        const ry = 10;
        const topY = y + ry;
        const botY = y + pos.h - ry;
        return (`<path d="M ${x.toFixed(1)} ${topY.toFixed(1)} L ${x.toFixed(1)} ${botY.toFixed(1)} A ${rx.toFixed(1)} ${ry} 0 0 0 ${(x + pos.w).toFixed(1)} ${botY.toFixed(1)} L ${(x + pos.w).toFixed(1)} ${topY.toFixed(1)} Z" fill="${fill}" stroke="${stroke}" stroke-width="2" />` +
            `<ellipse cx="${pos.cx.toFixed(1)}" cy="${topY.toFixed(1)}" rx="${rx.toFixed(1)}" ry="${ry}" fill="${esc(theme.badgeBg)}" stroke="${stroke}" stroke-width="2" />`);
    }
    if (shape === "note") {
        const fold = 14;
        const x2 = x + pos.w;
        const y2 = y + pos.h;
        return (`<path d="M ${x.toFixed(1)} ${y.toFixed(1)} L ${(x2 - fold).toFixed(1)} ${y.toFixed(1)} L ${x2.toFixed(1)} ${(y + fold).toFixed(1)} L ${x2.toFixed(1)} ${y2.toFixed(1)} L ${x.toFixed(1)} ${y2.toFixed(1)} Z" fill="${fill}" stroke="${stroke}" stroke-width="1.9" stroke-linejoin="round" />` +
            `<path d="M ${(x2 - fold).toFixed(1)} ${y.toFixed(1)} L ${(x2 - fold).toFixed(1)} ${(y + fold).toFixed(1)} L ${x2.toFixed(1)} ${(y + fold).toFixed(1)}" fill="none" stroke="${stroke}" stroke-width="1.5" />`);
    }
    if (shape === "cloud") {
        return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${pos.w.toFixed(1)}" height="${pos.h.toFixed(1)}" rx="22" fill="${fill}" stroke="${stroke}" stroke-width="2" stroke-dasharray="6 3" />`;
    }
    return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${pos.w.toFixed(1)}" height="${pos.h.toFixed(1)}" rx="12" fill="${fill}" stroke="${stroke}" stroke-width="2" />`;
}
function renderStandardNode(pos) {
    const theme = resolveAccent(pos.node.accent);
    const shapeSvg = renderNodeShape(pos, theme);
    const left = pos.cx - pos.w / 2;
    const top = pos.cy - pos.h / 2;
    const iconCx = left + 22;
    const iconCy = top + pos.h / 2;
    const textX = left + 40;
    const maxLabelChars = Math.max(12, Math.floor((pos.w - 48) / 7.2));
    const labelLines = wrapSvgLabel(pos.node.label, maxLabelChars, 2);
    const detailLines = pos.node.detail ? wrapSvgLabel(pos.node.detail, maxLabelChars + 4, 2) : [];
    const totalTextLines = labelLines.length + detailLines.length;
    let cursorY = pos.cy - ((totalTextLines - 1) * 14) / 2 + 4;
    const textParts = [];
    for (const line of labelLines) {
        textParts.push(`<text x="${textX.toFixed(1)}" y="${cursorY.toFixed(1)}" fill="${esc(theme.text)}" font-size="12.5" font-weight="700" font-family="system-ui, sans-serif">${esc(line)}</text>`);
        cursorY += 15;
    }
    for (const line of detailLines) {
        textParts.push(`<text x="${textX.toFixed(1)}" y="${cursorY.toFixed(1)}" fill="${esc(theme.subtext)}" font-size="10.5" font-weight="400" font-family="system-ui, sans-serif">${esc(line)}</text>`);
        cursorY += 13;
    }
    return (`<g class="wb-node" data-node-id="${esc(pos.node.id)}">` +
        shapeSvg +
        `<circle cx="${iconCx.toFixed(1)}" cy="${iconCy.toFixed(1)}" r="13.5" fill="${esc(theme.badgeBg)}" stroke="${esc(theme.stroke)}" stroke-opacity="0.55" stroke-width="1.2" />` +
        `<g transform="translate(${(iconCx - 9.5).toFixed(1)}, ${(iconCy - 9.5).toFixed(1)}) scale(0.79)">${renderWhiteboardIconPaths(pos.node.icon, theme.stroke)}</g>` +
        textParts.join("") +
        `</g>`);
}
function renderEdgesSvg(edges, posMap, curved, arrowId) {
    const parts = [];
    for (const edge of edges) {
        const src = posMap.get(edge.source_id);
        const tgt = posMap.get(edge.target_id);
        if (!src || !tgt) {
            continue;
        }
        const dx = tgt.cx - src.cx;
        const dy = tgt.cy - src.cy;
        const dist = Math.hypot(dx, dy) || 1;
        const ux = dx / dist;
        const uy = dy / dist;
        // Anchor near bounding box perimeter for crisp arrow termination
        const startOffset = Math.min(dist * 0.4, Math.abs(ux) > Math.abs(uy) ? src.w / 2 : src.h / 2);
        const endOffset = Math.min(dist * 0.4, Math.abs(ux) > Math.abs(uy) ? tgt.w / 2 : tgt.h / 2);
        const x1 = src.cx + ux * startOffset;
        const y1 = src.cy + uy * startOffset;
        const x2 = tgt.cx - ux * (endOffset + 4);
        const y2 = tgt.cy - uy * (endOffset + 4);
        const dashAttr = edge.style === "dashed" ? ` stroke-dasharray="6 4"` : "";
        let d;
        let midX;
        let midY;
        if (curved) {
            const cx1 = x1 + (x2 - x1) * 0.5;
            const cy1 = y1;
            const cx2 = x1 + (x2 - x1) * 0.5;
            const cy2 = y2;
            d = `M ${x1.toFixed(1)} ${y1.toFixed(1)} C ${cx1.toFixed(1)} ${cy1.toFixed(1)}, ${cx2.toFixed(1)} ${cy2.toFixed(1)}, ${x2.toFixed(1)} ${y2.toFixed(1)}`;
            midX = (x1 + x2) / 2;
            midY = (y1 + y2) / 2;
        }
        else {
            d = `M ${x1.toFixed(1)} ${y1.toFixed(1)} L ${x2.toFixed(1)} ${y2.toFixed(1)}`;
            midX = (x1 + x2) / 2;
            midY = (y1 + y2) / 2;
        }
        parts.push(`<path d="${d}" fill="none" stroke="#7dd3fc" stroke-opacity="0.72" stroke-width="2"${dashAttr} marker-end="url(#${arrowId})" />`);
        if (edge.label.trim()) {
            const safeLbl = edge.label.trim().slice(0, 26);
            const pillW = Math.max(44, safeLbl.length * 6.5 + 14);
            parts.push(`<rect x="${(midX - pillW / 2).toFixed(1)}" y="${(midY - 10).toFixed(1)}" width="${pillW.toFixed(1)}" height="19" rx="9" fill="#0b1322" stroke="#38bdf8" stroke-opacity="0.45" stroke-width="1" />` +
                `<text x="${midX.toFixed(1)}" y="${(midY + 3).toFixed(1)}" text-anchor="middle" fill="#e0f2fe" font-size="10" font-weight="600" font-family="system-ui, sans-serif">${esc(safeLbl)}</text>`);
        }
    }
    return parts.join("");
}
function layoutFlowchart(slide, arrowId) {
    const nodes = slide.nodes;
    const inDegree = new Map();
    const adj = new Map();
    for (const n of nodes) {
        inDegree.set(n.id, 0);
        adj.set(n.id, []);
    }
    for (const e of slide.edges) {
        if (adj.has(e.source_id) && inDegree.has(e.target_id) && e.source_id !== e.target_id) {
            adj.get(e.source_id)?.push(e.target_id);
            inDegree.set(e.target_id, (inDegree.get(e.target_id) ?? 0) + 1);
        }
    }
    // Cycle-safe topological layering
    const depth = new Map();
    const queue = [];
    for (const n of nodes) {
        if ((inDegree.get(n.id) ?? 0) === 0) {
            depth.set(n.id, 0);
            queue.push(n.id);
        }
    }
    if (queue.length === 0 && nodes[0]) {
        depth.set(nodes[0].id, 0);
        queue.push(nodes[0].id);
    }
    let head = 0;
    while (head < queue.length) {
        const curr = queue[head];
        head += 1;
        if (!curr) {
            continue;
        }
        const currDepth = depth.get(curr) ?? 0;
        for (const nxt of adj.get(curr) ?? []) {
            const nextDepth = Math.min(currDepth + 1, nodes.length - 1);
            if (!depth.has(nxt)) {
                depth.set(nxt, nextDepth);
                queue.push(nxt);
            }
        }
    }
    for (let i = 0; i < nodes.length; i += 1) {
        const n = nodes[i];
        if (n && !depth.has(n.id)) {
            depth.set(n.id, Math.floor(i / 2));
        }
    }
    const layers = new Map();
    for (const n of nodes) {
        const d = depth.get(n.id) ?? 0;
        const bucket = layers.get(d) ?? [];
        bucket.push(n);
        layers.set(d, bucket);
    }
    const sortedLayerKeys = [...layers.keys()].sort((a, b) => a - b);
    // Split any overly wide layer (> 3 items) into clean sub-rows
    const rows = [];
    for (const key of sortedLayerKeys) {
        const items = layers.get(key) ?? [];
        for (let i = 0; i < items.length; i += 3) {
            rows.push(items.slice(i, i + 3));
        }
    }
    const width = 760;
    const nodeW = 210;
    const nodeH = 74;
    const rowGap = 44;
    const padY = 34;
    const height = Math.max(260, padY * 2 + rows.length * nodeH + Math.max(0, rows.length - 1) * rowGap);
    const posMap = new Map();
    rows.forEach((row, rIdx) => {
        const cy = padY + rIdx * (nodeH + rowGap) + nodeH / 2;
        const count = row.length;
        const gapX = 28;
        const totalW = count * nodeW + Math.max(0, count - 1) * gapX;
        const startX = (width - totalW) / 2 + nodeW / 2;
        row.forEach((node, cIdx) => {
            posMap.set(node.id, {
                node,
                cx: startX + cIdx * (nodeW + gapX),
                cy,
                w: nodeW,
                h: nodeH,
            });
        });
    });
    const edgesSvg = renderEdgesSvg(slide.edges, posMap, true, arrowId);
    const nodesSvg = [...posMap.values()].map(renderStandardNode).join("");
    return { width, height, body: `${edgesSvg}${nodesSvg}` };
}
function layoutMindmap(slide) {
    const nodes = slide.nodes;
    const width = 780;
    const height = 470;
    const centerX = width / 2;
    const centerY = height / 2;
    const root = nodes[0];
    if (!root) {
        return { width, height, body: "" };
    }
    const children = nodes.slice(1);
    const posMap = new Map();
    posMap.set(root.id, {
        node: root,
        cx: centerX,
        cy: centerY,
        w: 204,
        h: 76,
    });
    const radiusX = children.length <= 4 ? 245 : 270;
    const radiusY = children.length <= 4 ? 150 : 172;
    const childW = children.length <= 6 ? 192 : 172;
    const childH = 66;
    children.forEach((child, idx) => {
        const angle = (idx * 2 * Math.PI) / Math.max(1, children.length) - Math.PI / 2;
        const cx = centerX + Math.cos(angle) * radiusX;
        const cy = centerY + Math.sin(angle) * radiusY;
        posMap.set(child.id, { node: child, cx, cy, w: childW, h: childH });
    });
    // Connect explicit edges; if none provided, radiate from root
    const effectiveEdges = slide.edges.length > 0
        ? slide.edges
        : children.map((ch) => ({
            source_id: root.id,
            target_id: ch.id,
            label: "",
            style: "solid",
        }));
    const spokes = [];
    for (const edge of effectiveEdges) {
        const src = posMap.get(edge.source_id);
        const tgt = posMap.get(edge.target_id);
        if (!src || !tgt) {
            continue;
        }
        const theme = resolveAccent(tgt.node.accent);
        const ctrlX = (src.cx + tgt.cx) / 2;
        const ctrlY = src.cy + (tgt.cy - src.cy) * 0.15;
        const dashAttr = edge.style === "dashed" ? ` stroke-dasharray="5 4"` : "";
        spokes.push(`<path d="M ${src.cx.toFixed(1)} ${src.cy.toFixed(1)} Q ${ctrlX.toFixed(1)} ${ctrlY.toFixed(1)}, ${tgt.cx.toFixed(1)} ${tgt.cy.toFixed(1)}" fill="none" stroke="${esc(theme.stroke)}" stroke-opacity="0.72" stroke-width="2.4"${dashAttr} />`);
    }
    const rootHalo = `<ellipse cx="${centerX}" cy="${centerY}" rx="116" ry="50" fill="none" stroke="#38bdf8" stroke-opacity="0.28" stroke-width="2" stroke-dasharray="4 4" />`;
    const nodesSvg = [...posMap.values()].map(renderStandardNode).join("");
    return { width, height, body: `${spokes.join("")}${rootHalo}${nodesSvg}` };
}
function layoutArchitecture(slide, arrowId) {
    const nodes = slide.nodes;
    const groups = new Map();
    nodes.forEach((n, idx) => {
        const gName = n.group.trim() || `Capa ${Math.floor(idx / 3) + 1}`;
        const list = groups.get(gName) ?? [];
        list.push(n);
        groups.set(gName, list);
    });
    const laneEntries = [...groups.entries()];
    const width = 780;
    const lanePadX = 28;
    const laneHeaderH = 26;
    const nodeH = 72;
    const laneGap = 24;
    const padY = 24;
    const posMap = new Map();
    const laneRects = [];
    let cursorY = padY;
    for (const [laneTitle, laneNodes] of laneEntries) {
        const subRows = [];
        for (let i = 0; i < laneNodes.length; i += 3) {
            subRows.push(laneNodes.slice(i, i + 3));
        }
        const laneH = laneHeaderH + subRows.length * nodeH + Math.max(0, subRows.length - 1) * 18 + 24;
        laneRects.push(`<g class="wb-lane">` +
            `<rect x="${lanePadX}" y="${cursorY}" width="${width - lanePadX * 2}" height="${laneH}" rx="14" fill="rgba(15, 23, 42, 0.55)" stroke="#334155" stroke-width="1.5" stroke-dasharray="6 4" />` +
            `<text x="${lanePadX + 14}" y="${cursorY + 19}" fill="#94a3b8" font-size="11" font-weight="700" letter-spacing="0.06em" font-family="system-ui, sans-serif">${esc(laneTitle.toUpperCase())}</text>` +
            `</g>`);
        subRows.forEach((row, rIdx) => {
            const cy = cursorY + laneHeaderH + 12 + rIdx * (nodeH + 18) + nodeH / 2;
            const nodeW = Math.min(212, Math.floor((width - lanePadX * 2 - 36 - (row.length - 1) * 20) / row.length));
            const totalW = row.length * nodeW + Math.max(0, row.length - 1) * 20;
            const startX = (width - totalW) / 2 + nodeW / 2;
            row.forEach((node, cIdx) => {
                posMap.set(node.id, {
                    node,
                    cx: startX + cIdx * (nodeW + 20),
                    cy,
                    w: nodeW,
                    h: nodeH,
                });
            });
        });
        cursorY += laneH + laneGap;
    }
    const height = Math.max(280, cursorY);
    const edgesSvg = renderEdgesSvg(slide.edges, posMap, false, arrowId);
    const nodesSvg = [...posMap.values()].map(renderStandardNode).join("");
    return { width, height, body: `${laneRects.join("")}${edgesSvg}${nodesSvg}` };
}
function layoutSketchCards(slide, arrowId) {
    const nodes = slide.nodes;
    const cols = nodes.length <= 2 ? nodes.length : nodes.length <= 4 ? 2 : 3;
    const width = 780;
    const padX = 28;
    const padY = 26;
    const gapX = 20;
    const gapY = 20;
    const cardW = Math.floor((width - padX * 2 - gapX * (cols - 1)) / Math.max(1, cols));
    const cardH = 148;
    const rowCount = Math.ceil(nodes.length / Math.max(1, cols));
    const height = Math.max(220, padY * 2 + rowCount * cardH + Math.max(0, rowCount - 1) * gapY);
    const posMap = new Map();
    const cardsSvg = [];
    nodes.forEach((node, idx) => {
        const col = idx % cols;
        const row = Math.floor(idx / cols);
        const x = padX + col * (cardW + gapX);
        const y = padY + row * (cardH + gapY);
        const cx = x + cardW / 2;
        const cy = y + cardH / 2;
        posMap.set(node.id, { node, cx, cy, w: cardW, h: cardH });
        const theme = resolveAccent(node.accent);
        const maxTitleChars = Math.max(14, Math.floor((cardW - 32) / 7.5));
        const titleLines = wrapSvgLabel(node.label, maxTitleChars, 2);
        const detailLines = node.detail ? wrapSvgLabel(node.detail, maxTitleChars + 5, 3) : [];
        let textY = y + 78;
        const textEls = [];
        for (const line of titleLines) {
            textEls.push(`<text x="${cx.toFixed(1)}" y="${textY.toFixed(1)}" text-anchor="middle" fill="${esc(theme.text)}" font-size="13" font-weight="700" font-family="system-ui, sans-serif">${esc(line)}</text>`);
            textY += 16;
        }
        textY += 2;
        for (const line of detailLines) {
            textEls.push(`<text x="${cx.toFixed(1)}" y="${textY.toFixed(1)}" text-anchor="middle" fill="${esc(theme.subtext)}" font-size="10.8" font-family="system-ui, sans-serif">${esc(line)}</text>`);
            textY += 13.5;
        }
        cardsSvg.push(`<g class="wb-sketch-card" data-node-id="${esc(node.id)}">` +
            `<rect x="${x}" y="${y}" width="${cardW}" height="${cardH}" rx="16" fill="${esc(theme.fill)}" stroke="${esc(theme.stroke)}" stroke-width="2" />` +
            `<path d="M ${(x + 18).toFixed(1)} ${(y + 62).toFixed(1)} Q ${cx.toFixed(1)} ${(y + 57).toFixed(1)}, ${(x + cardW - 18).toFixed(1)} ${(y + 62).toFixed(1)}" fill="none" stroke="${esc(theme.stroke)}" stroke-opacity="0.35" stroke-width="1.4" stroke-linecap="round" />` +
            `<circle cx="${cx.toFixed(1)}" cy="${(y + 32).toFixed(1)}" r="20" fill="${esc(theme.badgeBg)}" stroke="${esc(theme.stroke)}" stroke-width="1.6" />` +
            `<g transform="translate(${(cx - 12).toFixed(1)}, ${(y + 20).toFixed(1)})">${renderWhiteboardIconPaths(node.icon, theme.stroke)}</g>` +
            textEls.join("") +
            `</g>`);
    });
    const edgesSvg = renderEdgesSvg(slide.edges, posMap, true, arrowId);
    return { width, height, body: `${edgesSvg}${cardsSvg.join("")}` };
}
export function renderWhiteboardSlideSvg(slide) {
    const kind = resolveVisualKind(slide.visual_kind);
    const gridId = `wb-dot-grid-${slide.slide_index}`;
    const arrowId = `wb-arrow-${slide.slide_index}`;
    const layout = kind === "flowchart"
        ? layoutFlowchart(slide, arrowId)
        : kind === "mindmap"
            ? layoutMindmap(slide)
            : kind === "architecture"
                ? layoutArchitecture(slide, arrowId)
                : layoutSketchCards(slide, arrowId);
    const ariaLabel = `Pizarra: ${slide.title} (${VISUAL_KIND_LABELS[kind]})`;
    return (`<svg class="wb-canvas-svg" viewBox="0 0 ${layout.width} ${layout.height}" role="img" aria-label="${esc(ariaLabel)}" xmlns="http://www.w3.org/2000/svg">` +
        `<defs>` +
        `<pattern id="${gridId}" width="22" height="22" patternUnits="userSpaceOnUse">` +
        `<circle cx="2" cy="2" r="1.15" fill="#334155" fill-opacity="0.55" />` +
        `</pattern>` +
        `<marker id="${arrowId}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">` +
        `<path d="M 0 1.5 L 9 5 L 0 8.5 z" fill="#7dd3fc" />` +
        `</marker>` +
        `</defs>` +
        `<rect width="${layout.width}" height="${layout.height}" rx="14" fill="#09101d" />` +
        `<rect width="${layout.width}" height="${layout.height}" rx="14" fill="url(#${gridId})" />` +
        layout.body +
        `</svg>`);
}
export function renderWhiteboardView(model) {
    const deck = model.activeWhiteboard ?? null;
    const history = model.whiteboardHistory ?? [];
    if (!deck && model.status === "loading") {
        return `
      <section class="wb-shell wb-shell--empty" data-view-status="loading" role="status" aria-live="polite" aria-label="Cargando pizarra visual">
        <div class="wb-empty-board">
          <h2 class="wb-empty-title">Cargando pizarra visual…</h2>
          <p class="wb-empty-copy">Sincronizando las láminas activas con el puente local de Nova.</p>
        </div>
      </section>
    `.trim();
    }
    if (!deck || deck.slides.length === 0) {
        return `
      <section class="wb-shell wb-shell--empty" data-view-status="empty" aria-label="Pizarra visual vacía">
        <div class="wb-empty-board">
          <div class="wb-empty-doodle" aria-hidden="true">
            <svg viewBox="0 0 120 72" width="120" height="72">
              <rect x="6" y="6" width="108" height="60" rx="12" fill="#0f172a" stroke="#38bdf8" stroke-width="1.8" stroke-dasharray="5 4" />
              <g transform="translate(22, 24)">${renderWhiteboardIconPaths("bulb", "#fbbf24")}</g>
              <path d="M 50 36 L 70 36" stroke="#38bdf8" stroke-width="2" stroke-linecap="round" />
              <g transform="translate(74, 24)">${renderWhiteboardIconPaths("flow", "#34d399")}</g>
            </svg>
          </div>
          <h2 class="wb-empty-title">Pizarra interactiva de Nova</h2>
          <p class="wb-empty-copy">
            Pide a Nova que te explique cualquier tema por voz o chat para armar láminas con dibujitos, diagramas y mapas mentales en vivo.
          </p>
          <div class="wb-example-chips">
            <button type="button" class="btn btn--secondary wb-example-btn" data-action="whiteboard-example" data-prompt="Explícame en la pizarra cómo funciona OAuth2">
              OAuth2 paso a paso
            </button>
            <button type="button" class="btn btn--secondary wb-example-btn" data-action="whiteboard-example" data-prompt="Ármame un mapa mental sobre arquitectura hexagonal">
              Mapa mental: Hexagonal
            </button>
            <button type="button" class="btn btn--secondary wb-example-btn" data-action="whiteboard-example" data-prompt="Dibújame en la pizarra cómo funciona el event loop de Python">
              Event loop visual
            </button>
          </div>
        </div>
      </section>
    `.trim();
    }
    const safeIdx = Math.max(0, Math.min(deck.active_slide_index, deck.slides.length - 1));
    const slide = deck.slides[safeIdx] ?? deck.slides[0];
    if (!slide) {
        return "";
    }
    const kind = resolveVisualKind(slide.visual_kind);
    const kindLabel = VISUAL_KIND_LABELS[kind];
    const svgHtml = renderWhiteboardSlideSvg(slide);
    const historySelectHtml = history.length > 1
        ? `
        <label class="sr-only" for="nova-wb-history-select">Cambiar pizarra reciente</label>
        <select id="nova-wb-history-select" class="wb-history-select" data-action="select-whiteboard-deck" aria-label="Pizarras recientes en memoria">
          ${history
            .map((h) => `<option value="${esc(h.deck_id)}"${h.deck_id === deck.deck_id ? " selected" : ""}>${esc(h.topic)} (${h.slide_count})</option>`)
            .join("")}
        </select>
      `
        : "";
    const calloutsHtml = slide.callouts.length > 0
        ? `
        <ul class="wb-callouts" aria-label="Notas clave de la lámina">
          ${slide.callouts.map((c) => `<li class="wb-callout-note">${esc(c)}</li>`).join("")}
        </ul>
      `
        : "";
    const slideDotsHtml = deck.slides
        .map((s, i) => `
        <button
          type="button"
          class="wb-slide-pill ${i === safeIdx ? "wb-slide-pill--active" : ""}"
          data-action="whiteboard-goto"
          data-slide-index="${i}"
          aria-label="Ir a lámina ${i + 1}: ${esc(s.title)}"
          ${i === safeIdx ? 'aria-current="step"' : ""}
        >
          ${i + 1}
        </button>
      `)
        .join("");
    return `
    <section class="wb-shell" data-view-status="success" data-deck-id="${esc(deck.deck_id)}" data-slide-index="${safeIdx}" aria-label="Pizarra visual: ${esc(deck.topic)}">
      <div class="wb-toolbar">
        <div class="wb-toolbar-meta">
          <span class="wb-kind-badge">${esc(kindLabel)}</span>
          <h2 class="wb-topic-title" title="${esc(deck.topic)}">${esc(deck.topic)}</h2>
        </div>
        <div class="wb-toolbar-actions">
          ${historySelectHtml}
          <button
            type="button"
            class="btn btn--secondary wb-action-btn"
            data-action="whiteboard-open-browser"
            title="Abrir pizarra en ventana completa del navegador"
            aria-label="Abrir pizarra en navegador"
          >
            ⤢ Navegador
          </button>
          <button
            type="button"
            class="btn btn--ghost wb-action-btn"
            data-action="whiteboard-export-html"
            title="Exportar presentación autocontenida en HTML"
            aria-label="Exportar HTML"
          >
            ↓ HTML
          </button>
        </div>
      </div>

      <div class="wb-slide-header">
        <h3 class="wb-slide-title">${esc(slide.title)}</h3>
        ${slide.subtitle ? `<p class="wb-slide-subtitle">${esc(slide.subtitle)}</p>` : ""}
      </div>

      <div class="wb-stage" role="region" aria-label="Tablero visual de Nova">
        ${svgHtml}
      </div>

      ${calloutsHtml}

      <div class="wb-narration-box" aria-live="polite">
        <span class="wb-narration-tag">Explicación</span>
        <p class="wb-narration-text">${esc(slide.narration)}</p>
      </div>

      <nav class="wb-nav" aria-label="Navegación de láminas">
        <button
          type="button"
          class="btn btn--secondary wb-nav-btn"
          data-action="whiteboard-prev"
          aria-label="Lámina anterior"
          ${safeIdx <= 0 ? "disabled" : ""}
        >
          ← Anterior
        </button>
        <div class="wb-slide-pills" role="group" aria-label="Selector de láminas">
          ${slideDotsHtml}
        </div>
        <button
          type="button"
          class="btn btn--secondary wb-nav-btn"
          data-action="whiteboard-next"
          aria-label="Lámina siguiente"
          ${safeIdx >= deck.slides.length - 1 ? "disabled" : ""}
        >
          Siguiente →
        </button>
      </nav>
    </section>
  `.trim();
}
/**
 * Generate a 100% self-contained, script-free HTML5 snapshot of all whiteboard slides (AC-7 & AC-9).
 */
export function buildExportableWhiteboardHtml(deck) {
    const safeTopic = esc(deck.topic);
    const safeSummary = deck.summary ? `<p class="summary">${esc(deck.summary)}</p>` : "";
    const slidesHtml = deck.slides
        .map((slide, idx) => {
        const kindLabel = VISUAL_KIND_LABELS[resolveVisualKind(slide.visual_kind)];
        const svg = renderWhiteboardSlideSvg(slide);
        const callouts = slide.callouts.length > 0
            ? `<ul class="callouts">${slide.callouts.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`
            : "";
        return `
        <article class="slide">
          <header class="slide-head">
            <span class="badge">${esc(kindLabel)} · Lámina ${idx + 1} / ${deck.slides.length}</span>
            <h2>${esc(slide.title)}</h2>
            ${slide.subtitle ? `<p class="sub">${esc(slide.subtitle)}</p>` : ""}
          </header>
          <div class="stage">${svg}</div>
          ${callouts}
          <p class="narration">${esc(slide.narration)}</p>
        </article>
      `.trim();
    })
        .join("\n");
    return [
        "<!doctype html>",
        '<html lang="es">',
        "<head>",
        '  <meta charset="utf-8" />',
        '  <meta name="viewport" content="width=device-width, initial-scale=1" />',
        `  <title>Nova Pizarra — ${safeTopic}</title>`,
        "  <style>",
        "    :root { color-scheme: dark; }",
        "    body { margin: 0; padding: 2rem 1.25rem; background: #060b14; color: #f8fafc; font-family: system-ui, -apple-system, sans-serif; }",
        "    .wrap { max-width: 960px; margin: 0 auto; display: flex; flex-direction: column; gap: 1.5rem; }",
        "    h1 { margin: 0 0 0.4rem; font-size: 1.65rem; color: #38bdf8; }",
        "    .summary { margin: 0; color: #cbd5e1; line-height: 1.5; }",
        "    .slide { background: #0f172a; border: 1px solid #1e293b; border-radius: 16px; padding: 1.25rem; display: flex; flex-direction: column; gap: 0.85rem; }",
        "    .badge { display: inline-block; font-size: 0.75rem; font-weight: 700; color: #38bdf8; text-transform: uppercase; letter-spacing: 0.05em; }",
        "    .slide-head h2 { margin: 0.25rem 0 0.15rem; font-size: 1.2rem; }",
        "    .sub { margin: 0; color: #94a3b8; font-size: 0.9rem; }",
        "    .stage svg { width: 100%; height: auto; display: block; border-radius: 12px; }",
        "    .callouts { list-style: none; padding: 0; margin: 0; display: flex; flex-wrap: wrap; gap: 0.5rem; }",
        "    .callouts li { background: #1e293b; border-left: 3px solid #fbbf24; border-radius: 8px; padding: 0.45rem 0.7rem; font-size: 0.85rem; color: #fef3c7; }",
        "    .narration { margin: 0; padding: 0.75rem 0.9rem; background: #0b1322; border-radius: 10px; color: #e2e8f0; line-height: 1.55; font-size: 0.92rem; }",
        "  </style>",
        "</head>",
        "<body>",
        '  <main class="wrap">',
        `    <header><h1>${safeTopic}</h1>${safeSummary}</header>`,
        slidesHtml,
        "  </main>",
        "</body>",
        "</html>",
    ].join("\n");
}
