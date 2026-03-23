"""Auto-redeem resolved positions via poly_web3 (Builder relayer).

Calls redeemPositions through the Safe proxy wallet.
Requires Builder API credentials in .env.
"""
from __future__ import annotations

import os
import time

import config

try:
    from py_builder_relayer_client.client import RelayClient
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
    from py_clob_client.client import ClobClient
    from poly_web3 import RELAYER_URL, PolyWeb3Service
    HAS_POLY_WEB3 = True
except ImportError:
    HAS_POLY_WEB3 = False


def redeem_all() -> int:
    """Redeem all resolved positions. Returns number redeemed."""
    if not HAS_POLY_WEB3:
        print("[REDEEM] poly-web3 not installed — skip")
        return 0

    builder_key = os.getenv("BUILDER_API_KEY", "")
    builder_secret = os.getenv("BUILDER_SECRET", "")
    builder_passphrase = os.getenv("BUILDER_PASSPHRASE", "")

    if not builder_key or not builder_secret or not builder_passphrase:
        print("[REDEEM] Builder credentials not in .env — skip")
        return 0

    if not config.POLY_PRIVATE_KEY:
        return 0

    try:
        client = ClobClient(
            config.CLOB_HOST,
            key=config.POLY_PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=1,  # Proxy wallet
            funder=config.POLY_FUNDER_ADDRESS,
        )
        client.set_api_creds(client.create_or_derive_api_creds())

        relayer = RelayClient(
            RELAYER_URL,
            config.CHAIN_ID,
            config.POLY_PRIVATE_KEY,
            BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=builder_key,
                    secret=builder_secret,
                    passphrase=builder_passphrase,
                )
            ),
        )

        service = PolyWeb3Service(clob_client=client, relayer_client=relayer)
        results = service.redeem_all(batch_size=10)
        redeemed = len([r for r in results if r])
        if redeemed > 0:
            print(f"[REDEEM] ✅ Redeemed {redeemed} positions")
        return redeemed
    except Exception as e:
        print(f"[REDEEM] ❌ Error: {e}")
        return 0
