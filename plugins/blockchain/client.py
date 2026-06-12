"""Web3 client layer — RPC connection, reads, gas, and signed broadcasts.

This module is the only place that talks to a chain. It builds a Web3
instance per network, reads balances / gas / receipts, handles the minimal
ERC-20 surface, and signs+broadcasts transactions with a decrypted account.
All amounts cross this boundary in human units (ether / token units); wei
conversion lives here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from eth_utils import to_checksum_address
from web3 import Web3

from . import chains
from .chains import Network

# Minimal ERC-20 ABI — balance, metadata, transfer, approve, allowance.
ERC20_ABI: List[dict] = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]


class ChainError(Exception):
    """RPC / connectivity / on-chain revert error."""


def web3_for(network: Optional[str] = None) -> Tuple[Web3, Network]:
    net = chains.get_network(network)
    w3 = Web3(Web3.HTTPProvider(net.rpc_url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise ChainError(f"Cannot reach RPC for {net.label} ({net.rpc_url}).")
    return w3, net


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def native_balance(address: str, network: Optional[str] = None) -> Dict[str, Any]:
    w3, net = web3_for(network)
    addr = to_checksum_address(address)
    wei = w3.eth.get_balance(addr)
    return {
        "address": addr, "network": net.name, "symbol": net.symbol,
        "balance_wei": wei, "balance": str(Web3.from_wei(wei, "ether")),
    }


def erc20_metadata(w3: Web3, token: str) -> Tuple[Any, int, str]:
    c = w3.eth.contract(address=to_checksum_address(token), abi=ERC20_ABI)
    try:
        decimals = c.functions.decimals().call()
    except Exception:
        decimals = 18
    try:
        symbol = c.functions.symbol().call()
    except Exception:
        symbol = "TOKEN"
    return c, int(decimals), str(symbol)


def token_raw_balance(w3: Web3, token: str, owner: str) -> int:
    """Raw (undecimated) ERC-20 balance of ``owner`` on an existing w3.

    Reuses the caller's connection (unlike :func:`token_balance`, which opens
    its own) so transfer pre-flight can check the token balance cheaply.
    """
    c = w3.eth.contract(address=to_checksum_address(token), abi=ERC20_ABI)
    return int(c.functions.balanceOf(to_checksum_address(owner)).call())


def token_balance(address: str, token: str, network: Optional[str] = None) -> Dict[str, Any]:
    w3, net = web3_for(network)
    addr = to_checksum_address(address)
    c, decimals, symbol = erc20_metadata(w3, token)
    raw = c.functions.balanceOf(addr).call()
    return {
        "address": addr, "network": net.name, "token": to_checksum_address(token),
        "symbol": symbol, "decimals": decimals,
        "balance_raw": raw, "balance": str(Decimal(raw) / (Decimal(10) ** decimals)),
    }


def gas_snapshot(network: Optional[str] = None) -> Dict[str, Any]:
    w3, net = web3_for(network)
    gas_price = w3.eth.gas_price
    return {
        "network": net.name,
        "gas_price_wei": gas_price,
        "gas_price_gwei": str(Web3.from_wei(gas_price, "gwei")),
    }


def tx_status(tx_hash: str, network: Optional[str] = None) -> Dict[str, Any]:
    w3, net = web3_for(network)
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception:
        return {"network": net.name, "tx_hash": tx_hash, "status": "pending_or_unknown",
                "explorer_url": net.explorer_tx(tx_hash)}
    return {
        "network": net.name, "tx_hash": tx_hash,
        "status": "success" if receipt.status == 1 else "failed",
        "block_number": receipt.blockNumber,
        "gas_used": receipt.gasUsed,
        "explorer_url": net.explorer_tx(tx_hash),
    }


def tx_history(address: str, network: Optional[str] = None, limit: int = 10) -> Dict[str, Any]:
    """Recent transactions for an address via the Etherscan V2 unified API.

    One ETHERSCAN_API_KEY works across all Etherscan-family chains, routed by
    chainid. Raises ChainError with guidance when the key is missing or the
    chain is unsupported.
    """
    import os

    import requests

    net = chains.get_network(network)
    api_key = os.getenv("ETHERSCAN_API_KEY")
    if not api_key:
        raise ChainError(
            "ETHERSCAN_API_KEY is not set. Add it to ~/.hermes/.env (free key "
            "from etherscan.io). One key works across Ethereum, Base, Arbitrum, "
            "Optimism, Polygon, BNB, and Avalanche via the Etherscan V2 API."
        )
    addr = to_checksum_address(address)
    limit = max(1, min(int(limit), 100))
    resp = requests.get(
        "https://api.etherscan.io/v2/api",
        params={
            "chainid": net.chain_id, "module": "account", "action": "txlist",
            "address": addr, "startblock": 0, "endblock": 99999999,
            "page": 1, "offset": limit, "sort": "desc", "apikey": api_key,
        },
        timeout=30,
    )
    data = resp.json()
    # status "0" with "No transactions found" is a valid empty result.
    if str(data.get("status")) != "1":
        msg = data.get("message", "")
        result = data.get("result")
        if "No transactions found" in str(msg) or result == []:
            return {"network": net.name, "address": addr, "count": 0, "transactions": []}
        raise ChainError(f"Etherscan V2 error for {net.label}: {msg} {result if isinstance(result, str) else ''}".strip())
    txs = []
    for tx in data.get("result", [])[:limit]:
        txs.append({
            "tx_hash": tx.get("hash"),
            "from": tx.get("from"),
            "to": tx.get("to"),
            "value": str(Web3.from_wei(int(tx.get("value", 0)), "ether")),
            "symbol": net.symbol,
            "timestamp": int(tx.get("timeStamp", 0)),
            "block": int(tx.get("blockNumber", 0)),
            "status": "success" if tx.get("isError") == "0" else "failed",
            "explorer_url": net.explorer_tx(tx.get("hash", "")),
        })
    return {"network": net.name, "address": addr, "count": len(txs), "transactions": txs}


def read_contract(address: str, abi: list, method: str, args: list,
                  network: Optional[str] = None) -> Dict[str, Any]:
    w3, net = web3_for(network)
    c = w3.eth.contract(address=to_checksum_address(address), abi=abi)
    fn = c.functions[method](*(args or []))
    result = fn.call()
    return {"network": net.name, "address": to_checksum_address(address),
            "method": method, "result": _jsonable(result)}


# --------------------------------------------------------------------------
# transaction building / signing
# --------------------------------------------------------------------------

def to_wei_amount(amount: str, decimals: int = 18) -> int:
    return int(Decimal(str(amount)) * (Decimal(10) ** decimals))


# Fallback gas limits used when estimation reverts (e.g. previewing a
# transfer from an account that can't yet cover gas). Lets the preview
# still render with an insufficient-funds warning instead of a raw error.
_GAS_FALLBACK = {"native": 21_000, "token": 65_000, "contract": 150_000, "deploy": 1_500_000}


def _estimate_gas(w3: Web3, tx: Dict[str, Any], kind: str) -> Tuple[int, bool]:
    """Return (gas_limit, estimated). Falls back to a standard limit on revert."""
    try:
        return int(w3.eth.estimate_gas(tx)), True
    except Exception:
        return _GAS_FALLBACK[kind], False


def build_native_transfer(w3: Web3, net: Network, sender: str, to: str,
                          amount_eth: str) -> Dict[str, Any]:
    value = Web3.to_wei(Decimal(str(amount_eth)), "ether")
    tx = {
        "from": sender, "to": to_checksum_address(to), "value": value,
        "chainId": net.chain_id, "nonce": w3.eth.get_transaction_count(sender),
    }
    tx["gas"], tx["_gas_estimated"] = _estimate_gas(w3, dict(tx), "native")
    _apply_fees(w3, tx)
    return tx


def build_token_transfer(w3: Web3, net: Network, sender: str, token: str, to: str,
                         amount: str) -> Tuple[Dict[str, Any], int, str]:
    c, decimals, symbol = erc20_metadata(w3, token)
    raw = to_wei_amount(amount, decimals)
    fn = c.functions.transfer(to_checksum_address(to), raw)
    gas, estimated = _estimate_gas(w3, {"from": sender, "to": to_checksum_address(token),
                                        "data": fn._encode_transaction_data()}, "token")
    tx = fn.build_transaction({
        "from": sender, "chainId": net.chain_id, "gas": gas,
        "nonce": w3.eth.get_transaction_count(sender),
    })
    tx["_gas_estimated"] = estimated
    _apply_fees(w3, tx)
    return tx, decimals, symbol


def build_contract_write(w3: Web3, net: Network, sender: str, address: str,
                         abi: list, method: str, args: list,
                         value_eth: str = "0") -> Dict[str, Any]:
    c = w3.eth.contract(address=to_checksum_address(address), abi=abi)
    fn = c.functions[method](*(args or []))
    params: Dict[str, Any] = {
        "from": sender, "chainId": net.chain_id,
        "nonce": w3.eth.get_transaction_count(sender),
    }
    if value_eth and Decimal(str(value_eth)) > 0:
        params["value"] = Web3.to_wei(Decimal(str(value_eth)), "ether")
    est_tx = {"from": sender, "to": to_checksum_address(address),
              "data": fn._encode_transaction_data()}
    if "value" in params:
        est_tx["value"] = params["value"]
    params["gas"], estimated = _estimate_gas(w3, est_tx, "contract")
    tx = fn.build_transaction(params)
    tx["_gas_estimated"] = estimated
    _apply_fees(w3, tx)
    return tx


def _apply_fees(w3: Web3, tx: Dict[str, Any]) -> None:
    """Fill gas + EIP-1559 fees. Assumes gas limit is already set by the caller."""
    if "gas" not in tx:
        tx["gas"], _ = _estimate_gas(w3, dict(tx), "contract")
    try:
        base = w3.eth.get_block("latest").get("baseFeePerGas")
    except Exception:
        base = None
    if base is not None:
        priority = w3.eth.max_priority_fee
        tx["maxPriorityFeePerGas"] = priority
        tx["maxFeePerGas"] = base * 2 + priority
    else:  # legacy chains
        tx["gasPrice"] = w3.eth.gas_price


def estimate_cost_wei(tx: Dict[str, Any]) -> int:
    gas = int(tx.get("gas", 0))
    price = int(tx.get("maxFeePerGas") or tx.get("gasPrice") or 0)
    return gas * price


def sign_and_send(account, tx: Dict[str, Any], net: Network) -> Dict[str, Any]:
    w3 = Web3(Web3.HTTPProvider(net.rpc_url, request_kwargs={"timeout": 30}))
    # Strip internal markers (e.g. _gas_estimated) — not valid tx fields.
    tx = {k: v for k, v in tx.items() if not k.startswith("_")}
    signed = account.sign_transaction(tx)
    try:
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception as exc:
        raise ChainError(f"Broadcast failed: {exc}") from exc
    h = tx_hash.hex()
    if not h.startswith("0x"):
        h = "0x" + h
    return {
        "status": "submitted", "tx_hash": h,
        "network": net.name, "explorer_url": net.explorer_tx(h),
        "from": tx.get("from"), "to": tx.get("to"),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, int) and abs(value) > 2**53:
        return str(value)
    return value
