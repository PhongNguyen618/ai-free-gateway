"""Central Telegram Hub Notifier for AI Free Gateway All-in-One Hub.

Dispatches real-time alerts to the user's Telegram whenever:
- System restarts/deploys on Render
- High-alpha airdrops are discovered
- Daily farming cycles complete & earn points
- Airdrop profits are routed to the Smart Money meme bot
- Smart money / whale wallet trade signals occur
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("telegram_notifier")


class TelegramHubNotifier:
    """Manages Telegram alert dispatching with rate-limiting and formatting."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ):
        self.bot_token = (bot_token or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        self.chat_id = (chat_id or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
        self._lock = threading.Lock()
        self._last_sent_time: float = 0.0

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Sends a message to the configured Telegram chat."""
        if not self.is_configured:
            logger.debug("Telegram notification skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured.")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json", "User-Agent": "AIFreeGatewayTelegram/1.0"},
            method="POST",
        )

        with self._lock:
            # Enforce 1 second delay between messages to respect Telegram limits
            now = time.time()
            if now - self._last_sent_time < 1.0:
                time.sleep(1.0 - (now - self._last_sent_time))
            self._last_sent_time = time.time()

        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                if resp.status == 200:
                    logger.info("Telegram notification delivered successfully.")
                    return True
        except Exception as e:
            logger.warning(f"Telegram notification dispatch error: {e}")
            return False

        return False

    def handle_event(self, event: Dict[str, Any]) -> None:
        """Formats and broadcasts a SharedEventBus event to Telegram."""
        if not self.is_configured:
            return

        topic = event.get("topic", "").lower()
        payload = event.get("payload", {})
        source = event.get("source_project", "hub")

        msg = ""

        if topic == "system.startup":
            msg = (
                "🚀 <b>[AI Free Gateway] Hệ sinh thái đã khởi động!</b>\n\n"
                f"• Máy chủ: <code>{payload.get('server_url', 'Render Cloud')}</code>\n"
                f"• Chế độ: <b>{payload.get('mode', '24/7 Cloud')}</b>\n"
                "• Trạng thái: Cả 3 con bot (Gateway, Airdrop Hunter, Smart Money) đã sẵn sàng hoạt động!"
            )

        elif topic == "airdrop.alpha_opportunity":
            msg = (
                "🎯 <b>[Airdrop Hunter] Phát hiện Kèo Airdrop Mới!</b>\n\n"
                f"• Dự án: <b>{payload.get('top_campaign', 'N/A')}</b>\n"
                f"• Điểm Alpha: <b>{payload.get('alpha_score', 0)}/100</b>\n"
                f"• Chi phí: <b>{payload.get('cost_type', 'FREE')}</b>\n"
                f"• Tổng số kèo phát hiện: {payload.get('total_found', 1)}\n\n"
                "<i>Hệ thống đã tự động thêm vào lịch cày hàng ngày.</i>"
            )

        elif topic == "airdrop.cycle_completed":
            msg = (
                "⚡ <b>[Airdrop Hunter] Hoàn thành Chu kỳ Cày Nhiệm vụ!</b>\n\n"
                f"• Số ví đã cày: <b>{payload.get('wallets_farmed', 1)} ví</b>\n"
                f"• Điểm tích lũy mới: <b>+{payload.get('total_points_earned', 0)} pts</b>\n"
                f"• Phí gas đã dùng: <b>${payload.get('total_cost_usd', 0):.5f}</b>\n\n"
                "<i>Chuỗi ngày Active Days và Anti-Sybil Streak đã được bảo toàn.</i>"
            )

        elif topic == "airdrop.capital_routed":
            msg = (
                "💸 <b>[Thu Hoạch Airdrop] Đã Bơm Vốn Sang Bot Săn Meme!</b>\n\n"
                f"• Vốn mới trích sang: <b>+${payload.get('capital_added_usd', 0):.2f} USD</b> (80% lãi)\n"
                f"• Giữ lại phí gas dự phòng: <b>${payload.get('retained_gas_usd', 0):.2f} USD</b>\n"
                f"• Đích nhận: <b>{payload.get('target_agent', 'smart-money-agent')}</b>\n\n"
                "🔥 <i>Bot Smart Money đã tăng quy mô lệnh để săn meme token!</i>"
            )

        elif "smart_money" in topic or "signal" in topic or topic == "news.hot_token":
            token = payload.get("token") or payload.get("symbol") or "Hot Token"
            msg = (
                "🐋 <b>[Smart Money] Tín hiệu Ví Cá Mập Gom Hàng!</b>\n\n"
                f"• Token: <b>{token}</b>\n"
                f"• Hành động: <b>{payload.get('action', 'BUY')}</b>\n"
                f"• Tự tin: <b>{int(float(payload.get('confidence', 0.85))*100)}%</b>\n"
                f"• Chuỗi: <b>{payload.get('chain', 'Solana').upper()}</b>\n"
            )

        if msg:
            # Send in background thread to avoid blocking caller
            threading.Thread(target=self.send_message, args=(msg,), daemon=True).start()

    def send_summary_report(self, summary_data: Dict[str, Any]) -> bool:
        """Sends an on-demand comprehensive status report."""
        msg = (
            "📊 <b>[BÁO CÁO TỔNG QUAN HỆ SINH THÁI 3 DỰ ÁN]</b>\n\n"
            f"• <b>Trạng thái Gateway:</b> 🟢 ONLINE 24/7\n"
            f"• <b>Tổng số ví đang cày:</b> {summary_data.get('total_wallets', 0)} ví\n"
            f"• <b>Tổng chuỗi ngày active:</b> {summary_data.get('cumulative_active_days', 0)} ngày\n"
            f"• <b>Tổng điểm Airdrop:</b> {summary_data.get('total_farming_points', 0)} pts\n"
            f"• <b>Doanh thu Airdrop:</b> ${summary_data.get('total_airdrop_rewards_usd', 0):.2f}\n"
            f"• <b>Vốn đã bơm sang Bot Meme:</b> ${summary_data.get('total_routed_to_meme_usd', 0):.2f}\n"
            f"• <b>Phí gas mạng Base L2:</b> {summary_data.get('gas_price_gwei', 0.006):.4f} Gwei\n\n"
            "<i>Hệ thống tự động vận hành trên Render Cloud.</i>"
        )
        return self.send_message(msg)
