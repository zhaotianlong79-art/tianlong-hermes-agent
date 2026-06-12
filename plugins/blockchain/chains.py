"""EVM network registry + persisted state for the blockchain plugin.

Holds the built-in chain table (mainnets + testnets), lets the user add
custom RPCs at runtime, and persists the active wallet / active network /
custom networks under ``$HERMES_HOME/blockchain/state.json`` so each
profile keeps its own state.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Optional

from hermes_constants import get_hermes_home


@dataclass(frozen=True)
class Network:
    name: str          # canonical key, e.g. "base"
    label: str         # human label, e.g. "Base"
    chain_id: int
    rpc_url: str
    symbol: str        # native token symbol
    explorer: str = "" # block-explorer base URL (no trailing slash)
    testnet: bool = False

    def explorer_tx(self, tx_hash: str) -> str:
        return f"{self.explorer}/tx/{tx_hash}" if self.explorer else ""

    def explorer_address(self, addr: str) -> str:
        return f"{self.explorer}/address/{addr}" if self.explorer else ""


# Public RPCs are fine for reads and light writes; for production volume the
# user should add a custom RPC (Alchemy/Infura/etc.) via blockchain_network add.
_BUILTIN: Dict[str, Network] = {
    n.name: n
    for n in [
        # ---- mainnets ----
        Network("ethereum", "Ethereum", 1, "https://eth.llamarpc.com", "ETH", "https://etherscan.io"),
        Network("base", "Base", 8453, "https://mainnet.base.org", "ETH", "https://basescan.org"),
        Network("arbitrum", "Arbitrum One", 42161, "https://arb1.arbitrum.io/rpc", "ETH", "https://arbiscan.io"),
        Network("optimism", "OP Mainnet", 10, "https://mainnet.optimism.io", "ETH", "https://optimistic.etherscan.io"),
        Network("polygon", "Polygon", 137, "https://polygon-rpc.com", "POL", "https://polygonscan.com"),
        Network("bnb", "BNB Smart Chain", 56, "https://bsc-dataseed.binance.org", "BNB", "https://bscscan.com"),
        Network("avalanche", "Avalanche C-Chain", 43114, "https://api.avax.network/ext/bc/C/rpc", "AVAX", "https://snowtrace.io"),
        # ---- testnets ----
        Network("sepolia", "Ethereum Sepolia", 11155111, "https://ethereum-sepolia-rpc.publicnode.com", "ETH", "https://sepolia.etherscan.io", testnet=True),
        Network("holesky", "Ethereum Holesky", 17000, "https://ethereum-holesky-rpc.publicnode.com", "ETH", "https://holesky.etherscan.io", testnet=True),
        Network("base-sepolia", "Base Sepolia", 84532, "https://sepolia.base.org", "ETH", "https://sepolia.basescan.org", testnet=True),
        Network("arbitrum-sepolia", "Arbitrum Sepolia", 421614, "https://sepolia-rollup.arbitrum.io/rpc", "ETH", "https://sepolia.arbiscan.io", testnet=True),
    ]
}

_DEFAULT_NETWORK = "base"

_lock = threading.RLock()


def _state_dir() -> Path:
    d = get_hermes_home() / "blockchain"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path() -> Path:
    return _state_dir() / "state.json"


def _load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    _state_path().write_text(json.dumps(state, indent=2, ensure_ascii=False), "utf-8")


# --------------------------------------------------------------------------
# custom networks
# --------------------------------------------------------------------------

def _custom_networks() -> Dict[str, Network]:
    out: Dict[str, Network] = {}
    for key, raw in (_load_state().get("custom_networks") or {}).items():
        try:
            out[key] = Network(
                name=key,
                label=raw.get("label", key),
                chain_id=int(raw["chain_id"]),
                rpc_url=raw["rpc_url"],
                symbol=raw.get("symbol", "ETH"),
                explorer=raw.get("explorer", ""),
                testnet=bool(raw.get("testnet", False)),
            )
        except Exception:
            continue
    return out


def add_custom_network(name: str, rpc_url: str, chain_id: int,
                       symbol: str = "ETH", label: str = "",
                       explorer: str = "", testnet: bool = False) -> Network:
    """Persist a user-supplied chain. Raises ValueError on a builtin clash."""
    key = name.strip().lower().replace(" ", "-")
    if key in _BUILTIN:
        raise ValueError(f"'{key}' is a built-in network and cannot be overridden")
    net = Network(key, label or name, int(chain_id), rpc_url.strip(),
                  symbol.strip() or "ETH", explorer.strip(), bool(testnet))
    with _lock:
        state = _load_state()
        customs = state.setdefault("custom_networks", {})
        customs[key] = {k: v for k, v in asdict(net).items() if k != "name"}
        _save_state(state)
    return net


def all_networks() -> Dict[str, Network]:
    merged = dict(_BUILTIN)
    merged.update(_custom_networks())
    return merged


def get_network(name: Optional[str] = None) -> Network:
    """Resolve a network by name, or the active one when name is None."""
    networks = all_networks()
    key = (name or active_network_name()).strip().lower()
    if key not in networks:
        raise ValueError(
            f"Unknown network '{key}'. Known: {', '.join(sorted(networks))}. "
            f"Add a custom RPC with blockchain_network add."
        )
    return networks[key]


# --------------------------------------------------------------------------
# active selection
# --------------------------------------------------------------------------

def active_network_name() -> str:
    return _load_state().get("active_network") or _DEFAULT_NETWORK


def set_active_network(name: str) -> Network:
    net = get_network(name)
    with _lock:
        state = _load_state()
        state["active_network"] = net.name
        _save_state(state)
    return net


def active_wallet_name() -> Optional[str]:
    return _load_state().get("active_wallet")


def set_active_wallet(name: str) -> None:
    with _lock:
        state = _load_state()
        state["active_wallet"] = name
        _save_state(state)
