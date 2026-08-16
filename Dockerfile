FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY pyproject.toml ./
COPY aihot_bridge ./aihot_bridge

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 appuser

USER appuser

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn aihot_bridge.main:app --host 0.0.0.0 --port ${PORT}"]
