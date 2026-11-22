# Sourcing Desk: one container runs the API, the worker and the built dashboard.

FROM node:22-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SOURCING_DB=/data/sourcing.db FRONTEND_DIST=/app/frontend/dist PORT=10000
WORKDIR /app
COPY backend/ ./backend/
RUN pip install --no-cache-dir ./backend
COPY --from=frontend /app/frontend/dist ./frontend/dist
COPY deploy/start.sh ./start.sh
RUN mkdir -p /data && useradd --create-home app && chown -R app /data && chmod +x start.sh
USER app
EXPOSE 10000
CMD ["./start.sh"]
