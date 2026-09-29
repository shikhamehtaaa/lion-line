FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# Install dependencies first (cached layer), exactly as pinned in uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
ENV PORT=8080
CMD ["uv", "run", "--frozen", "--no-sync", "python", "app.py"]
