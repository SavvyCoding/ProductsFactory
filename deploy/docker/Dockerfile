# ProductFactory Agent Image
# Built once on Windows with: docker build -t productfactory-agent ./deploy/docker/
# Launched per-session by poller.py — never run manually.
#
# Runtimes installed:
#   Python 3.11 + 3.12 (via pyenv at /opt/pyenv, accessible to all users)
#   Node.js 18 LTS + 20 LTS (via n, default 20)
#   Go 1.22
#   Claude Code CLI (latest)
#   GitHub CLI (gh)
#
# Security:
#   Runs as non-root user 'agent' (UID 1001) so --dangerously-skip-permissions works
#   ~/.claude mounted :ro at runtime (OAuth tokens — never writable)
#   ~/.ssh    mounted :ro at runtime (deploy key — never writable)
#   /workspace bound to product dir only
#   Isolated bridge network (productfactory-net) — not the host network

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# ── Layer 1: System packages ──────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget ca-certificates gnupg \
    git openssh-client \
    build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
    libsqlite3-dev libncursesw5-dev xz-utils tk-dev libxml2-dev \
    libxmlsec1-dev libffi-dev liblzma-dev \
    ffmpeg \
    jq unzip \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 1b: Common runtime system binaries (DB clients, diagram + doc tools) ─
# Non-pip-installable binaries products commonly invoke from AC recipes/tests,
# so the coder doesn't Block on them (runtime-tool rule: pip tools → declare in
# requirements-dev.txt; system binaries → must be pre-baked here).
#   Tier 1: psql (postgresql-client; factory is Postgres-centric), sqlite3,
#           redis-cli (redis-tools), dot (graphviz, diagram generation).
#   Tier 2 (doc/PDF products, e.g. DocumentSign): poppler-utils (pdftotext),
#           pandoc, wkhtmltopdf, tesseract-ocr (OCR).
# Queued 2026-06-09; activates on next agent-image build.
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client sqlite3 redis-tools graphviz \
    poppler-utils pandoc wkhtmltopdf tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 2: GitHub CLI (gh) ──────────────────────────────────────────────────
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

# ── Layer 3: pyenv + Python (installed to /opt/pyenv — accessible to all users) ──
ENV PYENV_ROOT=/opt/pyenv
ENV PATH=$PYENV_ROOT/bin:$PYENV_ROOT/shims:$PATH

RUN curl https://pyenv.run | bash && \
    pyenv install 3.11.9 && \
    pyenv install 3.12.4 && \
    pyenv global 3.12.4 && \
    chmod -R a+rx /opt/pyenv

# ── Layer 4: Python toolchain ─────────────────────────────────────────────────
RUN pyenv global 3.12.4 && \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        pip-audit \
        pytest pytest-cov pytest-json-report pytest-asyncio \
        httpx requests \
        pydantic \
        ruff pre-commit black flake8 mypy playwright \
        Pillow moviepy edge-tts && \
    pyenv global 3.11.9 && \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        pip-audit \
        pytest pytest-cov pytest-json-report pytest-asyncio \
        httpx requests \
        pydantic \
        ruff pre-commit black flake8 mypy playwright \
        Pillow moviepy edge-tts && \
    pyenv global 3.12.4

# ── Layer 5: Node.js 20 LTS + n ──────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/* && \
    npm install -g n && \
    n 18 && \
    n 20

# ── Layer 6: Claude Code CLI ──────────────────────────────────────────────────
# Pin to a specific version so agent behaviour is reproducible across builds.
# To upgrade: update the version tag here and rebuild with deploy/docker/build.sh.
# Check latest version at: https://www.npmjs.com/package/@anthropic-ai/claude-code
RUN npm install -g @anthropic-ai/claude-code@latest

