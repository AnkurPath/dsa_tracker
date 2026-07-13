# DSA Tracker — production image
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# System deps (bcrypt / timezone data for IST)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata

# Install dependencies first (better layer caching)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# App source
COPY app ./app
COPY templates ./templates
COPY static ./static
COPY main.py ./

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# Bind all interfaces inside the container; SQLite file lives under /data when mounted
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
