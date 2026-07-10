# adscrub: pipeline CLI in one image. No web frontend yet (see docs/PLAN.md M4).
#
# Every pipeline stage is a one-shot command, e.g.:
#   docker compose run --rm adscrub ingest
#   docker compose run --rm adscrub chapters

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

# dependency layer first: rebuilds only when the lockfile changes
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    ADSCRUB_DB=/app/data/adscrub.db

# gosu drops from root to the unprivileged `adscrub` user after the entrypoint fixes
# ownership of /app/data. uid/gid 568 matches TrueNAS SCALE's standard "apps" account,
# same convention as hark/tiltmeter.
RUN apt-get update && apt-get install -y --no-install-recommends gosu ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 568 adscrub \
    && useradd --system --uid 568 --gid 568 --no-create-home adscrub

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME ["/app/data"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["adscrub", "stats"]
