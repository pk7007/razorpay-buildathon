"""Test-wide isolation.

`Store()` defaults to `data/reconciliation.db` — the database a developer's
running app is using. Without this, `pytest` writes its runs and its exceptions
into that queue: open the app after a test run and it is full of rows nobody
created. Pointing the store at a throwaway file for the whole session keeps the
suite from touching real state.

This has to happen at import time, before any module reads the environment or
`get_store()` memoises a connection.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="afc-tests-"))
os.environ.setdefault("RECON_DB_PATH", str(_TMP / "test.db"))
