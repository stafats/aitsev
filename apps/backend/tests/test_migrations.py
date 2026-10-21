"""The Alembic migrations must build exactly the schema the ORM models describe."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _alembic(db_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env={"AITSEV_DATABASE_URL": db_url, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_upgrade_check_and_downgrade(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    up = _alembic(url, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    check = _alembic(url, "check")
    assert check.returncode == 0, check.stdout + check.stderr
    down = _alembic(url, "downgrade", "base")
    assert down.returncode == 0, down.stderr
    again = _alembic(url, "upgrade", "head")
    assert again.returncode == 0, again.stderr
