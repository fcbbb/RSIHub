#!/bin/sh
# dsh Node bootstrap for task containers.
#
# The runtime tree carries two Node builds because a task image can be either
# glibc (Debian/Ubuntu) or musl (Alpine), and a Node built for one cannot even
# load on the other: glibc Node on musl dies with `cannot execute: required file
# not found`, musl Node on glibc with the same. `DshAgent._ensure_node` installs
# this file as /usr/local/bin/node (shadowing the image's own Node) only when
# that Node cannot boot dsh -- no Node at all, or a release without
# `import.meta.main` (< 22.19).
#
# Selection is by libc, then verified by actually starting the candidate, so an
# image with both loaders (e.g. Alpine + gcompat) is judged by what works
# rather than by which files exist. The musl build needs the libstdc++/libgcc
# that travel next to it; exporting that path for the glibc build would poison
# it, so each candidate gets its own environment.

# In a task container the tree is staged at the fixed runtime root; when this
# file is run straight out of the image or an extracted tree, fall back to the
# directory the dispatcher itself lives in.
base=${DSH_NODE_BIN_DIR:-/opt/evolve-dsh/dsh/node-bin}
if [ -z "${DSH_NODE_BIN_DIR:-}" ] && [ ! -d "$base" ]; then
    case $0 in
        */*) base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) ;;
    esac
fi
musl_node="$base/musl-node/node"
glibc_node="$base/node-glibc"
musl_libdir="$base/musl-node${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

is_musl() {
    for loader in /lib/ld-musl-*.so.1 /lib64/ld-musl-*.so.1; do
        [ -e "$loader" ] && return 0
    done
    if command -v ldd >/dev/null 2>&1; then
        ldd --version 2>&1 | grep -qi musl && return 0
    fi
    return 1
}

usable() {
    # $1 = binary, $2 = LD_LIBRARY_PATH value for that binary (may be empty)
    [ -x "$1" ] || return 1
    if [ -n "$2" ]; then
        LD_LIBRARY_PATH="$2" "$1" -e '' >/dev/null 2>&1
    else
        "$1" -e '' >/dev/null 2>&1
    fi
}

run_musl() {
    LD_LIBRARY_PATH="$musl_libdir"
    export LD_LIBRARY_PATH
    exec "$musl_node" "$@"
}

if is_musl; then
    usable "$musl_node" "$musl_libdir" && run_musl "$@"
    usable "$glibc_node" "" && exec "$glibc_node" "$@"
else
    usable "$glibc_node" "" && exec "$glibc_node" "$@"
    usable "$musl_node" "$musl_libdir" && run_musl "$@"
fi

echo "node: no Node build under $base can start in this container" >&2
echo "node: expected node-glibc/ and musl-node/ from the dsh runtime image" >&2
exit 127