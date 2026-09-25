# The LLM harness: talks to the Doom API server and to Ollama.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY doom_harness ./doom_harness
RUN pip install .

# Playbooks (the instructions given to the model) live outside the package so they
# can be edited and mounted without rebuilding: see docker-compose.yml.
COPY playbooks ./playbooks
ENV HARNESS_PLAYBOOK_DIR=/app/playbooks \
    HARNESS_RUNS_DIR=/app/runs

ENTRYPOINT ["python", "-m", "doom_harness"]
CMD ["play"]
