"""Shared test fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.database.database import Database
from app.paths import AppPaths, reset_paths_cache


@pytest.fixture
def app_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AppPaths]:
    """An isolated application data directory for a single test."""
    root = tmp_path / "AppData"
    monkeypatch.setenv("CARTWRIGHT_DATA_DIR", str(root))
    reset_paths_cache()
    paths = AppPaths(root=root).ensure()
    yield paths
    reset_paths_cache()


@pytest.fixture
def database(app_paths: AppPaths) -> Iterator[Database]:
    """A migrated, empty database on disk."""
    db = Database(app_paths.database_file, backups_dir=app_paths.backups_dir)
    db.migrate()
    yield db
    db.close_all()


@pytest.fixture(autouse=True)
def _no_real_user_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fail loudly if a test would touch the real %LOCALAPPDATA% directory.

    Without this, a missing fixture silently writes into the developer's (or a
    user's) live data directory.
    """
    if "CARTWRIGHT_DATA_DIR" not in os.environ:
        monkeypatch.setenv("CARTWRIGHT_DATA_DIR", str(tmp_path / "guard"))
        reset_paths_cache()
