# -*- coding: utf-8 -*-
"""
sk_crypto.py
============
Fernet-based encryption layer for Stripe secret keys stored in data.json.

Environment variables
---------------------
STRIPE_KEY_ENCRYPTION_SECRET
    Base64-url-encoded 32-byte Fernet key.
    If NOT set, a new key is generated, printed to stdout once, and the
    process exits — forcing the operator to persist it in their secrets.
    This prevents silent fall-back to plaintext storage.

Usage (inside file1.py)
-----------------------
    from sk_crypto import encrypt_sk, decrypt_sk, migrate_data_json

    # on write:
    set_user_sk(user_id, encrypt_sk(raw_sk))

    # on read:
    raw = decrypt_sk(get_user_sk_raw(user_id))

    # at startup (once):
    migrate_data_json()
"""

import os
import sys
import json
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# ── Fernet import (cryptography package) ──────────────────────────────────────
try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    logger.critical(
        "[SK-CRYPTO] 'cryptography' package not installed. "
        "Run:  pip install cryptography"
    )
    raise

# ── Encryption key bootstrap ──────────────────────────────────────────────────
_ENV_VAR = "STRIPE_KEY_ENCRYPTION_SECRET"
_raw_secret = os.environ.get(_ENV_VAR, "").strip()

if not _raw_secret:
    # Generate a brand-new key and force the operator to save it.
    _generated = Fernet.generate_key().decode()
    print(
        "\n"
        "╔══════════════════════════════════════════════════════════════╗\n"
        "║  SK-CRYPTO: STRIPE_KEY_ENCRYPTION_SECRET not set!           ║\n"
        "║                                                              ║\n"
        "║  A new Fernet key has been generated for you:               ║\n"
        f"║  {_generated:<60} ║\n"
        "║                                                              ║\n"
        "║  Add this to your environment secrets as:                   ║\n"
        "║  STRIPE_KEY_ENCRYPTION_SECRET=<key above>                   ║\n"
        "║                                                              ║\n"
        "║  Then restart the bot. Exiting now.                         ║\n"
        "╚══════════════════════════════════════════════════════════════╝\n",
        flush=True
    )
    sys.exit(1)

try:
    _FERNET = Fernet(_raw_secret.encode())
except Exception as _e:
    logger.critical(
        f"[SK-CRYPTO] Invalid STRIPE_KEY_ENCRYPTION_SECRET: {_e}. "
        "Generate a fresh key with:  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )
    sys.exit(1)

# ── Prefix used to mark encrypted values so we can detect plaintext ───────────
# Fernet tokens always start with "gAAAAAB" — we rely on that characteristic
# plus a short sentinel prefix we prepend so the check is unambiguous.
_ENC_PREFIX = "ENC:"

# ── Thread lock for migration ─────────────────────────────────────────────────
_migrate_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def is_encrypted(value: Optional[str]) -> bool:
    """Return True if *value* was produced by encrypt_sk()."""
    if not isinstance(value, str):
        return False
    return value.startswith(_ENC_PREFIX)


def encrypt_sk(plaintext: str) -> str:
    """
    Encrypt a Stripe SK key and return a storable string.

    Raises
    ------
    ValueError
        If *plaintext* does not look like a Stripe key (must start with
        ``sk_live_`` or ``sk_test_``).
    """
    if not isinstance(plaintext, str) or not (
        plaintext.startswith("sk_live_") or plaintext.startswith("sk_test_")
    ):
        raise ValueError(
            "Invalid Stripe SK key: must start with 'sk_live_' or 'sk_test_'."
        )
    if is_encrypted(plaintext):
        # Already encrypted — return as-is (idempotent)
        return plaintext
    token = _FERNET.encrypt(plaintext.encode()).decode()
    return _ENC_PREFIX + token


def decrypt_sk(stored: Optional[str]) -> Optional[str]:
    """
    Decrypt a stored SK value.  Returns:
    - The plaintext SK string on success.
    - None if *stored* is None / empty.
    - The original value unchanged if it is NOT encrypted
      (backward-compat: plaintext keys still work).

    Never raises — decryption errors are logged and None is returned.
    """
    if not stored:
        return None

    if not is_encrypted(stored):
        # Backward-compat: value is plaintext (pre-encryption migration)
        logger.warning(
            "[SK-CRYPTO] Found plaintext SK key in data store — "
            "run migrate_data_json() at startup to encrypt it."
        )
        return stored

    try:
        token = stored[len(_ENC_PREFIX):]
        return _FERNET.decrypt(token.encode()).decode()
    except InvalidToken:
        logger.error(
            "[SK-CRYPTO] InvalidToken: cannot decrypt SK key. "
            "Was STRIPE_KEY_ENCRYPTION_SECRET changed? The key is now invalid."
        )
        return None
    except Exception as exc:
        logger.error(f"[SK-CRYPTO] Unexpected decryption error: {exc}")
        return None


def migrate_data_json(path: str = "data.json") -> int:
    """
    Scan *path* for plaintext Stripe SK keys and encrypt them in-place.

    Returns the number of keys migrated.  Safe to call at startup — already-
    encrypted values are skipped automatically (idempotent).
    """
    migrated = 0
    with _migrate_lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return 0
        except Exception as exc:
            logger.error(f"[SK-CRYPTO] migrate_data_json: cannot read {path}: {exc}")
            return 0

        changed = False
        for uid, udata in data.items():
            if not isinstance(udata, dict):
                continue
            raw = udata.get("sk_key")
            if not raw or is_encrypted(raw):
                continue  # absent or already encrypted → skip
            # Validate that it looks like a Stripe key before encrypting
            if not (isinstance(raw, str) and (
                raw.startswith("sk_live_") or raw.startswith("sk_test_")
            )):
                logger.warning(
                    f"[SK-CRYPTO] uid={uid}: sk_key value looks invalid "
                    f"({raw[:12]}...), skipping migration."
                )
                continue
            try:
                data[uid]["sk_key"] = encrypt_sk(raw)
                migrated += 1
                changed = True
                logger.info(f"[SK-CRYPTO] Encrypted SK key for uid={uid}.")
            except Exception as exc:
                logger.error(
                    f"[SK-CRYPTO] Failed to encrypt SK for uid={uid}: {exc}"
                )

        if changed:
            # Atomic write: write to temp file first, then replace
            tmp_path = path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                os.replace(tmp_path, path)
                logger.info(
                    f"[SK-CRYPTO] Migration complete. {migrated} key(s) encrypted."
                )
            except Exception as exc:
                logger.error(f"[SK-CRYPTO] Failed to write migrated data: {exc}")
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    return migrated
