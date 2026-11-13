"""Desktop native window host using pywebview for frameless transparent widget."""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger("nova.desktop_window")


def run_window(
    url: str,
    *,
    frameless: bool = True,
    on_top: bool = True,
    transparent: bool = True,
    width: int = 360,
    height: int = 640,
) -> int:
    """Launch loopback UI inside a desktop frameless window with transparency."""
    try:
        import webview

        webview.create_window(
            "Nova",
            url=url,
            width=width,
            height=height,
            frameless=frameless,
            on_top=on_top,
            transparent=transparent,
            easy_drag=True,
        )
        webview.start(gui="qt", debug=False)
        return 0
    except Exception as exc:
        logger.warning("Native webview window unavailable (%s)", exc)
        return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m nova.adapters.desktop_window <loopback_url>", file=sys.stderr)
        sys.exit(2)
    sys.exit(run_window(sys.argv[1]))
