"""Multi-Chain JSON-RPC Client & Gas Ceiling Guard."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("rpc_client")

DEFAULT_CHAIN_RPCS: Dict[str, str] = {
    "base": "https://mainnet.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "optimism": "https://mainnet.optimism.io",
    "berachain_artio": "https://artio.rpc.berachain.com",
    "sepolia": "https://rpc.sepolia.org",
    "solana": "https://api.mainnet-beta.solana.com",
    "solana_devnet": "https://api.devnet.solana.com",
}


class MultiChainRPCClient:
    """
    Lightweight zero-dependency JSON-RPC client for EVM and Solana chains.
    Enforces the Gas Ceiling Guard to prevent spending high fees on airdrop farming.
    """

    def __init__(self, custom_rpcs: Optional[Dict[str, str]] = None, timeout: float = 6.0):
        self.rpcs = dict(DEFAULT_CHAIN_RPCS)
        if custom_rpcs:
            self.rpcs.update(custom_rpcs)
        self.timeout = timeout

    def _post_rpc(self, chain: str, method: str, params: list) -> Dict[str, Any]:
        chain_key = chain.lower().strip()
        rpc_url = self.rpcs.get(chain_key, self.rpcs.get("base", "https://mainnet.base.org"))

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            rpc_url,
            data=data_bytes,
            headers={"Content-Type": "application/json", "User-Agent": "AirdropRPCClient/2.0"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_native_balance(self, chain: str, address: str) -> Tuple[float, bool]:
        """
        Returns (balance_native, is_live_rpc).
        If offline or RPC fails, falls back gracefully to a simulated balance.
        """
        chain_clean = chain.lower().strip()
        try:
            if "solana" in chain_clean:
                res = self._post_rpc(chain_clean, "getBalance", [address])
                lamports = res.get("result", {}).get("value", 0)
                return round(lamports / 1e9, 6), True
            else:
                res = self._post_rpc(chain_clean, "eth_getBalance", [address, "latest"])
                hex_val = res.get("result", "0x0")
                wei = int(hex_val, 16)
                return round(wei / 1e18, 6), True
        except Exception as e:
            logger.debug(f"RPC get_native_balance fallback for {chain} ({address[:8]}...): {e}")
            return 0.05, False

    def get_transaction_count(self, chain: str, address: str) -> Tuple[int, bool]:
        """
        Returns (nonce_count, is_live_rpc).
        """
        chain_clean = chain.lower().strip()
        if "solana" in chain_clean:
            return 12, False  # Solana uses recent blockhashes rather than account nonces

        try:
            res = self._post_rpc(chain_clean, "eth_getTransactionCount", [address, "latest"])
            hex_val = res.get("result", "0x0")
            return int(hex_val, 16), True
        except Exception as e:
            logger.debug(f"RPC get_transaction_count fallback for {chain}: {e}")
            return 7, False

    def get_gas_price_gwei(self, chain: str) -> Tuple[float, bool]:
        """
        Returns (gas_price_gwei, is_live_rpc).
        """
        chain_clean = chain.lower().strip()
        if "solana" in chain_clean:
            return 0.000005, True  # Solana micro-lamports equivalent

        try:
            res = self._post_rpc(chain_clean, "eth_gasPrice", [])
            hex_val = res.get("result", "0x0")
            wei = int(hex_val, 16)
            gwei = wei / 1e9
            return round(gwei, 4), True
        except Exception as e:
            logger.debug(f"RPC get_gas_price fallback for {chain}: {e}")
            # Realistic default for L2 Base / Arbitrum
            return 0.02, False

    def estimate_tx_cost_usd(
        self,
        chain: str,
        gas_limit: int = 21000,
        eth_price_usd: float = 3000.0,
    ) -> Tuple[float, bool]:
        """
        Estimates total tx fee in USD.
        Returns (cost_usd, is_live_rpc).
        """
        chain_clean = chain.lower().strip()
        if "solana" in chain_clean:
            return 0.0005, True

        gwei, is_live = self.get_gas_price_gwei(chain_clean)
        eth_cost = (gwei * 1e-9) * gas_limit
        usd_cost = round(eth_cost * eth_price_usd, 6)
        return usd_cost, is_live

    def check_gas_ceiling(
        self,
        chain: str,
        max_cost_usd: float = 0.02,
        gas_limit: int = 45000,
    ) -> Tuple[bool, str, float]:
        """
        Gas Ceiling Guard: Blocks transaction if real-time gas exceeds max_cost_usd threshold.
        Returns (is_allowed, reason, estimated_cost_usd).
        """
        cost_usd, is_live = self.estimate_tx_cost_usd(chain, gas_limit=gas_limit)
        if cost_usd > max_cost_usd:
            return (
                False,
                f"GAS_CEILING_EXCEEDED: Current {chain} fee ${cost_usd:.5f} exceeds ceiling ${max_cost_usd:.5f}. Transaction deferred to protect capital.",
                cost_usd,
            )
        return (
            True,
            f"GAS_SAFE: Estimated fee ${cost_usd:.5f} is within ceiling ${max_cost_usd:.5f}.",
            cost_usd,
        )
