FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GHOST_HOME=/data/workspaces \
    GHOST_IN_DOCKER=1 GHOST_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[web]"

RUN useradd --create-home --uid 1000 ghost && mkdir /data && chown ghost /data
USER ghost
VOLUME /data

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "ghost.web.app:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080"]
