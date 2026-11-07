#!/usr/bin/env python3
"""Allow DNS in Harbor's egress-control sidecar policy.

Harbor 0.18's sidecar rejects every non-TCP packet, including UDP DNS, so
nothing inside an allowlist sandbox can resolve a hostname. Harbor 0.24 added
`nft_dns_rules`, but it only allows the resolver address from the container's
/etc/resolv.conf (127.0.0.11). Docker's embedded resolver then forwards the
query to the host's upstream nameserver from the same network namespace, and
that packet is rejected too.

This script inserts (0.18) or overrides (0.24+) `nft_dns_rules` so the sidecar
allows DNS to:

  * 127.0.0.11, Docker's embedded resolver; and
  * every nameserver from the host's /etc/resolv.conf, passed on the command
    line by prepare.sh (the sidecar cannot read the host file itself).

The patch is idempotent: running it again with the same inputs leaves the file
byte-identical, so Harbor's content-addressed sidecar image is not rebuilt.

usage: patch_sidecar_dns.py NETWORK_POLICY [NAMESERVER...]
"""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

START = "# >>> rsihub local DNS allowance"
END = "# <<< rsihub local DNS allowance"
RESOLVER = "127.0.0.11"


def normalize(nameservers: list[str]) -> list[str]:
    seen: list[str] = []
    for value in [RESOLVER, *nameservers]:
        value = value.split("%", 1)[0].strip()
        if not value:
            continue
        try:
            ipaddress.ip_address(value)
        except ValueError:
            print(f"ignoring non-address nameserver: {value!r}", file=sys.stderr)
            continue
        if value not in seen:
            seen.append(value)
    return seen


def block(nameservers: list[str]) -> str:
    loop = " ".join(nameservers)
    return f"""{START}
# Harbor 0.18 rejects all non-TCP egress, including UDP DNS; 0.24 allows only
# the container resolver and misses Docker's upstream query. Allow both.
nft_dns_rules() {{
  action="$1"
  for ns in {loop}; do
    case "$ns" in
      *:*) printf '    ip6 daddr %s udp dport 53 %s\\n' "$ns" "$action"
           printf '    ip6 daddr %s tcp dport 53 %s\\n' "$ns" "$action" ;;
      *)   printf '    ip daddr %s udp dport 53 %s\\n' "$ns" "$action"
           printf '    ip daddr %s tcp dport 53 %s\\n' "$ns" "$action" ;;
    esac
  done
}}
{END}
"""


def apply(text: str, nameservers: list[str]) -> str:
    start = text.find(START)
    if start != -1:
        end = text.find(END, start)
        if end == -1:
            raise SystemExit("found the start marker without the end marker")
        end += len(END)
        if text[end : end + 1] == "\n":
            end += 1
        text = text[:start] + text[end:]

    anchor = "setup_nftables() {"
    if anchor not in text:
        raise SystemExit("network-policy has no setup_nftables() function")
    text = text.replace(anchor, block(nameservers) + anchor, 1)

    for action in ("return", "accept"):
        call = f"$(nft_dns_rules {action})"
        if call in text:
            continue
        needle = f"    fib daddr type local {action}\n"
        if needle not in text:
            raise SystemExit(f"network-policy has no 'fib daddr type local {action}' anchor")
        text = text.replace(needle, needle + f"    {call}\n", 1)
    return text


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: patch_sidecar_dns.py NETWORK_POLICY [NAMESERVER...]")
    path = Path(sys.argv[1])
    if not path.is_file():
        raise SystemExit(f"not a file: {path}")
    original = path.read_text()
    patched = apply(original, normalize(sys.argv[2:]))
    if patched == original:
        print(f"sidecar DNS policy already patched: {path}")
        return
    path.write_text(patched)
    print(f"patched sidecar DNS policy: {path} (dns: {' '.join(normalize(sys.argv[2:]))})")


if __name__ == "__main__":
    main()
