# ---- Base: shared Python + system deps + maestro install ----
FROM python:3.14-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        nodejs \
        npm \
        procps \
        jq \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY constraints.txt .
COPY maestro/ maestro/
# The sibling packages maestro_sdk/ and maestro_review/ live in their own source
# trees and are deliberately excluded from maestro's own distribution
# (pyproject.toml packages.find exclude). They must be copied and installed
# separately: at runtime PYTHONPATH=/workspace puts the bind-mounted repo root on
# sys.path, where maestro_sdk/ is a src-layout dir with no top-level __init__.py
# and so resolves as a half-empty PEP 420 namespace — `import maestro_sdk`
# succeeds but every submodule import (e.g. maestro_sdk.mcp_config, which
# maestro/providers/cli_paced.py needs) raises, aborting in-container gate runs at
# collection. Installing a real distribution here makes site-packages win over the
# namespace shadow. Matches .github/workflows/quality-gates.yml.
COPY maestro_sdk/ maestro_sdk/
COPY maestro_review/ maestro_review/
# [dev] extras include ruff, mypy, pytest — required at runtime because the
# agent runs quality gates during its self-review evolution cycle. [email]
# bakes the optional aiosmtplib/aioimaplib libs so the loop container's
# MAESTRO_EMAIL_* surface works without a network round-trip on start.
# Install the siblings editable BEFORE the main package so their distributions
# are resolvable when maestro is imported.
RUN pip install --no-cache-dir -e "maestro_sdk/" -e "maestro_review/" \
    && pip install --no-cache-dir -c constraints.txt ".[dev,email]"

ARG PYPROJECT_SHA256=""
LABEL pyproject.sha256=$PYPROJECT_SHA256

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ---- Production: non-root user, entrypoint, bind-mount workspace ----
FROM base AS production

COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# gosu: lightweight setuid replacement for stepping down from root.
# The entrypoint runs as root to fix bind-mount permissions, then drops
# to the unprivileged maestro user via gosu.
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — reduces blast radius if the agent executes unexpected commands.
# UID/GID must match the production host to avoid bind-mount permission issues.
RUN groupadd -g 1002 maestro && useradd -u 1002 -g 1002 -m maestro

# Imports resolve from the bind-mounted workspace at runtime,
# so code updates are picked up on container restart without rebuilding.
ENV PYTHONPATH=/workspace

# NOTE: No USER directive — the entrypoint starts as root to fix bind-mount
# permissions, then drops to maestro via gosu.
WORKDIR /workspace

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["loop"]
