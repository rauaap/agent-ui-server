FROM ghcr.io/astral-sh/uv:0.11.15 AS uv

FROM fedora:latest

COPY --from=uv /uv /uvx /usr/local/bin/

# Pi requires Node >= 22.19; the build fails loudly here rather than leaving Pi
# to die inside its own bundle on a too-old runtime.
RUN dnf install -y --setopt=install_weak_deps=False \
        nodejs npm python3 git bash bubblewrap ca-certificates make podman fuse-overlayfs \
    && dnf clean all \
    && node -e 'const [a,b]=process.versions.node.split(".").map(Number); if (a<22||(a===22&&b<19)) { console.error("Node >= 22.19 required for Pi, got "+process.versions.node); process.exit(1); }' \
    && npm install -g @anthropic-ai/claude-code @earendil-works/pi-coding-agent

ENV SHELL=/bin/bash \
    HOME=/home/agent
RUN mkdir -p /home/agent

WORKDIR /app

# Dependencies first, without the project itself: this layer stays cached
# across source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .
RUN uv sync --locked --no-dev

CMD ["uv", "run", "--no-sync", "agent-ui-server"]
