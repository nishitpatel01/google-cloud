# Multi-stage production container for Risk & Contagion Intelligence (ACC-05)
# Stage 1 compiles the React 19 + TypeScript SPA; Stage 2 runs FastAPI + Uvicorn as non-root.

FROM node:22-slim AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/tsconfig.json frontend/vite.config.ts frontend/index.html ./
RUN npm install --no-package-lock --no-audit --no-fund
COPY frontend/src ./src
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    PORT=8080

WORKDIR /app

# Install pinned Python runtime dependencies
COPY backend/requirements.lock /app/backend/requirements.lock
RUN pip install --no-cache-dir -r /app/backend/requirements.lock

# Copy application code, curated public packs, recorded Replay fixtures, and compiled SPA
COPY backend/app /app/backend/app
COPY packs /app/packs
COPY data /app/data
COPY infra/sql /app/infra/sql
COPY --from=frontend-builder /build/frontend/dist /app/frontend/dist

# Run strictly as unprivileged non-root user
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER 10001

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
