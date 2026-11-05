"""
tests/test_file_permissions.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The Fly image copies ``backend/app`` as-is and runs as ``rainuser``: a module
that only its owner can read (e.g. 600, the default of some editors and tools)
raises ``PermissionError`` on import in production. ``fly deploy`` uploads the
working tree, so the on-disk mode is what counts, not git's.
"""

from __future__ import annotations

import stat
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "backend" / "app"


def test_backend_modules_are_world_readable() -> None:
    """Every ``backend/app/*.py`` must be readable by other users (644 or looser)."""
    unreadable = [
        f"{p.name} ({stat.S_IMODE(p.stat().st_mode):o})"
        for p in sorted(APP_DIR.glob("*.py"))
        if not p.stat().st_mode & stat.S_IROTH
    ]
    assert not unreadable, f"chmod 644 these files: {', '.join(unreadable)}"
