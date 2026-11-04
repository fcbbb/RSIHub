#!/usr/bin/env bash
# Shrink the globally installed dsh tree to what a headless task container uses.
#
# `npm install -g @deepseek-ai/dsh` installs the whole product: desktop/web
# client bundles, an Office conversion engine, speech recognition, and every
# platform's native prebuild. The profiles used here (sdk-minimal and headless)
# load none of that. This runs in the same Docker layer as the install on
# purpose: pruning in a later layer leaves the bytes in the image.
#
# Every package a profile can address by name is kept, except the desktop/web
# UI, the session inspector, and optional engines for features a terminal task
# container cannot use. The thin wrappers (`libreoffice-kit`, `sherpa-onnx-node`)
# stay resolvable so a profile naming them still boots; only the engine is gone.
set -euo pipefail

LIB=${DSH_RUNTIME_LIB:-/usr/local/lib/node_modules/@deepseek-ai/dsh}
[[ -d $LIB ]] || { echo "dsh runtime not found at $LIB" >&2; exit 1; }

before=$(du -sm "$LIB" | cut -f1)

# Build-time artifacts.
find "$LIB" -type f \( -name '*.map' -o -name '*.d.ts' -o -name '*.d.mts' -o -name '*.d.cts' \) -delete

# Package docs and test fixtures; LICENSE/NOTICE files stay.
find "$LIB" -type f \( -iname 'readme*' -o -iname 'changelog*' -o -iname 'history*' \) -delete
find "$LIB" -depth -type d \( -name test -o -name tests -o -name __tests__ -o -name docs -o -name examples \) -exec rm -rf {} +

# Desktop/web UI and the session inspector (`dsh-client-*` covers client-ui-*).
find "$LIB" -depth -type d \( \
    -name 'dsh-client-*' \
    -o -name 'dsh-web-app' \
    -o -name 'dsh-web-frontend' \
    -o -name 'dsh-host-frontend-static' \
    -o -name 'dsh-host-directory-picker*' \
    -o -name 'dsh-acp-app' \
    -o -name 'dsh-experimental-client-ui-*' \
    -o -name 'dsh-experimental-*inspector*' \
  \) -exec rm -rf {} +

# Optional engines: Office conversion (wasm is the only Linux backend), speech
# recognition (native), and sharp's wasm fallback.
find "$LIB" -depth -type d \( \
    -name 'libreoffice-kit-wasm' \
    -o -name 'sharp-wasm32' \
    -o -name 'sherpa-onnx-*' ! -name 'sherpa-onnx-node' \
  \) -exec rm -rf {} +

# Native prebuilds for other operating systems.
find "$LIB" -depth -type d -path '*/prebuilds/win32-*' -exec rm -rf {} +
find "$LIB" -depth -type d -path '*/prebuilds/darwin-*' -exec rm -rf {} +

after=$(du -sm "$LIB" | cut -f1)
echo "dsh runtime pruned: ${before} MB -> ${after} MB"