ARG RUNTIME_BASE=node:22-bookworm-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46
FROM ${RUNTIME_BASE}

ARG DSH_VERSION=0.2.1-rc.5
ARG SOURCE_REVISION=unknown
ARG BUILD_TIMESTAMP=unknown

LABEL org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.created="${BUILD_TIMESTAMP}" \
      io.evolve.dsh.version="${DSH_VERSION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      coreutils \
      curl \
      diffutils \
      file \
      findutils \
      git \
      jq \
      less \
      openssh-client \
      patch \
      procps \
      python3 \
      ripgrep \
      sed \
      tree \
      unzip \
      zip \
    && rm -rf /var/lib/apt/lists/*

# Prune in the same layer as the install: deleting files in a later layer
# leaves them in the image and only bloats the trial upload.
COPY prune-runtime.sh /tmp/prune-runtime.sh
RUN npm install --global "@deepseek-ai/dsh@${DSH_VERSION}" \
    && bash /tmp/prune-runtime.sh \
    && rm /tmp/prune-runtime.sh \
    && dsh --version

COPY headless.mutate.cordis.yml headless.analyst.cordis.yml /opt/evolve/dsh/

RUN git config --system --add safe.directory /app/task/workspace

WORKDIR /app