# ── Layer 6b: Playwright + browsers (baked-in) ────────────────────────────────
# 2026-05-08 incident (#394): the per-session `npx playwright install` path
# delivered chrome-headless-shell to the agent's home dir without a usable
# exec bit, looking like EACCES at test time. The coder cannot repair this
# from inside its session — five fix_attempts fought executablePath
# workarounds before auto-block. Solution: pre-install at image build time
# into a shared, world-readable location with the exec bit forced on.
# pf-verify-env.sh validates the binary on every session start.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN mkdir -p /opt/ms-playwright && \
    npx --yes playwright@latest install --with-deps chromium chromium-headless-shell && \
    find /opt/ms-playwright -type d -exec chmod a+rx {} + && \
    find /opt/ms-playwright -type f \( -name 'chrome' -o -name 'chrome-headless-shell' -o -name 'headless_shell' -o -name 'node' \) -exec chmod a+rx {} +

# ── Layer 7: Go 1.22 ─────────────────────────────────────────────────────────
ENV GOVERSION=1.22.4
ENV GOROOT=/usr/local/go
ENV PATH=$GOROOT/bin:$PATH

RUN curl -fsSL "https://go.dev/dl/go${GOVERSION}.linux-amd64.tar.gz" \
      | tar -C /usr/local -xz

# ── Layer 8: Go security tools ────────────────────────────────────────────────
# Install govulncheck to a system-wide location
RUN GOPATH=/tmp/gobuild go install golang.org/x/vuln/cmd/govulncheck@latest && \
    mv /tmp/gobuild/bin/govulncheck /usr/local/bin/govulncheck && \
    rm -rf /tmp/gobuild

# ── Layer 9: Non-root agent user ─────────────────────────────────────────────
# Claude's --dangerously-skip-permissions is blocked when running as root.
# Create user 'agent' (UID 1001) for all runtime operations.
RUN useradd -m -u 1001 -s /bin/bash agent && \
    # SSH known hosts for git push (github, gitlab, bitbucket)
    mkdir -p /home/agent/.ssh && chmod 700 /home/agent/.ssh && \
    ssh-keyscan -H github.com    >> /home/agent/.ssh/known_hosts && \
    ssh-keyscan -H gitlab.com    >> /home/agent/.ssh/known_hosts && \
    ssh-keyscan -H bitbucket.org >> /home/agent/.ssh/known_hosts && \
    chmod 600 /home/agent/.ssh/known_hosts && \
    # Go workspace for agent
    mkdir -p /home/agent/go/bin && \
    # Workspace dir writable by agent
    mkdir -p /workspace && chown agent:agent /workspace && \
    # ollama_agent location
    mkdir -p /app && \
    chown -R agent:agent /home/agent && \
    chown agent:agent /app

# ── Layer 10: Git identity (for agent user) ───────────────────────────────────
ENV GIT_AUTHOR_NAME="ProductFactory Agent"
ENV GIT_AUTHOR_EMAIL="productfactory@noreply.localhost"
ENV GIT_COMMITTER_NAME="ProductFactory Agent"
ENV GIT_COMMITTER_EMAIL="productfactory@noreply.localhost"

RUN git config --global --add safe.directory /workspace && \
    su -c "git config --global user.name 'ProductFactory Agent' && \
           git config --global user.email 'productfactory@noreply.localhost' && \
           git config --global init.defaultBranch main && \
           git config --global --add safe.directory /workspace" agent

# ── Layer 11: Ollama agent runner ─────────────────────────────────────────────
# Copy the full orchestrator package so ollama_agent can import agent_loop etc.
COPY orchestrator/ /app/orchestrator/
COPY orchestrator/ollama_agent.py /app/ollama_agent.py
RUN chmod -R a+rx /app/orchestrator /app/ollama_agent.py && \
    pyenv global 3.12.4 && pip install --no-cache-dir httpx

# ── Layer 12: Verify installation ────────────────────────────────────────────
RUN python3 --version && \
    node --version && \
    npm --version && \
    go version && \
    gh --version && \
    git --version && \
    pip-audit --version && \
    (govulncheck -version 2>&1 || true) && \
    claude --version && \
    python3 /app/ollama_agent.py --help

