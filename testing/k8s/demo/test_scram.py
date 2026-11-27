"""Tests for scram.py (standard library unittest; run from the repo root:
python3 -m unittest discover -s testing/k8s/demo -p 'test_*.py')."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scram  # noqa: E402

# RFC 7677 section 3 example exchange (user "user", password "pencil").
SALT = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
NONCE = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
AUTH_MESSAGE = (
    f"n=user,r=rOprNGfwEbeRWgbNEkqO,r={NONCE},s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
    f"c=biws,r={NONCE}"
).encode()
SERVER_SIGNATURE = "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
CLIENT_PROOF = "dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ="

SHAPE = re.compile(r"^SCRAM-SHA-256\$4096:[A-Za-z0-9+/]{22}==\$[A-Za-z0-9+/]{43}=:[A-Za-z0-9+/]{43}=$")


def _keys(verifier: str) -> tuple[bytes, bytes]:
    _, rest = verifier.split("$", 1)
    _, keys = rest.split("$")
    stored, server = keys.split(":")
    return base64.b64decode(stored), base64.b64decode(server)


class VerifierTest(unittest.TestCase):
    def test_rfc7677_server_signature(self) -> None:
        _, server_key = _keys(scram.verifier("pencil", SALT))
        sig = hmac.new(server_key, AUTH_MESSAGE, hashlib.sha256).digest()
        self.assertEqual(base64.b64encode(sig).decode(), SERVER_SIGNATURE)

    def test_rfc7677_stored_key_checks_the_client_proof(self) -> None:
        # The server verifies ClientProof by H(ClientProof XOR ClientSignature) == StoredKey.
        stored_key, _ = _keys(scram.verifier("pencil", SALT))
        client_signature = hmac.new(stored_key, AUTH_MESSAGE, hashlib.sha256).digest()
        proof = base64.b64decode(CLIENT_PROOF)
        client_key = bytes(a ^ b for a, b in zip(proof, client_signature))
        self.assertEqual(hashlib.sha256(client_key).digest(), stored_key)

    def test_format_and_salt(self) -> None:
        v = scram.verifier("pencil", SALT)
        self.assertRegex(v, SHAPE)
        self.assertTrue(v.startswith("SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ==$"))


class MainTest(unittest.TestCase):
    def _run(self, stdin: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), redirect_stdout(out), redirect_stderr(err):
            rc = scram.main()
        return rc, out.getvalue(), err.getvalue()

    def test_reads_first_line_and_prints_verifier(self) -> None:
        rc, out, err = self._run("0123456789abcdef0123456789abcdef0123456789abcdef\nignored\n")
        self.assertEqual(rc, 0)
        self.assertRegex(out.strip(), SHAPE)
        self.assertNotIn("0123456789abcdef", out)
        self.assertEqual(err, "")

    def test_salt_is_random(self) -> None:
        _, a, _ = self._run("pw\n")
        _, b, _ = self._run("pw\n")
        self.assertNotEqual(a, b)

    def test_refuses_empty(self) -> None:
        for stdin in ("", "\n", "\r\n"):
            rc, out, err = self._run(stdin)
            self.assertEqual(rc, 2)
            self.assertEqual(out, "")
            self.assertIn("empty", err)

    def test_refuses_non_ascii_and_control(self) -> None:
        for stdin in ("pässword\n", "pass\tword\n"):
            rc, out, err = self._run(stdin)
            self.assertEqual(rc, 2)
            self.assertEqual(out, "")
            self.assertIn("printable ASCII", err)
            self.assertNotIn(stdin.strip(), err)  # the input is never echoed


if __name__ == "__main__":
    unittest.main()
