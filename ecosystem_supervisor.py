"""Multi-Process Ecosystem Supervisor for Hugging Face Spaces & Render Cloud 24/7.

Orchestrates, monitors, and keeps alive:
1. AI Free Gateway & Agent Hub (:PORT)
2. Airdrop Hunter Agent Daemon (Auto 0đ Streak & Anti-Sybil)
3. Smart Money Agent Hub Node (Meme Sniping & Whale Signals)
4. Anti-Sleep Keep-Alive Loop (Keeps Render Free tier awake 24/7)
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

# Setup encoding for cross-platform logs
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
PORT = int(os.getenv("PORT", "7860"))
GATEWAY_URL = f"http://127.0.0.1:{PORT}"
GATEWAY_KEY = os.getenv("GATEWAY_API_KEY", "sk-local-dev-token")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://aifreegateway.onrender.com").rstrip("/")

BANNER = rf"""
========================================================================
    🌟 ALL-IN-ONE AGENT ECOSYSTEM SUPERVISOR - CLOUD 24/7 🌟
    Port: {PORT} | Gateway: {GATEWAY_URL}
    1. AI Free Gateway & All-in-One Hub
    2. Airdrop Hunter Agent Daemon (Auto 0đ Streak & Anti-Sybil)
    3. Smart Money Agent Hub Node (Meme Sniping & Whale Signals)
    4. Anti-Sleep Keep-Alive (Ping every 8 min)
========================================================================
"""

processes: list[subprocess.Popen] = []


def signal_handler(sig, frame):
    print("\n[!] Shutting down all ecosystem processes gracefully...")
    for p in processes:
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    time.sleep(1)
    for p in processes:
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
    sys.exit(0)


def wait_for_gateway(timeout_sec: float = 20.0) -> bool:
    start_t = time.time()
    url = f"{GATEWAY_URL}/health"
    while time.time() - start_t < timeout_sec:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "EcosystemSupervisor/1.0"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def start_render_keep_alive():
    """Background thread that pings public URL every 8 minutes to prevent Render Free sleep."""
    def _ping_loop():
        # Wait 2 minutes after startup before first ping
        time.sleep(120)
        target_url = f"{RENDER_EXTERNAL_URL}/health"
        print(f"[*] Kích hoạt Anti-Sleep Keep-Alive loop tới: {target_url} (mỗi 480 giây)")
        while True:
            try:
                req = urllib.request.Request(target_url, headers={"User-Agent": "RenderKeepAlive/1.0"})
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    if resp.status == 200:
                        print(f"[{time.strftime('%H:%M:%S')}] 🟢 Anti-Sleep Ping OK -> {target_url}")
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] ⚠️ Anti-Sleep Ping notice: {e}")
            time.sleep(480)

    t = threading.Thread(target=_ping_loop, daemon=True, name="RenderKeepAliveThread")
    t.start()


def notify_system_startup():
    """Publishes system.startup event to Shared Event Bus so Telegram Notifier alerts user."""
    try:
        url = f"{GATEWAY_URL}/api/hub/events/publish"
        payload = {
            "topic": "system.startup",
            "source_project": "ecosystem_supervisor",
            "payload": {
                "server_url": RENDER_EXTERNAL_URL,
                "mode": "24/7 Cloud Autonomous",
                "timestamp": time.time(),
            },
            "priority": "high",
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {GATEWAY_KEY}",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5.0)
        print("[*] Đã phát sự kiện 'system.startup' lên Event Bus và Telegram Notifier.")
    except Exception as e:
        print(f"[!] Không thể phát system.startup: {e}")


def main():
    print(BANNER)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["GATEWAY_URL"] = GATEWAY_URL
    env["GATEWAY_HUB_URL"] = GATEWAY_URL
    env["GATEWAY_API_KEY"] = GATEWAY_KEY
    env["LLM_BASE_URL"] = f"{GATEWAY_URL}/v1"

    # 1. Start AI Free Gateway
    print("[1/3] Khởi động AI Free Gateway & Agent Hub...")
    cmd_gateway = [
        sys.executable,
        "-m",
        "uvicorn",
        "src.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(PORT),
    ]
    p_gateway = subprocess.Popen(cmd_gateway, cwd=str(BASE_DIR), env=env)
    processes.append(p_gateway)

    # Wait for gateway to become healthy
    print("[*] Đang chờ Gateway sẵn sàng tại port", PORT, "...")
    if wait_for_gateway(timeout_sec=25.0):
        print("    -> Gateway đã ONLINE 100%!")
        notify_system_startup()
    else:
        print("    -> Cảnh báo: Gateway khởi động chậm hơn dự kiến, tiếp tục chạy các bot vệ tinh...")

    # 2. Start Airdrop Hunter Agent Daemon
    airdrop_dir = BASE_DIR / "ecosystem" / "airdrop_hunter"
    if (airdrop_dir / "main.py").exists():
        print("[2/3] Khởi động Airdrop Hunter Agent (Tự động cày Airdrop 0đ & Anti-Sybil)...")
        airdrop_env = env.copy()
        airdrop_env["PYTHONPATH"] = str(airdrop_dir)
        cmd_airdrop = [sys.executable, str(airdrop_dir / "main.py"), "--daemon"]
        p_airdrop = subprocess.Popen(cmd_airdrop, cwd=str(airdrop_dir), env=airdrop_env)
        processes.append(p_airdrop)
    else:
        print("[!] Không tìm thấy airdrop_hunter trong ecosystem, bỏ qua.")

    # 3. Start Smart Money Agent Hub Node
    smart_dir = BASE_DIR / "ecosystem" / "smart_money"
    if (smart_dir / "hub_bridge.py").exists():
        print("[3/3] Khởi động Smart Money Agent (Săn Meme Token & Nhận Vốn Airdrop)...")
        smart_env = env.copy()
        smart_env["PYTHONPATH"] = str(smart_dir)
        cmd_smart = [sys.executable, str(smart_dir / "hub_bridge.py")]
        p_smart = subprocess.Popen(cmd_smart, cwd=str(smart_dir), env=smart_env)
        processes.append(p_smart)
    else:
        print("[!] Không tìm thấy smart_money trong ecosystem, bỏ qua.")

    # 4. Start Render Anti-Sleep Loop
    start_render_keep_alive()

    print("\n" + "=" * 72)
    print("🚀 TOÀN BỘ HỆ SINH THÁI 3 DỰ ÁN ĐANG CHẠY ĐỒNG THỜI 24/7!")
    print(f"👉 Truy cập Web Dashboard: {GATEWAY_URL}")
    print("=" * 72 + "\n")

    # Supervisor Health Monitor Loop
    try:
        while True:
            # Check gateway health
            if p_gateway.poll() is not None:
                print("[CRITICAL] AI Free Gateway stopped unexpectedly! Exiting supervisor.")
                break

            time.sleep(10)
    except KeyboardInterrupt:
        signal_handler(None, None)


if __name__ == "__main__":
    main()
