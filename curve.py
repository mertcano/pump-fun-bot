"""pump.fun bonding-curve parsing and quote math.

This module is the single source of truth for the bonding curve. It used to be
copy-pasted into ``buy.py``, ``sell.py`` and ``learning-examples/fetch_price.py``
with divergent annotations, which meant a fix to one copy silently did not apply
to the others.

All monetary values are computed in integer base units (lamports, token base
units). Floats are only produced by the human-facing display helpers, because a
float-derived ``u64`` can silently truncate to zero or overflow the field.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

from construct import Flag, Int64ul, Struct
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient

# Precalculated discriminator of the pump.fun bonding-curve account.
# See learning-examples/calculate_discriminator.py.
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 6966180631402821399)

TOKEN_DECIMALS: Final[int] = 6
LAMPORTS_PER_SOL: Final[int] = 1_000_000_000

# Upper bound of a Solana ``u64``. Any amount outside [0, U64_MAX] cannot be
# serialised into the instruction data and must be rejected before sending.
U64_MAX: Final[int] = 2**64 - 1

# pump.fun charges a protocol fee on the SOL side of a trade. The exact rate has
# changed over time, so we subtract a conservative reserve here. The reserve is
# applied in the direction that makes the slippage floor *easier* to satisfy,
# which is the only safe direction: an over-estimated floor reverts every sell.
PROTOCOL_FEE_RATE: Final[float] = 0.01


class CurveStateError(RuntimeError):
    """Raised when a bonding-curve account cannot be decoded or is unusable."""


class AmountOutOfRangeError(ValueError):
    """Raised when a computed amount does not fit into a Solana ``u64`` field."""


class BondingCurveState:
    """Decoded pump.fun bonding-curve account."""

    _STRUCT = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
    )

    def __init__(self, data: bytes) -> None:
        parsed = self._STRUCT.parse(data[8:])
        self.__dict__.update(parsed)

    @property
    def migrated(self) -> bool:
        """True once the curve has graduated to the AMM and can no longer be traded."""
        return bool(self.complete)


async def get_pump_curve_state(conn: AsyncClient, curve_address: Pubkey) -> BondingCurveState:
    """Fetch and decode the bonding curve at ``curve_address``."""
    response = await conn.get_account_info(curve_address)
    if not response.value or not response.value.data:
        raise CurveStateError(f"No bonding curve data at {curve_address}")

    data = response.value.data
    if data[:8] != EXPECTED_DISCRIMINATOR:
        raise CurveStateError(
            f"Unexpected discriminator at {curve_address}: {data[:8].hex()}"
        )

    return BondingCurveState(data)


def assert_tradable(curve_state: BondingCurveState) -> None:
    """Reject a curve that cannot be traded any more.

    A ``complete`` (migrated) curve has been drained into the Raydium AMM. Sending
    a pump.fun instruction against it always fails, so fail before paying fees.
    """
    if curve_state.migrated:
        raise CurveStateError("Bonding curve is complete (migrated to the AMM); not tradable")
    if curve_state.virtual_sol_reserves <= 0 or curve_state.virtual_token_reserves <= 0:
        raise CurveStateError("Invalid reserve state: virtual reserves must be positive")


def calculate_pump_curve_price(curve_state: BondingCurveState) -> float:
    """Spot price in SOL per token.

    Display only. Never use this to derive a slippage floor: spot price ignores
    both price impact and the protocol fee, so it systematically over-estimates
    the SOL actually received.
    """
    if curve_state.virtual_token_reserves <= 0 or curve_state.virtual_sol_reserves <= 0:
        raise CurveStateError("Invalid reserve state")

    return (curve_state.virtual_sol_reserves / LAMPORTS_PER_SOL) / (
        curve_state.virtual_token_reserves / 10**TOKEN_DECIMALS
    )


def expected_sol_out_lamports(curve_state: BondingCurveState, token_amount: int) -> int:
    """Expected SOL proceeds, in lamports, for selling ``token_amount`` base units.

    For the pump.fun constant-product curve, selling ``n`` of ``virtual_token_reserves``
    withdraws ``(n * virtual_sol_reserves) / (virtual_token_reserves + n)`` lamports.
    This is strictly less than ``spot_price * n`` for every ``n`` in ``(0, vtok)``,
    which is exactly why deriving a floor from spot price can never be satisfied.
    """
    assert_tradable(curve_state)
    if token_amount <= 0:
        raise AmountOutOfRangeError("token_amount must be positive")
    if token_amount >= curve_state.virtual_token_reserves:
        raise AmountOutOfRangeError(
            "token_amount must be smaller than the virtual token reserves"
        )

    numerator = token_amount * curve_state.virtual_sol_reserves
    denominator = curve_state.virtual_token_reserves + token_amount
    return numerator // denominator


def min_sol_output_lamports(
    curve_state: BondingCurveState,
    token_amount: int,
    slippage: float,
    fee_rate: float = PROTOCOL_FEE_RATE,
) -> int:
    """Compute a satisfiable ``min_sol_output`` for a sell instruction.

    The floor is derived from the curve integral (post-trade reserves), not from
    the spot price, and the protocol fee reserve is subtracted before the
    slippage factor is applied.
    """
    if not 0.0 <= slippage < 1.0:
        raise ValueError(f"slippage must be in [0, 1), got {slippage!r}")

    gross_lamports = expected_sol_out_lamports(curve_state, token_amount)
    # Apply the fee and slippage factors on integers, rounding down at each step,
    # so the result is always <= the true net proceeds.
    net_lamports = int(gross_lamports * (1.0 - fee_rate) * (1.0 - slippage))

    if net_lamports <= 0:
        raise AmountOutOfRangeError(
            "min_sol_output rounded down to 0 lamports; increase the position size "
            "or loosen the slippage tolerance"
        )
    if net_lamports >= gross_lamports:
        # Defensive: an unsatisfiable floor would revert every sell.
        raise AmountOutOfRangeError("computed min_sol_output exceeds expected proceeds")
    return net_lamports


def tokens_for_sol_amount(
    curve_state: BondingCurveState, amount_lamports: int
) -> int:
    """Token base units obtainable for ``amount_lamports`` of SOL.

    Mirrors the pump.fun buy curve: adding ``lamports`` of virtual SOL withdraws
    ``lamports * virtual_token_reserves / (virtual_sol_reserves + lamports)`` tokens.
    Computed in integer arithmetic and range-checked before it is serialised.
    """
    assert_tradable(curve_state)
    if amount_lamports <= 0:
        raise AmountOutOfRangeError("amount_lamports must be positive")

    numerator = amount_lamports * curve_state.virtual_token_reserves
    denominator = curve_state.virtual_sol_reserves + amount_lamports
    token_units = numerator // denominator

    if token_units <= 0:
        raise AmountOutOfRangeError(
            f"{amount_lamports} lamports buys 0 tokens at the current curve price; "
            f"increase BUY_AMOUNT"
        )
    if token_units > U64_MAX:
        raise AmountOutOfRangeError(
            f"computed token amount {token_units} exceeds the u64 instruction field"
        )
    return token_units


def require_u64(value: int, field_name: str) -> int:
    """Validate that ``value`` fits into the instruction's ``u64`` field."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise AmountOutOfRangeError(f"{field_name} must be an integer, got {value!r}")
    if value < 0:
        raise AmountOutOfRangeError(f"{field_name} must be non-negative, got {value}")
    if value > U64_MAX:
        raise AmountOutOfRangeError(
            f"{field_name} = {value} exceeds the u64 maximum {U64_MAX}"
        )
    return value


@dataclass(frozen=True)
class TradeResult:
    """Outcome of a buy or sell, derived from post-transaction chain state.

    ``signature`` alone is not a success signal: a transaction that lands with a
    runtime error still reaches ``confirmed`` status. These fields are read back
    from the chain so a caller can never mistake a reverted trade for a fill.
    """

    signature: str
    status: str  # "confirmed" | "reverted" | "unknown"
    sol_spent_lamports: int = 0
    tokens_delta: int = 0
    sol_received_lamports: int = 0
    detail: str = ""
