"""Blockchain plugin — EVM wallets, transfers, and contract calls.

Bundled + ``kind: backend`` so it auto-loads on startup (see
``hermes_cli/plugins.py``). Registers five tools into the ``blockchain``
toolset; enable it with ``hermes tools`` to expose them to the agent.

Real signing: transactions are signed locally with eth-account and
broadcast to the chosen EVM RPC. Every asset-changing action is gated by
a two-step CONFIRM flow (see ``tools.py``). Keys are stored encrypted with
``HERMES_WALLET_PASSWORD`` and never returned to the model.
"""

from __future__ import annotations

# web3 + eth-account are heavy, provider-specific deps — lazy-installed at
# first registration via tools/lazy_deps.py (feature "plugin.blockchain"),
# never in the base install. See pyproject's [project.optional-dependencies].
_TOOLS_SPEC = (
    ("blockchain_wallet",   "WALLET_SCHEMA",   "handle_wallet",   "👛"),
    ("blockchain_network",  "NETWORK_SCHEMA",  "handle_network",  "🔗"),
    ("blockchain_transfer", "TRANSFER_SCHEMA", "handle_transfer", "💸"),
    ("blockchain_swap",     "SWAP_SCHEMA",     "handle_swap",     "🔄"),
    ("blockchain_contract", "CONTRACT_SCHEMA", "handle_contract", "📜"),
    ("blockchain_tx",       "TX_SCHEMA",       "handle_tx",       "🔍"),
)


def register(ctx) -> None:
    """Register all blockchain tools. Called once by the plugin loader.

    Ensures web3/eth-account are present (lazy-install) before importing the
    handler module, which imports web3 at module load. If the deps can't be
    installed the plugin stays dormant rather than crashing plugin discovery.
    """
    try:
        from tools.lazy_deps import ensure
        ensure("plugin.blockchain", prompt=False)
    except Exception:
        # Deps unavailable (offline, lazy installs disabled, etc.). Importing
        # the tools below would raise; bail out so the loader records this
        # plugin as unavailable instead of aborting the whole scan.
        return

    from plugins.blockchain import tools as _t

    for name, schema_attr, handler_attr, emoji in _TOOLS_SPEC:
        ctx.register_tool(
            name=name,
            toolset="blockchain",
            schema=getattr(_t, schema_attr),
            handler=getattr(_t, handler_attr),
            check_fn=_t.check_available,
            emoji=emoji,
        )
