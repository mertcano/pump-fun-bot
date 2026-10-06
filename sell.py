"""Sell side of the pump.fun trading bot.

Key correctness property: a trade is only reported as successful after the
transaction outcome has been read back from the chain. Confirmation status alone
proves inclusion, not execution.
"""

from __future__ import annotations

import asyncio
import struct

import base58
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solana.transaction import Transaction
from spl.token.instructions import get_associated_token_address

from config import (
    PUMP_EVENT_AUTHORITY,
    PUMP_FEE,
    PUMP_GLOBAL,
    PUMP_PROGRAM,
    PRIVATE_KEY,
    RPC_ENDPOINT,
    SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM,
    SYSTEM_PROGRAM,
    SYSTEM_RENT,
    SYSTEM_TOKEN_PROGRAM,
)
from curve import (
    TOKEN_DECIMALS,
    TradeResult,
    calculate_pump_curve_price,
    get_pump_curve_state,
    min_sol_output_lamports,
    require_u64,
)

# Precalculated discriminator of the pump.fun `sell` instruction.
SELL_DISCRIMINATOR: bytes = struct.pack("<Q", 12502976635542562355)


class TransientTradeError(RuntimeError):
    """A failure that is worth retrying (RPC hiccup, blockhash expiry, 429)."""


class PermanentTradeError(RuntimeError):
    """A failure that will never succeed on retry (bad accounts, slippage)."""


def load_payer() -> Keypair:
    """Load the paying wallet from the environment-provided secret key."""
    return Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))


async def get_token_balance(conn: AsyncClient, associated_token_account: Pubkey) -> int:
    """Token balance in base units."""
    response = await conn.get_token_account_balance(associated_token_account)
    if response.value:
        return int(response.value.amount)
    return 0


async def fetch_transaction_meta(conn: AsyncClient, signature: str):
    """Return the transaction meta for ``signature``, or None if unavailable."""
    try:
        response = await conn.get_transaction(
            signature,
            max_supported_transaction_version=0,
            encoding="json",
        )
    except Exception:
        return None
    return getattr(response, "value", None)


def classify_transaction(meta) -> str:
    """Return ``"confirmed"``, ``"reverted"`` or ``"unknown"`` from a tx meta."""
    if meta is None:
        return "unknown"
    err = meta.get("err") if isinstance(meta, dict) else getattr(meta, "err", None)
    if err is None:
        return "confirmed"
    return "reverted"


async def sell_token(
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_bonding_curve: Pubkey,
    slippage: float = 0.25,
    max_retries: int = 5,
) -> TradeResult | None:
    """Sell the full token balance back to the bonding curve.

    Returns a :class:`TradeResult` whose ``status`` reflects the on-chain outcome,
    or ``None`` when there was nothing to sell or every attempt failed.
    """
    if not 0.0 <= slippage < 1.0:
        raise ValueError(f"slippage must be in [0, 1), got {slippage!r}")

    payer = load_payer()

    async with AsyncClient(RPC_ENDPOINT) as client:
        associated_token_account = get_associated_token_address(payer.pubkey(), mint)

        token_balance = await get_token_balance(client, associated_token_account)
        token_balance_decimal = token_balance / 10**TOKEN_DECIMALS
        print(f"Token balance: {token_balance_decimal}")
        if token_balance == 0:
            print("No tokens to sell.")
            return None

        curve_state = await get_pump_curve_state(client, bonding_curve)
        token_price_sol = calculate_pump_curve_price(curve_state)
        print(f"Price per Token: {token_price_sol:.20f} SOL")

        # Derive the slippage floor from the curve integral, not from spot price.
        # Spot price over-estimates proceeds for every position size, which makes
        # the resulting floor mathematically unsatisfiable.
        try:
            min_sol_output = min_sol_output_lamports(curve_state, token_balance, slippage)
        except Exception as exc:
            # Permanent: retrying the same position cannot make the floor valid.
            print(f"Refusing to sell: {exc}")
            return None

        print(f"Selling {token_balance_decimal} tokens")
        print(f"Minimum SOL output: {min_sol_output / 1_000_000_000:.10f} SOL")

        for attempt in range(max_retries):
            try:
                accounts = [
                    AccountMeta(pubkey=PUMP_GLOBAL, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=PUMP_FEE, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
                    AccountMeta(
                        pubkey=associated_bonding_curve, is_signer=False, is_writable=True
                    ),
                    AccountMeta(
                        pubkey=associated_token_account, is_signer=False, is_writable=True
                    ),
                    AccountMeta(pubkey=payer.pubkey(), is_signer=True, is_writable=True),
                    AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
                    AccountMeta(
                        pubkey=SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM,
                        is_signer=False,
                        is_writable=False,
                    ),
                    AccountMeta(pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=SYSTEM_RENT, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
                ]

                data = (
                    SELL_DISCRIMINATOR
                    + struct.pack("<Q", require_u64(token_balance, "token_amount"))
                    + struct.pack("<Q", require_u64(min_sol_output, "min_sol_output"))
                )
                sell_ix = Instruction(PUMP_PROGRAM, data, accounts)

                # Refetched every attempt so a retry never reuses a blockhash that
                # may have expired while the previous attempt was backing off.
                recent_blockhash = await client.get_latest_blockhash()
                transaction = Transaction()
                transaction.add(sell_ix)
                transaction.recent_blockhash = recent_blockhash.value.blockhash

                # Preflight is left enabled: it surfaces insufficient funds and
                # invalid accounts locally, before a fee is spent on mainnet.
                tx = await client.send_transaction(
                    transaction,
                    payer,
                    opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed),
                )

                signature = str(tx.value)
                print(f"Transaction sent: https://explorer.solana.com/tx/{signature}")

                await client.confirm_transaction(tx.value, commitment="confirmed")

                # Inclusion is not execution. Read the runtime error back out.
                meta = await fetch_transaction_meta(client, signature)
                status = classify_transaction(meta)
                if status != "confirmed":
                    print(f"Transaction did not execute successfully (status={status}).")
                    return TradeResult(signature=signature, status=status)

                print("Transaction confirmed and executed successfully")
                return TradeResult(signature=signature, status="confirmed")

            except PermanentTradeError as exc:
                # Never retry a deterministic failure: it only burns fees.
                print(f"Permanent failure, not retrying: {exc}")
                return None
            except Exception as exc:
                print(f"Attempt {attempt + 1} failed: {exc}")
                if attempt < max_retries - 1:
                    # Exponential backoff with jitter so multiple instances do not
                    # resynchronise onto the same retry schedule.
                    wait_time = 2**attempt + (asyncio.get_running_loop().time() % 1)
                    print(f"Retrying in {wait_time:.2f} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    print("Max retries reached. Unable to complete the transaction.")

    return None
