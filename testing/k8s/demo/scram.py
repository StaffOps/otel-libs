"""Print a PostgreSQL SCRAM-SHA-256 verifier for the password read on stdin.

Used by demo.sh (specs/demo-environment DE-D13): the role passwords never
reach PostgreSQL in plaintext and never appear in a process list. The password
is the first line of stdin (trailing newline stripped); the verifier is
written to stdout in the format PostgreSQL stores in pg_authid.rolpassword:

    SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>

(RFC 5802 section 3, RFC 7677; PostgreSQL scram_build_secret). Standard
library only; run with `python3 -I`. The password is used as UTF-8 bytes
without SASLprep, which equals SASLprep for the ASCII passwords demo.sh
generates; non-ASCII input is refused rather than guessed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys

ITERATIONS = 4096  # PostgreSQL's default scram_iterations
SALT_BYTES = 16  # PostgreSQL's SCRAM_DEFAULT_SALT_LEN


def verifier(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    """Return the SCRAM-SHA-256 verifier for password, salt and iterations."""
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def main() -> int:
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        print("scram: empty password on stdin", file=sys.stderr)
        return 2
    if not password.isascii() or not password.isprintable():
        print("scram: password must be printable ASCII", file=sys.stderr)
        return 2
    print(verifier(password, os.urandom(SALT_BYTES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
