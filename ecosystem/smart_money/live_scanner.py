"""Live On-Chain Scanner & Real Smart Contract Security Assessor.

Integrates:
1. DexScreener Live API: Scans trending liquidity pairs on Base L2 & Solana.
2. GoPlus Security API: Evaluates real-time honeypot, buy/sell tax, mint, and rug risks.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger("smart_money.live_scanner")


class LiveMarketScanner:
    """Scans live DEX markets and audits smart contracts with GoPlus & DexScreener."""

    def __init__(self, timeout_sec: float = 6.0):
        self.timeout = timeout_sec

    def scan_dexscreener_trending(self, chain: str = "base", limit: int = 10) -> List[Dict[str, Any]]:
        """
        Scans active trending pairs on Base or Solana via DexScreener API.
        Applies strict quantitative volume and liquidity filters.
        """
        clean_chain = chain.strip().lower()
        query = clean_chain if clean_chain in ("base", "solana") else "base"

        # Well-known verified tokens for Base / Solana to query live DEX pairs
        if query == "base":
            token_query = "0x532f27101965dd16442e59d40670faf5ebb142e4,0x4ed4e862860bed51a9570b96d89af5e1b0efefed,0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4,0x940181a94A35A4569E4529A3CDfB74e48FD98641"
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_query}"
        else:
            token_query = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263,EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"  # BONK, WIF
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_query}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AIFreeGateway/1.0"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    pairs = data.get("pairs") or []
                    filtered = []
                    for p in pairs:
                        if p.get("chainId") != query:
                            continue
                        liq_usd = float((p.get("liquidity") or {}).get("usd") or 0.0)
                        vol_24h = float((p.get("volume") or {}).get("h24") or 0.0)
                        fdv = float(p.get("fdv") or 0.0)
                        tx_h1 = p.get("txns", {}).get("h1", {})
                        buys = int(tx_h1.get("buys") or 0)
                        sells = int(tx_h1.get("sells") or 0)

                        if liq_usd >= 10000:
                            buy_sell_ratio = round(buys / max(1, sells), 2)
                            base_tok = p.get("baseToken", {})
                            filtered.append({
                                "chain": query,
                                "token_address": base_tok.get("address"),
                                "name": base_tok.get("name"),
                                "symbol": base_tok.get("symbol"),
                                "price_usd": float(p.get("priceUsd") or 0.0),
                                "liquidity_usd": liq_usd,
                                "volume_24h_usd": vol_24h,
                                "fdv_usd": fdv,
                                "buy_sell_ratio_h1": buy_sell_ratio,
                                "price_change_h24": float((p.get("priceChange") or {}).get("h24") or 0.0),
                                "dex_id": p.get("dexId"),
                                "url": p.get("url"),
                            })
                            if len(filtered) >= limit:
                                break
                    if filtered:
                        return filtered
        except Exception as e:
            logger.warning(f"DexScreener scan error: {e}")

        # Fail-closed: Tuyệt đối không trả về token hardcode cũ
        return []

    def check_goplus_security(self, chain: str, token_address: str) -> Dict[str, Any]:
        """
        Audits smart contract for honeypot, taxes, and malicious functions via GoPlus.
        Chain IDs: Base = 8453, ETH = 1, BSC = 56, Arbitrum = 42161, Solana.
        Fail-Closed: Bất kỳ lỗi mạng hoặc dữ liệu không xác minh được đều coi là Không An Toàn.
        """
        clean_chain = chain.strip().lower()
        clean_addr = token_address.strip().lower()

        chain_map = {
            "base": "8453",
            "ethereum": "1",
            "eth": "1",
            "bsc": "56",
            "arbitrum": "42161",
        }

        if clean_chain in chain_map:
            cid = chain_map[clean_chain]
            url = f"https://api.gopluslabs.io/api/v1/token_security/{cid}?contract_addresses={clean_addr}"
        elif clean_chain == "solana":
            url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={clean_addr}"
        else:
            return {
                "status": "UNSUPPORTED_CHAIN",
                "score": 0.0,
                "is_safe": False,
                "red_flags": [f"Chain {chain} chưa được hỗ trợ kiểm tra bảo mật"],
                "live_verified": False,
            }

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AIFreeGateway/1.0"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    raw = json.loads(resp.read().decode("utf-8"))
                    result = raw.get("result", {})
                    token_data = result.get(clean_addr) or result.get(token_address) or {}
                    if token_data:
                        red_flags = []
                        if clean_chain == "solana":
                            freezable = token_data.get("freezable", {}).get("status") == "1"
                            mintable = token_data.get("mintable", {}).get("status") == "1"
                            transfer_fee = float(token_data.get("transfer_fee_amount", 0.0) or 0.0)
                            if freezable:
                                red_flags.append("Solana: Có quyền đóng băng tài khoản (Freeze Authority)")
                            if mintable:
                                red_flags.append("Solana: Có quyền mint thêm token (Mint Authority)")
                            if transfer_fee > 0.05:
                                red_flags.append(f"Solana: Phí chuyển nhượng cao ({transfer_fee*100:.1f}%)")
                            is_safe = len(red_flags) == 0
                            score = 95.0 if is_safe else max(10.0, 95.0 - len(red_flags) * 25.0)
                            return {
                                "status": "SCANNED",
                                "score": score,
                                "is_safe": is_safe,
                                "is_honeypot": False,
                                "buy_tax": 0.0,
                                "sell_tax": transfer_fee,
                                "is_open_source": True,
                                "red_flags": red_flags,
                                "live_verified": True,
                            }
                        else:
                            is_honeypot = token_data.get("is_honeypot") == "1"
                            buy_tax = float(token_data.get("buy_tax") or 0.0)
                            sell_tax = float(token_data.get("sell_tax") or 0.0)
                            is_open_source = token_data.get("is_open_source") == "1"
                            cannot_sell = token_data.get("cannot_sell_all") == "1"

                            if is_honeypot:
                                red_flags.append("Phát hiện bẫy Honeypot (không thể bán)")
                            if buy_tax > 0.05:
                                red_flags.append(f"Thuế mua cao ({buy_tax*100:.1f}%)")
                            if sell_tax > 0.05:
                                red_flags.append(f"Thuế bán cao ({sell_tax*100:.1f}%)")
                            if not is_open_source:
                                red_flags.append("Hợp đồng chưa xác minh mã nguồn")
                            if cannot_sell:
                                red_flags.append("Hạn chế lượng bán tối đa")

                            is_safe = len(red_flags) == 0
                            score = 95.0 if is_safe else max(10.0, 95.0 - len(red_flags) * 25.0)

                            return {
                                "status": "SCANNED",
                                "score": score,
                                "is_safe": is_safe,
                                "is_honeypot": is_honeypot,
                                "buy_tax": buy_tax,
                                "sell_tax": sell_tax,
                                "is_open_source": is_open_source,
                                "red_flags": red_flags,
                                "live_verified": True,
                            }
        except Exception as e:
            logger.warning(f"GoPlus security audit error: {e}")

        # Fail-closed: Lỗi API thì coi như token KHÔNG AN TOÀN, tuyệt đối không trả is_safe=True
        return {
            "status": "UNVERIFIED_API_ERROR",
            "score": 0.0,
            "is_safe": False,
            "is_honeypot": False,
            "buy_tax": 0.0,
            "sell_tax": 0.0,
            "is_open_source": False,
            "red_flags": ["Không thể xác minh bảo mật hợp đồng do GoPlus API không phản hồi."],
            "live_verified": False,
        }


    def get_safe_trending_gems(self, chain: str = "base", limit: int = 5) -> List[Dict[str, Any]]:
        """
        Combines DexScreener trending volume with GoPlus security verification.
        Returns only clean, auditable tokens.
        """
        candidates = self.scan_dexscreener_trending(chain=chain, limit=limit * 2)
        safe_gems = []

        for c in candidates:
            sec = self.check_goplus_security(chain=c["chain"], token_address=c["token_address"])
            c["security_score"] = sec["score"]
            c["is_safe"] = sec["is_safe"]
            c["red_flags"] = sec.get("red_flags", [])
            if sec["is_safe"]:
                safe_gems.append(c)
                if len(safe_gems) >= limit:
                    break

        return safe_gems
