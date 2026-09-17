# The research API, packaged so it runs the same on a laptop, a university server or a cloud VM.
#
# **Why this exists:** the service currently runs from a working copy on one Windows laptop, with
# a `.env` beside it and a PowerShell script to start it. That is fine for development and is not
# something anyone can hand to an IT department. Everything the service needs is already
# environment-driven (`api/app/config.py`), so the only thing missing was a way to ship it.
#
# **What is deliberately not in here:** `llama-server`. It is its own image
# (`ghcr.io/ggml-org/llama.cpp:server`) in docker-compose.yml, reached over `LLAMA_SERVER_URL`,
# which is the same seam the app already uses. Keeping them apart means the API can be rebuilt
# and redeployed without disturbing a process that takes minutes to read 2.4 GB of weights.

FROM python:3.12-slim AS base

# uv, because the lockfile is uv's and `pip install -r` would silently resolve differently.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /srv/quitsmoke

# Dependencies before source, so a code change does not re-resolve the whole environment.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# Non-root. The service writes only to the export and backup directories, and those are volumes.
RUN useradd --system --uid 10001 quitsmoke \
    && mkdir -p /srv/quitsmoke/exports /srv/quitsmoke/backups \
    && chown -R quitsmoke:quitsmoke /srv/quitsmoke
USER quitsmoke

# 0.0.0.0 inside the container only. What is reachable from outside is the published port, which
# is the host's decision - the default in `config.py` stays 127.0.0.1 so a bare `uvicorn` on a
# laptop is still bound to loopback.
ENV API_HOST=0.0.0.0 \
    API_PORT=8000

EXPOSE 8000

# The database check, not just "the process is alive": the API starts fine with an unreachable
# database and then fails every request, which is the failure worth restarting on.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys,json; \
r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)); \
sys.exit(0 if r.get('database')=='ok' else 1)"

CMD ["uv", "run", "uvicorn", "api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
