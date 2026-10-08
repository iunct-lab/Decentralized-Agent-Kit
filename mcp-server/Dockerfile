FROM python:3.10-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy dependency files
COPY pyproject.toml uv.lock .python-version ./

# Install dependencies
RUN uv sync --frozen --no-install-project

# Copy source code
COPY main.py sandbox.py command_sandbox.py ./
# docker CLI (client only) for SANDBOX_MODE=docker. It reaches a daemon only when
# the host's Docker socket is mounted, which docker-compose.yml does not do.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY README.md .

# Install project
RUN uv sync --frozen

# srt (anthropics/sandbox-runtime) for MCP_COMMAND_SANDBOX=srt, only with
# --build-arg WITH_SRT=1 (docker-compose.command-sandbox.yml). The default image
# does not carry Node.js or srt (docs/design/command-sandbox.md).
ARG WITH_SRT=0
RUN if [ "$WITH_SRT" = "1" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends nodejs npm bubblewrap socat ripgrep \
      && npm install -g @anthropic-ai/sandbox-runtime@0.0.77 \
      && npm cache clean --force \
      && rm -rf /var/lib/apt/lists/*; \
    fi
COPY srt-settings.json ./

# Expose port (if needed, though MCP usually runs stdio or specific transport)
# FastMCP uses SSE by default on a port if run with specific args, or stdio.
# We will run it as a script.

# Run application
CMD ["uv", "run", "main.py"]
