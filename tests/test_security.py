"""Security regressions worth a test.

Scope note: this service has no accounts, no database and no stored data, so
there is no authn/authz surface to test — a run is a pure function of the batch
it is handed. What is left is input handling, path safety, resource limits and
not leaking internals, which is what these cover.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from finance_controller import api
from finance_controller.api import app
from finance_controller.ingest import load_dataset


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    api._hits.clear()
    yield
    api._hits.clear()


TRAVERSALS = [
    "../../../etc/passwd",
    "..\\..\\windows\\win.ini",
    "clean/../../..",
    "%2e%2e%2f",
    "/etc/passwd",
    ".",
    "..",
]


@pytest.mark.parametrize("bad", TRAVERSALS)
def test_dataset_name_cannot_escape_the_datasets_dir(client, bad):
    assert client.post("/api/reconcile", json={"dataset": bad}).status_code == 404


@pytest.mark.parametrize("bad", TRAVERSALS)
def test_load_dataset_validates_its_own_argument(bad):
    """Defence in depth: the loader joins its argument onto a path, so it must
    not rely on every caller having validated first."""
    with pytest.raises((ValueError, FileNotFoundError)):
        load_dataset(bad)


def test_no_stack_traces_or_paths_leak_in_errors(client):
    r = client.post(
        "/api/reconcile/upload",
        files={"payments": ("x.json", io.BytesIO(b"{broken"), "application/json")},
    )
    assert r.status_code == 422
    body = r.text.lower()
    for leak in ("traceback", "site-packages", "c:\\", "/home/", ".py\", line"):
        assert leak not in body, f"error body leaked {leak!r}"


def test_oversized_file_is_refused_before_parsing(client):
    big = b"id,amount\n" + b"x,1\n" * 3_000_000       # > 8 MB
    r = client.post(
        "/api/reconcile/upload",
        files={"payments": ("big.csv", io.BytesIO(big), "text/csv")},
    )
    assert r.status_code == 413


def test_security_headers_present_on_html(client):
    r = client.get("/")
    csp = r.headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_health_does_not_leak_secrets(client):
    body = client.get("/api/health").json()
    # it may say WHETHER a key is configured, never anything derived from one
    assert set(body) == {"status", "version", "resolver", "razorpay_configured", "datasets"}
    assert isinstance(body["razorpay_configured"], bool)
    assert "key" not in str(body).lower()


# --------------------------------------------------------------------------- #
# Resource exhaustion — found by attacking the running service, not by reading
# --------------------------------------------------------------------------- #


def test_the_expensive_reads_are_rate_limited_too(client):
    """/api/evaluation reruns the held-out sweep and /api/benchmark reconciles
    26,000 records. The limiter originally covered POST only, which left the two
    most expensive endpoints in the service open to anyone who could send a GET.
    """
    api._hits.clear()
    codes = [client.get("/api/evaluation").status_code for _ in range(api._RATE_LIMIT + 6)]
    assert 429 in codes, (
        f"{len(codes)} requests to the most expensive endpoint, none limited"
    )
    api._hits.clear()


def test_cheap_reads_are_not_rate_limited(client):
    """The console itself makes several reads per screen. Limiting those would
    lock out an ordinary user clicking around."""
    api._hits.clear()
    codes = [client.get("/api/exceptions/summary").status_code
             for _ in range(api._RATE_LIMIT + 6)]
    assert 429 not in codes, "browsing the worklist trips the limiter"
    api._hits.clear()


def test_a_cold_cache_is_computed_once_not_once_per_caller(client):
    """`if key not in _CACHE` is not a cache on a cold instance -- it is a
    stampede. Four concurrent first requests each paid the full cost."""
    import threading
    import time as _time

    api._CACHE.clear()
    api._hits.clear()
    t = _time.perf_counter()
    client.get("/api/evaluation")
    alone = max(_time.perf_counter() - t, 0.05)

    api._CACHE.clear()
    api._hits.clear()
    started = _time.perf_counter()
    threads = [threading.Thread(target=lambda: client.get("/api/evaluation"))
               for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    together = _time.perf_counter() - started

    assert together < alone * 2.5, (
        f"4 concurrent cold requests took {together:.2f}s against {alone:.2f}s for "
        f"one — each caller is recomputing instead of sharing the result"
    )
    api._hits.clear()
