"""Regression tests for the cookie crypto path (v10 AES-GCM + DPAPI).

Background: pycryptodome was missing from the build venv and every v10/v20
decrypt silently died in _decrypt's except; the strip heuristic also
truncated long no-prefix values. These tests make both failure classes loud.
"""
import base64
import ctypes
import ctypes.wintypes
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser_cookies as bc  # noqa: E402
from Crypto.Cipher import AES  # noqa: E402  (required dependency: loud ImportError if missing)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(data):
    bin_ = DATA_BLOB(len(data), ctypes.cast(
        ctypes.create_string_buffer(data, len(data)),
        ctypes.POINTER(ctypes.c_char)))
    bout = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(bin_), None, None, None, None, 0, ctypes.byref(bout)):
        raise OSError("CryptProtectData failed")
    out = ctypes.string_at(bout.pbData, bout.cbData)
    ctypes.windll.kernel32.LocalFree(bout.pbData)
    return out


def _v10_blob(key, plaintext):
    nonce = os.urandom(12)
    c = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = c.encrypt_and_digest(plaintext)
    return b"v10" + nonce + ct + tag


class TestCryptoDependency(unittest.TestCase):
    def test_pycryptodome_present(self):
        self.assertIsNotNone(
            importlib.util.find_spec("Crypto"),
            "pycryptodome missing: every v10/v20 cookie decrypt silently "
            "fails (the original bug)")

    def test_decrypt_roundtrip_long_value_no_hash(self):
        # 200 printable chars: the old strip heuristic lost exactly 32 bytes
        key = os.urandom(32)
        val = "A" * 200
        self.assertEqual(bc._decrypt(_v10_blob(key, val.encode()), key), val)

    def test_decrypt_strips_appbound_hash_prefix(self):
        # Edge/WebView2 app-bound layout: 32 RAW bytes + value
        key = os.urandom(32)
        val = b"fake-value-123"
        pt = os.urandom(32) + val
        self.assertEqual(bc._decrypt(_v10_blob(key, pt), key), val.decode())

    def test_crypto_status_counts_failures(self):
        key = os.urandom(32)
        wrong = os.urandom(32)
        before = bc.crypto_status().get("ValueError", 0)
        out = bc._decrypt(_v10_blob(key, b"x" * 40), wrong)
        self.assertEqual(out, "")
        self.assertEqual(bc.crypto_status().get("ValueError", 0), before + 1)


@unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
class TestWebviewReaderSyntheticProfile(unittest.TestCase):
    def test_full_synthetic_profile_roundtrip(self):
        tmp = tempfile.mkdtemp(prefix="peak_wv_test_")
        try:
            prof = os.path.join(tmp, "EBWebView", "Default", "Network")
            os.makedirs(prof, exist_ok=True)
            key = os.urandom(32)
            ls = {"os_crypt": {"encrypted_key": base64.b64encode(
                b"DPAPI" + _dpapi_protect(key)).decode()}}
            for lsp in (os.path.join(tmp, "Local State"),
                        os.path.join(tmp, "EBWebView", "Local State")):
                with open(lsp, "w", encoding="utf-8") as f:
                    json.dump(ls, f)
            rows = [
                ("__Secure-1PSID", "L" * 200, True),  # long, no hash prefix
                ("SAPISID", "fake-sapisid-xyz", True),
                ("PREF", "fake-pref", False),
            ]
            con = sqlite3.connect(os.path.join(prof, "Cookies"))
            con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, "
                        "encrypted_value BLOB, value TEXT, is_secure INTEGER, "
                        "path TEXT)")
            for name, val, secure in rows:
                con.execute("INSERT INTO cookies VALUES (?,?,?,?,?,?)",
                            (".youtube.com", name,
                             _v10_blob(key, val.encode()) if secure else b"",
                             "" if secure else val, 1 if secure else 0, "/"))
            con.commit()
            con.close()
            got = bc.yt_cookies_dict_webview(tmp)
            for name, val, secure in rows:
                self.assertEqual(got.get(name), val,
                                 "cookie %s did not round-trip" % name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
