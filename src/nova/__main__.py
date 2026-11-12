"""Module execution entry point (`python -m nova`) delegating to `nova-desktop`."""

from __future__ import annotations

from nova.adapters.desktop_launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
