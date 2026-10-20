# Trida AI Blueprint — Open-Model Coding Agent on Baseten
# Multi-stage build; the runtime image runs as a non-root user.

FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml constraints.txt README.md ./
COPY src ./src
RUN pip install --no-cache-dir -r constraints.txt \
 && pip install --no-cache-dir --no-deps .

FROM python:3.12-slim
RUN useradd --create-home appuser
WORKDIR /app
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/uvicorn /usr/local/bin/coding-agent /usr/local/bin/coding-agent-demo /usr/local/bin/
COPY workshop ./workshop
COPY evals/gold ./evals/gold
COPY migrations ./migrations
COPY profiles ./profiles
COPY prices ./prices
COPY taxonomy ./taxonomy
COPY skills ./skills
RUN mkdir -p /app/data && chown -R appuser:appuser /app/data
USER appuser
ENV SQLITE_PATH=/app/data/codingagent.db \
    GOLD_DIR=/app/evals/gold \
    MIGRATIONS_DIR=/app/migrations \
    PROFILES_DIR=/app/profiles \
    DEPLOYMENT_PROFILES_DIR=/app/profiles/deployment \
    PRICES_DIR=/app/prices \
    TAXONOMY_DIR=/app/taxonomy \
    SKILLS_DIR=/app/skills \
    TARGETS_DIR=/app/data/targets \
    RUNS_DIR=/app/data/runs \
    ARTIFACTS_DIR=/app/data/artifacts
EXPOSE 8000
CMD ["uvicorn", "open_model_coding_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
