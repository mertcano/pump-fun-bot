"""Buy side of the pump.fun trading bot, plus the block-subscription decoder.

Key correctness properties:
  * the buy amount is computed in integer token base units and range-checked
    before it is serialised into the instruction's ``u64`` fields;
  * the paying wallet's balance is verified before the transaction is built;
  * the associated token account is *confirmed* to exist before the buy that
    depends on it is sent;
  * the trade is only reported as successful after the on-chain outcome has been
    read back.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import time
from pathlib import Path

import base58
import websockets
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solana.transaction import Transaction
from spl.token.instructions import get_associated_token_address

import spl.token.instructions as spl_token

from config import (
    BUY_AMOUNT,
    BUY_SLIPPAGE,
    FEE_BUFFER_LAMPORTS,
    PUMP_EVENT_AUTHORITY,
    PUMP_FEE,
    PUMP_GLOBAL,
    PUMP_PROGRAM,
    RPC_ENDPOINT,
    SYSTEM_PROGRAM,
    SYSTEM_RENT,
    SYSTEM_TOKEN_PROGRAM,
)
from curve import (
    AmountOutOfRangeError,
    TradeResult,
    assert_tradable,
    calculate_pump_curve_price,
    get_pump_curve_state,
    require_u64,
    tokens_for_sol_amount,
)
from sell import (
    PermanentTradeError,
    TransientTradeError,
    classify_transaction,
    fetch_transaction_meta,
    load_payer,
)

# Precalculated discriminators. See learning-examples/calculate_discriminator.py
BUY_DISCRIMINATOR: bytes = struct.pack("<Q", 16927863322537952870)
CREATE_DISCRIMINATOR: int = 8576854823835016728

# The IDL is resolved relative to this file rather than the process CWD, so the
# bot can be started from any working directory (systemd, a service wrapper).
IDL_PATH: Path = Path(__file__).resolve().parent / "idl" / "pump_fun_idl.json"


def load_idl(file_path: Path) -> dict:
    """Load and cache the Anchor IDL used to decode ``create`` instructions."""
    with open(file_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


_IDL: dict | None = None


def get_idl() -> dict:
    """Return the cached IDL, loading it at most once per process."""
    global _IDL
    if _IDL is None:
        _IDL = load_idl(IDL_PATH)
    return _IDL


def decode_create_instruction(ix_data: bytes, ix_def: dict, accounts: list[str]) -> dict:
    """Decode a pump.fun ``create`` instruction payload and its accounts."""
    args: dict = {}
    offset = 8  # Skip the 8-byte Anchor discriminator

    for arg in ix_def["args"]:
        if arg["type"] == "string":
            length = struct.unpack_from("<I", ix_data, offset)[0]
            offset += 4
            value = ix_data[offset : offset + length].decode("utf-8")
            offset += length
        elif arg["type"] == "publicKey":
            value = base64.b64encode(ix_data[offset : offset + 32]).decode("utf-8")
            offset += 32
        else:
            raise ValueError(f"Unsupported type: {arg['type']}")

        args[arg["name"]] = value

    # Account layout of the `create` instruction, per the IDL.
    args["mint"] = accounts[0]
    args["bondingCurve"] = accounts[2]
    args["associatedBondingCurve"] = accounts[3]
    args["user"] = accounts[7]
    return args


def resolve_account_keys(message) -> list[str]:
    """Return every account key of a versioned message as a list of strings.

    ``message.account_keys`` only covers statically declared addresses. Indices
    in ``CompiledInstruction.accounts`` address the *combined* static + loaded
    key space, so a message that uses an address lookup table would be indexed
    wrongly (or raise IndexError). ``get_account_keys()`` resolves both.
    """
    try:
        return [str(key) for key in message.get_account_keys()]
    except AttributeError:
        # Older client versions expose only the static list.
        return [str(key) for key in message.account_keys]


async def subscribe_to_program(websocket) -> int:
    """Send exactly one ``blockSubscribe`` request and return its JSON-RPC id."""
    request_id = 1
    subscription_message = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "blockSubscribe",
            "params": [
                {"mentionsAccountOrProgram": str(PUMP_PROGRAM)},
                {
                    "commitment": "confirmed",
                    "encoding": "base64",
                    "showRewards": False,
                    "transactionDetails": "full",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        }
    )
    await websocket.send(subscription_message)
    print(f"Subscribed to blocks mentioning program: {PUMP_PROGRAM}")
    return request_id


async def listen_for_create_transaction(websocket, subscription_id: int | None = None):
    """Await the next pump.fun token creation on an already-subscribed socket.

    ``subscription_id`` is accepted so a caller can assert that a message belongs
    to this subscription rather than to a stale one.
    """
    idl = get_idl()
    create_ix_def = next(instr for instr in idl["instructions"] if instr["name"] == "create")

    ping_interval = 20
    last_ping_time = time.time()

    while True:
        try:
            current_time = time.time()
            if current_time - last_ping_time > ping_interval:
                await websocket.ping()
                last_ping_time = current_time

            response = await asyncio.wait_for(websocket.recv(), timeout=30)
            data = json.loads(response)

            # Ignore the subscription acknowledgement and any non-notification frame.
            if subscription_id is not None and data.get("id") == subscription_id:
                if "error" in data:
                    raise TransientTradeError(
                        f"blockSubscribe rejected by the RPC node: {data['error']}"
                    )
                continue

            if data.get("method") != "blockNotification":
                continue

            params = data.get("params") or {}
            result = params.get("result") or {}
            block = (result.get("value") or {}).get("block") or {}

            for tx in block.get("transactions", []):
                if not isinstance(tx, dict) or "transaction" not in tx:
                    continue

                try:
                    tx_data_decoded = base64.b64decode(tx["transaction"][0])
                    transaction = VersionedTransaction.from_bytes(tx_data_decoded)
                    account_keys = resolve_account_keys(transaction.message)
                except Exception as exc:
                    print(f"Skipping undecodable transaction: {exc}")
                    continue

                for ix in transaction.message.instructions:
                    if account_keys[ix.program_id_index] != str(PUMP_PROGRAM):
                        continue

                    ix_data = bytes(ix.data)
                    if len(ix_data) < 8:
                        continue
                    discriminator = struct.unpack("<Q", ix_data[:8])[0]
                    if discriminator != CREATE_DISCRIMINATOR:
                        continue

                    try:
                        ix_accounts = [account_keys[index] for index in ix.accounts]
                    except IndexError as exc:
                        # A loaded address we cannot resolve means we cannot trust
                        # the decoded accounts; never trade on a partial decode.
                        raise TransientTradeError(
                            f"Instruction references an unresolved account: {exc}"
                        ) from exc

                    return decode_create_instruction(ix_data, create_ix_def, ix_accounts)

        except asyncio.TimeoutError:
            print("No data received for 30 seconds, sending ping...")
            await websocket.ping()
            last_ping_time = time.time()
        except websockets.exceptions.ConnectionClosed:
            print("WebSocket connection closed. Reconnecting...")
            raise


async def ensure_associated_token_account(
    client: AsyncClient, payer: Keypair, associated_token_account: Pubkey, mint: Pubkey
) -> None:
    """Create the payer ATA if missing and wait until it is actually on chain.

    Broadcasting the create and immediately sending the buy leaves a race: the buy
    names the ATA as writable and reverts if the create has not landed yet.
    """
    account_info = await client.get_account_info(associated_token_account)
    if account_info.value is not None:
        print(f"Associated token account already exists: {associated_token_account}")
        return

    print("Creating associated token account...")
    create_ata_ix = spl_token.create_associated_token_account(
        payer=payer.pubkey(),
        owner=payer.pubkey(),
        mint=mint,
    )
    transaction = Transaction()
    transaction.add(create_ata_ix)
    recent_blockhash = await client.get_latest_blockhash()
    transaction.recent_blockhash = recent_blockhash.value.blockhash

    tx = await client.send_transaction(
        transaction,
        payer,
        opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed),
    )
    signature = str(tx.value)
    await client.confirm_transaction(tx.value, commitment="confirmed")

    meta = await fetch_transaction_meta(client, signature)
    status = classify_transaction(meta)
    if status != "confirmed":
        raise PermanentTradeError(
            f"Associated token account creation did not execute (status={status})"
        )

    # Confirm the account is readable before the buy that depends on it is built.
    account_info = await client.get_account_info(associated_token_account)
    if account_info.value is None:
        raise TransientTradeError(
            "Associated token account still not visible after confirmation"
        )

    print(f"Associated token account created: {associated_token_account}")


async def buy_token(
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_bonding_curve: Pubkey,
    amount: float = BUY_AMOUNT,
    slippage: float = BUY_SLIPPAGE,
    max_retries: int = 5,
) -> TradeResult | None:
    """Buy a new pump.fun token with ``amount`` SOL.

    Returns a :class:`TradeResult` reflecting the on-chain outcome, or ``None``
    when the trade could not be placed.
    """
    if not 0.0 <= slippage < 1.0:
        raise ValueError(f"slippage must be in [0, 1), got {slippage!r}")
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount!r}")

    payer = load_payer()

    async with AsyncClient(RPC_ENDPOINT) as client:
        associated_token_account = get_associated_token_address(payer.pubkey(), mint)
        amount_lamports = int(amount * 1_000_000_000)

        curve_state = await get_pump_curve_state(client, bonding_curve)
        assert_tradable(curve_state)
        token_price_sol = calculate_pump_curve_price(curve_state)

        # Compute the token amount in integer base units and range-check it before
        # anything is serialised. A float round-trip can truncate to 0 tokens or
        # overflow the u64 field, both of which revert the instruction.
        try:
            token_units = tokens_for_sol_amount(curve_state, amount_lamports)
        except AmountOutOfRangeError as exc:
            # Deterministic: the same inputs would fail identically on a retry.
            print(f"Refusing to buy: {exc}")
            return None

        # Maximum SOL the curve may take, including the slippage ceiling.
        max_amount_lamports = require_u64(
            int(amount_lamports * (1 + slippage)) + FEE_BUFFER_LAMPORTS, "max_amount_lamports"
        )

        # Never broadcast a transaction that is guaranteed to be underfunded.
        balance_response = await client.get_balance(payer.pubkey())
        balance_lamports = balance_response.value
        required_lamports = max_amount_lamports
        if balance_lamports < required_lamports:
            print(
                f"Insufficient balance: have {balance_lamports} lamports, "
                f"need at least {required_lamports}. Skipping token."
            )
            return None

        await ensure_associated_token_account(client, payer, associated_token_account, mint)

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
                    AccountMeta(pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=SYSTEM_RENT, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
                ]

                data = (
                    BUY_DISCRIMINATOR
                    + struct.pack("<Q", require_u64(token_units, "token_amount"))
                    + struct.pack("<Q", max_amount_lamports)
                )
                buy_ix = Instruction(PUMP_PROGRAM, data, accounts)

                # Refetched every attempt so a retry never reuses a stale blockhash.
                recent_blockhash = await client.get_latest_blockhash()
                transaction = Transaction()
                transaction.add(buy_ix)
                transaction.recent_blockhash = recent_blockhash.value.blockhash

                # Preflight stays enabled: it surfaces "insufficient funds for fee"
                # and "invalid account" locally instead of on mainnet.
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
                return TradeResult(
                    signature=signature,
                    status="confirmed",
                    sol_spent_lamports=amount_lamports,
                    tokens_delta=token_units,
                )

            except PermanentTradeError as exc:
                print(f"Permanent failure, not retrying: {exc}")
                return None
            except Exception as exc:
                print(f"Attempt {attempt + 1} failed: {exc}")
                if attempt < max_retries - 1:
                    wait_time = 2**attempt + (asyncio.get_running_loop().time() % 1)
                    print(f"Retrying in {wait_time:.2f} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    print("Max retries reached. Unable to complete the transaction.")

    return None
