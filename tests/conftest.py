"""Test-wide isolation: the database, and the credentials.

Two things leak into a test run from the developer's own machine, and both were
found the hard way.

**The database.** `Store()` defaults to `data/reconciliation.db` -- the database
a developer's running app is using. Without this, `pytest` writes its runs and
its exceptions into that queue: open the app after a test run and it is full of
rows nobody created.

**The credentials.** `config.py` calls `load_dotenv()` at import, so a populated
`.env` puts a real Anthropic key and real Razorpay keys into `SETTINGS`. Every
`run_bundled()` in the suite then resolves its residual through the *live* API:
a full run made hundreds of billed calls and took over ten minutes, where the
same suite takes about ten seconds hermetically. Someone who clones this repo,
follows the README's setup section, and runs `pytest` should not be charged for
it, and should not have their test results depend on a network.

So the suite runs with those variables explicitly empty. `load_dotenv()` does
not overwrite a variable that is already present in `os.environ`, so setting
them here -- before `finance_controller` is imported anywhere -- wins over
`.env` without touching the file.

This weakens nothing. Tests that exercise the LLM or Razorpay paths inject their
own settings with `dataclasses.replace(...)` and `monkeypatch.setattr(...)`,
which is what a test asserting on a credential-dependent branch has to do
anyway. The live paths are verified deliberately, by `scripts/verify_llm.py` and
`scripts/verify_razorpay.py`, not as a side effect of running the suite.

All of this has to happen at import time, before any module reads the
environment or `get_store()` memoises a connection.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="afc-tests-"))
os.environ.setdefault("RECON_DB_PATH", str(_TMP / "test.db"))

# assignment, not setdefault: the point is to beat .env, not defer to it
for _var in ("ANTHROPIC_API_KEY", "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"):
    os.environ[_var] = ""
