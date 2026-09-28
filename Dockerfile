FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
# Unraid convention: nobody:users (99:100). Override with --user if needed.
RUN groupadd -g 100 -o users 2>/dev/null; useradd -u 99 -g 100 -o -M -s /usr/sbin/nologin app
COPY --from=build --chown=99:100 /app /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    JFCURATOR_CONFIG=/config/config.yaml \
    JFCURATOR_INTERVAL=3600
VOLUME /config
USER 99:100
ENTRYPOINT ["jfcurator"]
CMD ["daemon"]
