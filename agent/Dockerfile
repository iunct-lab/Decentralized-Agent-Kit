FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --no-install-project

# Copy agent definition
COPY dak_agent ./dak_agent
COPY skills ./skills
COPY provider_skills ./provider_skills
COPY profiles ./profiles
COPY agent_config.yaml ./agent_config.yaml
COPY README.md .
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Install project
RUN uv sync

# Expose port
EXPOSE 8000

# Use entrypoint to dynamically generate agent.json from AGENT_PUBLIC_URL
ENTRYPOINT ["/app/entrypoint.sh"]
# ADK の app に /approvals を足した入口（dak_agent/server.py）。セッションの保存先は
# SESSION_SERVICE_URI（docker-compose.yml）。google-adk v2 は async SQLAlchemy を使うため
# async ドライバ(asyncpg)の URL が要る
CMD ["uv", "run", "uvicorn", "dak_agent.server:app", "--host", "0.0.0.0", "--port", "8000"]
