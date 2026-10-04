FROM python:3.13-slim

ARG HERMES_REF=v2026.9.24
ARG HERMES_BAND_REF=d78c3d98a8ff812d2f96c327be34d443ef853edd
ARG DOCKER_CLI_VERSION=27.5.1
ARG BUILDX_VERSION=v0.21.2

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates nodejs npm jq ripgrep procps \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
    | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
 && mkdir -p /usr/local/lib/docker/cli-plugins \
 && curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-buildx \
    "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-amd64" \
 && chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx

RUN pip install --no-cache-dir uv \
 && git clone --depth 1 --branch "${HERMES_REF}" https://github.com/NousResearch/hermes-agent.git /opt/hermes \
 && cd /opt/hermes \
 && UV_PROJECT_ENVIRONMENT=/opt/hermes/.venv uv sync --frozen --no-dev --extra messaging --python /usr/local/bin/python3.13 \
 && uv pip install --python /opt/hermes/.venv/bin/python \
    "hermes-band @ git+https://github.com/band-ai/hermes-band-platform.git@${HERMES_BAND_REF}" \
 && rm -rf /root/.cache
ENV PATH="/opt/hermes/.venv/bin:${PATH}"

COPY harness-requirements.txt /tmp/harness-requirements.txt
RUN python -m venv /opt/harness \
 && /opt/harness/bin/pip install --no-cache-dir -r /tmp/harness-requirements.txt
COPY harness start-seat wait-for-docker /usr/local/bin/
COPY docker-client-config.json /etc/docker-client/config.json
RUN chmod +x /usr/local/bin/harness /usr/local/bin/start-seat /usr/local/bin/wait-for-docker

RUN useradd -m -u 1000 agent \
 && git config --system safe.directory '*' \
 && git config --system init.defaultBranch main

USER agent
WORKDIR /work
ENTRYPOINT ["start-seat"]
CMD ["gateway", "run", "--no-supervise"]
