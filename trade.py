"""Orchestration entry point for the pump.fun trading bot.

``trade.py`` owns the WebSocket lifecycle, the buy/sell cycle and the trade log.
``buy.py`` and ``sell.py`` expose only the buy and sell primitives.

Fixes applied here:
  * exactly one ``blockSubscribe`` per socket connection, instead of a fresh
    subscription on every reconnect-free re-entry (which double-traded a token);
  * a seen-mint guard so a duplicate notification cannot buy the same token twice;
  * timezone-aware log timestamps and real on-chain results in the trade log
    instead of a stale spot price reused for both legs;
  * output paths anchored to this file rather than the process CWD.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import websockets
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient

from config import (
    BUY_AMOUNT,
    BUY_SLIPPAGE,
    POST_BUY_HOLD_SECONDS,
    PRE_BUY_DELAY_SECONDS,
    RPC_ENDPOINT,
    SELL_SLIPPAGE,
    WSS_ENDPOINT,
)
from curve import calculate_pump_curve_price, get_pump_curve_state
from buy import (
    buy_token,
    listen_for_create_transaction,
    subscribe_to_program,
)
from sell import sell_token

# Output is written next to this file, not into whatever CWD the process happens
# to inherit from a service manager.
TRADES_DIR: Path = Path(__file__).resolve().parent / "trades"

# Solana addresses are base58: no "/", no "\", no ".". Anything else is a bug or
# an injection attempt and must never reach the filesystem.
_SAFE_MINT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def log_trade(action: str, token_data: dict, result, price: float | None = None) -> None:
    """Append one JSON line describing a trade outcome.

    The previous implementation wrote the same spot price for both the buy and
    the sell leg, which made every logged trade look exactly break-even.
    """
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "token_address": token_data.get("mint"),
        "price_sol": price,
        "status": getattr(result, "status", "unknown"),
        "tx_hash": getattr(result, "signature", None),
        "sol_spent_lamports": getattr(result, "sol_spent_lamports", 0),
        "tokens_delta": getattr(result, "tokens_delta", 0),
        "sol_received_lamports": getattr(result, "sol_received_lamports", 0),
        "detail": getattr(result, "detail", ""),
    }
    with open(TRADES_DIR / "trades.log", "a", encoding="utf-8") as log_file:
        json.dump(log_entry, log_file)
        log_file.write("\n")


def save_token_metadata(token_data: dict) -> Path | None:
    """Persist the decoded token metadata next to the trade log."""
    mint_address = token_data.get("mint", "")
    if not _SAFE_MINT.match(mint_address):
        print(f"Refusing to write metadata for unexpected mint value: {mint_address!r}")
        return None

    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    file_path = TRADES_DIR / f"{mint_address}.txt"
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(token_data, indent=2))
    print(f"Token information saved to {file_path}")
    return file_path


def matches_filters(token_data: dict, match_string: str | None, bro_address: str | None) -> bool:
    """Return True when the token passes the user's name/symbol and creator filters."""
    if match_string:
        needle = match_string.lower()
        haystack = f"{token_data.get('name', '')} {token_data.get('symbol', '')}".lower()
        if needle not in haystack:
            print(f"Token does not match the criteria '{match_string}'. Skipping...")
            return False

    if bro_address and token_data.get("user") != bro_address:
        print(f"Token not created by the specified user '{bro_address}'. Skipping...")
        return False

    return True


