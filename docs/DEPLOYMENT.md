# Deployment

The service is a single stateless Python process. No database, no queue, no
object store, no session state — a reconciliation is a pure function of the
batch it is handed. That makes deployment unusually boring, which is the point.

## Docker (recommended)

```bash
docker build -t finance-controller .
docker run -p 8000:8000 finance-controller
```

Then `http://localhost:8000`.

The image is `python:3.11-slim`, runs `uvicorn` on `:8000`, and has a
`HEALTHCHECK` hitting `/api/health`. It regenerates the benchmark datasets at
build time, so a broken generator fails the build rather than shipping.

> **Verified in CI, not by hand.** Docker is not installed on the machine this
> was written on. The `docker build · run · reconcile` job builds the image,
> starts the container and reconciles a dataset through it on every push to
> `main`; it is green. If you change the Dockerfile, watch that job rather than
> trusting a local build you may not be able to run.

## Render

`render.yaml` is a ready blueprint:

```yaml
services:
  - type: web
    runtime: docker
    healthCheckPath: /api/health
```

Connect the repo at [render.com](https://render.com) → **New → Blueprint**.
It picks up `render.yaml`, builds the Dockerfile, and health-checks
`/api/health`. Free tier works; expect a cold start of ~30 s after idle, so hit
the URL once before a live demo.

## Any Procfile host (Railway, Heroku, Fly)

```
web: python -m uvicorn finance_controller.api:app --host 0.0.0.0 --port ${PORT:-8000}
```

`$PORT` is honoured, which is all most platforms need.

## Bare process

```bash
pip install -r requirements.txt
pip install -e .
python -m uvicorn finance_controller.api:app --host 0.0.0.0 --port 8000
```

## Environment variables

**Every one is optional.** With no environment at all the service runs and every
published number is reproducible — the residual resolver falls back to a
deterministic heuristic. See `.env.example`.

| Variable | Effect if unset |
| --- | --- |
| `ANTHROPIC_API_KEY` | resolver runs the heuristic instead of the LLM |
| `LLM_MODEL` | `claude-sonnet-5` |
| `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` | `20` / `2` |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | the test-mode pull is unavailable; files still work |
| `AMOUNT_TOLERANCE_PAISE` | `100` (₹1 of cross-source slack) |
| `SETTLEMENT_LAG_DAYS` | `2` (Razorpay T+2) |
| `RESOLVER_ACCEPT_THRESHOLD` | `0.72` |

Use **test-mode** Razorpay keys only. This service has no reason to see a live
key, and nothing in it should ever be pointed at production financial data.

## Scaling

Vertical first: the engine is single-process CPU-bound and does ~9,000–13,500
records/sec (see [`METRICS.md`](METRICS.md)). A merchant-month is a few thousand
rows, so one small instance serves many merchants.

If you do run replicas, two things are per-process and you should know it:

- **Rate limiting** is in-memory, so the limit becomes per-replica. That is a
  deliberate trade — a Redis dependency would buy nothing for a stateless demo.
  Put the limit at the load balancer if you need it global.
- **The `/api/evaluation` and `/api/benchmark` caches** are per-process, so the
  first request to each replica is slow. Harmless; they are pure functions.

Nothing else is shared, so replicas need no coordination.

## Production checklist

Things this deliberately does **not** have, which you would want before putting
it in front of real merchants:

- [ ] **Authentication** — currently none, because there is nothing to protect.
      The moment it touches real merchant data this becomes mandatory.
- [ ] **TLS** — terminate at the platform or a reverse proxy.
- [ ] **CORS** — no policy is set because the API is same-origin with its own UI.
      Add an explicit allowlist before another origin calls it.
- [ ] **Distributed rate limiting** — see above.
- [ ] **Log shipping** — logs go to stdout with request ids; nothing collects them.
- [ ] **Real settlement recon report** as a join key, which is what would fix the
      ambiguous-split limitation described in `METRICS.md`.

## What is already handled

- Security headers on every response: CSP, `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy`
- Rate limiting (30 POST/min/IP), 8 MB per file, 100k rows per request
- Unhandled errors return a request id, never a stack trace
- `Cache-Control: no-cache` on assets so a redeploy cannot serve a stale bundle
- gzip above 1 KB
- Uploaded filenames sanitised out of error bodies
- No secrets in the image; no `.env` in the repo or its history
