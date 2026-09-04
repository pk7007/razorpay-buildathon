FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY web/ ./web/
COPY data/ ./data/
COPY scripts/ ./scripts/
RUN pip install --no-cache-dir -e .

# the benchmark datasets are committed; regenerate anyway to prove determinism
RUN python scripts/make_datasets.py

EXPOSE 8000
# Shell form on purpose, so ${PORT} expands. Render (and most PaaS) assign a
# port through $PORT and expect the process to bind it; the exec form that
# used to be here hardcoded 8000, so the platform health check could hit a
# port nothing was listening on. The :-8000 default keeps a local
# `docker run -p 8000:8000` working, and CI unchanged.
HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import os,urllib.request,sys; p=os.getenv('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/api/health').status==200 else 1)"

CMD python -m uvicorn finance_controller.api:app --host 0.0.0.0 --port ${PORT:-8000}
