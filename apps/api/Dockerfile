FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
COPY . .
RUN uv sync --frozen --package eventlens-api --no-dev

EXPOSE 8000
CMD ["uv", "run", "--package", "eventlens-api", "uvicorn", "eventlens_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
