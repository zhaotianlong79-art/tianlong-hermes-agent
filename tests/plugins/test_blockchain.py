"""Tests for the blockchain plugin (EVM wallets / transfers / swap / tx).

Network-free: live RPC calls are monkeypatched. The autouse conftest fixture
already isolates HERMES_HOME to a per-test tempdir, so keystores and state
never touch the real ~/.hermes.
"""

from __future__ import annotations

import json

import pytest

# web3 / eth-account are an opt-in extra (pyproject `[blockchain]`,
# lazy-installed via "plugin.blockchain"). Skip the whole module cleanly when
# they aren't installed instead of erroring out at collection time.
pytest.importorskip("web3")
pytest.importorskip("eth_account")

import plugins.blockchain as bc_init
from plugins.blockchain import chains, client, safety, tools, uniswap, wallet

# Hardhat test account #0 — deterministic key/address pair (never holds funds).
HH_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
HH_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
ALICE = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


@pytest.fixture
def pw(monkeypatch):
    monkeypatch.setenv("HERMES_WALLET_PASSWORD", "test-pw")


def _r(s):
    return json.loads(s)


# ---------------------------------------------------------------------------
# registration & schemas
# ---------------------------------------------------------------------------

def test_register_exposes_all_tools_in_blockchain_toolset():
    from tools.registry import registry

    captured = []

    class Ctx:
        def register_tool(self, **kw):
            captured.append(kw)
            registry.register(**kw)

    bc_init.register(Ctx())
    names = {c["name"] for c in captured}
    assert names == {
        "blockchain_wallet", "blockchain_network", "blockchain_transfer",
        "blockchain_swap", "blockchain_contract", "blockchain_tx",
    }
    assert all(c["toolset"] == "blockchain" for c in captured)
    assert "blockchain" in registry.get_registered_toolset_names()


