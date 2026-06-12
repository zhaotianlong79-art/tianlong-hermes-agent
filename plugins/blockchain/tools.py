"""Blockchain tool schemas + handlers (EVM, real signing).

Five action-based tools mirroring the doc's tool categories:

  blockchain_wallet    create / import / list / use / show / balance
  blockchain_network   list / current / use / add / gas
  blockchain_transfer  native or ERC-20 transfer  (two-step CONFIRM)
  blockchain_contract  read / write / deploy       (two-step CONFIRM)
  blockchain_tx        status

Every asset-changing call follows the doc's confirmation protocol: the
first call returns a structured preview (gas, risk level, warnings,
need_confirmation=true) and broadcasts NOTHING. The agent shows it to the
user; only after the user replies CONFIRM does the agent re-call with
confirm=true to actually sign and send.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from tools.registry import tool_error, tool_result

from . import chains, client, safety, uniswap, wallet
from .safety import SafetyError
from .uniswap import SwapError
from .wallet import WalletError

_STR = {"type": "string"}


def check_available() -> bool:
    """Tools are available once web3 is importable (always, post-install)."""
    try:
        import web3  # noqa: F401
        return True
    except Exception:
        return False


def _err(exc: Exception) -> str:
    if isinstance(exc, (WalletError, SafetyError, SwapError, client.ChainError, ValueError)):
        return tool_error(str(exc))
    return tool_error(f"{type(exc).__name__}: {exc}")


def _confirmed(args: dict) -> bool:
    """Whether the user has explicitly authorized broadcasting.

    Deliberately STRICT: a real boolean ``true`` (the schema type) or the
    literal word ``confirm`` only. Loose affirmatives like "yes"/"1"/"ok" are
    NOT accepted — the two-step gate exists so the agent can't accidentally
    sign real funds by passing a casual string. The user types CONFIRM; the
    agent translates that to confirm=true.
    """
    raw = args.get("confirm")
    if isinstance(raw, bool):
        return raw
    return isinstance(raw, str) and raw.strip().lower() == "confirm"


def _preview(action: str, *, network: str, risk: str, warnings: List[str],
             gas_cost_wei: int, symbol: str, **fields) -> str:
    out: Dict[str, Any] = {
        "success": True,
        "need_confirmation": True,
        "action": action,
        "network": network,
        "risk_level": risk,
        "estimated_gas_native": f"{gas_cost_wei / 1e18:.8f}".rstrip("0").rstrip("."),
        "estimated_gas_symbol": symbol,
    }
    out.update(fields)
    if warnings:
        out["warnings"] = warnings
    out["instruction"] = (
        "Show this preview to the user. Only re-call this tool with confirm=true "
        "after the user explicitly replies CONFIRM."
    )
    return tool_result(out)


# ==========================================================================
# blockchain_wallet
# ==========================================================================

WALLET_SCHEMA = {
    "name": "blockchain_wallet",
    "description": (
        "Manage EVM wallets. Actions: 'create' (new random wallet), 'import' "
        "(from private_key or mnemonic), 'list', 'use' (set active wallet), "
        "'show' (active wallet address), 'balance' (native + optional ERC-20 "
        "token). Keys are stored encrypted with HERMES_WALLET_PASSWORD and are "
        "NEVER returned. Never asks for or reveals private keys/mnemonics in "
        "plain text beyond the one import call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "import", "list", "use", "show", "balance"]},
            "name": {"type": "string", "description": "Wallet name (letters/digits/-/_)."},
            "private_key": {"type": "string", "description": "For action=import."},
            "mnemonic": {"type": "string", "description": "For action=import (BIP-39)."},
            "address": {"type": "string", "description": "For action=balance; defaults to active wallet."},
            "token": {"type": "string", "description": "ERC-20 contract for a token balance."},
            "network": {"type": "string", "description": "Network key; defaults to active."},
            "overwrite": {"type": "boolean"},
        },
        "required": ["action"],
    },
}


def handle_wallet(args: dict, **kw) -> str:
    action = str(args.get("action", "")).strip().lower()
    try:
        if action == "create":
            name = args["name"]
            addr = wallet.create_wallet(name, overwrite=bool(args.get("overwrite")))
            chains.set_active_wallet(name)
            return tool_result(success=True, action="create", name=name, address=addr,
                               active=True, note="Active wallet set. Back up your keystore "
                               "under $HERMES_HOME/blockchain/keystores.")
        if action == "import":
            name = args["name"]
            if args.get("private_key"):
                addr = wallet.import_private_key(name, args["private_key"], bool(args.get("overwrite")))
            elif args.get("mnemonic"):
                addr = wallet.import_mnemonic(name, args["mnemonic"], overwrite=bool(args.get("overwrite")))
            else:
                return tool_error("Provide either private_key or mnemonic to import.")
            chains.set_active_wallet(name)
            return tool_result(success=True, action="import", name=name, address=addr, active=True)
        if action == "list":
            return tool_result(success=True, action="list", wallets=wallet.list_wallets(),
                               active=chains.active_wallet_name())
        if action == "use":
            name = args["name"]
            if not wallet.wallet_exists(name):
                return tool_error(f"Wallet '{name}' not found.")
            chains.set_active_wallet(name)
            return tool_result(success=True, action="use", active=name, address=wallet.get_address(name))
        if action == "show":
            name = chains.active_wallet_name()
            if not name:
                return tool_error("No active wallet. Create or import one first.")
            return tool_result(success=True, action="show", active=name, address=wallet.get_address(name))
        if action == "balance":
            address = args.get("address")
            if not address:
                name = chains.active_wallet_name()
                if not name:
                    return tool_error("No address given and no active wallet.")
                address = wallet.get_address(name)
            address = safety.validate_address(address)
            net = args.get("network")
            result = client.native_balance(address, net)
            if args.get("token"):
                result["token_balance"] = client.token_balance(address, args["token"], net)
            return tool_result(success=True, action="balance", **result)
        return tool_error(f"Unknown wallet action '{action}'.")
    except KeyError as exc:
        return tool_error(f"Missing required argument: {exc}")
    except Exception as exc:
        return _err(exc)


# ==========================================================================
# blockchain_network
# ==========================================================================

NETWORK_SCHEMA = {
    "name": "blockchain_network",
    "description": (
        "Manage EVM networks. Actions: 'list' (all known chains), 'current' "
        "(active network), 'use' (switch active network), 'add' (register a "
        "custom RPC), 'gas' (current gas price). Supports mainnets (ethereum, "
        "base, arbitrum, optimism, polygon, bnb, avalanche) and testnets "
        "(sepolia, holesky, base-sepolia, arbitrum-sepolia)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "current", "use", "add", "gas"]},
            "name": {"type": "string"},
            "rpc_url": {"type": "string", "description": "For action=add."},
            "chain_id": {"type": "integer", "description": "For action=add."},
            "symbol": {"type": "string", "description": "Native token symbol for action=add."},
            "explorer": {"type": "string", "description": "Block explorer base URL for action=add."},
            "testnet": {"type": "boolean"},
        },
        "required": ["action"],
    },
}


def _net_dict(net) -> dict:
    return {"name": net.name, "label": net.label, "chain_id": net.chain_id,
            "symbol": net.symbol, "testnet": net.testnet, "explorer": net.explorer}


def handle_network(args: dict, **kw) -> str:
    action = str(args.get("action", "")).strip().lower()
    try:
        if action == "list":
            nets = [_net_dict(n) for n in chains.all_networks().values()]
            return tool_result(success=True, action="list", networks=nets,
                               active=chains.active_network_name())
        if action == "current":
            return tool_result(success=True, action="current", **_net_dict(chains.get_network()))
        if action == "use":
            net = chains.set_active_network(args["name"])
            return tool_result(success=True, action="use", **_net_dict(net))
        if action == "add":
            net = chains.add_custom_network(
                name=args["name"], rpc_url=args["rpc_url"], chain_id=int(args["chain_id"]),
                symbol=args.get("symbol", "ETH"), label=args.get("name", ""),
                explorer=args.get("explorer", ""), testnet=bool(args.get("testnet")),
            )
            return tool_result(success=True, action="add", **_net_dict(net))
        if action == "gas":
            return tool_result(success=True, action="gas", **client.gas_snapshot(args.get("name")))
        return tool_error(f"Unknown network action '{action}'.")
    except KeyError as exc:
        return tool_error(f"Missing required argument: {exc}")
    except Exception as exc:
        return _err(exc)


# ==========================================================================
# blockchain_transfer
# ==========================================================================

TRANSFER_SCHEMA = {
    "name": "blockchain_transfer",
    "description": (
        "Transfer native coin or an ERC-20 token from the active wallet. "
        "TWO-STEP: call WITHOUT confirm to get a preview (gas, risk, warnings); "
        "show it to the user; only call again with confirm=true after the user "
        "replies CONFIRM. Never set confirm=true on the user's behalf."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient address."},
            "amount": {"type": "string", "description": "Human amount, e.g. '0.1' or '100'."},
            "token": {"type": "string", "description": "ERC-20 contract; omit for native coin."},
            "network": {"type": "string", "description": "Defaults to active network."},
            "wallet_name": {"type": "string", "description": "Defaults to active wallet."},
            "confirm": {"type": "boolean", "description": "Set true ONLY after the user types CONFIRM."},
        },
        "required": ["to", "amount"],
    },
}


def handle_transfer(args: dict, **kw) -> str:
    try:
        to = safety.validate_address(args["to"], field="to")
        amount = str(args["amount"])
        token = args.get("token")
        net_name = args.get("network")
        w3, net = client.web3_for(net_name)
        acct = wallet.require_account(args.get("wallet_name"))
        sender = acct.address

        warnings = safety.check_recipient(to)
        is_token = bool(token)

        if is_token:
            tx, decimals, symbol = client.build_token_transfer(w3, net, sender, token, to, amount)
            display_token = symbol
        else:
            tx = client.build_native_transfer(w3, net, sender, to, amount)
            decimals, display_token = 18, net.symbol

        gas_cost = client.estimate_cost_wei(tx)
        native_wei = w3.eth.get_balance(sender)
        value_wei = int(tx.get("value", 0)) if not is_token else 0
        warnings += safety.check_balance(balance_wei=native_wei, value_wei=value_wei,
                                          gas_cost_wei=gas_cost, symbol=net.symbol)
        if is_token:
            # Native balance check above only covers gas; verify the token
            # balance itself can cover the transfer amount.
            token_raw = client.token_raw_balance(w3, token, sender)
            amount_raw = client.to_wei_amount(amount, decimals)
            warnings += safety.check_token_balance(
                balance_raw=token_raw, amount_raw=amount_raw,
                symbol=display_token, decimals=decimals)
        risk = safety.classify_transfer(is_token=is_token)

        if not tx.get("_gas_estimated", True):
            warnings.append("Gas could not be estimated on-chain (likely insufficient "
                            "balance) — using a default gas limit for this preview.")

        if not _confirmed(args):
            return _preview(
                "transfer", network=net.name, risk=risk, warnings=warnings,
                gas_cost_wei=gas_cost, symbol=net.symbol,
                **{"from": sender, "to": to, "amount": amount, "token": display_token,
                   "token_address": token or None},
            )

        result = client.sign_and_send(acct, tx, net)
        result.update(success=True, action="transfer", amount=amount,
                      token=display_token, risk_level=risk)
        return tool_result(result)
    except KeyError as exc:
        return tool_error(f"Missing required argument: {exc}")
    except Exception as exc:
        return _err(exc)


# ==========================================================================
# blockchain_contract
# ==========================================================================

CONTRACT_SCHEMA = {
    "name": "blockchain_contract",
    "description": (
        "Interact with a smart contract. Actions: 'read' (call a view method, "
        "free, no confirmation), 'write' (send a state-changing call — covers "
        "approve/swap/stake/bridge/NFT mint via the right abi+method), 'deploy' "
        "(deploy bytecode). 'write' and 'deploy' are TWO-STEP: preview first, "
        "then confirm=true only after the user replies CONFIRM. Unknown/"
        "unverified contracts are flagged Critical risk."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "deploy"]},
            "address": {"type": "string", "description": "Contract address (read/write)."},
            "abi": {"type": "array", "items": {"type": "object"}, "description": "Contract ABI (JSON array)."},
            "method": {"type": "string", "description": "Function name (read/write)."},
            "args": {"type": "array", "description": "Positional arguments for the method."},
            "value": {"type": "string", "description": "Native coin to send with a write, e.g. '0.01'."},
            "bytecode": {"type": "string", "description": "For action=deploy."},
            "constructor_args": {"type": "array", "description": "For action=deploy."},
            "verified": {"type": "boolean", "description": "Whether the user has verified the contract is trustworthy."},
            "network": {"type": "string"},
            "wallet_name": {"type": "string"},
            "confirm": {"type": "boolean", "description": "Set true ONLY after the user types CONFIRM."},
        },
        "required": ["action"],
    },
}


def _coerce_abi(raw: Any) -> list:
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, list):
        return raw
    raise SafetyError("abi must be a JSON array.")


def handle_contract(args: dict, **kw) -> str:
    action = str(args.get("action", "")).strip().lower()
    try:
        if action == "read":
            return tool_result(success=True, action="read", **client.read_contract(
                address=safety.validate_address(args["address"]),
                abi=_coerce_abi(args["abi"]), method=args["method"],
                args=args.get("args") or [], network=args.get("network"),
            ))

        if action == "write":
            address = safety.validate_address(args["address"])
            abi = _coerce_abi(args["abi"])
            method = args["method"]
            w3, net = client.web3_for(args.get("network"))
            acct = wallet.require_account(args.get("wallet_name"))
            tx = client.build_contract_write(
                w3, net, acct.address, address, abi, method,
                args.get("args") or [], value_eth=str(args.get("value", "0")),
            )
            gas_cost = client.estimate_cost_wei(tx)
            call_args = args.get("args") or []
            risk = safety.classify_contract_write(
                verified=args.get("verified"), method=method, args=call_args)
            warnings: List[str] = []
            if method.lower() in safety._APPROVE_METHODS and \
                    safety._approve_amount(call_args) is not None and \
                    safety._approve_amount(call_args) >= safety.MAX_UINT256:
                warnings.append("UNLIMITED approval — this grants the spender permission to "
                                "move ALL of this token from your wallet, now and forever. "
                                "Prefer approving only the exact amount you need.")
            elif risk == safety.CRITICAL:
                warnings.append("Unverified/unknown contract call — review the target and "
                                "calldata carefully before confirming.")
            if not _confirmed(args):
                return _preview("contract_write", network=net.name, risk=risk, warnings=warnings,
                                gas_cost_wei=gas_cost, symbol=net.symbol,
                                **{"from": acct.address, "contract": address, "method": method,
                                   "args": client._jsonable(args.get("args") or [])})
            result = client.sign_and_send(acct, tx, net)
            result.update(success=True, action="contract_write", method=method, risk_level=risk)
            return tool_result(result)

        if action == "deploy":
            w3, net = client.web3_for(args.get("network"))
            acct = wallet.require_account(args.get("wallet_name"))
            abi = _coerce_abi(args["abi"])
            contract = w3.eth.contract(abi=abi, bytecode=args["bytecode"])
            tx = contract.constructor(*(args.get("constructor_args") or [])).build_transaction({
                "from": acct.address, "chainId": net.chain_id,
                "nonce": w3.eth.get_transaction_count(acct.address),
            })
            client._apply_fees(w3, tx)
            gas_cost = client.estimate_cost_wei(tx)
            if not _confirmed(args):
                return _preview("contract_deploy", network=net.name, risk=safety.HIGH,
                                warnings=["Deploying a contract is irreversible."],
                                gas_cost_wei=gas_cost, symbol=net.symbol,
                                **{"from": acct.address})
            result = client.sign_and_send(acct, tx, net)
            result.update(success=True, action="contract_deploy", risk_level=safety.HIGH)
            return tool_result(result)

        return tool_error(f"Unknown contract action '{action}'.")
    except KeyError as exc:
        return tool_error(f"Missing required argument: {exc}")
    except Exception as exc:
        return _err(exc)


# ==========================================================================
# blockchain_tx
# ==========================================================================

TX_SCHEMA = {
    "name": "blockchain_tx",
    "description": (
        "On-chain transaction lookup. Actions: 'status' (default) — look up one "
        "tx by hash (success/failed/pending, block, gas, explorer link); "
        "'history' — recent transactions for an address via Etherscan V2 "
        "(needs ETHERSCAN_API_KEY; defaults to the active wallet)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "history"]},
            "tx_hash": {"type": "string", "description": "For action=status."},
            "address": {"type": "string", "description": "For action=history; defaults to active wallet."},
            "limit": {"type": "integer", "description": "Max txs for history (1-100, default 10)."},
            "network": {"type": "string", "description": "Defaults to active network."},
        },
    },
}


# ==========================================================================
# blockchain_swap  (Uniswap V3)
# ==========================================================================

SWAP_SCHEMA = {
    "name": "blockchain_swap",
    "description": (
        "Swap tokens on Uniswap V3 from the active wallet. Use 'native' for the "
        "chain's native coin (ETH/POL/etc.) as token_in or token_out. TWO-STEP: "
        "call WITHOUT confirm to get a quote preview (expected out, min out after "
        "slippage, whether an approval is needed, gas, risk); show it to the user; "
        "only call again with confirm=true after the user replies CONFIRM. On "
        "confirm, any required ERC-20 approval is sent first, then the swap. "
        "Supported chains: ethereum, base, arbitrum, optimism, polygon."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "token_in": {"type": "string", "description": "ERC-20 address or 'native'."},
            "token_out": {"type": "string", "description": "ERC-20 address or 'native'."},
            "amount": {"type": "string", "description": "Human amount of token_in, e.g. '0.5'."},
            "fee": {"type": "integer", "description": "Pool fee tier: 500, 3000 (default), or 10000."},
            "slippage_bps": {"type": "integer", "description": "Max slippage in basis points (default 50 = 0.5%)."},
            "network": {"type": "string"},
            "wallet_name": {"type": "string"},
            "router": {"type": "string", "description": "Override SwapRouter02 address (unsupported chains)."},
            "quoter": {"type": "string", "description": "Override QuoterV2 address."},
            "weth": {"type": "string", "description": "Override wrapped-native address."},
            "confirm": {"type": "boolean", "description": "Set true ONLY after the user types CONFIRM."},
        },
        "required": ["token_in", "token_out", "amount"],
    },
}


def _norm_token(raw: Any) -> str:
    s = str(raw).strip()
    if s.lower() in {"native", "eth", "matic", "pol", "bnb", "avax"}:
        return uniswap.NATIVE
    return safety.validate_address(s, field="token")


def handle_swap(args: dict, **kw) -> str:
    try:
        token_in = _norm_token(args["token_in"])
        token_out = _norm_token(args["token_out"])
        if token_in == token_out:
            return tool_error("token_in and token_out are the same.")
        amount = str(args["amount"])
        fee = int(args.get("fee", 3000))
        slippage = int(args.get("slippage_bps", 50))

        w3, net = client.web3_for(args.get("network"))
        acct = wallet.require_account(args.get("wallet_name"))
        dep = uniswap.deployment_for(net, args.get("router"), args.get("quoter"), args.get("weth"))

        q = uniswap.quote(w3, net, token_in=token_in, token_out=token_out, amount=amount,
                          fee=fee, slippage_bps=slippage, dep=dep)
        approve_needed = uniswap.needs_approval(
            w3, token_in=token_in, owner=acct.address, amount_raw=q["amount_in_raw"], dep=dep)

        warnings: List[str] = []
        if approve_needed:
            warnings.append(f"An ERC-20 approval of {q['token_in']} to the Uniswap router "
                            "will be sent first (a separate transaction).")
        if slippage >= 300:
            warnings.append(f"High slippage tolerance ({slippage} bps) — you may receive "
                            "significantly less than expected.")

        if not _confirmed(args):
            # Gas for the swap leg only (best-effort; approval adds ~46k).
            return _preview(
                "swap", network=net.name, risk=safety.MEDIUM, warnings=warnings,
                gas_cost_wei=0, symbol=net.symbol,
                **{"from": acct.address, "dex": "Uniswap V3", "fee_tier": fee,
                   "slippage_bps": slippage, "token_in": q["token_in"], "token_out": q["token_out"],
                   "amount_in": q["amount_in"], "expected_out": q["expected_out"],
                   "min_out": q["min_out"], "approve_needed": approve_needed},
            )

        steps: List[Dict[str, Any]] = []
        if approve_needed:
            approve_tx = uniswap.build_approve(
                w3, net, token=token_in, owner=acct.address,
                amount_raw=q["amount_in_raw"], dep=dep)
            approve_res = client.sign_and_send(acct, approve_tx, net)
            steps.append({"step": "approve", **approve_res})
            w3.eth.wait_for_transaction_receipt(approve_res["tx_hash"], timeout=180)

        swap_tx = uniswap.build_swap(
            w3, net, sender=acct.address, token_in=token_in, token_out=token_out,
            q=q, fee=fee, dep=dep)
        swap_res = client.sign_and_send(acct, swap_tx, net)
        steps.append({"step": "swap", **swap_res})

        return tool_result(success=True, action="swap", network=net.name,
                           dex="Uniswap V3", risk_level=safety.MEDIUM,
                           token_in=q["token_in"], token_out=q["token_out"],
                           amount_in=q["amount_in"], expected_out=q["expected_out"],
                           min_out=q["min_out"], steps=steps)
    except KeyError as exc:
        return tool_error(f"Missing required argument: {exc}")
    except Exception as exc:
        return _err(exc)


def handle_tx(args: dict, **kw) -> str:
    action = str(args.get("action", "status")).strip().lower()
    try:
        if action == "history":
            address = args.get("address")
            if not address:
                name = chains.active_wallet_name()
                if not name:
                    return tool_error("No address given and no active wallet.")
                address = wallet.get_address(name)
            address = safety.validate_address(address)
            return tool_result(success=True, action="history", **client.tx_history(
                address, args.get("network"), int(args.get("limit", 10))))
        # default: status
        if not args.get("tx_hash"):
            return tool_error("tx_hash is required for action=status.")
        return tool_result(success=True, action="status",
                           **client.tx_status(args["tx_hash"], args.get("network")))
    except Exception as exc:
        return _err(exc)
