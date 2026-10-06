"""Runtime configuration for the pump.fun trading bot.

Security note: every secret and endpoint is read from the process environment.
Nothing sensitive may be written into this file or any other tracked file --
this module is committed to version control, the environment is not.
"""

import os

from solders.pubkey import Pubkey

# System & pump.fun addresses
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
PUMP_EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
PUMP_FEE = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
SYSTEM_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_RENT = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
SOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
LAMPORTS_PER_SOL = 1_000_000_000

# Trading parameters
BUY_AMOUNT = float(os.environ.get("BUY_AMOUNT", "0.0001"))  # SOL spent per buy
BUY_SLIPPAGE = float(os.environ.get("BUY_SLIPPAGE", "0.2"))  # fraction, e.g. 0.2 == 20%
SELL_SLIPPAGE = float(os.environ.get("SELL_SLIPPAGE", "0.2"))  # fraction, e.g. 0.2 == 20%

# Extra SOL reserved on top of the buy amount so base + priority fees and the
# worst-case slippage ceiling can never leave the transaction underfunded.
FEE_BUFFER_LAMPORTS = int(os.environ.get("FEE_BUFFER_LAMPORTS", str(10_000)))

# Delay between a `create` event and the buy instruction. A non-zero value makes
# the bot structurally unable to win the launch race it is built for; 0 is the
# only sensible default for a sniping bot.
PRE_BUY_DELAY_SECONDS = float(os.environ.get("PRE_BUY_DELAY_SECONDS", "15"))

# Exit delay in non-marry mode.
POST_BUY_HOLD_SECONDS = float(os.environ.get("POST_BUY_HOLD_SECONDS", "20"))

_PLACEHOLDERS = {
    "SOLANA_NODE_RPC_ENDPOINT",
    "SOLANA_NODE_WSS_ENDPOINT",
    "SOLANA_PRIVATE_KEY",
    "your_rpc_endpoint",
    "your_wss_endpoint",
    "your_base58_private_key",
}


def _require_env(name: str) -> str:
    """Read a required environment variable, failing loudly.

    An unresolved placeholder is treated as a missing value on purpose: a silent
    string default would otherwise surface much later as an opaque base58 or
    connection error, long after the operator believes the bot is configured.
    """
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            f"Copy config.example.py or export {name} before starting the bot."
        )
    if value in _PLACEHOLDERS:
        raise RuntimeError(
            f"Environment variable {name} still holds an unresolved placeholder "
            f"({value!r}). Set it to a real value before starting the bot."
        )
    return value


# Your nodes. For low-latency transaction propagation see
# https://docs.chainstack.com/docs/warp-transactions
RPC_ENDPOINT = _require_env("SOLANA_NODE_RPC_ENDPOINT")
WSS_ENDPOINT = _require_env("SOLANA_NODE_WSS_ENDPOINT")

# Base58 encoded ed25519 secret key of the paying wallet. Treat it as a live
# credential: never log it, never commit it, and rotate it if it is ever leaked.
PRIVATE_KEY = _require_env("SOLANA_PRIVATE_KEY")
