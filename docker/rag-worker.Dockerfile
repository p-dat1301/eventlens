FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev

ENTRYPOINT ["uv", "run", "--no-dev", "eventlens-rag-worker"]
CMD ["--help"]
