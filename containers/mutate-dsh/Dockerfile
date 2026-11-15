ARG RUNTIME_BASE=node:22-bookworm-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46

# ---------------------------------------------------------------------------
# Musl payload source.
#
# SWE-Atlas (and other benchmark) task images are a mix of glibc and musl
# (Alpine). A Node built against one C library cannot start under the other:
# the loader is absent (`cannot execute: required file not found`), and a musl
# Node additionally needs libstdc++/libgcc next to it. The final image therefore
# carries both builds under `node-bin/` and a dispatcher that picks at run time.
# ---------------------------------------------------------------------------
FROM node:22-alpine@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202 AS node-musl

# The dsh install lives in its own stage so the musl native builds below can
# read the exact dependency versions that were resolved.
FROM ${RUNTIME_BASE} AS dsh-base

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

# The API-key adapter sends only `x-api-key`. Gateways that require
# `Authorization: Bearer` (the shared vLLM endpoint behind /api/inference/v1)
# answer 401, which makes every dsh role fail. Send both headers; the patch
# fails the build if the upstream source no longer matches.
COPY patch-api-key-auth.cjs /tmp/patch-api-key-auth.cjs
RUN node /tmp/patch-api-key-auth.cjs \
      /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm-deepseek-api-key/lib/index.js \
    && node --check /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm-deepseek-api-key/lib/index.js \
    && rm /tmp/patch-api-key-auth.cjs

# node-pty ships prebuilt binaries that target glibc. On musl the prebuild still
# loads (`require` succeeds) and then segfaults on the first PTY spawn, so the
# dsh tree needs a musl build; lib/utils.js picks it by libc (node-pty-libc.patch).
FROM node:22-alpine@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202 AS pty-musl

# Build exactly the node-pty version dsh resolved, not a hand-maintained pin.
COPY --from=dsh-base /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/node-pty/package.json /tmp/node-pty-package.json
RUN set -eux; \
    apk add --no-cache build-base linux-headers python3; \
    mkdir -p /pty; \
    cd /pty; \
    npm init -y >/dev/null; \
    version=$(node -p "require('/tmp/node-pty-package.json').version"); \
    # --ignore-scripts skips node-pty's prebuild download; build from source.
    npm install --ignore-scripts --no-audit --no-fund "node-pty@${version}"; \
    npx --yes node-gyp rebuild --directory=/pty/node_modules/node-pty; \
    test -f /pty/node_modules/node-pty/build/Release/pty.node

FROM dsh-base

COPY node-dispatch.sh /tmp/node-dispatch.sh
COPY node-pty-libc.patch /tmp/node-pty-libc.patch
COPY --from=node-musl /usr/local/bin/node /tmp/musl-node
COPY --from=node-musl /usr/lib/libstdc++.so.6.0.34 /usr/lib/libgcc_s.so.1 /tmp/musl-libs/
COPY --from=pty-musl /pty/node_modules/node-pty/build/Release/pty.node /tmp/pty-musl.node

# Native addons that dsh's dependency tree cannot install on this glibc base:
# npm skips the musl platform packages (and the musl build of the require hook),
# yet a musl task image loads exactly those. Same versions as the glibc ones the
# install resolved, so the two libc families run identical code.
RUN set -eux; \
    lib=/usr/local/lib/node_modules/@deepseek-ai/dsh; \
    tmp=$(mktemp -d); \
    fetch() { \
      pkg=$1; dest=$2; \
      file=$(npm pack --silent --pack-destination "$tmp" "$pkg"); \
      mkdir -p "$dest"; \
      tar -xzf "$tmp/$file" -C "$dest" --strip-components=1; \
    }; \
    require_builtin=$(node -p "require('$lib/node_modules/node-addon-require-builtin/package.json').version"); \
    sharp=$(node -p "require('$lib/node_modules/sharp/package.json').version"); \
    vips=$(node -p "require('$lib/node_modules/@img/sharp-libvips-linux-x64/package.json').version"); \
    fetch "node-addon-require-builtin-linux-x64-musl@${require_builtin}" \
      "$lib/node_modules/node-addon-require-builtin-linux-x64-musl"; \
    fetch "@img/sharp-linuxmusl-x64@${sharp}" "$lib/node_modules/@img/sharp-linuxmusl-x64"; \
    fetch "@img/sharp-libvips-linuxmusl-x64@${vips}" "$lib/node_modules/@img/sharp-libvips-linuxmusl-x64"; \
    rm -rf "$tmp"

# The Node payload travels inside the dsh tree, so both runtime sources --
# `DSH_RUNTIME_IMAGE` (docker cp of this directory) and `DSH_RUNTIME_DIR` (tar of
# it) -- deliver it to the task container with no extra host step.
RUN set -eux; \
    lib=/usr/local/lib/node_modules/@deepseek-ai/dsh; \
    node_bin="$lib/node-bin"; \
    mkdir -p "$node_bin/musl-node"; \
    cp /usr/local/bin/node "$node_bin/node-glibc"; \
    cp /tmp/musl-node "$node_bin/musl-node/node"; \
    cp /tmp/musl-libs/libstdc++.so.6.0.34 "$node_bin/musl-node/"; \
    ln -sf libstdc++.so.6.0.34 "$node_bin/musl-node/libstdc++.so.6"; \
    cp /tmp/musl-libs/libgcc_s.so.1 "$node_bin/musl-node/"; \
    cp /tmp/node-dispatch.sh "$node_bin/dispatch"; \
    chmod 755 "$node_bin/dispatch" "$node_bin/musl-node/node" "$node_bin/node-glibc"; \
    mkdir -p "$lib/node_modules/node-pty/build/Release-musl"; \
    cp /tmp/pty-musl.node "$lib/node_modules/node-pty/build/Release-musl/pty.node"; \
    patch -p1 -d "$lib/node_modules/node-pty" < /tmp/node-pty-libc.patch; \
    rm -rf /tmp/musl-node /tmp/musl-libs /tmp/pty-musl.node /tmp/node-dispatch.sh /tmp/node-pty-libc.patch

COPY headless.mutate.cordis.yml headless.analyst.cordis.yml /opt/evolve/dsh/

RUN git config --system --add safe.directory /app/task/workspace

WORKDIR /app