@pytest.mark.parametrize("schema", [
    tools.WALLET_SCHEMA, tools.NETWORK_SCHEMA, tools.TRANSFER_SCHEMA,
    tools.SWAP_SCHEMA, tools.CONTRACT_SCHEMA, tools.TX_SCHEMA,
])
def test_schemas_are_well_formed(schema):
    json.dumps(schema)  # serializable
    assert schema["name"].startswith("blockchain_")
    assert schema["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# safety
# ---------------------------------------------------------------------------

def test_validate_address_accepts_and_checksums():
    assert safety.validate_address(HH_ADDR.lower()) == HH_ADDR


@pytest.mark.parametrize("bad", ["", "0xnope", "not-an-address", "0x123"])
def test_validate_address_rejects(bad):
    with pytest.raises(safety.SafetyError):
        safety.validate_address(bad)


def test_burn_address_warns():
    assert safety.is_burn_address(safety.ZERO_ADDRESS)
    assert safety.check_recipient("0x000000000000000000000000000000000000dEaD")


def test_check_balance_flags_insufficient():
    warns = safety.check_balance(balance_wei=0, value_wei=10**18,
                                 gas_cost_wei=10**15, symbol="ETH")
    assert warns and "Insufficient" in warns[0]


def test_risk_classification():
    assert safety.classify_transfer(is_token=False) == safety.LOW
    assert safety.classify_approve(amount=safety.MAX_UINT256) == safety.CRITICAL
    assert safety.classify_approve(amount=1000) == safety.HIGH
    assert safety.classify_approve(amount=None) == safety.HIGH
    assert safety.classify_contract_write(verified=None, method="foo") == safety.CRITICAL
    assert safety.classify_contract_write(verified=True, method="swap") == safety.MEDIUM
    assert safety.classify_contract_write(verified=True, method="stake") == safety.HIGH


def test_classify_contract_write_approve_amount_drives_risk():
    spender = ALICE
    # Unlimited approve is Critical even on a "verified" contract.
    assert safety.classify_contract_write(
        verified=True, method="approve",
        args=[spender, safety.MAX_UINT256]) == safety.CRITICAL
    # Hex-encoded unlimited amount is recognised too.
    assert safety.classify_contract_write(
        verified=True, method="approve",
        args=[spender, hex(safety.MAX_UINT256)]) == safety.CRITICAL
    # Bounded approve on a verified contract -> High.
    assert safety.classify_contract_write(
        verified=True, method="approve", args=[spender, 1000]) == safety.HIGH
    # Bounded approve on an UNVERIFIED contract stays Critical.
    assert safety.classify_contract_write(
        verified=False, method="approve", args=[spender, 1000]) == safety.CRITICAL
    # increaseAllowance follows the same amount-at-index-1 shape.
    assert safety.classify_contract_write(
        verified=True, method="increaseAllowance",
        args=[spender, safety.MAX_UINT256]) == safety.CRITICAL


def test_check_token_balance_flags_insufficient():
    warns = safety.check_token_balance(balance_raw=5, amount_raw=10,
                                       symbol="USDC", decimals=6)
    assert warns and "Insufficient USDC" in warns[0]
    assert safety.check_token_balance(balance_raw=10, amount_raw=10,
                                      symbol="USDC", decimals=6) == []


# ---------------------------------------------------------------------------
# wallet
# ---------------------------------------------------------------------------

def test_wallet_requires_password(monkeypatch):
    monkeypatch.delenv("HERMES_WALLET_PASSWORD", raising=False)
    out = _r(tools.handle_wallet({"action": "create", "name": "w"}))
    assert "HERMES_WALLET_PASSWORD" in out["error"]


def test_wallet_import_derives_known_address_and_encrypts(pw):
    from hermes_constants import get_hermes_home

    out = _r(tools.handle_wallet({"action": "import", "name": "hh0", "private_key": HH_KEY}))
    assert out["address"] == HH_ADDR and out["active"]

    ks = get_hermes_home() / "blockchain" / "keystores" / "hh0.json"
    raw = ks.read_text()
    assert HH_KEY[2:] not in raw and '"crypto"' in raw  # encrypted at rest


def test_wallet_wrong_password_rejected(pw, monkeypatch):
    tools.handle_wallet({"action": "import", "name": "hh0", "private_key": HH_KEY})
    monkeypatch.setenv("HERMES_WALLET_PASSWORD", "wrong")
    with pytest.raises(wallet.WalletError):
        wallet.load_account("hh0")


def test_wallet_create_list_use_show(pw):
    tools.handle_wallet({"action": "create", "name": "one"})
    tools.handle_wallet({"action": "create", "name": "two"})
    listed = _r(tools.handle_wallet({"action": "list"}))
    assert {w["name"] for w in listed["wallets"]} == {"one", "two"}
    tools.handle_wallet({"action": "use", "name": "one"})
    assert _r(tools.handle_wallet({"action": "show"}))["active"] == "one"


def test_wallet_name_validation(pw):
    out = _r(tools.handle_wallet({"action": "create", "name": "bad name!"}))
    assert "1-64 chars" in out["error"]


def test_wallet_import_rejects_bad_private_key(pw):
    out = _r(tools.handle_wallet({"action": "import", "name": "bad", "private_key": "0xnothex"}))
    assert "Invalid private key" in out["error"]
    assert not wallet.wallet_exists("bad")


def test_wallet_import_rejects_bad_mnemonic(pw):
    out = _r(tools.handle_wallet({"action": "import", "name": "bad",
                                  "mnemonic": "not a real bip39 phrase at all"}))
    assert "Invalid mnemonic" in out["error"]
    assert not wallet.wallet_exists("bad")


def test_wallet_import_requires_key_or_mnemonic(pw):
    out = _r(tools.handle_wallet({"action": "import", "name": "x"}))
    assert "private_key or mnemonic" in out["error"]


def test_wallet_overwrite_protection(pw):
    tools.handle_wallet({"action": "import", "name": "hh0", "private_key": HH_KEY})
    # second import without overwrite is refused
    out = _r(tools.handle_wallet({"action": "import", "name": "hh0", "private_key": HH_KEY}))
    assert "already exists" in out["error"]
    # overwrite=True replaces it (different key -> different address)
    other = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
    out2 = _r(tools.handle_wallet({"action": "import", "name": "hh0",
                                   "private_key": other, "overwrite": True}))
    assert out2["address"] == ALICE


def test_wallet_decrypt_sign_roundtrip_matches_address(pw):
    """Correct password decrypts the key and signs a tx that recovers to the
    wallet's own address — the full encrypt -> decrypt -> sign closure."""
    from eth_account import Account

    tools.handle_wallet({"action": "import", "name": "hh0", "private_key": HH_KEY})
    acct = wallet.load_account("hh0")
    assert acct.address == HH_ADDR
    tx = {"to": ALICE, "value": 1, "gas": 21000, "maxFeePerGas": 10**9,
          "maxPriorityFeePerGas": 10**9, "nonce": 0, "chainId": 8453}
    signed = acct.sign_transaction(tx)
    assert Account.recover_transaction(signed.raw_transaction) == HH_ADDR


def test_list_wallets_skips_corrupt_keystore(pw):
    from hermes_constants import get_hermes_home

    tools.handle_wallet({"action": "import", "name": "good", "private_key": HH_KEY})
    ks_dir = get_hermes_home() / "blockchain" / "keystores"
    (ks_dir / "corrupt.json").write_text("{ not valid json", "utf-8")
    names = {w["name"] for w in wallet.list_wallets()}
    assert "good" in names and "corrupt" not in names


# ---------------------------------------------------------------------------
# networks
# ---------------------------------------------------------------------------

def test_default_and_switch_network():
    assert chains.active_network_name() == "base"
    out = _r(tools.handle_network({"action": "use", "name": "arbitrum"}))
    assert out["chain_id"] == 42161
    assert chains.active_network_name() == "arbitrum"


def test_unknown_network_raises():
    with pytest.raises(ValueError):
        chains.get_network("nope-chain")


def test_add_custom_network_and_clash():
    net = chains.add_custom_network("mychain", "https://rpc.example", 9999, symbol="X")
    assert net.chain_id == 9999
    assert chains.get_network("mychain").rpc_url == "https://rpc.example"
    with pytest.raises(ValueError):
        chains.add_custom_network("base", "https://x", 1)  # builtin clash


# ---------------------------------------------------------------------------
# confirm gate (the safety-critical path)
# ---------------------------------------------------------------------------

class _FakeEth:
    def get_balance(self, _addr):
        return 5 * 10**18


class _FakeW3:
    eth = _FakeEth()


class _FakeAcct:
    address = HH_ADDR


@pytest.fixture
def fake_transfer_chain(monkeypatch):
    net = chains.get_network("base")
    monkeypatch.setattr(client, "web3_for", lambda n=None: (_FakeW3(), net))
    monkeypatch.setattr(wallet, "require_account", lambda n=None: _FakeAcct())
    monkeypatch.setattr(client, "build_native_transfer",
                        lambda *a, **k: {"to": ALICE, "value": 10**16, "gas": 21000,
                                         "maxFeePerGas": 10**9, "_gas_estimated": True})
    sent = {}

    def _fake_send(acct, tx, net):
        sent["tx"] = tx
        return {"status": "submitted", "tx_hash": "0xabc", "network": net.name}

    monkeypatch.setattr(client, "sign_and_send", _fake_send)
    return sent


def test_transfer_preview_does_not_broadcast(fake_transfer_chain):
    out = _r(tools.handle_transfer({"to": ALICE, "amount": "0.01"}))
    assert out["need_confirmation"] is True
    assert out["risk_level"] == safety.LOW
    assert "tx_hash" not in out
    assert "tx" not in fake_transfer_chain  # nothing signed/sent


def test_transfer_confirm_broadcasts(fake_transfer_chain):
    out = _r(tools.handle_transfer({"to": ALICE, "amount": "0.01", "confirm": True}))
    assert out["success"] and out["tx_hash"] == "0xabc"
    assert "tx" in fake_transfer_chain  # signed exactly once


def test_transfer_rejects_bad_recipient():
    out = _r(tools.handle_transfer({"to": "0xnot", "amount": "1"}))
    assert "valid EVM address" in out["error"]


# ---------------------------------------------------------------------------
# uniswap helpers
# ---------------------------------------------------------------------------

def test_deployment_lookup_and_override():
    net = chains.get_network("base")
    dep = uniswap.deployment_for(net)
    assert dep["router"] and dep["quoter"] and dep["weth"]


def test_deployment_unknown_chain_requires_override():
    chains.add_custom_network("weirdchain", "https://x", 4242)
    net = chains.get_network("weirdchain")
    with pytest.raises(uniswap.SwapError):
        uniswap.deployment_for(net)
    # explicit overrides succeed
    dep = uniswap.deployment_for(
        net, router=HH_ADDR, quoter=ALICE, weth=HH_ADDR)
    assert dep["router"] == HH_ADDR


def test_resolve_token_native_maps_to_weth():
    weth = "0x4200000000000000000000000000000000000006"
    assert uniswap._resolve_token(uniswap.NATIVE, weth) == weth
    assert uniswap._resolve_token(HH_ADDR.lower(), weth) == HH_ADDR


def test_swap_same_token_rejected(pw, monkeypatch):
    net = chains.get_network("base")
    monkeypatch.setattr(client, "web3_for", lambda n=None: (_FakeW3(), net))
    monkeypatch.setattr(wallet, "require_account", lambda n=None: _FakeAcct())
    out = _r(tools.handle_swap({"token_in": HH_ADDR, "token_out": HH_ADDR, "amount": "1"}))
    assert out["error"] == "token_in and token_out are the same."


# ---------------------------------------------------------------------------
# tx history (Etherscan V2)
# ---------------------------------------------------------------------------

def test_history_requires_api_key(monkeypatch):
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    out = _r(tools.handle_tx({"action": "history", "address": HH_ADDR}))
    assert "ETHERSCAN_API_KEY" in out["error"]


def test_history_parses_etherscan_payload(monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "key")

    class _Resp:
        def json(self):
            return {"status": "1", "message": "OK", "result": [{
                "hash": "0xdead", "from": HH_ADDR, "to": ALICE,
                "value": str(10**18), "timeStamp": "1700000000",
                "blockNumber": "123", "isError": "0",
            }]}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    out = _r(tools.handle_tx({"action": "history", "address": HH_ADDR, "network": "ethereum"}))
    assert out["count"] == 1
    tx = out["transactions"][0]
    assert tx["tx_hash"] == "0xdead" and tx["status"] == "success" and tx["value"] == "1"


def test_history_empty_result(monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "key")

    class _Resp:
        def json(self):
            return {"status": "0", "message": "No transactions found", "result": []}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    out = _r(tools.handle_tx({"action": "history", "address": HH_ADDR, "network": "base"}))
    assert out["count"] == 0 and out["transactions"] == []


def test_history_real_error_raises(monkeypatch):
    """A non-empty Etherscan error (rate limit / bad key) must surface, not be
    swallowed as an empty result."""
    monkeypatch.setenv("ETHERSCAN_API_KEY", "key")

    class _Resp:
        def json(self):
            return {"status": "0", "message": "NOTOK",
                    "result": "Max rate limit reached"}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    out = _r(tools.handle_tx({"action": "history", "address": HH_ADDR, "network": "base"}))
    assert "error" in out and "rate limit" in out["error"].lower()


# ---------------------------------------------------------------------------
# confirm-gate strictness (the safety-critical string coercion)
# ---------------------------------------------------------------------------

def test_confirmed_only_accepts_true_or_literal_confirm():
    assert tools._confirmed({"confirm": True}) is True
    assert tools._confirmed({"confirm": "confirm"}) is True
    assert tools._confirmed({"confirm": "CONFIRM"}) is True
    # Loose affirmatives must NOT broadcast funds.
    for loose in [False, "yes", "1", "true", "ok", "y", 1, None, "", "sure"]:
        assert tools._confirmed({"confirm": loose}) is False


def test_transfer_loose_confirm_string_does_not_broadcast(fake_transfer_chain):
    out = _r(tools.handle_transfer({"to": ALICE, "amount": "0.01", "confirm": "yes"}))
    assert out["need_confirmation"] is True
    assert "tx" not in fake_transfer_chain  # nothing signed/sent


# ---------------------------------------------------------------------------
# token transfer balance check
# ---------------------------------------------------------------------------

def test_token_transfer_insufficient_balance_warns(monkeypatch):
    net = chains.get_network("base")
    monkeypatch.setattr(client, "web3_for", lambda n=None: (_FakeW3(), net))
    monkeypatch.setattr(wallet, "require_account", lambda n=None: _FakeAcct())
    monkeypatch.setattr(client, "build_token_transfer",
                        lambda *a, **k: ({"to": ALICE, "gas": 65000, "maxFeePerGas": 10**9,
                                          "_gas_estimated": True}, 6, "USDC"))
    # wallet holds 5 raw units, tries to send 100 * 10**6
    monkeypatch.setattr(client, "token_raw_balance", lambda w3, token, owner: 5)

    out = _r(tools.handle_transfer({"to": ALICE, "amount": "100", "token": HH_ADDR}))
    assert out["need_confirmation"] is True
    assert any("Insufficient USDC" in w for w in out.get("warnings", []))


# ---------------------------------------------------------------------------
# real signing path (sign_and_send) — NOT mocked away
# ---------------------------------------------------------------------------

class _FakeProvider:
    def __init__(self, *a, **k):
        pass


def _patch_sign_send_w3(monkeypatch, send_impl):
    """Patch client.Web3 so sign_and_send signs for real but the broadcast is
    intercepted by send_impl(raw) -> bytes/raises."""
    class _Eth:
        def send_raw_transaction(self, raw):
            return send_impl(raw)

    class _W3:
        HTTPProvider = staticmethod(_FakeProvider)

        def __init__(self, *a, **k):
            self.eth = _Eth()

    monkeypatch.setattr(client, "Web3", _W3)


def test_sign_and_send_strips_internal_fields_and_signs(monkeypatch):
    from eth_account import Account

    captured = {}

    def _send(raw):
        captured["raw"] = raw
        return bytes.fromhex("ab" * 32)

    _patch_sign_send_w3(monkeypatch, _send)
    acct = Account.from_key(HH_KEY)
    net = chains.get_network("base")
    tx = {"from": HH_ADDR, "to": ALICE, "value": 10**15, "gas": 21000,
          "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**9,
          "nonce": 7, "chainId": net.chain_id, "_gas_estimated": True}

    out = client.sign_and_send(acct, tx, net)
    assert out["status"] == "submitted" and out["tx_hash"].startswith("0x")
    # The signed tx recovers to the wallet -> chainId/nonce/fields were signed,
    # and the "_gas_estimated" marker was stripped (eth_account would have
    # raised on an unknown field otherwise).
    assert Account.recover_transaction(captured["raw"]) == HH_ADDR


def test_sign_and_send_wraps_broadcast_failure(monkeypatch):
    from eth_account import Account

    def _boom(raw):
        raise RuntimeError("nonce too low")

    _patch_sign_send_w3(monkeypatch, _boom)
    acct = Account.from_key(HH_KEY)
    net = chains.get_network("base")
    tx = {"from": HH_ADDR, "to": ALICE, "value": 1, "gas": 21000,
          "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**9,
          "nonce": 0, "chainId": net.chain_id}
    with pytest.raises(client.ChainError) as ei:
        client.sign_and_send(acct, tx, net)
    assert "Broadcast failed" in str(ei.value) and "nonce too low" in str(ei.value)


# ---------------------------------------------------------------------------
# gas estimation fallback
# ---------------------------------------------------------------------------

def test_estimate_gas_falls_back_on_revert():
    class _Eth:
        def estimate_gas(self, tx):
            raise RuntimeError("execution reverted")

    class _W3:
        eth = _Eth()

    gas, estimated = client._estimate_gas(_W3(), {}, "native")
    assert gas == client._GAS_FALLBACK["native"] and estimated is False


def test_transfer_preview_warns_when_gas_unestimated(monkeypatch):
    net = chains.get_network("base")
    monkeypatch.setattr(client, "web3_for", lambda n=None: (_FakeW3(), net))
    monkeypatch.setattr(wallet, "require_account", lambda n=None: _FakeAcct())
    monkeypatch.setattr(client, "build_native_transfer",
                        lambda *a, **k: {"to": ALICE, "value": 10**16, "gas": 21000,
                                         "maxFeePerGas": 10**9, "_gas_estimated": False})
    out = _r(tools.handle_transfer({"to": ALICE, "amount": "0.01"}))
    assert any("Gas could not be estimated" in w for w in out.get("warnings", []))


# ---------------------------------------------------------------------------
# contract write risk — unlimited approve flows through handle_contract
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_contract_chain(monkeypatch):
    net = chains.get_network("base")
    monkeypatch.setattr(client, "web3_for", lambda n=None: (_FakeW3(), net))
    monkeypatch.setattr(wallet, "require_account", lambda n=None: _FakeAcct())
    monkeypatch.setattr(client, "build_contract_write",
                        lambda *a, **k: {"to": HH_ADDR, "gas": 60000, "maxFeePerGas": 10**9})
    monkeypatch.setattr(client, "estimate_cost_wei", lambda tx: 10**14)
    sent = {}
    monkeypatch.setattr(client, "sign_and_send",
                        lambda acct, tx, net: sent.update(tx=tx) or
                        {"status": "submitted", "tx_hash": "0xfeed", "network": net.name})
    return sent


_APPROVE_ABI = [{"name": "approve", "type": "function",
                 "inputs": [{"name": "spender", "type": "address"},
                            {"name": "amount", "type": "uint256"}],
                 "outputs": [{"name": "", "type": "bool"}]}]


def test_unlimited_approve_is_critical_via_handle_contract(fake_contract_chain):
    out = _r(tools.handle_contract({
        "action": "write", "address": HH_ADDR, "abi": _APPROVE_ABI,
        "method": "approve", "args": [ALICE, str(safety.MAX_UINT256)],
        "verified": True,
    }))
    assert out["need_confirmation"] is True
    assert out["risk_level"] == safety.CRITICAL
    assert any("UNLIMITED" in w for w in out.get("warnings", []))
    assert "tx" not in fake_contract_chain  # nothing broadcast in preview


def test_bounded_verified_approve_is_high(fake_contract_chain):
    out = _r(tools.handle_contract({
        "action": "write", "address": HH_ADDR, "abi": _APPROVE_ABI,
        "method": "approve", "args": [ALICE, "1000"], "verified": True,
    }))
    assert out["risk_level"] == safety.HIGH
    assert not any("UNLIMITED" in w for w in out.get("warnings", []))


# ---------------------------------------------------------------------------
# swap execution: routing, value, multicall, slippage, ordering
# ---------------------------------------------------------------------------

def test_apply_slippage_math_and_clamping():
    assert uniswap.apply_slippage(10_000, 50) == 9_950      # 0.5%
    assert uniswap.apply_slippage(10_000, 0) == 10_000      # no tolerance
    assert uniswap.apply_slippage(10_000, -5) == 10_000     # clamped to 0
    assert uniswap.apply_slippage(10_000, 99_999) == 0      # clamped to 100%


def test_swap_routing_directions():
    tok = HH_ADDR
    # native in -> value carries the input; recipient is the sender
    eth_out, recip, value = uniswap.swap_routing(uniswap.NATIVE, tok, ALICE, 500)
    assert eth_out is False and recip == ALICE and value == 500
    # native out -> output lands in router for unwrap; no msg.value
    eth_out, recip, value = uniswap.swap_routing(tok, uniswap.NATIVE, ALICE, 500)
    assert eth_out is True and recip == uniswap._ADDRESS_THIS and value == 0
    # token -> token -> straight to sender, no value
    eth_out, recip, value = uniswap.swap_routing(tok, ALICE, HH_ADDR, 500)
    assert eth_out is False and recip == HH_ADDR and value == 0


def test_build_swap_native_in_sets_msg_value(monkeypatch):
    from web3 import Web3

    monkeypatch.setattr(client, "_apply_fees", lambda w3, tx: tx.update(maxFeePerGas=10**9))
    net = chains.get_network("base")
    dep = uniswap.deployment_for(net)
    out_tok = Web3.to_checksum_address("0x" + "22" * 20)
    q = {"in_addr": dep["weth"], "out_addr": out_tok,
         "amount_in_raw": 10**18, "min_out_raw": 900}
    tx = uniswap.build_swap(Web3(), net, sender=HH_ADDR, token_in=uniswap.NATIVE,
                            token_out=out_tok, q=q, fee=3000, dep=dep, nonce=0)
    assert tx["value"] == 10**18 and tx["to"] == dep["router"]
    assert tx["data"][:10] == "0x04e45aaf"  # exactInputSingle selector


def test_build_swap_native_out_uses_multicall(monkeypatch):
    from web3 import Web3

    monkeypatch.setattr(client, "_apply_fees", lambda w3, tx: tx.update(maxFeePerGas=10**9))
    net = chains.get_network("base")
    dep = uniswap.deployment_for(net)
    in_tok = Web3.to_checksum_address("0x" + "22" * 20)
    q = {"in_addr": in_tok, "out_addr": dep["weth"],
         "amount_in_raw": 10**18, "min_out_raw": 900}
    tx = uniswap.build_swap(Web3(), net, sender=HH_ADDR, token_in=in_tok,
                            token_out=uniswap.NATIVE, q=q, fee=3000, dep=dep, nonce=0)
    assert "value" not in tx  # ERC-20 in -> no msg.value
    assert tx["data"][:10] == "0xac9650d8"  # multicall selector


@pytest.fixture
def fake_swap_chain(monkeypatch):
    net = chains.get_network("base")
    order = []

    class _Eth:
        def get_balance(self, _a):
            return 5 * 10**18

        def wait_for_transaction_receipt(self, h, timeout=None):
            order.append(("receipt", h))
            return {"status": 1}

    class _W3:
        eth = _Eth()

    monkeypatch.setattr(client, "web3_for", lambda n=None: (_W3(), net))
    monkeypatch.setattr(wallet, "require_account", lambda n=None: _FakeAcct())
    monkeypatch.setattr(uniswap, "quote", lambda *a, **k: {
        "token_in": "USDC", "token_out": "ETH", "amount_in": "100",
        "amount_in_raw": 100 * 10**6, "expected_out": "0.05",
        "expected_out_raw": 5 * 10**16, "min_out": "0.0497",
        "min_out_raw": 497 * 10**14, "in_addr": HH_ADDR, "out_addr": ALICE,
        "in_decimals": 6, "out_decimals": 18})
    monkeypatch.setattr(uniswap, "needs_approval", lambda *a, **k: True)
    monkeypatch.setattr(uniswap, "build_approve", lambda *a, **k: {"kind": "approve"})
    monkeypatch.setattr(uniswap, "build_swap", lambda *a, **k: {"kind": "swap"})

    def _send(acct, tx, net):
        order.append(("send", tx.get("kind")))
        return {"status": "submitted", "tx_hash": "0x" + tx.get("kind", "x"), "network": net.name}

    monkeypatch.setattr(client, "sign_and_send", _send)
    return order


def test_swap_preview_does_not_broadcast(fake_swap_chain):
    out = _r(tools.handle_swap({"token_in": HH_ADDR, "token_out": "native", "amount": "100"}))
    assert out["need_confirmation"] is True
    assert out["risk_level"] == safety.MEDIUM
    assert out["approve_needed"] is True
    assert fake_swap_chain == []  # nothing sent


def test_swap_confirm_approves_before_swapping(fake_swap_chain):
    out = _r(tools.handle_swap({"token_in": HH_ADDR, "token_out": "native",
                                "amount": "100", "confirm": True}))
    assert out["success"] is True
    # approve is sent, its receipt awaited, THEN the swap is sent.
    assert fake_swap_chain == [("send", "approve"), ("receipt", "0xapprove"), ("send", "swap")]
    assert [s["step"] for s in out["steps"]] == ["approve", "swap"]


def test_swap_high_slippage_warns(fake_swap_chain):
    out = _r(tools.handle_swap({"token_in": HH_ADDR, "token_out": "native",
                                "amount": "100", "slippage_bps": 500}))
    assert any("High slippage" in w for w in out.get("warnings", []))


# ---------------------------------------------------------------------------
# tx status + contract read
# ---------------------------------------------------------------------------

class _Receipt:
    def __init__(self, status):
        self.status = status
        self.blockNumber = 123
        self.gasUsed = 21000


def _patch_status_w3(monkeypatch, receipt_or_exc):
    net = chains.get_network("base")

    class _Eth:
        def get_transaction_receipt(self, h):
            if isinstance(receipt_or_exc, Exception):
                raise receipt_or_exc
            return receipt_or_exc

    class _W3:
        eth = _Eth()

    monkeypatch.setattr(client, "web3_for", lambda n=None: (_W3(), net))


def test_tx_status_success(monkeypatch):
    _patch_status_w3(monkeypatch, _Receipt(1))
    out = _r(tools.handle_tx({"action": "status", "tx_hash": "0xabc"}))
    assert out["status"] == "success" and out["block_number"] == 123


def test_tx_status_failed(monkeypatch):
    _patch_status_w3(monkeypatch, _Receipt(0))
    out = _r(tools.handle_tx({"action": "status", "tx_hash": "0xabc"}))
    assert out["status"] == "failed"


def test_tx_status_pending(monkeypatch):
    _patch_status_w3(monkeypatch, RuntimeError("not found"))
    out = _r(tools.handle_tx({"action": "status", "tx_hash": "0xabc"}))
    assert out["status"] == "pending_or_unknown"


def test_tx_status_requires_hash():
    out = _r(tools.handle_tx({"action": "status"}))
    assert "tx_hash is required" in out["error"]


def test_read_contract_parses_result(monkeypatch):
    net = chains.get_network("base")

    class _Fn:
        def __init__(self, val):
            self._val = val

        def call(self):
            return self._val

    class _Functions:
        def __getitem__(self, method):
            return lambda *a: _Fn(42)

    class _Contract:
        functions = _Functions()

    class _Eth:
        def contract(self, address, abi):
            return _Contract()

    class _W3:
        eth = _Eth()

    monkeypatch.setattr(client, "web3_for", lambda n=None: (_W3(), net))
    out = _r(tools.handle_contract({"action": "read", "address": HH_ADDR,
                                    "abi": [], "method": "totalSupply", "args": []}))
    assert out["result"] == 42 and out["method"] == "totalSupply"


# ---------------------------------------------------------------------------
# _jsonable helper
# ---------------------------------------------------------------------------

def test_jsonable_converts_bytes_and_big_ints():
    assert client._jsonable(b"\x12\x34") == "0x1234"
    big = 2**60
    assert client._jsonable(big) == str(big)
    assert client._jsonable(5) == 5  # small ints stay native
    assert client._jsonable([b"\x01", 2**60]) == ["0x01", str(2**60)]
