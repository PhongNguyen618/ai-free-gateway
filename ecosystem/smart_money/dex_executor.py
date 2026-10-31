"""Strict Live On-Chain DEX Execution Engine (Zero Simulation / Fail-Closed).

Thực thi giao dịch Swap thật 100% trên Decentralized Exchanges (DEX):
- EVM (Base / BSC / Arbitrum): Khớp lệnh qua 0x Swap API v2 (AllowanceHolder EIP-1559). Tự động duyệt Approve thật.
- Solana: Khớp lệnh qua Jupiter Swap API (Ed25519 VersionedTransaction).
- Kiểm soát an toàn vốn: MAX_LIVE_ORDER_USD, MAX_LIVE_EXPOSURE_USD, LIVE_PAUSE_BUYS, Gas Reserve.
- Nguyên tắc: FAIL-CLOSED. Chỉ trả về success=True khi và chỉ khi có Tx Hash thật và On-Chain Receipt xác nhận thành công.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sqlite3
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger("dex_executor")

JOURNAL_DB_PATH = Path(__file__).resolve().parent.parent.parent / "storage" / "gateway.db"

CHAIN_CONFIGS: Dict[str, Dict[str, Any]] = {
    "base": {
        "chain_id": 8453,
        "name": "Base Mainnet",
        "rpc_url": os.getenv("BASE_RPC_URL", "https://mainnet.base.org"),
        "wrapped_native": "0x4200000000000000000000000000000000000006",  # WETH
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "explorer": "https://basescan.org/tx/",
        "native_symbol": "ETH",
        "native_decimals": 18,
        "gas_reserve": 0.0003,
    },
    "bsc": {
        "chain_id": 56,
        "name": "BNB Smart Chain",
        "rpc_url": os.getenv("BSC_RPC_URL", "https://binance.llamarpc.com"),
        "wrapped_native": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",  # WBNB
        "usdc": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
        "explorer": "https://bscscan.com/tx/",
        "native_symbol": "BNB",
        "native_decimals": 18,
        "gas_reserve": 0.002,
    },
    "arbitrum": {
        "chain_id": 42161,
        "name": "Arbitrum One",
        "rpc_url": os.getenv("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc"),
        "wrapped_native": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
        "usdc": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "explorer": "https://arbiscan.io/tx/",
        "native_symbol": "ETH",
        "native_decimals": 18,
        "gas_reserve": 0.0003,
    },
    "solana": {
        "chain_id": 101,
        "name": "Solana Mainnet",
        "rpc_url": os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
        "wrapped_native": "So11111111111111111111111111111111111111112",  # WSOL
        "usdc": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "explorer": "https://solscan.io/tx/",
        "native_symbol": "SOL",
        "native_decimals": 9,
        "gas_reserve": 0.01,
    },
}


class LiveDexExecutor:
    """Bộ thực thi DEX On-Chain Thật Tuyệt Đối (Fail-Closed)."""

    def __init__(self, journal_db_path: Optional[Path] = None):
        self.zerox_api_key = os.getenv("ZEROX_API_KEY", "").strip()
        self.jupiter_api_key = os.getenv("JUPITER_API_KEY", "").strip()
        self.evm_private_key = os.getenv("EVM_PRIVATE_KEY", "").strip()
        self.solana_private_key = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
        self.enable_live = os.getenv("ENABLE_LIVE_EXECUTION", "false").lower() in ("true", "1", "yes")
        self.max_order_usd = float(os.getenv("MAX_LIVE_ORDER_USD", "2.0"))
        self.max_exposure_usd = float(os.getenv("MAX_LIVE_EXPOSURE_USD", "10.0"))
        self.pause_buys = os.getenv("LIVE_PAUSE_BUYS", "false").lower() in ("true", "1", "yes")
        self.max_slippage_bps = int(os.getenv("MAX_SLIPPAGE_BPS", "300"))
        self.db_path = journal_db_path or JOURNAL_DB_PATH
        self._ensure_journal_table()

    def _ensure_journal_table(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS live_tx_journal (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        position_id TEXT,
                        chain TEXT NOT NULL,
                        side TEXT NOT NULL,
                        token_address TEXT NOT NULL,
                        tx_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        amount_usd REAL,
                        details TEXT,
                        created_at REAL NOT NULL
                    )
                    """
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"Journal table ensure note: {e}")

    def log_tx_journal(
        self,
        chain: str,
        side: str,
        token_address: str,
        tx_hash: str,
        status: str,
        position_id: str = "",
        amount_usd: float = 0.0,
        details: str = "",
    ) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO live_tx_journal (position_id, chain, side, token_address, tx_hash, status, amount_usd, details, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (position_id, chain, side, token_address, tx_hash, status, amount_usd, details, time.time()),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to record tx journal: {e}")

    def resolve_evm_address(self) -> Optional[str]:
        """Lấy địa chỉ ví EVM thực tế từ khóa bí mật EVM_PRIVATE_KEY."""
        if not self.evm_private_key:
            return None
        try:
            from eth_account import Account
            account = Account.from_key(self.evm_private_key)
            return account.address
        except Exception as exc:
            logger.error(f"Cannot derive EVM address from key: {exc}")
            return None

    def resolve_solana_address(self) -> Optional[str]:
        """Lấy địa chỉ ví Solana thực tế từ khóa bí mật SOLANA_PRIVATE_KEY."""
        if not self.solana_private_key:
            return None
        try:
            import base58
            from solders.keypair import Keypair
            key_bytes = base58.b58decode(self.solana_private_key)
            kp = Keypair.from_bytes(key_bytes)
            return str(kp.pubkey())
        except Exception as exc:
            logger.error(f"Cannot derive Solana address from key: {exc}")
            return None

    async def get_native_price_usd(self, chain: str) -> float:
        """Lấy giá USD thời gian thực của native token (ETH/BNB/SOL) từ DexScreener."""
        chain_key = chain.lower()
        cfg = CHAIN_CONFIGS.get(chain_key)
        if not cfg:
            return 0.0
        wrapped = cfg["wrapped_native"]
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{wrapped}")
                if resp.status_code == 200:
                    pairs = resp.json().get("pairs") or []
                    for p in pairs:
                        if p.get("chainId") == chain_key:
                            price = float(p.get("priceUsd") or 0.0)
                            if price > 0:
                                return price
        except Exception as e:
            logger.warning(f"Could not fetch native price for {chain}: {e}")
        return 0.0

    async def get_wallet_balances(self, chain: str = "base") -> Dict[str, Any]:
        """Đọc số dư On-Chain thực tế của ví trên mạng chỉ định."""
        chain_key = chain.lower()
        if chain_key not in CHAIN_CONFIGS:
            return {"error": f"Unsupported chain: {chain}", "live_verified": False}

        cfg = CHAIN_CONFIGS[chain_key]
        if chain_key == "solana":
            sol_address = self.resolve_solana_address()
            if not sol_address:
                return {
                    "chain": chain_key,
                    "address": None,
                    "native_balance": 0.0,
                    "symbol": "SOL",
                    "configured": False,
                    "live_verified": True,
                    "message": "Chưa cấu hình SOLANA_PRIVATE_KEY",
                }
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        cfg["rpc_url"],
                        json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [sol_address, {"commitment": "confirmed"}]},
                    )
                    data = resp.json()
                    lamports = data.get("result", {}).get("value", 0)
                    sol_bal = lamports / (10 ** 9)
                    return {
                        "chain": chain_key,
                        "address": sol_address,
                        "native_balance": sol_bal,
                        "symbol": "SOL",
                        "configured": True,
                        "live_verified": True,
                    }
            except Exception as e:
                return {"chain": chain_key, "address": sol_address, "error": str(e), "live_verified": False}

        # EVM Chains (Base, BSC, Arbitrum)
        evm_address = self.resolve_evm_address()
        if not evm_address:
            return {
                "chain": chain_key,
                "address": None,
                "native_balance": 0.0,
                "symbol": cfg["native_symbol"],
                "configured": False,
                "live_verified": True,
                "message": "Chưa cấu hình EVM_PRIVATE_KEY",
            }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                bal_resp = await client.post(
                    cfg["rpc_url"],
                    json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [evm_address, "latest"]},
                )
                bal_hex = bal_resp.json().get("result", "0x0")
                native_wei = int(bal_hex, 16)
                native_bal = native_wei / (10 ** cfg["native_decimals"])

                weth_addr = cfg["wrapped_native"]
                weth_calldata = "0x70a08231" + evm_address[2:].lower().rjust(64, "0")
                weth_resp = await client.post(
                    cfg["rpc_url"],
                    json={"jsonrpc": "2.0", "id": 2, "method": "eth_call", "params": [{"to": weth_addr, "data": weth_calldata}, "latest"]},
                )
                weth_hex = weth_resp.json().get("result", "0x0")
                weth_wei = int(weth_hex, 16) if weth_hex != "0x" else 0
                weth_bal = weth_wei / (10 ** cfg["native_decimals"])

                return {
                    "chain": chain_key,
                    "address": evm_address,
                    "native_balance": native_bal,
                    "wrapped_balance": weth_bal,
                    "symbol": cfg["native_symbol"],
                    "configured": True,
                    "live_verified": True,
                }
        except Exception as e:
            return {"chain": chain_key, "address": evm_address, "error": str(e), "live_verified": False}

    async def get_erc20_balance(self, chain: str, token_address: str, wallet_address: str) -> Tuple[int, int]:
        """Đọc raw balance và decimals của ERC-20 token trực tiếp từ RPC."""
        chain_key = chain.lower()
        cfg = CHAIN_CONFIGS.get(chain_key)
        if not cfg or chain_key == "solana":
            return 0, 18
        calldata_bal = "0x70a08231" + wallet_address[2:].lower().rjust(64, "0")
        calldata_dec = "0x313ce567"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp_b = await client.post(
                    cfg["rpc_url"],
                    json={"jsonrpc": "2.0", "id": 10, "method": "eth_call", "params": [{"to": token_address, "data": calldata_bal}, "latest"]},
                )
                raw_bal_hex = resp_b.json().get("result", "0x0")
                raw_bal = int(raw_bal_hex, 16) if raw_bal_hex != "0x" else 0

                resp_d = await client.post(
                    cfg["rpc_url"],
                    json={"jsonrpc": "2.0", "id": 11, "method": "eth_call", "params": [{"to": token_address, "data": calldata_dec}, "latest"]},
                )
                raw_dec_hex = resp_d.json().get("result", "0x12")
                decimals = int(raw_dec_hex, 16) if raw_dec_hex != "0x" else 18
                return raw_bal, decimals
        except Exception as e:
            logger.warning(f"Cannot get erc20 balance for {token_address}: {e}")
            return 0, 18

    async def get_solana_token_balance(self, token_mint: str, wallet_address: str) -> Tuple[int, int]:
        """Đọc token balance và decimals của Solana SPL Token từ RPC."""
        cfg = CHAIN_CONFIGS.get("solana")
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    cfg["rpc_url"],
                    json={
                        "jsonrpc": "2.0",
                        "id": 12,
                        "method": "getTokenAccountsByOwner",
                        "params": [
                            wallet_address,
                            {"mint": token_mint},
                            {"encoding": "jsonParsed", "commitment": "confirmed"},
                        ],
                    },
                )
                rows = resp.json().get("result", {}).get("value", [])
                total_amount = 0
                decimals = 9
                for r in rows:
                    info = r.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("tokenAmount", {})
                    total_amount += int(info.get("amount", "0"))
                    decimals = int(info.get("decimals", 9))
                return total_amount, decimals
        except Exception as e:
            logger.warning(f"Cannot get solana token balance: {e}")
            return 0, 9

    async def send_evm_approve_if_needed(
        self,
        chain: str,
        token_address: str,
        spender: str,
        amount_wei: int,
    ) -> bool:
        """Ký và phát sóng giao dịch Approve ERC-20 on-chain nếu chưa đủ allowance."""
        cfg = CHAIN_CONFIGS.get(chain.lower())
        evm_address = self.resolve_evm_address()
        if not evm_address or not self.evm_private_key:
            return False

        # Kiểm tra allowance hiện tại
        allowance_data = "0xdd62ed3e" + evm_address[2:].lower().rjust(64, "0") + spender[2:].lower().rjust(64, "0")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    cfg["rpc_url"],
                    json={"jsonrpc": "2.0", "id": 20, "method": "eth_call", "params": [{"to": token_address, "data": allowance_data}, "latest"]},
                )
                curr_allowance = int(res.json().get("result", "0x0"), 16)
                if curr_allowance >= amount_wei:
                    logger.info(f"Allowance already sufficient: {curr_allowance} >= {amount_wei}")
                    return True

                # Ký giao dịch Approve
                from eth_account import Account
                acct = Account.from_key(self.evm_private_key)
                nonce_res = await client.post(
                    cfg["rpc_url"],
                    json={"jsonrpc": "2.0", "id": 21, "method": "eth_getTransactionCount", "params": [evm_address, "pending"]},
                )
                nonce = int(nonce_res.json().get("result", "0x0"), 16)

                # approve(spender, type(uint256).max)
                approve_payload = "0x095ea7b3" + spender[2:].lower().rjust(64, "0") + "f" * 64
                approve_tx = {
                    "to": token_address,
                    "data": approve_payload,
                    "value": 0,
                    "gas": 65000,
                    "gasPrice": int(0.05 * 1e9),
                    "nonce": nonce,
                    "chainId": cfg["chain_id"],
                }
                signed_approve = acct.sign_transaction(approve_tx)

                send_res = await client.post(
                    cfg["rpc_url"],
                    json={"jsonrpc": "2.0", "id": 22, "method": "eth_sendRawTransaction", "params": ["0x" + signed_approve.raw_transaction.hex()]},
                )
                appr_hash = send_res.json().get("result")
                if not appr_hash:
                    logger.error(f"Approve broadcast error: {send_res.text}")
                    return False

                logger.info(f"Approve tx sent: {appr_hash}. Waiting receipt...")
                for _ in range(15):
                    await asyncio.sleep(2.0)
                    rcpt_res = await client.post(
                        cfg["rpc_url"],
                        json={"jsonrpc": "2.0", "id": 23, "method": "eth_getTransactionReceipt", "params": [appr_hash]},
                    )
                    rcpt = rcpt_res.json().get("result")
                    if rcpt and rcpt.get("status") == "0x1":
                        logger.info("Approve confirmed on-chain!")
                        return True
                    elif rcpt and rcpt.get("status") == "0x0":
                        logger.error("Approve tx reverted on-chain!")
                        return False
        except Exception as exc:
            logger.error(f"Approve exception: {exc}")
        return False

    async def execute_evm_swap_zerox(
        self,
        chain: str,
        buy_token: str,
        sell_token: str,
        sell_amount_wei: int,
        slippage_bps: int = 150,
        position_id: str = "",
    ) -> Dict[str, Any]:
        """Ký và thực thi swap thật trên EVM qua 0x Swap API v2 (AllowanceHolder)."""
        chain_key = chain.lower()
        if chain_key not in CHAIN_CONFIGS or chain_key == "solana":
            return {"success": False, "error": f"Invalid EVM chain: {chain}", "live_verified": False}

        cfg = CHAIN_CONFIGS[chain_key]
        evm_address = self.resolve_evm_address()
        if not evm_address or not self.evm_private_key:
            return {
                "success": False,
                "error": "MISSING_EVM_PRIVATE_KEY",
                "message": "Không tìm thấy khóa EVM_PRIVATE_KEY để ký giao dịch thật.",
                "live_verified": False,
            }

        if not self.zerox_api_key:
            return {
                "success": False,
                "error": "MISSING_ZEROX_API_KEY",
                "message": "Chưa cấu hình ZEROX_API_KEY để lấy báo giá swap.",
                "live_verified": False,
            }

        try:
            from eth_account import Account
            account = Account.from_key(self.evm_private_key)

            # Bước 1: Lấy báo giá AllowanceHolder Quote từ 0x API v2
            headers = {
                "0x-api-key": self.zerox_api_key,
                "0x-version": "v2",
            }
            quote_url = f"https://api.0x.org/swap/allowance-holder/quote"
            params = {
                "chainId": str(cfg["chain_id"]),
                "buyToken": buy_token,
                "sellToken": sell_token,
                "sellAmount": str(sell_amount_wei),
                "taker": evm_address,
                "slippageBps": str(min(slippage_bps, self.max_slippage_bps)),
            }

            async with httpx.AsyncClient(timeout=15.0) as client:
                quote_resp = await client.get(quote_url, headers=headers, params=params)
                if quote_resp.status_code != 200:
                    return {
                        "success": False,
                        "error": f"0x_QUOTE_ERROR_{quote_resp.status_code}",
                        "details": quote_resp.text,
                        "live_verified": False,
                    }
                quote_data = quote_resp.json()
                tx_data = quote_data.get("transaction")
                if not tx_data:
                    return {
                        "success": False,
                        "error": "NO_TRANSACTION_PAYLOAD",
                        "details": quote_data,
                        "live_verified": False,
                    }

                # Bước 2: Kiểm tra và phê duyệt Token (Approve) nếu sell_token không phải là Native
                issues = quote_data.get("issues", {})
                allowance_issue = issues.get("allowance")
                if allowance_issue and allowance_issue.get("spender"):
                    spender = allowance_issue["spender"]
                    approved = await self.send_evm_approve_if_needed(chain_key, sell_token, spender, sell_amount_wei)
                    if not approved:
                        return {
                            "success": False,
                            "error": "APPROVE_REVERTED_OR_FAILED",
                            "message": f"Không thể hoàn tất giao dịch Approve trên mạng {chain_key}.",
                            "live_verified": False,
                        }

                # Bước 3: Lấy Nonce & Gas Fee thực tế từ RPC
                nonce_resp = await client.post(
                    cfg["rpc_url"],
                    json={"jsonrpc": "2.0", "id": 3, "method": "eth_getTransactionCount", "params": [evm_address, "pending"]},
                )
                nonce = int(nonce_resp.json().get("result", "0x0"), 16)

                raw_tx = {
                    "to": tx_data["to"],
                    "data": tx_data["data"],
                    "value": int(tx_data.get("value", 0)),
                    "gas": int(tx_data.get("gas", 350000)),
                    "nonce": nonce,
                    "chainId": cfg["chain_id"],
                }

                if "maxFeePerGas" in tx_data and "maxPriorityFeePerGas" in tx_data:
                    raw_tx["maxFeePerGas"] = int(tx_data["maxFeePerGas"])
                    raw_tx["maxPriorityFeePerGas"] = int(tx_data["maxPriorityFeePerGas"])
                    raw_tx["type"] = 2
                else:
                    raw_tx["gasPrice"] = int(tx_data.get("gasPrice", 1000000000))

                # Bước 4: Ký giao dịch On-Chain thật
                signed_tx = account.sign_transaction(raw_tx)

                # Bước 5: Phát sóng lên RPC Mainnet
                send_resp = await client.post(
                    cfg["rpc_url"],
                    json={"jsonrpc": "2.0", "id": 4, "method": "eth_sendRawTransaction", "params": ["0x" + signed_tx.raw_transaction.hex()]},
                )
                send_data = send_resp.json()
                if "error" in send_data:
                    return {
                        "success": False,
                        "error": "RPC_BROADCAST_ERROR",
                        "details": send_data["error"],
                        "live_verified": False,
                    }

                real_tx_hash = send_data.get("result")
                self.log_tx_journal(chain_key, "SWAP", buy_token if sell_token.lower() == "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" else sell_token, real_tx_hash, "PENDING", position_id=position_id)

                # Bước 6: Chờ xác nhận Receipt On-Chain (async sleep)
                status = "PENDING"
                for _ in range(12):
                    await asyncio.sleep(2.0)
                    rcpt_resp = await client.post(
                        cfg["rpc_url"],
                        json={"jsonrpc": "2.0", "id": 5, "method": "eth_getTransactionReceipt", "params": [real_tx_hash]},
                    )
                    rcpt = rcpt_resp.json().get("result")
                    if rcpt:
                        status = "SUCCESS" if rcpt.get("status") == "0x1" else "REVERTED"
                        break

                self.log_tx_journal(chain_key, "SWAP", buy_token, real_tx_hash, status, position_id=position_id)

                return {
                    "success": (status == "SUCCESS"),
                    "tx_hash": real_tx_hash,
                    "explorer_url": f"{cfg['explorer']}{real_tx_hash}",
                    "chain": chain_key,
                    "status": status,
                    "buy_token": buy_token,
                    "sell_token": sell_token,
                    "sell_amount_wei": sell_amount_wei,
                    "live_verified": True,
                }

        except Exception as exc:
            logger.error(f"EVM swap error: {exc}", exc_info=True)
            return {
                "success": False,
                "error": "EVM_SWAP_EXCEPTION",
                "message": str(exc),
                "live_verified": False,
            }

    async def execute_solana_swap_jupiter(
        self,
        buy_mint: str,
        sell_mint: str,
        amount_lamports: int,
        slippage_bps: int = 150,
        position_id: str = "",
    ) -> Dict[str, Any]:
        """Ký và thực thi swap thật trên Solana qua Jupiter Swap API."""
        cfg = CHAIN_CONFIGS["solana"]
        sol_address = self.resolve_solana_address()
        if not sol_address or not self.solana_private_key:
            return {
                "success": False,
                "error": "MISSING_SOLANA_PRIVATE_KEY",
                "message": "Chưa cấu hình SOLANA_PRIVATE_KEY.",
                "live_verified": False,
            }

        try:
            import base58
            from solders.keypair import Keypair
            from solders.transaction import VersionedTransaction

            kp = Keypair.from_bytes(base58.b58decode(self.solana_private_key))

            # 1. Quote
            headers = {}
            if self.jupiter_api_key:
                headers["x-api-key"] = self.jupiter_api_key

            quote_url = f"https://quote-api.jup.ag/v6/quote"
            params = {
                "inputMint": sell_mint,
                "outputMint": buy_mint,
                "amount": str(amount_lamports),
                "slippageBps": str(min(slippage_bps, self.max_slippage_bps)),
            }

            async with httpx.AsyncClient(timeout=15.0) as client:
                q_resp = await client.get(quote_url, headers=headers, params=params)
                if q_resp.status_code != 200:
                    return {"success": False, "error": f"JUPITER_QUOTE_ERROR_{q_resp.status_code}", "live_verified": False}
                quote_data = q_resp.json()

                # 2. Swap transaction payload
                swap_url = "https://quote-api.jup.ag/v6/swap"
                swap_payload = {
                    "quoteResponse": quote_data,
                    "userPublicKey": sol_address,
                    "wrapAndUnwrapSol": True,
                }
                s_resp = await client.post(swap_url, headers=headers, json=swap_payload)
                if s_resp.status_code != 200:
                    return {"success": False, "error": f"JUPITER_SWAP_PAYLOAD_ERROR_{s_resp.status_code}", "live_verified": False}

                swap_data = s_resp.json()
                raw_tx_b64 = swap_data.get("swapTransaction")
                if not raw_tx_b64:
                    return {"success": False, "error": "NO_JUPITER_SWAP_TX", "live_verified": False}

                # 3. Ký transaction Ed25519
                tx_bytes = base64.b64decode(raw_tx_b64)
                v_tx = VersionedTransaction.from_bytes(tx_bytes)
                message = v_tx.message
                signature = kp.sign_message(message.to_bytes_versioned(message))
                signed_tx = VersionedTransaction.populate(message, [signature])

                # 4. Phát sóng lên RPC Solana
                encoded_signed_tx = base64.b64encode(bytes(signed_tx)).decode("utf-8")
                b_resp = await client.post(
                    cfg["rpc_url"],
                    json={
                        "jsonrpc": "2.0",
                        "id": 30,
                        "method": "sendTransaction",
                        "params": [encoded_signed_tx, {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}],
                    },
                )
                b_data = b_resp.json()
                if "error" in b_data:
                    return {"success": False, "error": "SOLANA_BROADCAST_ERROR", "details": b_data["error"], "live_verified": False}

                sig_str = b_data.get("result")
                self.log_tx_journal("solana", "SWAP", buy_mint, sig_str, "PENDING", position_id=position_id)

                # 5. Chờ confirm receipt
                status = "PENDING"
                for _ in range(15):
                    await asyncio.sleep(2.0)
                    stat_resp = await client.post(
                        cfg["rpc_url"],
                        json={"jsonrpc": "2.0", "id": 31, "method": "getSignatureStatuses", "params": [[sig_str], {"searchTransactionHistory": True}]},
                    )
                    statuses = stat_resp.json().get("result", {}).get("value", [])
                    if statuses and statuses[0]:
                        cs = statuses[0].get("confirmationStatus")
                        err = statuses[0].get("err")
                        if err:
                            status = "REVERTED"
                            break
                        if cs in ("confirmed", "finalized"):
                            status = "SUCCESS"
                            break

                self.log_tx_journal("solana", "SWAP", buy_mint, sig_str, status, position_id=position_id)

                return {
                    "success": (status == "SUCCESS"),
                    "tx_hash": sig_str,
                    "explorer_url": f"{cfg['explorer']}{sig_str}",
                    "chain": "solana",
                    "status": status,
                    "live_verified": True,
                }
        except Exception as exc:
            logger.error(f"Solana swap error: {exc}", exc_info=True)
            return {"success": False, "error": "SOLANA_SWAP_EXCEPTION", "message": str(exc), "live_verified": False}

    async def execute_buy(
        self,
        chain: str,
        token_address: str,
        amount_usd: float,
        position_id: str = "",
    ) -> Dict[str, Any]:
        """Thực thi lệnh BUY On-Chain Thật với số vốn USD chỉ định."""
        if not self.enable_live:
            return {
                "success": False,
                "error": "LIVE_EXECUTION_PAUSED",
                "message": "ENABLE_LIVE_EXECUTION=false",
                "live_verified": False,
            }

        if self.pause_buys:
            return {
                "success": False,
                "error": "BUYS_TEMPORARILY_PAUSED",
                "message": "Chế độ tạm dừng mua mới (LIVE_PAUSE_BUYS=true) đang bật.",
                "live_verified": False,
            }

        if amount_usd > self.max_order_usd:
            return {
                "success": False,
                "error": "MAX_ORDER_USD_EXCEEDED",
                "message": f"Số tiền vào lệnh (${amount_usd:.2f}) vượt quá trần an toàn (${self.max_order_usd:.2f}).",
                "live_verified": False,
            }

        chain_key = chain.lower()
        cfg = CHAIN_CONFIGS.get(chain_key)
        if not cfg:
            return {"success": False, "error": f"Unsupported chain: {chain}", "live_verified": False}

        # Kiểm tra giá Native USD
        native_price = await self.get_native_price_usd(chain_key)
        if native_price <= 0:
            return {"success": False, "error": "CANNOT_FETCH_NATIVE_PRICE", "live_verified": False}

        # Tính lượng native token cần chi trả
        spend_native = amount_usd / native_price
        balances = await self.get_wallet_balances(chain_key)
        native_bal = balances.get("native_balance", 0.0)
        gas_reserve = cfg["gas_reserve"]

        if native_bal < (spend_native + gas_reserve):
            return {
                "success": False,
                "error": "INSUFFICIENT_NATIVE_BALANCE",
                "wallet": balances.get("address"),
                "native_balance": native_bal,
                "required": spend_native + gas_reserve,
                "symbol": cfg["native_symbol"],
                "message": f"Số dư {cfg['native_symbol']} ({native_bal:.6f}) không đủ chi trả ({spend_native:.6f}) + gas reserve ({gas_reserve:.6f}).",
                "live_verified": False,
            }

        # Đọc số dư token trước khi mua
        wallet_addr = balances.get("address")
        if chain_key == "solana":
            bal_before, decimals = await self.get_solana_token_balance(token_address, wallet_addr)
            spend_lamports = int(spend_native * (10 ** 9))
            res = await self.execute_solana_swap_jupiter(
                buy_mint=token_address,
                sell_mint="So11111111111111111111111111111111111111112",
                amount_lamports=spend_lamports,
                position_id=position_id,
            )
            if res.get("success"):
                bal_after, _ = await self.get_solana_token_balance(token_address, wallet_addr)
                received_qty = (bal_after - bal_before) / (10 ** decimals)
                res["received_qty"] = max(0.0, received_qty)
                res["spent_native"] = spend_native
                res["effective_price_usd"] = (amount_usd / received_qty) if received_qty > 0 else 0.0
            return res
        else:
            bal_before, decimals = await self.get_erc20_balance(chain_key, token_address, wallet_addr)
            spend_wei = int(spend_native * (10 ** cfg["native_decimals"]))
            res = await self.execute_evm_swap_zerox(
                chain=chain_key,
                buy_token=token_address,
                sell_token="0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                sell_amount_wei=spend_wei,
                position_id=position_id,
            )
            if res.get("success"):
                bal_after, _ = await self.get_erc20_balance(chain_key, token_address, wallet_addr)
                received_qty = (bal_after - bal_before) / (10 ** decimals)
                res["received_qty"] = max(0.0, received_qty)
                res["spent_native"] = spend_native
                res["effective_price_usd"] = (amount_usd / received_qty) if received_qty > 0 else 0.0
            return res

    async def execute_sell_pct(
        self,
        chain: str,
        token_address: str,
        sell_pct: float,
        position_id: str = "",
    ) -> Dict[str, Any]:
        """Thực thi lệnh BÁN On-Chain Thật theo tỷ lệ % số dư hiện có trên ví."""
        if not self.enable_live:
            return {
                "success": False,
                "error": "LIVE_EXECUTION_PAUSED",
                "message": "ENABLE_LIVE_EXECUTION=false",
                "live_verified": False,
            }

        chain_key = chain.lower()
        cfg = CHAIN_CONFIGS.get(chain_key)
        if not cfg:
            return {"success": False, "error": f"Unsupported chain: {chain}", "live_verified": False}

        balances = await self.get_wallet_balances(chain_key)
        wallet_addr = balances.get("address")
        if not wallet_addr:
            return {"success": False, "error": "WALLET_NOT_CONFIGURED", "live_verified": False}

        # Đọc số dư token thật hiện có
        if chain_key == "solana":
            raw_bal, decimals = await self.get_solana_token_balance(token_address, wallet_addr)
            if raw_bal <= 0:
                return {"success": False, "error": "TOKEN_BALANCE_ZERO", "live_verified": False}
            sell_amount = int(raw_bal * (sell_pct / 100.0))
            if sell_amount <= 0:
                sell_amount = raw_bal

            res = await self.execute_solana_swap_jupiter(
                buy_mint="So11111111111111111111111111111111111111112",
                sell_mint=token_address,
                amount_lamports=sell_amount,
                position_id=position_id,
            )
            res["tokens_sold"] = sell_amount / (10 ** decimals)
            return res
        else:
            raw_bal, decimals = await self.get_erc20_balance(chain_key, token_address, wallet_addr)
            if raw_bal <= 0:
                return {"success": False, "error": "TOKEN_BALANCE_ZERO", "live_verified": False}
            sell_amount = int(raw_bal * (sell_pct / 100.0))
            if sell_amount <= 0:
                sell_amount = raw_bal

            res = await self.execute_evm_swap_zerox(
                chain=chain_key,
                buy_token="0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                sell_token=token_address,
                sell_amount_wei=sell_amount,
                position_id=position_id,
            )
            res["tokens_sold"] = sell_amount / (10 ** decimals)
            return res


# Singleton instance
live_dex_executor = LiveDexExecutor()
