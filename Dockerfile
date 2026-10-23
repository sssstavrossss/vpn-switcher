# Small image: Python, the Docker client, and the Compose plugin.
# The Docker *daemon* is not included. The client talks to the host's
# daemon through the mounted socket. See the README for why that socket
# has to stay on a private network.

FROM python:3.12-slim-bookworm

# Pinned so a rebuild downloads the same tools. The static docker binary
# is the client only (we throw the rest of the tarball away). The Compose
# plugin has to live on Docker's cli-plugins path and be named
# docker-compose, otherwise `docker compose` will not find it.
ARG DOCKER_VERSION=29.9.2
ARG COMPOSE_VERSION=5.6.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) arch="x86_64" ;; \
      arm64) arch="aarch64" ;; \
      *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_VERSION}.tgz" \
      | tar -xz -C /tmp docker/docker; \
    mv /tmp/docker/docker /usr/local/bin/docker; \
    rmdir /tmp/docker; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
      "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-${arch}"; \
    chmod 755 /usr/local/bin/docker /usr/local/lib/docker/cli-plugins/docker-compose; \
    docker compose version; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

# uid 1000. The socket's group id is different on every host, so it is
# not baked in: compose `group_add` adds it when the container starts.
# Override `user:` if the stack's .env is owned by someone else.
RUN useradd --create-home --uid 1000 --user-group --shell /usr/sbin/nologin switcher

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

USER switcher
ENV HOME=/home/switcher \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
# One process. The switch lock and the login limiter live in memory.
CMD ["python", "-m", "app"]
