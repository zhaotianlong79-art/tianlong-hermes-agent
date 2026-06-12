# blockchain plugin

EVM blockchain tools for Hermes — wallets, transfers, contract calls, and tx
lookup with **real signing**. Bundled + `kind: backend`, so it auto-loads on
startup. Enable the `blockchain` toolset with `hermes tools` to expose the
tools to the agent.

## Setup

1. Install web3 (already a runtime dep of this plugin):
   ```bash
   uv pip install "web3>=7,<8"
   ```
2. Set the keystore password as a credential in `~/.hermes/.env`:
   ```
   HERMES_WALLET_PASSWORD=your-strong-passphrase
   ```
   This encrypts every keystore at rest and is required to create, import, or
   sign. It is read only from the environment — never from tool arguments — so
   it never enters the conversation or logs.
3. Enable the toolset: `hermes tools` → check **blockchain**.

## Tools

| Tool | Actions |
|---|---|
| `blockchain_wallet` | create, import, list, use, show, balance |
| `blockchain_network` | list, current, use, add, gas |
| `blockchain_transfer` | native / ERC-20 transfer (two-step CONFIRM) |
| `blockchain_swap` | Uniswap V3 swap — quote + slippage min-out + auto-approve (two-step CONFIRM) |
| `blockchain_contract` | read, write, deploy (write/deploy: two-step CONFIRM) |
| `blockchain_tx` | status (by hash) / history (Etherscan V2, needs `ETHERSCAN_API_KEY`) |

## Optional keys (`~/.hermes/.env`)

- `ETHERSCAN_API_KEY` — enables `blockchain_tx` history (one key, all chains via Etherscan V2).

## Safety model

- **Two-step CONFIRM.** Transfer / contract-write / deploy first return a
  preview (`need_confirmation: true`) with gas, risk level, and warnings and
  broadcast nothing. The agent shows it to the user; only after the user types
  CONFIRM does it re-call with `confirm: true`.
- **Risk levels.** Low (transfer) · Medium (swap) · High (bridge / approve /
  stake / deploy) · Critical (unknown contract, unlimited approve).
- **Pre-flight checks.** Address validity, zero/burn-address detection,
  native-balance vs value+gas, unverified-contract flag.
- **Keys never leave.** Stored as scrypt-encrypted Web3 keystores under
  `$HERMES_HOME/blockchain/keystores/`; never returned to the model.

## Files

- `chains.py` — network registry (mainnets + testnets + custom RPC) and state.
- `wallet.py` — encrypted keystore create/import/decrypt (eth-account).
- `safety.py` — address/amount validation and risk classification.
- `client.py` — Web3 connection, reads, gas, ERC-20, signing/broadcast.
- `tools.py` — tool schemas + handlers.
- `__init__.py` — registers the five tools into the `blockchain` toolset.

The companion `skills/blockchain-agent` skill carries the agent-facing
behavior (the CONFIRM protocol and risk discipline); invoke it with
`/blockchain-agent`.
