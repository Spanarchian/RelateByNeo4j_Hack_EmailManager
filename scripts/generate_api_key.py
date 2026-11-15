"""Generate an API key for a client and print the entry to add to API_KEYS.

Usage: uv run task api-key -- <name> <role>[,<role>...] [human|service]
The key is shown once; only its SHA-256 digest belongs in configuration.
"""

from __future__ import annotations

import json
import sys

from email_service.security.authentication import generate_api_key
from email_service.security.permissions import PrincipalKind, Role


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    name, roles = argv[0], [Role(r.strip()) for r in argv[1].split(",")]
    kind = PrincipalKind(argv[2]) if len(argv) > 2 else PrincipalKind.HUMAN
    key, digest = generate_api_key()
    entry = {
        "name": name,
        "key_sha256": digest,
        "roles": [r.value for r in roles],
        "kind": kind.value,
    }
    print(f"API key for {name} (give this to the client, it is not stored):\n  {key}\n")
    print("Add this object to the API_KEYS JSON array:")
    print("  " + json.dumps(entry))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
