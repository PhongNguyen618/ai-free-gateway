---
title: Ai Free Gateway
emoji: 🚀
colorFrom: green
colorTo: green
sdk: gradio
app_file: app.py
pinned: false
license: mit
short_description: All in one agent hub
---

# AI Free Gateway 🛡️⚡

> **Local Zero-Trust OpenAI-Compatible Proxy & Free-Tier AI Harvester**  
> Tự động quét và định tuyến yêu cầu tới các API LLM miễn phí (OpenRouter verified $0, Groq, Gemini Free Tier, GitHub Models) kết hợp hệ thống ngăn chặn thất thoát dữ liệu cục bộ (Zero-Trust DLP), phê duyệt Human-in-the-Loop và hỗ trợ streaming SSE.

---

## 🌟 Tính Năng Nổi Bật

1. **Zero External Telemetry DLP**: Quét regex chuyên sâu kết hợp tính toán Shannon Entropy cục bộ 100% offline. Phát hiện và xử lý trên toàn bộ các kênh truyền (messages content, tool_calls arguments, function_call, tools schema & descriptions):
   - AWS Keys, OpenAI Keys, GitHub Tokens, Google API Keys, Slack Tokens, JWT.
   - Private Keys (RSA/EC/DSA/PGP), Passwords trong Connection String.
   - Thông tin định danh cá nhân (Email, Số điện thoại Việt Nam & Quốc tế, Số CCCD 12 số, Thẻ tín dụng kiểm tra chuẩn thuật toán Luhn).
   - Địa chỉ IP nội bộ (10.x.x.x, 172.16-31.x.x, 192.168.x.x, 127.0.0.1).
2. **Reversible Pseudonymization & JSON Tool Restoration**: Thay thế tự động các dữ liệu nhạy cảm bằng token định danh `<REDACTED_TYPE_N>` và khôi phục nguyên trạng khi nhận kết quả từ LLM (cả text content và tool call arguments). Tool arguments được phân tích cú pháp JSON đệ quy để đảm bảo ký tự nháy, gạch chéo ngược hay xuống dòng trong dữ liệu khôi phục không làm vỡ cấu trúc JSON upstream.
3. **SSE Streaming Support & Terminal Guarantees**: Khôi phục token trực tiếp theo thời gian thực ngay trên Server-Sent Events stream (`stream=True`) với bộ đệm chống đứt đoạn token (`StreamingRestorer`), hỗ trợ đa choice độc lập và incremental UTF-8 decoder. Bộ đệm tool argument phân tách độc lập theo `(choice_index, tool_index)`, xả toàn bộ dữ liệu trước `finish_reason` và trước `[DONE]`.
4. **Admission Control & Giới Hạn Tài Nguyên (N01)**: Gating kích thước request body (mặc định 2 MiB) ngay ở tầng ASGI trước khi phân tích JSON hay quét DLP. Điều phối tối đa 8 completions đồng thời và 32 HITL approvals chờ duyệt; giải phóng slot ngay khi chờ duyệt để tránh deadlock. Giới hạn response upstream (16 MiB) và timeout deadline tổng thể cho toàn bộ chuỗi fallback.
5. **Human-in-the-Loop (HITL) Gate & Disconnect Safety (N03)**: Hàng đợi bất đồng bộ với atomic SQLite state transitions cho phép phê duyệt hoặc từ chối trên Web Dashboard. Tự động dọn dẹp bộ nhớ và đánh dấu hủy (`cancelled`) khi client ngắt kết nối (disconnect) giữa chừng, bảo toàn quyết định đã commit của quản trị viên và không gọi upstream lãng phí.
6. **Bảo Vệ Origin/Host & Ngăn Chặn DNS Rebinding Trình Duyệt (N04)**: Kiểm tra nghiêm ngặt `Host` header theo allowlist loopback; đối chiếu Origin đầy đủ gồm scheme, host và port để chặn CSRF/DNS rebinding; xử lý an toàn các ký tự Unicode trong Authorization header.
7. **Single-Worker Instance Lifecycle & Kernel Lock (N05)**: Khóa file OS-level độc quyền (`msvcrt.locking` trên Windows, `fcntl.flock` trên Linux/macOS) bảo vệ database SQLite; ngăn chặn tiến trình thứ hai ghi đè PID hoặc corrupt DB. Script `stop.bat` đối chiếu PID, executable và project root trước khi dừng tiến trình.
8. **Retry Budget & Error Boundaries (N06)**: Giới hạn ngân sách fallback (`max_attempts=3`), phân tích `Retry-After` (cả số giây và định dạng HTTP date). Lỗi 401/403 loại bỏ ngay provider khỏi request hiện tại; lỗi 400/422 dừng ngay lập tức mà không retry mù quáng; kiểm tra key trước khi gửi mạng; bảo toàn tương tác tool calls/tool results theo khối nguyên tử khi prune lịch sử cho Groq.
9. **Fail-Closed DNS Pinning (N07)**: `PinnedNetworkBackend` chặn tuyệt đối kết nối tới các domain chưa được ghim IP công khai (`fail_closed=True`), bảo toàn hostname gốc cho TLS SNI.
10. **Trạng Thái Vận Hành Trung Thực & Tracing (N08)**: Endpoint `/health` kiểm tra liveness thuần túy và `/ready` kiểm tra trạng thái khởi tạo/catalog mà không tốn network call. Tự động gắn mã `X-Correlation-ID` trên mọi request và error payload.

---

## 📁 Cấu Trúc Dự Án

