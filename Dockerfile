FROM ghcr.io/astral-sh/uv:0.11.15 AS uv

FROM fedora:latest

COPY --from=uv /uv /uvx /usr/local/bin/

RUN dnf install -y --setopt=install_weak_deps=False \
        nodejs npm python3 git bash ca-certificates make podman fuse-overlayfs \
    && dnf clean all \
    && npm install -g @anthropic-ai/claude-code opencode-ai

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
