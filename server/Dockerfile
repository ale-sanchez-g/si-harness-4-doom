# Doom game server: headless ViZDoom (bundles the engine + Freedoom WADs) behind FastAPI.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY doom_server ./doom_server
RUN pip install . \
 && mkdir -p /wads \
 && python -c "import vizdoom, scipy, fastapi; print('vizdoom', vizdoom.__version__)"

# Drop doom.wad / doom2.wad into /wads (docker-compose mounts ./wads) to play the
# commercial games; Freedoom Phase 1 + 2 are bundled and work out of the box.
ENV DOOM_HOST=0.0.0.0 \
    DOOM_PORT=8000 \
    DOOM_WAD_DIR=/wads

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" || exit 1

CMD ["python", "-m", "doom_server"]