```text
ai-free-gateway/
├── config/
│   ├── settings.yaml            # Cấu hình providers, rate limits, whitelist domains, DLP blocklist
│   └── .env.example             # Template API keys (OPENROUTER_API_KEY, GROQ_API_KEY, ...)
├── src/
│   ├── __init__.py
│   ├── main.py                  # App factory create_app(), auth middleware, CORS, lifespan
│   ├── lifecycle.py             # OS-backed InstanceLock, process metadata, atomic PID tracking
│   ├── config_loader.py         # Pydantic models load & validate settings.yaml và .env
│   ├── storage/
│   │   └── db.py                # SQLite bất đồng bộ (aiosqlite): models_pool, quota_state, dlp_audit_logs, approvals
│   ├── security/
│   │   ├── sanitizer.py         # Regex + Shannon Entropy scanner, reversible pseudonymization
│   │   ├── approval_gate.py     # Hàng đợi bất đồng bộ Human-in-the-Loop approval (atomic state)
│   │   └── egress_guard.py      # Zero-Trust HTTP client & fail-closed DNS Pinning backend
│   ├── discovery/
│   │   └── harvester.py         # Polling OpenRouter $0 models & snapshot reconciliation
│   └── router/
│       ├── balancer.py          # Chọn free model tối ưu, atomic turn pruning, candidate chain
│       └── openai_proxy.py      # Endpoints chuẩn OpenAI: /v1/chat/completions & /v1/models
├── ui/
│   └── index.html               # 100% Offline Dark-Mode Dashboard (XSS-safe DOM, sessionStorage)
├── tests/                       # 105 automated unit & integration tests (100% passing offline)
├── requirements.txt             # Runtime dependencies
├── requirements-dev.txt         # Dev and test dependencies (pytest, pytest-asyncio, pytest-cov)
├── pytest.ini
├── start.bat                    # Script khởi chạy nhanh trên Windows
├── start_background.vbs         # Script chạy ngầm hoàn toàn không hiện cửa sổ console
├── stop.bat                     # Script dừng an toàn Gateway (xác thực process identity)
├── update.bat                   # 1-Click tự động cập nhật code mới từ GitHub
├── install_autostart.bat        # Cài đặt tự động chạy ngầm cùng Windows
└── uninstall_autostart.bat      # Gỡ bỏ tự động chạy cùng Windows
```

---

## 🚀 Hướng Dẫn Cài Đặt & Sử Dụng

### 1. Khởi chạy & Quản lý trên Windows
- **`start.bat`**: Khởi chạy Gateway trong cửa sổ dòng lệnh để theo dõi log trực tiếp. (Lưu ý: Nếu đóng cửa sổ này, Gateway sẽ tắt).
- **`start_background.vbs`**: Khởi chạy Gateway ở chế độ **chạy ngầm hoàn toàn** (không hiện bất kỳ cửa sổ console nào).
- **`install_autostart.bat`**: Đăng ký Gateway tự động chạy ngầm mỗi khi máy tính bật lên.
- **`stop.bat`**: Dừng an toàn Gateway đang chạy ngầm bằng cách kiểm tra PID và process identity.
- **`update.bat`**: Kéo code mới nhất từ GitHub, cập nhật thư viện và restart Gateway.

### 2. Cài đặt thủ công
Yêu cầu Python 3.11+.

```bash
# Tạo môi trường ảo
python -m venv .venv

# Kích hoạt môi trường (Windows PowerShell)
.\.venv\Scripts\Activate.ps1

# Cài đặt thư viện
pip install -r requirements.txt
```

### 3. Cấu hình API Keys (Tùy chọn)
Sao chép file `config/.env.example` thành `config/.env` và nhập các API key:
```bash
cp config/.env.example config/.env
```
Các key miễn phí khuyên dùng:
- `GROQ_API_KEY`: Miễn phí tốc độ cao từ [Groq Console](https://console.groq.com/).
- `OPENROUTER_API_KEY`: Miễn phí hàng chục model $0 từ [OpenRouter](https://openrouter.ai/).
- `GEMINI_API_KEY`: Miễn phí từ [Google AI Studio](https://aistudio.google.com/).

### 4. Endpoints & URLs
- **Web Dashboard**: `http://127.0.0.1:8000/`
- **OpenAI Base URL**: `http://127.0.0.1:8000/v1`
- **Liveness Probe**: `http://127.0.0.1:8000/health`
- **Readiness Probe**: `http://127.0.0.1:8000/ready`
- **Swagger Docs**: `http://127.0.0.1:8000/docs`

---

## 💾 Hướng Dẫn Sao Lưu SQLite Database (WAL-Safe)

Vì hệ thống sử dụng chế độ SQLite WAL (`PRAGMA journal_mode=WAL`), không nên copy trực tiếp file `gateway.db` khi server đang hoạt động. Sử dụng SQLite Online Backup API:

```python
import sqlite3

def backup_db(source_path="storage/gateway.db", backup_path="storage/gateway_backup.db"):
    src = sqlite3.connect(source_path)
    dest = sqlite3.connect(backup_path)
    try:
        src.backup(dest)
        print("Sao lưu database thành công!")
    finally:
        dest.close()
        src.close()
```

---

## 🧪 Chạy Kiểm Thử (Automated Tests)

Chạy toàn bộ 105 automated tests hoàn toàn offline:

```bash
.\.venv\Scripts\pytest -q
```

Kết quả: **105 passed in ~26s** (100% offline, không chạm DB thật, không gọi mạng ngoài, cô lập cấu hình và test database tạm).
