#!/usr/bin/env python3
"""Generate bcrypt hash for APP_PASSWORD_HASH."""

import sys

import bcrypt


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/hash_password.py 'your-secure-password'", file=sys.stderr)
        sys.exit(1)
    password = sys.argv[1]
    if len(password) < 12:
        print("Warning: use at least 12 characters for production.", file=sys.stderr)
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12))
    print(hashed.decode("utf-8"))


if __name__ == "__main__":
    main()
