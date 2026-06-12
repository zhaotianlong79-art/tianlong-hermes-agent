"""Uniswap V3 swap helper — quote (QuoterV2) + execute (SwapRouter02).

A high-level wrapper over the doc's "Swap" capability. Handles:
  - ERC-20 -> ERC-20
  - native (ETH) -> ERC-20   (router wraps via msg.value)
  - ERC-20 -> native (ETH)   (multicall: swap to router, then unwrapWETH9)

Quotes via QuoterV2.quoteExactInputSingle for slippage-protected minOut, and
reports whether an ERC-20 approval to the router is still required.

Router/Quoter/WETH addresses are the canonical Uniswap V3 deployments per
chain. They can be overridden per call for chains not listed here. ALWAYS
verify addresses for high-value swaps.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from eth_utils import to_checksum_address
from web3 import Web3

from . import client
from .chains import Network

# Sentinels used by SwapRouter02 for recipient routing.
_MSG_SENDER = "0x0000000000000000000000000000000000000001"
_ADDRESS_THIS = "0x0000000000000000000000000000000000000002"

# Canonical Uniswap V3 deployments (SwapRouter02, QuoterV2, wrapped native).
_DEPLOYMENTS: Dict[str, Dict[str, str]] = {
    "ethereum": {
        "router": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    },
    "arbitrum": {
        "router": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "weth": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    },
    "optimism": {
        "router": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "weth": "0x4200000000000000000000000000000000000006",
    },
    "polygon": {
        "router": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "weth": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",  # WMATIC/WPOL
    },
    "base": {
        "router": "0x2626664c2603336E57B271c5C0b26F421741e481",
        "quoter": "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
        "weth": "0x4200000000000000000000000000000000000006",
    },
}

QUOTER_V2_ABI = [{
    "inputs": [{"components": [
        {"name": "tokenIn", "type": "address"},
        {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "fee", "type": "uint24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"},
    ], "name": "params", "type": "tuple"}],
    "name": "quoteExactInputSingle",
    "outputs": [
        {"name": "amountOut", "type": "uint256"},
        {"name": "sqrtPriceX96After", "type": "uint160"},
        {"name": "initializedTicksCrossed", "type": "uint32"},
        {"name": "gasEstimate", "type": "uint256"},
    ],
    "stateMutability": "nonpayable", "type": "function",
}]

SWAP_ROUTER_02_ABI = [
    {"inputs": [{"components": [
        {"name": "tokenIn", "type": "address"},
        {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"},
        {"name": "recipient", "type": "address"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMinimum", "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"},
    ], "name": "params", "type": "tuple"}],
     "name": "exactInputSingle", "outputs": [{"name": "amountOut", "type": "uint256"}],
     "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "amountMinimum", "type": "uint256"}, {"name": "recipient", "type": "address"}],
     "name": "unwrapWETH9", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "data", "type": "bytes[]"}],
     "name": "multicall", "outputs": [{"name": "results", "type": "bytes[]"}],
     "stateMutability": "payable", "type": "function"},
]

NATIVE = "native"


class SwapError(Exception):
    """Swap-specific failure (unsupported chain, no quote)."""


def apply_slippage(out_raw: int, slippage_bps: int) -> int:
    """Minimum acceptable output after tolerating ``slippage_bps`` basis points.

    1 bp = 0.01%. 50 bps = 0.5%. Clamped to a sane [0, 10000] range so a bad
    caller can't produce a negative floor (which would disable slippage
    protection entirely) or one above the quote.
    """
    bps = max(0, min(int(slippage_bps), 10_000))
    return int(out_raw) * (10_000 - bps) // 10_000


def _deployment(net: Network, router: Optional[str], quoter: Optional[str],
                weth: Optional[str]) -> Dict[str, str]:
    d = dict(_DEPLOYMENTS.get(net.name, {}))
    if router:
        d["router"] = router
    if quoter:
        d["quoter"] = quoter
    if weth:
        d["weth"] = weth
    if not all(d.get(k) for k in ("router", "quoter", "weth")):
        raise SwapError(
            f"Uniswap V3 addresses unknown for '{net.name}'. Pass router, "
            f"quoter and weth explicitly, or use a supported chain "
            f"({', '.join(sorted(_DEPLOYMENTS))})."
        )
    return {k: to_checksum_address(v) for k, v in d.items()}


def _resolve_token(token: str, weth: str) -> str:
    if token == NATIVE:
        return weth
    return to_checksum_address(token)


def quote(w3: Web3, net: Network, *, token_in: str, token_out: str, amount: str,
          fee: int, slippage_bps: int, dep: Dict[str, str]) -> Dict[str, Any]:
    """Return decimals, symbols, raw amounts, and slippage-protected minOut."""
    in_addr = _resolve_token(token_in, dep["weth"])
    out_addr = _resolve_token(token_out, dep["weth"])

    in_dec = 18 if token_in == NATIVE else client.erc20_metadata(w3, in_addr)[1]
    out_c, out_dec, out_sym = (None, 18, net.symbol) if token_out == NATIVE \
        else client.erc20_metadata(w3, out_addr)
    in_sym = net.symbol if token_in == NATIVE else client.erc20_metadata(w3, in_addr)[2]

    amount_raw = int(Decimal(str(amount)) * (Decimal(10) ** in_dec))
    quoter = w3.eth.contract(address=dep["quoter"], abi=QUOTER_V2_ABI)
    try:
        out_raw = quoter.functions.quoteExactInputSingle(
            (in_addr, out_addr, amount_raw, int(fee), 0)
        ).call()[0]
    except Exception as exc:
        raise SwapError(
            f"No Uniswap V3 quote for this pair at fee tier {fee} on {net.label} "
            f"({exc}). Try a different fee tier (500 / 3000 / 10000)."
        ) from exc
    min_out_raw = apply_slippage(out_raw, slippage_bps)
    return {
        "token_in": in_sym, "token_out": out_sym,
        "amount_in": str(amount), "amount_in_raw": amount_raw,
        "expected_out": str(Decimal(out_raw) / (Decimal(10) ** out_dec)),
        "expected_out_raw": out_raw,
        "min_out": str(Decimal(min_out_raw) / (Decimal(10) ** out_dec)),
        "min_out_raw": min_out_raw,
        "in_addr": in_addr, "out_addr": out_addr,
        "in_decimals": in_dec, "out_decimals": out_dec,
    }


def needs_approval(w3: Web3, *, token_in: str, owner: str, amount_raw: int,
                   dep: Dict[str, str]) -> bool:
    if token_in == NATIVE:
        return False
    c = w3.eth.contract(address=_resolve_token(token_in, dep["weth"]), abi=client.ERC20_ABI)
    allowance = c.functions.allowance(to_checksum_address(owner), dep["router"]).call()
    return allowance < amount_raw


def build_approve(w3: Web3, net: Network, *, token: str, owner: str,
                  amount_raw: int, dep: Dict[str, str]) -> Dict[str, Any]:
    c = w3.eth.contract(address=_resolve_token(token, dep["weth"]), abi=client.ERC20_ABI)
    fn = c.functions.approve(dep["router"], amount_raw)
    tx = fn.build_transaction({
        "from": to_checksum_address(owner), "chainId": net.chain_id,
        "nonce": w3.eth.get_transaction_count(to_checksum_address(owner)),
        "gas": client._GAS_FALLBACK["token"],
    })
    client._apply_fees(w3, tx)
    return tx


def swap_routing(token_in: str, token_out: str, sender: str,
                 amount_in_raw: int) -> Tuple[bool, str, int]:
    """Pure routing decision for a swap. Returns (eth_out, recipient, value).

    - token_out == native -> output must land in the router (recipient =
      _ADDRESS_THIS) so a follow-up unwrapWETH9 can send native to the user.
    - otherwise the router sends the output token straight to the sender.
    - token_in == native -> the input is sent as msg.value; else value = 0.
    """
    eth_out = token_out == NATIVE
    recipient = _ADDRESS_THIS if eth_out else to_checksum_address(sender)
    value = int(amount_in_raw) if token_in == NATIVE else 0
    return eth_out, to_checksum_address(recipient), value


def build_swap(w3: Web3, net: Network, *, sender: str, token_in: str, token_out: str,
               q: Dict[str, Any], fee: int, dep: Dict[str, str],
               nonce: Optional[int] = None) -> Dict[str, Any]:
    router = w3.eth.contract(address=dep["router"], abi=SWAP_ROUTER_02_ABI)
    sender = to_checksum_address(sender)
    eth_out, recipient, value = swap_routing(token_in, token_out, sender, q["amount_in_raw"])
    params = (q["in_addr"], q["out_addr"], int(fee), recipient,
              q["amount_in_raw"], q["min_out_raw"], 0)

    tx_params: Dict[str, Any] = {
        "from": sender, "chainId": net.chain_id,
        "nonce": nonce if nonce is not None else w3.eth.get_transaction_count(sender),
    }
    if value:
        tx_params["value"] = value

    if eth_out:
        # multicall([exactInputSingle -> router, unwrapWETH9 -> sender])
        swap_data = router.functions.exactInputSingle(params)._encode_transaction_data()
        unwrap_data = router.functions.unwrapWETH9(
            q["min_out_raw"], sender)._encode_transaction_data()
        fn = router.functions.multicall([swap_data, unwrap_data])
    else:
        fn = router.functions.exactInputSingle(params)

    # Assemble the tx directly from encoded calldata rather than via
    # fn.build_transaction(), which eagerly RPCs for fee data that
    # _apply_fees immediately overwrites (and which can't run offline).
    tx = {
        **tx_params,
        "to": dep["router"],
        "data": fn._encode_transaction_data(),
        "gas": client._GAS_FALLBACK["contract"] * 2,  # swaps are heavier
    }
    client._apply_fees(w3, tx)
    return tx


def deployment_for(net: Network, router=None, quoter=None, weth=None) -> Dict[str, str]:
    return _deployment(net, router, quoter, weth)
