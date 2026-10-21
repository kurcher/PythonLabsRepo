# syntax=docker/dockerfile:1.7
# findex web service — multi-stage, uv-based, runs as a non-root user.
#   docker build -t findex .
#   docker run --rm -p 8000:8000 findex                     # demo index baked into the image
#   docker run --rm -p 8000:8000 -e INDEX_PATH=/idx/index.bin -v "$PWD/build:/idx" findex

ARG PYTHON=3.13

# 1) dependencies + the findex package into /srv/.venv (no dev tools, no source tree needed later)
FROM python:${PYTHON}-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /srv
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

# 2) a small demo corpus + index, built at image-build time (the corpus never lives in git)
FROM build AS corpus
ARG BOOKS=20
COPY scripts/fetch_gutenberg.py scripts/split_chapters.py ./scripts/
RUN .venv/bin/python scripts/fetch_gutenberg.py --limit ${BOOKS} --delay 0.5 \
 && .venv/bin/python scripts/split_chapters.py \
 && .venv/bin/findex index data/chapters --out data/index.bin --positions --executor serial \
 && rm -rf data/gutenberg

# 3) runtime: just Python, the venv and the data — no uv, no compilers, no build cache
FROM python:${PYTHON}-slim AS runtime
RUN useradd --create-home --uid 10001 findex
WORKDIR /srv
COPY --from=build --chown=findex /srv/.venv ./.venv
COPY --from=corpus --chown=findex /srv/data ./data
USER findex
ENV PATH=/srv/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    INDEX_PATH=/srv/data/index.bin \
    PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=60s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=2)"
# shell form so $PORT (set by Render/Railway/Fly) is expanded at start-up
CMD findex serve --host 0.0.0.0 --port ${PORT}
