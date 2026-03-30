# ProductFactory Agent Image
# Built once on Windows with: docker build -t productfactory-agent ./deploy/docker/
# Launched per-session by poller.py — never run manually.
#
# Runtimes installed:
#   Python 3.11 + 3.12 (via pyenv, default 3.12)
#   Node.js 18 LTS + 20 LTS (via n, default 20)
#   Go 1.22
#   Claude Code CLI (latest)
#   GitHub CLI (gh)
#
# Security:
#   ~/.claude mounted :ro at runtime (OAuth tokens — never writable)
#   ~/.ssh    mounted :ro at runtime (deploy key — never writable)
#   /workspace bound to product dir only
#   Isolated bridge network — no --network host

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# ── Layer 1: System packages ──────────────────────────────────────────────────
# Keep this layer stable — it changes least often.
RUN apt-get update && apt-get install -y --no-install-recommends \
    # essentials
    curl wget ca-certificates gnupg \
    # git + ssh (needed for commits, push, PR)
    git openssh-client \
    # build tools (pyenv + compiled extensions)
    build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
    libsqlite3-dev libncursesw5-dev xz-utils tk-dev libxml2-dev \
    libxmlsec1-dev libffi-dev liblzma-dev \
    # utilities
    jq unzip \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 2: GitHub CLI (gh) ──────────────────────────────────────────────────
# Claude uses `gh pr create` to open PRs.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

# ── Layer 3: pyenv + Python 3.11 & 3.12 ──────────────────────────────────────
# pyenv lets Claude switch Python version per product (pyenv local X.Y.Z).
ENV PYENV_ROOT=/root/.pyenv
ENV PATH=$PYENV_ROOT/bin:$PYENV_ROOT/shims:$PATH

RUN curl https://pyenv.run | bash && \
    pyenv install 3.11.9 && \
    pyenv install 3.12.4 && \
    pyenv global 3.12.4

# ── Layer 4: Python toolchain (installed for both versions) ───────────────────
# Install in 3.12 (global default) and 3.11 so `pytest` is always available
# regardless of which version the product uses.
RUN pyenv global 3.12.4 && \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        pip-audit \
        pytest pytest-cov pytest-json-report pytest-asyncio \
        httpx requests \
        pydantic && \
    pyenv global 3.11.9 && \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        pip-audit \
        pytest pytest-cov pytest-json-report pytest-asyncio \
        httpx requests \
        pydantic && \
    pyenv global 3.12.4

# ── Layer 5: Node.js 20 LTS + n (version manager) ────────────────────────────
# n is a lightweight node version manager — no shell init required.
# `n 18` / `n 20` switches versions; Claude uses this based on product config.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/* && \
    npm install -g n && \
    n 18 && \
    n 20        # sets default back to 20

# ── Layer 6: Claude Code CLI ──────────────────────────────────────────────────
RUN npm install -g @anthropic-ai/claude-code

# ── Layer 7: Go 1.22 ─────────────────────────────────────────────────────────
ENV GOVERSION=1.22.4
ENV GOROOT=/usr/local/go
ENV GOPATH=/root/go
ENV PATH=$GOROOT/bin:$GOPATH/bin:$PATH

RUN curl -fsSL "https://go.dev/dl/go${GOVERSION}.linux-amd64.tar.gz" \
      | tar -C /usr/local -xz && \
    mkdir -p $GOPATH/bin

# ── Layer 8: Go security tools ────────────────────────────────────────────────
RUN go install golang.org/x/vuln/cmd/govulncheck@latest

# ── Layer 9: Git identity ─────────────────────────────────────────────────────
# Required for `git commit` inside the container.
ENV GIT_AUTHOR_NAME="ProductFactory Agent"
ENV GIT_AUTHOR_EMAIL="productfactory@noreply.localhost"
ENV GIT_COMMITTER_NAME="ProductFactory Agent"
ENV GIT_COMMITTER_EMAIL="productfactory@noreply.localhost"

# Avoid "dubious ownership" errors when /workspace is bind-mounted
RUN git config --global --add safe.directory /workspace && \
    git config --global user.name "ProductFactory Agent" && \
    git config --global user.email "productfactory@noreply.localhost" && \
    git config --global init.defaultBranch main

# ── Layer 10: SSH known hosts ─────────────────────────────────────────────────
# Pre-seed GitHub (and GitLab / Bitbucket) so `git push` never hangs
# waiting for host key confirmation.
RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh && \
    ssh-keyscan -H github.com    >> /root/.ssh/known_hosts && \
    ssh-keyscan -H gitlab.com    >> /root/.ssh/known_hosts && \
    ssh-keyscan -H bitbucket.org >> /root/.ssh/known_hosts && \
    chmod 600 /root/.ssh/known_hosts

# ── Layer 11: Results directory ───────────────────────────────────────────────
# Claude writes test JSON here; ensure it always exists.
RUN mkdir -p /workspace/Results

# ── Runtime mounts (provided by poller at launch — never baked in) ───────────
# /root/.claude  ← ~/.claude from Windows host  :ro  (OAuth session)
# /root/.ssh     ← deploy key dir from host     :ro  (overwrites known_hosts layer above)
# /workspace     ← product working_dir          :rw

# ── Verify installation ───────────────────────────────────────────────────────
RUN python3 --version && \
    node --version && \
    npm --version && \
    go version && \
    gh --version && \
    git --version && \
    pip-audit --version && \
    govulncheck -version && \
    claude --version

WORKDIR /workspace
