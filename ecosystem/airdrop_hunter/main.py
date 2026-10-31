"""Airdrop Hunter Agent - Main Entry Point.

Automated Airdrop Farming & AI Anti-Sybil System (Production V2.0).
Integrates directly with AI Free Gateway All-in-One Agent Hub.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
import time

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.config import FARMING_MODE, GATEWAY_URL, PROJECT_ID
from src.executor.reward_harvester import RewardHarvester
from src.hub_bridge import get_farming_components, start_hub_bridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("airdrop_main")

BANNER = r"""
========================================================================
           🚀 AIRDROP HUNTER AGENT V2.0 - PRODUCTION SYSTEM 🎯
   Săn Airdrop 0đ / Vốn Nhỏ - Giả Lập Người Thật - Nuôi Bot Meme
========================================================================
"""


def parse_args():
    parser = argparse.ArgumentParser(description="Airdrop Hunter Agent CLI V2.0")
    parser.add_argument("--scan", action="store_true", help="Quét và chấm điểm danh sách Airdrop tiềm năng (gồm DeFiLlama Live)")
    parser.add_argument("--farm", action="store_true", help="Chạy một chu kỳ cày nhiệm vụ & giữ chuỗi Active Days")
    parser.add_argument("--stats", action="store_true", help="Xem báo cáo tổng hợp ví, streak và doanh thu Airdrop")
    parser.add_argument("--route-rewards", action="store_true", help="Thu hoạch phần thưởng và chuyển 80% vốn sang smart-money-agent")
    parser.add_argument("--health", action="store_true", help="Kiểm tra RPC và sức khỏe on-chain dàn ví")
    parser.add_argument("--daemon", action="store_true", help="Chạy chế độ nền liên tục tự động lập lịch cày 24/7")
    return parser.parse_args()


def run_scan():
    db, radar, _, _, _ = get_farming_components()
    campaigns = radar.scan_and_evaluate(include_live_defillama=True)
    print("\n[🎯 KẾT QUẢ QUÉT AIRDROP TIỀM NĂNG (ALPHA RADAR V2.0)]")
    print(f"{'Điểm':<6} | {'Tên Dự Án':<42} | {'Loại':<14} | {'Chi Phí':<10}")
    print("-" * 80)
    for c in campaigns[:10]:
        print(f"{c['alpha_score']:<6.1f} | {c['name'][:40]:<42} | {c['category']:<14} | {c['cost_type']:<10}")
    print("-" * 80)


def run_farming():
    db, _, _, _, executor = get_farming_components()
    wallets = db.list_wallets()
    if not wallets:
        default_w = "0x71c6bf00a49be0834277b0292bfef32342ef0123"
        db.upsert_wallet(default_w)
        wallets = [db.get_wallet(default_w)]

    print(f"\n[⚡ BẮT ĐẦU CHU KỲ CÀY AIRDROP - CHẾ ĐỘ: {FARMING_MODE}]")
    for w in wallets:
        addr = w["address"]
        res = executor.run_daily_cycle_for_wallet(addr, respect_active_hours=False)
        print(f"Ví: {addr[:10]}...{addr[-6:]} | Persona: {res.get('persona')} | Điểm: +{res.get('points_earned')} pts | Chi phí: ${res.get('total_cost_usd'):.5f}")


def run_stats():
    db, _, _, _, _ = get_farming_components()
    summary = db.get_summary()
    print("\n[📊 THỐNG KÊ TỔNG QUAN AIRDROP HUNTER V2.0]")
    print(f"- Chế độ vận hành:             {FARMING_MODE}")
    print(f"- Số kèo đang theo dõi:        {summary['active_campaigns']}")
    print(f"- Tổng số ví trong dàn:        {summary['total_wallets']}")
    print(f"- Tổng số ngày active:         {summary['cumulative_active_days']} ngày")
    print(f"- Tổng điểm tích lũy:          {summary['total_farming_points']} pts")
    print(f"- Tổng gas đã chi tiêu:        ${summary['total_gas_spent_usd']:.4f}")
    print(f"- Số tác vụ thành công:        {summary['successful_actions_count']}")
    print(f"- Tổng Airdrop thu hoạch:      ${summary['total_airdrop_rewards_usd']:.2f}")
    print(f"- Đã bơm sang Bot Săn Meme:    ${summary['total_routed_to_meme_usd']:.2f}")


def run_route_rewards():
    db, _, _, _, _ = get_farming_components()
    harvester = RewardHarvester(db=db)
    res = harvester.route_capital_to_smart_money(profit_share_pct=0.80)
    print("\n[💸 KẾT QUẢ THU HOẠCH & CHUYỂN VỐN SANG SMART-MONEY-AGENT]")
    print(f"- Trạng thái:                 {res['status']}")
    print(f"- Số phần thưởng xử lý:       {res['processed_count']}")
    print(f"- Tổng giá trị thu hoạch:     ${res['total_value_usd']:.2f}")
    print(f"- Bơm sang Bot Săn Meme (80%): ${res['routed_to_meme_bot_usd']:.2f}")
    print(f"- Giữ lại phí Gas cày tiếp:   ${res['retained_gas_usd']:.2f}")


def run_health():
    db, _, persona_engine, rpc_client, _ = get_farming_components()
    gas_price, is_live_gas = rpc_client.get_gas_price_gwei("base")
    est_cost, _ = rpc_client.estimate_tx_cost_usd("base")
    print("\n[🩺 KIỂM TRA SỨC KHỎE ON-CHAIN & PHÍ GAS MẠNG LƯỚI]")
    print(f"- Base L2 Gas Price:  {gas_price:.4f} Gwei (RPC Live: {is_live_gas})")
    print(f"- Chi phí tx chuẩn:   ${est_cost:.5f}")
    wallets = db.list_wallets()
    for w in wallets:
        addr = w["address"]
        bal, live_bal = rpc_client.get_native_balance("base", addr)
        nonce, _ = rpc_client.get_transaction_count("base", addr)
        p = persona_engine.generate_persona_for_wallet(addr)
        print(f"Ví: {addr[:10]}... | Số dư: {bal} ETH | Nonce: {nonce} | Persona: {p['persona_name']} | Đang active: {persona_engine.is_active_now(p)}")


def run_daemon_loop():
    print("[*] Đang khởi chạy Autonomous Farming Scheduler (Vòng lặp tự hành 24/7)...")
    db, radar, persona_engine, _, executor = get_farming_components()
    harvester = RewardHarvester(db=db)

    # Seed initial wallets if empty
    stored_wallets = db.list_wallets()
    if not stored_wallets:
        init_wallets = [
            "0x71c6bf00a49be0834277b0292bfef32342ef0123",
            "0x89d24a6b4ccb1b6faa2625fe562bdd9a23260456",
        ]
        for w in init_wallets:
            p = persona_engine.generate_persona_for_wallet(w)
            db.upsert_wallet(w, chain_type="EVM", persona_id=p["archetype_id"], persona_details=p)
        print(f"[*] Đã khởi tạo {len(init_wallets)} ví cày mẫu ban đầu.")

    try:
        cycle_count = 0
        while True:
            cycle_count += 1
            print(f"\n--- [Chu kỳ tự hành #{cycle_count}] ---")
            # 1. Quét kèo
            campaigns = radar.scan_and_evaluate(include_live_defillama=True)
            if cycle_count == 1 and campaigns:
                top = campaigns[0]
                print(f"[*] Top Kèo Airdrop: {top['name']} ({top.get('alpha_score', 0)} pts)")

            # 2. Duyệt ví theo active window (chu kỳ 1 ép chạy để có dữ liệu on-chain ngay)
            wallets = db.list_wallets()
            for idx, w in enumerate(wallets):
                addr = w["address"]
                p = persona_engine.generate_persona_for_wallet(addr)
                # Cycle 1 runs at least 1 wallet to establish baseline immediately
                respect_hours = True if cycle_count > 1 else (idx > 0)
                if not respect_hours or persona_engine.is_active_now(p):
                    res = executor.run_daily_cycle_for_wallet(addr, respect_active_hours=respect_hours)
                    print(f"Ví {addr[:8]}... ({p['persona_name']}): Điểm +{res.get('points_earned', 0)}")

            # 3. Thu hoạch và chuyển vốn
            harvester.route_capital_to_smart_money(profit_share_pct=0.80)
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n[!] Dừng Airdrop Hunter Agent Daemon.")


def main():
    print(BANNER)
    args = parse_args()

    # Automatically start Agent Hub Bridge in background
    bridge_node = start_hub_bridge(background=True)
    if bridge_node:
        print(f"[*] Đã kích hoạt Agent Hub Bridge kết nối tới: {GATEWAY_URL}")
        print(f"[*] Đăng ký Node ID: '{PROJECT_ID}' trên Bảng Tin Chung Agent Hub.\n")
    else:
        print(f"[!] Chạy chế độ Standalone (Chưa phát hiện Gateway tại {GATEWAY_URL}).\n")

    if args.scan:
        run_scan()
    elif args.farm:
        run_farming()
    elif args.stats:
        run_stats()
    elif args.route_rewards:
        run_route_rewards()
    elif args.health:
        run_health()
    elif args.daemon:
        run_daemon_loop()
    else:
        # Default behavior: run scan + stats
        run_scan()
        run_stats()
        print("\n[💡 Mẹo]: Chạy 'python main.py --farm' để cày streak, '--route-rewards' để bơm vốn sang Meme bot, hoặc '--daemon' để tự hành 24/7.")


if __name__ == "__main__":
    main()
