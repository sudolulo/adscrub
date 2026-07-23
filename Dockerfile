# adscrub: pipeline CLI + feed server in one image.
#
# Default command serves the cleaned feed(s) over HTTP; every pipeline stage is
# also available as a one-shot command, e.g.:
#   docker compose run --rm adscrub ingest
#   docker compose run --rm adscrub chapters
#   docker compose run --rm adscrub transcribe
#   docker compose run --rm adscrub cut
#
# Build with --build-arg GPU=1 (or `docker compose -f compose.yaml -f compose.gpu.yaml
# build`) to pull in the cuBLAS/cuDNN extra for faster-whisper's CUDA path — only
# needed on a host that actually passes a GPU through (see CLAUDE.md).

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
ARG GPU=0

# dependency layer first: rebuilds only when the lockfile changes
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$GPU" = "1" ]; then uv sync --frozen --no-dev --no-install-project --extra gpu; \
    else uv sync --frozen --no-dev --no-install-project; fi

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$GPU" = "1" ]; then uv sync --frozen --no-dev --extra gpu; \
    else uv sync --frozen --no-dev; fi

ENV PATH="/app/.venv/bin:$PATH" \
    ADSCRUB_DB=/app/data/adscrub.db \
    ADSCRUB_DATA_DIR=/app/data

# gosu drops from root to the unprivileged `adscrub` user after the entrypoint fixes
# ownership of /app/data. uid/gid 568 matches TrueNAS SCALE's standard "apps" account,
# same convention as hark/tiltmeter.
# libchromaprint-tools provides fpcalc, which the `fingerprint`/`discover` tiers shell out to.
# Without it those tiers are not broken so much as INERT: fpcalc_available() returns False, the
# command exits with a tidy message, and an image that looks healthy silently never matches an
# ad. A missing system binary is the whole difference between the cheap tiers running and not.
RUN apt-get update && apt-get install -y --no-install-recommends gosu ffmpeg libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 568 adscrub \
    && useradd --system --uid 568 --gid 568 --no-create-home adscrub

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME ["/app/data"]
EXPOSE 8711

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["adscrub", "serve", "--bind", "0.0.0.0:8711"]