# ── Entrypoint — sets up git credential helper from GH_TOKEN, strips any
#                 embedded PAT from existing remote URLs (legacy repos), then
#                 execs the agent command. Runs as root just so it can chown
#                 nothing — actually no privileged work, just shell ops.
COPY deploy/docker/agent-entrypoint.sh /usr/local/bin/agent-entrypoint.sh
# Strip Windows \r from the entrypoint — see Dockerfile.orchestrator for rationale.
RUN sed -i 's/\r$//' /usr/local/bin/agent-entrypoint.sh \
 && chmod +x /usr/local/bin/agent-entrypoint.sh

# pf-verify-env.sh — Layer 2 mechanical preflight (chrome binary exec bit,
# pytest importable, etc). Agent-entrypoint.sh runs this before exec'ing
# the agent process; exit 42 short-circuits to env-not-ready handling in
# the orchestrator without charging a fix_attempt.
COPY deploy/docker/pf-verify-env.sh /usr/local/bin/pf-verify-env.sh
RUN sed -i 's/\r$//' /usr/local/bin/pf-verify-env.sh \
 && chmod +x /usr/local/bin/pf-verify-env.sh

# ── Layer: Playwright + Chromium for browser-automation features ─────────────
# Browser-automation ACs (Playwright e2e, screenshots, headless chromium) can't
# pip-install the browser binary — it needs `playwright install`. Pre-bake it so
# those features stop Blocking (canonical Playwright cascade: 1089/1090/1091/
# 1095). Browsers live in a shared, world-readable path so the non-root agent
# can use them; PLAYWRIGHT_BROWSERS_PATH persists into the runtime env. The
# `playwright` pip pkg is installed in the toolchain layer above. Queued
# 2026-06-09; activates on the next agent-image build.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pyenv global 3.12.4 && \
    python -m playwright install --with-deps chromium && \
    chmod -R a+rx /ms-playwright

# ── Make the pyenv shims dir writable by the non-root agent ──────────────────
# The earlier `chmod -R a+rx /opt/pyenv` grants read+execute but NOT write. pip
# is a pyenv shim: after installing a console-script package (uvicorn/fastapi/
# alembic/...) it runs `pyenv rehash`, which writes new shim files into
# /opt/pyenv/shims. Without write perms the rehash fails ("cannot rehash:
# /opt/pyenv/shims isn't writable") and `pip install` exits non-zero even though
# the deps installed fine — which the post-coder test-check read as env_broken,
# infinitely looping every Python product with console-script deps (testingcalc
# #1423, 2026-06-08). Must run AFTER the last pyenv operation, before USER agent.
RUN chmod -R 0777 "$PYENV_ROOT/shims"

# ── Switch to non-root agent user ────────────────────────────────────────────
USER agent
ENV HOME=/home/agent
ENV GOPATH=/home/agent/go
# /home/agent/.local/bin is where a NON-ROOT `pip install` (the coder declaring
# a tool in requirements-dev.txt) puts console scripts. Without it on PATH,
# `shutil.which("locust")` / `subprocess.run(["tool"])` fail even though the
# tool installed fine — so the coder concluded the tool was "missing" and
# reached for fake-script/PATH hacks (testingcalc #1438 locust loop, 2026-06-09).
# This line completes the "declare pip-installable tools in requirements-dev.txt"
# strategy: declared CLI tools are now actually reachable.
ENV PATH=$PYENV_ROOT/bin:$PYENV_ROOT/shims:/home/agent/.local/bin:$GOROOT/bin:/home/agent/go/bin:/usr/local/bin:$PATH

# ── Runtime mounts (provided by poller at launch — never baked in) ───────────
# /home/agent/.claude      ← ~/.claude from Windows host  :ro  (OAuth session)
# /home/agent/.claude.json ← ~/.claude.json from host     :ro  (Claude config)
# /home/agent/.ssh/id_ed25519 ← deploy key from host      :ro
# /workspace               ← product working_dir          :rw

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/agent-entrypoint.sh"]
