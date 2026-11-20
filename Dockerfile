FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends awscli curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY conf ./conf
COPY scripts ./scripts
COPY .streamlit ./.streamlit
ENV GP_ROOT=/app GP_DATA_DIR=/data PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["/app/scripts/run-task.sh"]
