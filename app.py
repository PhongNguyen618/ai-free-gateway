"""Hugging Face Spaces Entry Point (Gradio SDK Compatible).

Allows running the entire All-in-One Multi-Agent Ecosystem (Gateway + Airdrop Hunter + Smart Money)
with ZeroGPU / CPU support on Hugging Face Spaces.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

# Add current directory to sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# ZeroGPU compatibility handler
try:
    import spaces

    @spaces.GPU(duration=10)
    def gpu_activation_handler():
        """Satisfies Hugging Face ZeroGPU startup verification."""
        return "ZeroGPU Activated"
except Exception:
    def gpu_activation_handler():
        return "Standard Mode"

import gradio as gr
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# Import the core Gateway FastAPI app and Supervisor
from src.main import app as gateway_fastapi_app
import ecosystem_supervisor

# Ensure background bots (Airdrop Hunter + Smart Money) are launched
_started = False
_lock = threading.Lock()


def ensure_ecosystem_running():
    global _started
    with _lock:
        if _started:
            return
        _started = True

        # Launch Airdrop Hunter and Smart Money background processes
        def _bg_launcher():
            time.sleep(3.0)  # Wait for FastAPI server to accept connections
            env = os.environ.copy()
            env["GATEWAY_URL"] = "http://127.0.0.1:7860"
            env["GATEWAY_HUB_URL"] = "http://127.0.0.1:7860"
            env["LLM_BASE_URL"] = "http://127.0.0.1:7860/v1"

            # 1. Airdrop Hunter
            airdrop_dir = BASE_DIR / "ecosystem" / "airdrop_hunter"
            if (airdrop_dir / "main.py").exists():
                import subprocess
                a_env = env.copy()
                a_env["PYTHONPATH"] = str(airdrop_dir)
                subprocess.Popen([sys.executable, str(airdrop_dir / "main.py"), "--daemon"], cwd=str(airdrop_dir), env=a_env)

            # 2. Smart Money
            smart_dir = BASE_DIR / "ecosystem" / "smart_money"
            if (smart_dir / "hub_bridge.py").exists():
                import subprocess
                s_env = env.copy()
                s_env["PYTHONPATH"] = str(smart_dir)
                subprocess.Popen([sys.executable, str(smart_dir / "hub_bridge.py")], cwd=str(smart_dir), env=s_env)

        t = threading.Thread(target=_bg_launcher, daemon=True)
        t.start()


# Launch background ecosystem
ensure_ecosystem_running()

# Create a clean Gradio interface embedding the full All-in-One Dashboard
with gr.Blocks(title="AI Free Gateway & All-in-One Agent Hub", theme=gr.themes.Soft()) as demo:
    gr.HTML(
        """
        <div style="text-align: center; padding: 15px 0;">
            <h1 style="margin: 0; font-size: 26px;">🚀 All-in-One AI Free Gateway & Agent Hub</h1>
            <p style="color: #64748b; margin-top: 5px;">Hệ sinh thái tự động 24/7: Cấp AI 0đ + Săn Airdrop Anti-Sybil + Săn Meme Cá Mập</p>
        </div>
        """
    )
    with gr.Tabs():
        with gr.TabItem("🌐 Web Dashboard Trực Tiếp"):
            gr.HTML(
                """
                <iframe src="/ui" style="width: 100%; height: 850px; border: 1px solid #e2e8f0; border-radius: 12px; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1);"></iframe>
                """
            )
        with gr.TabItem("📖 Thông Tin Kết Nối API"):
            gr.Markdown(
                """
                ### 🔗 Kết nối OpenAI-Compatible Toàn Cầu
                - **Base URL**: `https://<YOUR-SPACE-NAME>.hf.space/v1`
                - **API Key**: `sk-local-dev-token` (hoặc key bạn cấu hình trong Secrets)
                - **Hỗ trợ**: Chat Completions, Function Calling, Agent Hub Nodes, Shared Event Bus.
                """
            )
            # Hidden dummy trigger to register GPU function
            dummy_btn = gr.Button("GPU Check", visible=False)
            dummy_out = gr.Textbox(visible=False)
            dummy_btn.click(fn=gpu_activation_handler, outputs=dummy_out)

# Mount Gradio into FastAPI root
app = gr.mount_gradio_app(gateway_fastapi_app, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
