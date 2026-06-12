"""Encrypted keystore management for the blockchain plugin.

Keys are stored as standard Web3 Secret Storage (scrypt) keystore JSON
files under ``$HERMES_HOME/blockchain/keystores/<name>.json`` — the same
format Geth and MetaMask export. Private keys are NEVER written in clear
text and NEVER returned to the agent.

The encryption password is read exclusively from the ``HERMES_WALLET_PASSWORD``
environment variable (a credential — belongs in ``~/.hermes/.env``), so it
never passes through tool arguments, the conversation, or the logs.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

from eth_account import Account

from hermes_constants import get_hermes_home

_lock = threading.RLock()
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Enable HD-wallet (mnemonic) features in eth-account.
Account.enable_unaudited_hdwallet_features()


class WalletError(Exception):
    """User-facing wallet error (bad name, missing password, wrong password)."""


def _keystore_dir() -> Path:
    d = get_hermes_home() / "blockchain" / "keystores"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _keystore_path(name: str) -> Path:
    if not _NAME_RE.match(name or ""):
        raise WalletError(
            "Wallet name must be 1-64 chars of letters, digits, '-' or '_'."
        )
    return _keystore_dir() / f"{name}.json"


def _password() -> str:
    pw = os.getenv("HERMES_WALLET_PASSWORD")
    if not pw:
        raise WalletError(
            "HERMES_WALLET_PASSWORD is not set. Add it to ~/.hermes/.env "
            "(it is a credential). It encrypts your keystores at rest and is "
            "required to create, import, or sign with a wallet."
        )
    return pw


def list_wallets() -> List[Dict[str, str]]:
    """Return [{name, address}] for every keystore on disk (no secrets)."""
    out: List[Dict[str, str]] = []
    for p in sorted(_keystore_dir().glob("*.json")):
        try:
            data = json.loads(p.read_text("utf-8"))
            addr = data.get("address", "")
            if addr and not addr.startswith("0x"):
                addr = "0x" + addr
            out.append({"name": p.stem, "address": addr})
        except Exception:
            continue
    return out


def wallet_exists(name: str) -> bool:
    return _keystore_path(name).exists()


def _save_keystore(name: str, private_key: str, *, overwrite: bool) -> str:
    path = _keystore_path(name)
    if path.exists() and not overwrite:
        raise WalletError(f"Wallet '{name}' already exists. Choose another name.")
    acct = Account.from_key(private_key)
    keystore = Account.encrypt(private_key, _password())
    with _lock:
        path.write_text(json.dumps(keystore), "utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return acct.address


def create_wallet(name: str, overwrite: bool = False) -> str:
    """Generate a fresh random account, persist it encrypted. Returns address."""
    acct = Account.create()
    return _save_keystore(name, acct.key.hex(), overwrite=overwrite)


def import_private_key(name: str, private_key: str, overwrite: bool = False) -> str:
    """Import from a hex private key. Returns the address."""
    pk = private_key.strip()
    if not pk.startswith("0x"):
        pk = "0x" + pk
    try:
        Account.from_key(pk)
    except Exception as exc:
        raise WalletError(f"Invalid private key: {exc}") from exc
    return _save_keystore(name, pk, overwrite=overwrite)


def import_mnemonic(name: str, mnemonic: str, account_index: int = 0,
                    overwrite: bool = False) -> str:
    """Import from a BIP-39 mnemonic (default derivation path). Returns address."""
    try:
        acct = Account.from_mnemonic(
            mnemonic.strip(),
            account_path=f"m/44'/60'/0'/0/{int(account_index)}",
        )
    except Exception as exc:
        raise WalletError(f"Invalid mnemonic: {exc}") from exc
    return _save_keystore(name, acct.key.hex(), overwrite=overwrite)


def get_address(name: str) -> str:
    """Read the address for a wallet without decrypting the key."""
    path = _keystore_path(name)
    if not path.exists():
        raise WalletError(f"Wallet '{name}' not found.")
    addr = json.loads(path.read_text("utf-8")).get("address", "")
    if addr and not addr.startswith("0x"):
        addr = "0x" + addr
    from eth_utils import to_checksum_address
    return to_checksum_address(addr)


def load_account(name: str):
    """Decrypt and return an eth_account LocalAccount for signing.

    The decrypted key stays inside this object; callers sign with it and
    never read ``.key`` out to the agent.
    """
    path = _keystore_path(name)
    if not path.exists():
        raise WalletError(f"Wallet '{name}' not found.")
    try:
        key = Account.decrypt(json.loads(path.read_text("utf-8")), _password())
    except ValueError as exc:
        raise WalletError(
            "Could not decrypt keystore — HERMES_WALLET_PASSWORD is wrong "
            "for this wallet."
        ) from exc
    return Account.from_key(key)


def require_account(name: Optional[str]):
    """Resolve the named wallet, or the active one when name is None."""
    from . import chains
    target = name or chains.active_wallet_name()
    if not target:
        raise WalletError(
            "No active wallet. Create one with blockchain_wallet (action=create) "
            "or select one with action=use."
        )
    return load_account(target)
