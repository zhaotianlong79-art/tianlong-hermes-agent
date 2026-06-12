"""Pre-flight safety checks and risk classification.

Implements the doc's guardrails: address validation (format / zero /
blacklist), balance & gas sufficiency, and a Low/Medium/High/Critical risk
level for every asset-changing action. None of these *block* on their own —
they feed the preview that the user must confirm with CONFIRM.
"""

from __future__ import annotations

from typing import List, Optional

from eth_utils import is_address, to_checksum_address

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Known dead/burn sinks — sending here destroys funds. Extend as needed.
_BURN_ADDRESSES = {
    ZERO_ADDRESS.lower(),
    "0x000000000000000000000000000000000000dead",
}

# Risk levels, mirroring the doc.
LOW = "Low"          # plain native / token transfer
MEDIUM = "Medium"    # DEX swap
HIGH = "High"        # bridge, approve, staking, deploy
CRITICAL = "Critical"  # unknown contract call, unlimited approve

# Unlimited ERC-20 allowance (uint256 max) — a Critical-risk approval.
MAX_UINT256 = (1 << 256) - 1


class SafetyError(Exception):
    """A hard safety failure (bad address, insufficient funds)."""


def validate_address(addr: str, *, field: str = "address") -> str:
    """Return the checksummed address or raise SafetyError."""
    if not addr or not isinstance(addr, str):
        raise SafetyError(f"{field} is required.")
    if not is_address(addr):
        raise SafetyError(f"{field} '{addr}' is not a valid EVM address.")
    return to_checksum_address(addr)


def is_burn_address(addr: str) -> bool:
    return (addr or "").lower() in _BURN_ADDRESSES


def check_recipient(addr: str) -> List[str]:
    """Return a list of human-readable warnings for a transfer recipient."""
    warnings: List[str] = []
    if is_burn_address(addr):
        warnings.append(
            "Recipient is a burn/zero address — funds sent here are PERMANENTLY LOST."
        )
    return warnings


def check_balance(*, balance_wei: int, value_wei: int, gas_cost_wei: int,
                  symbol: str) -> List[str]:
    """Warn (not raise) when native balance can't cover value + gas."""
    warnings: List[str] = []
    needed = value_wei + gas_cost_wei
    if balance_wei < needed:
        warnings.append(
            f"Insufficient {symbol}: balance covers "
            f"{_eth(balance_wei)} but transfer + gas needs ~{_eth(needed)}."
        )
    elif gas_cost_wei and balance_wei < value_wei + gas_cost_wei * 2:
        warnings.append("Balance is tight — little headroom left for gas after this tx.")
    return warnings


def check_token_balance(*, balance_raw: int, amount_raw: int, symbol: str,
                        decimals: int) -> List[str]:
    """Warn (not raise) when the ERC-20 token balance can't cover the amount."""
    warnings: List[str] = []
    if balance_raw < amount_raw:
        warnings.append(
            f"Insufficient {symbol}: balance is "
            f"{_units(balance_raw, decimals)} but the transfer needs "
            f"{_units(amount_raw, decimals)}."
        )
    return warnings


def classify_transfer(*, is_token: bool) -> str:
    return LOW


def classify_approve(*, amount: Optional[int]) -> str:
    """Risk for an ERC-20 approve. Unlimited (uint256-max) allowance is the
    doc's Critical case — it lets the spender drain the whole balance forever,
    regardless of whether the contract is otherwise 'verified'."""
    if amount is not None and amount >= MAX_UINT256:
        return CRITICAL
    return HIGH


# Method names that mean "grant a spender an allowance" across common ABIs.
_APPROVE_METHODS = {"approve", "increaseallowance", "permit"}


def _approve_amount(args) -> Optional[int]:
    """Best-effort extract the allowance amount from an approve-style call.

    Standard ERC-20 ``approve(spender, value)`` puts the amount at index 1;
    ``increaseAllowance`` matches the same shape. Returns None when we can't
    confidently read an integer so the caller falls back to method-based risk.
    """
    if not isinstance(args, (list, tuple)) or len(args) < 2:
        return None
    raw = args[1]
    try:
        return int(raw, 16) if isinstance(raw, str) and raw.startswith("0x") else int(raw)
    except (TypeError, ValueError):
        return None


def classify_contract_write(*, verified: Optional[bool], method: str = "",
                            args=None) -> str:
    """Risk for a generic contract write.

    Unlimited approve -> Critical (checked FIRST, even on a verified contract:
    an infinite allowance is dangerous no matter who the spender is).
    Unknown/unverified target -> Critical (the doc's "unknown contract call").
    A recognised standard method on a verified contract -> High/Medium.
    """
    m = (method or "").lower()
    if m in _APPROVE_METHODS:
        # Inspect the amount; unlimited is Critical regardless of `verified`.
        risk = classify_approve(amount=_approve_amount(args))
        if risk == CRITICAL:
            return CRITICAL
        # A bounded approve on an unverified contract is still Critical.
        return HIGH if verified else CRITICAL
    if m in {"swap", "swapexacttokensfortokens", "swapexactethfortokens",
             "exactinput", "exactinputsingle"}:
        return MEDIUM
    if m in {"bridge", "deposit", "depositerc20", "send", "relaytokens"}:
        return HIGH
    if m in {"stake", "unstake", "withdraw", "delegate"}:
        return HIGH
    if verified is False or verified is None:
        return CRITICAL
    return HIGH


def _eth(wei: int) -> str:
    return f"{wei / 1e18:.6f}".rstrip("0").rstrip(".")


def _units(raw: int, decimals: int) -> str:
    from decimal import Decimal
    s = f"{Decimal(raw) / (Decimal(10) ** decimals):.6f}"
    return s.rstrip("0").rstrip(".") or "0"
