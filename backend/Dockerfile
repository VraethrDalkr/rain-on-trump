# ---------- base image ----------
FROM python:3.12-slim AS base
WORKDIR /app

# Install runtime deps
RUN apt-get update \
  && apt-get install -y --no-install-recommends git curl \
  && rm -rf /var/lib/apt/lists/*

# ---------- builder: build wheels ----------
FROM base AS builder
RUN apt-get update \
  && apt-get install -y --no-install-recommends build-essential \
  && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---------- final image ----------
FROM base
# Install Python deps from pre-built wheels
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl

# Copy app code
COPY backend/app ./app

# Run as non-root
RUN adduser --disabled-login --gecos "" rainuser \
  && chown -R rainuser:rainuser /app
USER rainuser

# Healthcheck endpoint (you’ll need /healthz in your FastAPI app)
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s \
  CMD curl -f http://localhost:8080/healthz || exit 1

# Final startup
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
