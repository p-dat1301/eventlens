FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev

EXPOSE 8001
CMD ["uv", "run", "--no-dev", "uvicorn", "backend.rag.api_main:app", "--host", "0.0.0.0", "--port", "8001"]
