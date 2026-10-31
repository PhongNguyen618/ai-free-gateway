"""Tiered Position Manager with Moonbag x50 Scaling-Out Engine.

Implements quant-grade 4-tier scaling-out for Meme & Alpha trading:
- Entry: Hard Stop-Loss (-18%)
- TP1 (+40% / 1.4x): Sell 35% -> Move Stop-Loss to Breakeven (+2%)
- TP2 (+100% / 2.0x): Sell 30% -> Move Trailing Stop to +40% (109% capital recovered -> Free Ride!)
- TP3 (+300% / 4.0x): Sell 15% -> Dynamic Trailing Stop (-35% from ATH)
- Moonbag (20% remaining): Ride to 10x -> 25x -> 50x+ with scaling exits.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("smart_money.position_manager")

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "storage" / "gateway.db"


@dataclass
class PositionState:
    position_id: str
    token_address: str
    token_symbol: str
    chain: str
    entry_price_usd: float
    current_price_usd: float
    highest_price_usd: float
    initial_quantity: float
    remaining_quantity: float
    remaining_pct: float  # 1.0 down to 0.20 (moonbag)
    invested_usd: float
    realized_usd: float  # USD cash harvested from scaling out
    stop_loss_price_usd: float
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    moonbag_10x_hit: bool = False
    moonbag_25x_hit: bool = False
    moonbag_50x_hit: bool = False
    status: str = "OPEN"  # OPEN, CLOSED, MOONBAG
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None
    close_reason: Optional[str] = None
    events_log: List[str] = field(default_factory=list)

    @property
    def current_pnl_pct(self) -> float:
        if self.entry_price_usd <= 0:
            return 0.0
        return ((self.current_price_usd - self.entry_price_usd) / self.entry_price_usd) * 100.0

    @property
    def current_multiple(self) -> float:
        if self.entry_price_usd <= 0:
            return 1.0
        return self.current_price_usd / self.entry_price_usd

    @property
    def unrealized_value_usd(self) -> float:
        return self.remaining_quantity * self.current_price_usd

    @property
    def total_return_usd(self) -> float:
        return (self.realized_usd + self.unrealized_value_usd) - self.invested_usd


class TieredPositionManager:
    """Manages open trading positions and executes 4-tier scaling-out and moonbag protection."""

    def __init__(self, db_path: Optional[Path] = None, hub_node: Optional[Any] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.hub_node = hub_node
        self._ensure_db()

    def _ensure_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS smart_money_positions (
                    position_id TEXT PRIMARY KEY,
                    token_address TEXT NOT NULL,
                    token_symbol TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    entry_price_usd REAL NOT NULL,
                    current_price_usd REAL NOT NULL,
                    highest_price_usd REAL NOT NULL,
                    initial_quantity REAL NOT NULL,
                    remaining_quantity REAL NOT NULL,
                    remaining_pct REAL NOT NULL,
                    invested_usd REAL NOT NULL,
                    realized_usd REAL NOT NULL,
                    stop_loss_price_usd REAL NOT NULL,
                    tp1_hit INTEGER DEFAULT 0,
                    tp2_hit INTEGER DEFAULT 0,
                    tp3_hit INTEGER DEFAULT 0,
                    moonbag_10x_hit INTEGER DEFAULT 0,
                    moonbag_25x_hit INTEGER DEFAULT 0,
                    moonbag_50x_hit INTEGER DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    closed_at REAL,
                    close_reason TEXT,
                    events_log TEXT
                )
                """
            )
            conn.commit()

    def open_position(
        self,
        token_address: str,
        token_symbol: str,
        chain: str,
        entry_price_usd: float,
        invested_usd: float,
        initial_quantity: Optional[float] = None,
    ) -> PositionState:
        """Opens a new position with hard stop-loss at -18%."""
        clean_addr = token_address.strip()
        clean_symbol = token_symbol.strip().upper()
        clean_chain = chain.strip().lower()

        if entry_price_usd <= 0:
            raise ValueError("Entry price must be positive")
        if invested_usd <= 0:
            raise ValueError("Invested amount must be positive")

        qty = initial_quantity if (initial_quantity and initial_quantity > 0) else (invested_usd / entry_price_usd)
        hard_sl = entry_price_usd * 0.82  # -18% hard stop loss

        pos_id = f"pos_{clean_chain}_{int(time.time())}_{clean_symbol}"
        pos = PositionState(
            position_id=pos_id,
            token_address=clean_addr,
            token_symbol=clean_symbol,
            chain=clean_chain,
            entry_price_usd=entry_price_usd,
            current_price_usd=entry_price_usd,
            highest_price_usd=entry_price_usd,
            initial_quantity=qty,
            remaining_quantity=qty,
            remaining_pct=1.0,
            invested_usd=invested_usd,
            realized_usd=0.0,
            stop_loss_price_usd=hard_sl,
            status="OPEN",
            events_log=[f"Vào lệnh mua ${invested_usd:.2f} ({qty:.4f} {clean_symbol}) tại giá ${entry_price_usd:.6f}. SL cứng -18%."],
        )
        self._save_position(pos)
        self._notify_event("smart_money.position_opened", {
            "position_id": pos.position_id,
            "symbol": pos.token_symbol,
            "chain": pos.chain,
            "entry_price": pos.entry_price_usd,
            "invested_usd": pos.invested_usd,
            "stop_loss": pos.stop_loss_price_usd,
        })
        return pos

    def update_price(
        self,
        position_id: str,
        current_price_usd: float,
        liquidity_usd: Optional[float] = None,
    ) -> Tuple[PositionState, List[str]]:
        """
        Updates the live price of a position and executes tiered TP/SL scaling-out logic.
        Returns the updated position and list of triggered action messages.
        """
        pos = self.get_position(position_id)
        if not pos or pos.status == "CLOSED":
            return pos, []

        triggered_actions: List[str] = []
        now_ts = time.time()
        pos.current_price_usd = current_price_usd
        if current_price_usd > pos.highest_price_usd:
            pos.highest_price_usd = current_price_usd
        pos.updated_at = now_ts

        mult = pos.current_multiple
        pnl = pos.current_pnl_pct

        # 0. Emergency Rug/Liquidity check
        if liquidity_usd is not None and liquidity_usd < 10000:
            msg = f"CẢNH BÁO RÚT THANH KHOẢN: Liquidity giảm còn ${liquidity_usd:.0f}! Thoát toàn bộ vị thế khẩn cấp."
            pos.status = "CLOSED"
            pos.closed_at = now_ts
            pos.close_reason = "EMERGENCY_RUG_PULL"
            pos.realized_usd += pos.remaining_quantity * current_price_usd
            pos.remaining_quantity = 0.0
            pos.remaining_pct = 0.0
            pos.events_log.append(msg)
            triggered_actions.append(msg)
            self._save_position(pos)
            self._notify_event("smart_money.emergency_exit", {"position_id": pos.position_id, "symbol": pos.token_symbol, "reason": msg})
            return pos, triggered_actions

        # 1. Check Stop-Loss
        if current_price_usd <= pos.stop_loss_price_usd and pos.status != "CLOSED":
            msg = f"CHẠM STOP-LOSS: Giá ${current_price_usd:.6f} chạm mức dừng lỗ ${pos.stop_loss_price_usd:.6f}. Đóng toàn bộ {pos.remaining_pct*100:.0f}% còn lại."
            pos.status = "CLOSED"
            pos.closed_at = now_ts
            pos.close_reason = "STOP_LOSS_HIT"
            pos.realized_usd += pos.remaining_quantity * current_price_usd
            pos.remaining_quantity = 0.0
            pos.remaining_pct = 0.0
            pos.events_log.append(msg)
            triggered_actions.append(msg)
            self._save_position(pos)
            self._notify_event("smart_money.stop_loss_hit", {
                "position_id": pos.position_id,
                "symbol": pos.token_symbol,
                "final_realized_usd": pos.realized_usd,
                "total_return_usd": pos.total_return_usd,
            })
            return pos, triggered_actions

        # 2. Tier 1 TP: +40% (1.4x) -> Sell 35% initial qty, move SL to breakeven (+2%)
        if mult >= 1.40 and not pos.tp1_hit and pos.remaining_pct > 0.65:
            sell_qty = pos.initial_quantity * 0.35
            sell_val = sell_qty * current_price_usd
            pos.realized_usd += sell_val
            pos.remaining_quantity -= sell_qty
            pos.remaining_pct = round(pos.remaining_quantity / pos.initial_quantity, 4)
            pos.tp1_hit = True
            # Move Stop-Loss to Breakeven (+2% to cover gas/slippage)
            pos.stop_loss_price_usd = pos.entry_price_usd * 1.02
            msg = f"🎯 [TP1 +40%] Bán 35% token ({sell_qty:.4f} {pos.token_symbol}) thu về ${sell_val:.2f}. Dời SL lên Hòa Vốn ($+{pos.stop_loss_price_usd:.6f}). Lệnh đã an toàn 100%!"
            pos.events_log.append(msg)
            triggered_actions.append(msg)
            self._notify_event("smart_money.partial_tp_hit", {
                "position_id": pos.position_id,
                "tier": "TP1",
                "symbol": pos.token_symbol,
                "sold_usd": sell_val,
                "new_stop_loss": pos.stop_loss_price_usd,
            })

        # 3. Tier 2 TP: +100% (2.0x / X2) -> Sell 30% initial qty, move trailing SL to +40%
        if mult >= 2.0 and not pos.tp2_hit and pos.remaining_pct > 0.35:
            sell_qty = pos.initial_quantity * 0.30
            sell_val = sell_qty * current_price_usd
            pos.realized_usd += sell_val
            pos.remaining_quantity -= sell_qty
            pos.remaining_pct = round(pos.remaining_quantity / pos.initial_quantity, 4)
            pos.tp2_hit = True
            # Move Trailing Stop up to +40%
            pos.stop_loss_price_usd = pos.entry_price_usd * 1.40
            msg = f"🔥 [TP2 X2 FREE RIDE] Bán tiếp 30% ({sell_qty:.4f} {pos.token_symbol}) thu về ${sell_val:.2f}. Tổng rút: ${pos.realized_usd:.2f} (Đạt {pos.realized_usd/pos.invested_usd*100:.0f}% vốn gốc). 35% còn lại là LÃI RÒNG 0 ĐỒNG!"
            pos.events_log.append(msg)
            triggered_actions.append(msg)
            self._notify_event("smart_money.partial_tp_hit", {
                "position_id": pos.position_id,
                "tier": "TP2_FREE_RIDE",
                "symbol": pos.token_symbol,
                "sold_usd": sell_val,
                "new_stop_loss": pos.stop_loss_price_usd,
            })

        # 4. Tier 3 TP: +300% (4.0x / X4) -> Sell 15% initial qty, activate dynamic trailing stop (-35% from ATH)
        if mult >= 4.0 and not pos.tp3_hit and pos.remaining_pct > 0.20:
            sell_qty = pos.initial_quantity * 0.15
            sell_val = sell_qty * current_price_usd
            pos.realized_usd += sell_val
            pos.remaining_quantity -= sell_qty
            pos.remaining_pct = round(pos.remaining_quantity / pos.initial_quantity, 4)
            pos.tp3_hit = True
            pos.status = "MOONBAG"  # Enter Moonbag status!
            # Set dynamic trailing stop at -35% from highest price
            pos.stop_loss_price_usd = pos.highest_price_usd * 0.65
            msg = f"🚀 [TP3 X4 THU HOẠCH] Bán tiếp 15% ({sell_qty:.4f} {pos.token_symbol}) thu về ${sell_val:.2f}. Đã mở chế độ MOONBAG (Giữ {pos.remaining_pct*100:.0f}% gồng x10 ➔ x50+). Trailing Stop: -35% từ đỉnh."
            pos.events_log.append(msg)
            triggered_actions.append(msg)
            self._notify_event("smart_money.partial_tp_hit", {
                "position_id": pos.position_id,
                "tier": "TP3_MOONBAG_ACTIVATED",
                "symbol": pos.token_symbol,
                "sold_usd": sell_val,
                "new_stop_loss": pos.stop_loss_price_usd,
            })

        # 5. Moonbag Stage 1: 10x (+900%) -> Sell 5% initial qty
        if mult >= 10.0 and not pos.moonbag_10x_hit and pos.remaining_quantity > 0:
            sell_qty = pos.initial_quantity * 0.05
            sell_val = sell_qty * current_price_usd
            pos.realized_usd += sell_val
            pos.remaining_quantity -= sell_qty
            pos.remaining_pct = round(pos.remaining_quantity / pos.initial_quantity, 4)
            pos.moonbag_10x_hit = True
            pos.stop_loss_price_usd = pos.highest_price_usd * 0.60
            msg = f"🌟 [MOONBAG 10X REACHED!] Đồng {pos.token_symbol} đã x10! Bán nhẹ 5% chốt thêm ${sell_val:.2f}. Còn giữ {pos.remaining_pct*100:.0f}% gồng tiếp lên 50x!"
            pos.events_log.append(msg)
            triggered_actions.append(msg)
            self._notify_event("smart_money.moonbag_active", {
                "position_id": pos.position_id,
                "multiple": "10x",
                "symbol": pos.token_symbol,
                "sold_usd": sell_val,
            })

        # 6. Moonbag Stage 2: 50x (+4900%) -> The Ultimate Moonbag Target!
        if mult >= 50.0 and not pos.moonbag_50x_hit and pos.remaining_quantity > 0:
            sell_qty = pos.remaining_quantity  # Cash in remaining moonbag at 50x!
            sell_val = sell_qty * current_price_usd
            pos.realized_usd += sell_val
            pos.remaining_quantity = 0.0
            pos.remaining_pct = 0.0
            pos.moonbag_50x_hit = True
            pos.status = "CLOSED"
            pos.closed_at = now_ts
            pos.close_reason = "MOONBAG_50X_JACKPOT"
            msg = f"👑 [MOONBAG 50X HOÀN TẤT!] KÈO SIÊU PHẨM {pos.token_symbol} ĐÃ CHẠM MỐC 50X! Chốt toàn bộ Moonbag còn lại, thu về ${sell_val:.2f}. Tổng lãi ròng: +${pos.total_return_usd:.2f} USD!"
            pos.events_log.append(msg)
            triggered_actions.append(msg)
            self._notify_event("smart_money.moonbag_active", {
                "position_id": pos.position_id,
                "multiple": "50x_JACKPOT",
                "symbol": pos.token_symbol,
                "sold_usd": sell_val,
                "total_return_usd": pos.total_return_usd,
            })

        # Dynamic Trailing Stop update if in TP3 / Moonbag and price makes higher high
        if (pos.tp3_hit or pos.status == "MOONBAG") and pos.status != "CLOSED":
            new_ts = pos.highest_price_usd * 0.65
            if new_ts > pos.stop_loss_price_usd:
                pos.stop_loss_price_usd = new_ts

        self._save_position(pos)
        return pos, triggered_actions

    def get_position(self, position_id: str) -> Optional[PositionState]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM smart_money_positions WHERE position_id = ?", (position_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_position(dict(row))

    def get_open_positions(self) -> List[PositionState]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM smart_money_positions WHERE status IN ('OPEN', 'MOONBAG') ORDER BY created_at DESC")
            return [self._row_to_position(dict(r)) for r in cur.fetchall()]

    def get_all_positions(self, limit: int = 50) -> List[PositionState]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM smart_money_positions ORDER BY created_at DESC LIMIT ?", (limit,))
            return [self._row_to_position(dict(r)) for r in cur.fetchall()]

    def close_position_manual(self, position_id: str, reason: str = "MANUAL_CLOSE") -> Optional[PositionState]:
        pos = self.get_position(position_id)
        if not pos or pos.status == "CLOSED":
            return pos

        now_ts = time.time()
        sell_val = pos.remaining_quantity * pos.current_price_usd
        pos.realized_usd += sell_val
        pos.remaining_quantity = 0.0
        pos.remaining_pct = 0.0
        pos.status = "CLOSED"
        pos.closed_at = now_ts
        pos.close_reason = reason
        msg = f"Đóng vị thế thủ công ({reason}) tại giá ${pos.current_price_usd:.6f}. Thu về thêm ${sell_val:.2f} USD."
        pos.events_log.append(msg)
        self._save_position(pos)
        self._notify_event("smart_money.position_closed", {
            "position_id": pos.position_id,
            "symbol": pos.token_symbol,
            "reason": reason,
            "total_return_usd": pos.total_return_usd,
        })
        return pos

    def sync_live_prices_from_dexscreener(self) -> List[Dict[str, Any]]:
        """
        Polls live prices from DexScreener for all open positions,
        updates state, and executes auto scaling-out.
        """
        open_pos = self.get_open_positions()
        if not open_pos:
            return []

        results = []
        for pos in open_pos:
            addr = pos.token_address
            url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "AIFreeGateway/1.0"})
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        pairs = data.get("pairs") or []
                        if pairs:
                            # Use top volume pair
                            top_pair = sorted(pairs, key=lambda x: x.get("volume", {}).get("h24", 0), reverse=True)[0]
                            price_usd = float(top_pair.get("priceUsd") or 0.0)
                            liq_usd = float(top_pair.get("liquidity", {}).get("usd") or 0.0)
                            if price_usd > 0:
                                updated_pos, actions = self.update_price(pos.position_id, price_usd, liquidity_usd=liq_usd)
                                results.append({
                                    "position_id": pos.position_id,
                                    "symbol": pos.token_symbol,
                                    "new_price": price_usd,
                                    "multiple": f"{updated_pos.current_multiple:.2f}x",
                                    "pnl_pct": f"{updated_pos.current_pnl_pct:+.1f}%",
                                    "status": updated_pos.status,
                                    "actions": actions,
                                })
            except Exception as e:
                logger.debug(f"DexScreener price sync failed for {pos.token_symbol}: {e}")

        return results

    def _save_position(self, p: PositionState) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO smart_money_positions (
                    position_id, token_address, token_symbol, chain,
                    entry_price_usd, current_price_usd, highest_price_usd,
                    initial_quantity, remaining_quantity, remaining_pct,
                    invested_usd, realized_usd, stop_loss_price_usd,
                    tp1_hit, tp2_hit, tp3_hit, moonbag_10x_hit, moonbag_25x_hit, moonbag_50x_hit,
                    status, created_at, updated_at, closed_at, close_reason, events_log
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p.position_id, p.token_address, p.token_symbol, p.chain,
                    p.entry_price_usd, p.current_price_usd, p.highest_price_usd,
                    p.initial_quantity, p.remaining_quantity, p.remaining_pct,
                    p.invested_usd, p.realized_usd, p.stop_loss_price_usd,
                    int(p.tp1_hit), int(p.tp2_hit), int(p.tp3_hit),
                    int(p.moonbag_10x_hit), int(p.moonbag_25x_hit), int(p.moonbag_50x_hit),
                    p.status, p.created_at, p.updated_at, p.closed_at, p.close_reason,
                    json.dumps(p.events_log, ensure_ascii=False),
                )
            )
            conn.commit()

    def _row_to_position(self, d: Dict[str, Any]) -> PositionState:
        ev_log = []
        if d.get("events_log"):
            try:
                ev_log = json.loads(d["events_log"])
            except Exception:
                ev_log = [d["events_log"]]

        return PositionState(
            position_id=d["position_id"],
            token_address=d["token_address"],
            token_symbol=d["token_symbol"],
            chain=d["chain"],
            entry_price_usd=float(d["entry_price_usd"]),
            current_price_usd=float(d["current_price_usd"]),
            highest_price_usd=float(d["highest_price_usd"]),
            initial_quantity=float(d["initial_quantity"]),
            remaining_quantity=float(d["remaining_quantity"]),
            remaining_pct=float(d["remaining_pct"]),
            invested_usd=float(d["invested_usd"]),
            realized_usd=float(d["realized_usd"]),
            stop_loss_price_usd=float(d["stop_loss_price_usd"]),
            tp1_hit=bool(d["tp1_hit"]),
            tp2_hit=bool(d["tp2_hit"]),
            tp3_hit=bool(d["tp3_hit"]),
            moonbag_10x_hit=bool(d["moonbag_10x_hit"]),
            moonbag_25x_hit=bool(d.get("moonbag_25x_hit", 0)),
            moonbag_50x_hit=bool(d["moonbag_50x_hit"]),
            status=d["status"],
            created_at=float(d["created_at"]),
            updated_at=float(d["updated_at"]),
            closed_at=float(d["closed_at"]) if d.get("closed_at") else None,
            close_reason=d.get("close_reason"),
            events_log=ev_log,
        )

    def _notify_event(self, topic: str, payload: Dict[str, Any]) -> None:
        if self.hub_node and hasattr(self.hub_node, "publish_event"):
            try:
                self.hub_node.publish_event(topic=topic, payload=payload, priority="high")
            except Exception as e:
                logger.debug(f"Position event notify failed: {e}")