async def execute_trade_cycle(token_data: dict, marry_mode: bool = False) -> None:
    """Buy (and unless ``marry_mode``, sell) a freshly created token."""
    save_token_metadata(token_data)

    if PRE_BUY_DELAY_SECONDS > 0:
        print(f"Waiting {PRE_BUY_DELAY_SECONDS:g}s for things to stabilize...")
        await asyncio.sleep(PRE_BUY_DELAY_SECONDS)

    mint = Pubkey.from_string(token_data["mint"])
    bonding_curve = Pubkey.from_string(token_data["bondingCurve"])
    associated_bonding_curve = Pubkey.from_string(token_data["associatedBondingCurve"])

    async with AsyncClient(RPC_ENDPOINT) as client:
        curve_state = await get_pump_curve_state(client, bonding_curve)
        token_price_sol = calculate_pump_curve_price(curve_state)

    print(f"Bonding curve address: {bonding_curve}")
    print(f"Token price: {token_price_sol:.10f} SOL")
    print(
        f"Buying {BUY_AMOUNT:.6f} SOL worth of the new token "
        f"with {BUY_SLIPPAGE * 100:.1f}% slippage tolerance..."
    )

    buy_result = await buy_token(
        mint, bonding_curve, associated_bonding_curve, BUY_AMOUNT, BUY_SLIPPAGE
    )
    log_trade("buy", token_data, buy_result, token_price_sol)
    if buy_result is None or buy_result.status != "confirmed":
        print("Buy did not succeed; skipping the sell leg.")
        return

    if marry_mode:
        print("Marry mode enabled. Skipping sell operation.")
        return

    print(f"Waiting {POST_BUY_HOLD_SECONDS:g}s before selling...")
    await asyncio.sleep(POST_BUY_HOLD_SECONDS)

    print(f"Selling tokens with {SELL_SLIPPAGE * 100:.1f}% slippage tolerance...")
    sell_result = await sell_token(
        mint, bonding_curve, associated_bonding_curve, SELL_SLIPPAGE
    )
    log_trade("sell", token_data, sell_result)
    if sell_result is None or sell_result.status != "confirmed":
        print("Sell did not succeed.")


async def trade(
    websocket,
    match_string: str | None = None,
    bro_address: str | None = None,
    marry_mode: bool = False,
    yolo_mode: bool = False,
) -> None:
    """Consume token creations from an already-subscribed socket."""
    subscription_id = await subscribe_to_program(websocket)
    # A single process must never buy the same mint twice, even if the RPC node
    # replays a notification or a duplicate subscription is active.
    seen_mints: set[str] = set()

    while True:
        print("Waiting for a new token creation...")
        token_data = await listen_for_create_transaction(websocket, subscription_id)
        print("New token created:")
        print(json.dumps(token_data, indent=2))

        mint_address = token_data.get("mint")
        if mint_address in seen_mints:
            print(f"Already processed token {mint_address}; skipping duplicate.")
            if not yolo_mode:
                return
            continue
        seen_mints.add(mint_address)

        if not matches_filters(token_data, match_string, bro_address):
            if not yolo_mode:
                return
            continue

        await execute_trade_cycle(token_data, marry_mode=marry_mode)

        if not yolo_mode:
            return


async def main(
    yolo_mode: bool = False,
    match_string: str | None = None,
    bro_address: str | None = None,
    marry_mode: bool = False,
) -> None:
    """Connect, subscribe once, and run the trade loop with reconnects."""
    while True:
        try:
            async with websockets.connect(WSS_ENDPOINT) as websocket:
                await trade(websocket, match_string, bro_address, marry_mode, yolo_mode)
                if not yolo_mode:
                    return
        except websockets.exceptions.ConnectionClosed:
            print("WebSocket connection closed. Reconnecting...")
        except Exception as exc:
            # Catch Exception, never BaseException: a bare ``except:`` here would
            # swallow KeyboardInterrupt and make the bot unkillable from a terminal.
            print(f"An error occurred: {exc}")

        print("Waiting for 5 seconds before looking for the next token...")
        await asyncio.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trade tokens on Solana.")
    parser.add_argument(
        "--yolo", action="store_true", help="Run in YOLO mode (continuous trading)"
    )
    parser.add_argument(
        "--match", type=str, help="Only trade tokens with names or symbols matching this string"
    )
    parser.add_argument(
        "--bro", type=str, help="Only trade tokens created by this user address"
    )
    parser.add_argument(
        "--marry", action="store_true", help="Only buy tokens, skip selling"
    )
    args = parser.parse_args()
    asyncio.run(
        main(
            yolo_mode=args.yolo,
            match_string=args.match,
            bro_address=args.bro,
            marry_mode=args.marry,
        )
    )
