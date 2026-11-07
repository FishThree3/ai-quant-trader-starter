"""Migrations run only on a new temporary SQLite database, never the user's DB."""

import os
import subprocess
import sys


def test_upgrade_and_schema_match(tmp_path):
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{(tmp_path / 'schema.db').as_posix()}",
    }
    for args in (
        ("upgrade", "head"),
        ("check",),
        ("downgrade", "30f78d5ef935"),
        ("upgrade", "head"),
    ):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
