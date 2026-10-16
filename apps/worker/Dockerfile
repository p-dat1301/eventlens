FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
COPY . .
RUN uv sync --frozen --package eventlens-worker --no-dev

ENTRYPOINT ["uv", "run", "--package", "eventlens-worker", "eventlens-worker"]
CMD ["--help"]
