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
2. **Reversible Pseudonymization**: Thay thế tự động các dữ liệu nhạy cảm bằng token định danh `<REDACTED_TYPE_N>` và khôi phục nguyên trạng khi nhận kết quả từ LLM (cả text content và tool call arguments).
3. **SSE Streaming Support**: Khôi phục token trực tiếp theo thời gian thực ngay trên Server-Sent Events stream (`stream=True`) với bộ đệm chống đứt đoạn token (`StreamingRestorer`), hỗ trợ đa choice độc lập và incremental UTF-8 decoder không làm vỡ ký tự tiếng Việt / emoji.
4. **Human-in-the-Loop (HITL) Gate**: Hàng đợi bất đồng bộ với atomic SQLite state transitions cho phép quản trị viên phê duyệt hoặc từ chối các request chứa dữ liệu nhạy cảm ngay trên Web UI Dashboard.
5. **Egress Guard**: Chặn tuyệt đối các domain ngoài whitelist, kiểm tra DNS bất đồng bộ chống SSRF/private IP, đo chính xác kích thước payload trước khi gửi cho cả JSON và Stream, ngăn chặn HTTP redirect SSRF (`follow_redirects=False`) và vô hiệu hóa proxy môi trường (`trust_env=False`).
6. **Strict Free-Tier Harvester**: Chỉ tuyển chọn các model có giá prompt và completion được xác minh nghiêm ngặt bằng $0 trên OpenRouter; tự động loại bỏ các model tăng giá hoặc biến mất qua snapshot reconciliation.
7. **Smart Fallback Router**: Tự động chuyển vùng provider khi gặp lỗi 429 Rate Limit; skip ngay toàn bộ các model còn lại của provider đang bị cooldown trong cùng một request.
8. **100% Offline Modern Dark Dashboard**: Giao diện thuần túy không dùng CDN ngoài, bảo mật XSS tuyệt đối qua DOM APIs, hỗ trợ lưu Gateway API Key an toàn trong `sessionStorage`.

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
│   ├── config_loader.py         # Pydantic models load & validate settings.yaml và .env
│   ├── storage/
│   │   └── db.py                # SQLite bất đồng bộ (aiosqlite): models_pool, quota_state, dlp_audit_logs
│   ├── security/
│   │   ├── sanitizer.py         # Regex + Shannon Entropy scanner, reversible pseudonymization
│   │   ├── approval_gate.py     # Hàng đợi bất đồng bộ Human-in-the-Loop approval (atomic state)
│   │   └── egress_guard.py      # Zero-Trust HTTP client & async DNS SSRF filter
│   ├── discovery/
│   │   └── harvester.py         # Polling OpenRouter $0 models & snapshot reconciliation
│   └── router/
│       ├── balancer.py          # Chọn free model tối ưu, dedup alias (provider, model_name)
│       └── openai_proxy.py      # Endpoints chuẩn OpenAI: /v1/chat/completions & /v1/models
├── ui/
│   └── index.html               # 100% Offline Dark-Mode Dashboard (XSS-safe DOM, sessionStorage)
├── tests/                       # 47 automated tests (isolation, app factory, regression suite)
├── requirements.txt
├── pytest.ini
├── start.bat                    # Script khởi chạy nhanh trên Windows
├── stop.bat                     # Script dừng an toàn Gateway
├── update.bat                   # 1-Click tự động cập nhật code mới từ GitHub
├── install_autostart.bat        # Cài đặt tự động chạy ngầm cùng Windows
└── uninstall_autostart.bat      # Gỡ bỏ tự động chạy cùng Windows
```

---

## 🚀 Hướng Dẫn Cài Đặt & Sử Dụng

### 1. Khởi chạy & Quản lý 1-Click trên Windows
- **`start.bat`**: Nhấp đúp để tự động kiểm tra Python, tạo `.venv`, cài đặt `requirements.txt` nếu thiếu và chạy server.
- **`stop.bat`**: Nhấp đúp để dừng an toàn gateway dựa trên PID ghi trong `.gateway.pid` (không kill nhầm các ứng dụng khác).
- **`update.bat`**: Nhấp đúp để tự động kéo code mới nhất từ GitHub (`git pull`), cập nhật thư viện và khởi động lại Gateway.
- **`install_autostart.bat`**: Tự động cấu hình Gateway chạy ngầm mỗi khi mở máy tính Windows.
- **`uninstall_autostart.bat`**: Hủy cấu hình tự khởi động cùng Windows.

### 2. Cài đặt thủ công
Yêu cầu Python 3.11+.

```bash
# Tạo môi trường ảo
python -m venv .venv

# Kích hoạt môi trường (Windows PowerShell)
.\.venv\Scripts\Activate.ps1

# Cài đặt thư viện phụ thuộc
pip install -r requirements.txt
```

### 3. Cấu hình API Keys (Tùy chọn)
Sao chép file `.env.example` thành `.env` và nhập các API key bạn sở hữu:
```bash
cp config/.env.example config/.env
```
*(Nếu chưa có API Key, hệ thống vẫn khởi động bình thường với các model miễn phí được cấu hình sẵn trong pool).*

### 4. Khởi chạy Gateway Server
```bash
python -m src.main
```
- **Web Dashboard**: `http://127.0.0.1:8000/` hoặc `http://127.0.0.1:8000/ui`
- **OpenAI Base URL**: `http://127.0.0.1:8000/v1`

---

## 🔒 Quản Trị Bảo Mật & Giới Hạn Kỹ Thuật

1. **Dual Projection DLP & Audit Isolation**:
   - Tách biệt hoàn toàn hai hình chiếu dữ liệu: `outbound_payload` (tuân thủ cấu hình policy allow / redact / approve) và `audit_payload` (luôn luôn redact 100% các dữ liệu nhạy cảm).
   - Kể cả khi cấu hình `medium=allow`, địa chỉ email hoặc IP nội bộ thật sự được phép gửi ra upstream nhưng SQLite `dlp_audit_logs` và hàng đợi `pending_approvals` CHỈ lưu trữ token `<REDACTED_...>` an toàn, ngăn chặn triệt để rò rỉ dữ liệu nhạy cảm vào database nội bộ.
2. **DNS Pinning & Socket Address Pinning**:
   - Sử dụng `PinnedNetworkBackend` tích hợp trong `AsyncConnectionPool` để kết nối TCP trực tiếp tới địa chỉ IP công khai đã được xác minh.
   - Giữ nguyên hostname gốc cho TLS SNI (`server_hostname`) và HTTP `Host` header nhằm đảm bảo kiểm tra chứng chỉ hợp lệ và triệt tiêu nguy cơ Time-of-Check to Time-of-Use (TOCTOU) DNS Rebinding.
3. **Context Window Budgeting & Routing**:
   - Dự toán token chi tiết dựa trên toàn bộ messages, tool definitions, tool call arguments và output reserve (`max_tokens`/`max_completion_tokens`).
   - Yêu cầu model tường minh không tồn tại trong pool sẽ trả về mã `HTTP 404 model_not_found`; nếu model có trong pool nhưng không đủ context length sẽ trả về `HTTP 400 context_length_exceeded`.
4. **Bảo toàn Giao thức Tool Calling**:
   - Giữ nguyên vẹn định danh giao thức: `tool_call_id`, `tool_calls[].id`, `type`, `name`.
   - Parse và phục hồi an toàn chuỗi arguments JSON chứa escaped quotes, backslashes và newlines.
5. **Zero Upstream Error Leakage**:
   - Toàn bộ lỗi từ upstream (HTTP 4xx, 5xx) được chuẩn hóa nội bộ theo mã status và tên provider.
   - Tuyệt đối không lưu trữ hoặc trả về `resp.text` thô của upstream cho client, log hoặc dashboard nhằm ngăn chặn lộ API key hay dữ liệu nội bộ.
6. **Gateway Master Key**:
   - Khi cấu hình `gateway_api_key`, mọi API proxy `/v1/*` và dashboard `/api/*` đều yêu cầu xác thực bằng constant-time comparison.
   - Khi bind gateway ra host ngoài loopback (không phải `127.0.0.1`), bắt buộc phải có `gateway_api_key` hoặc cờ `allow_insecure_external: true`.
7. **Giới Hạn Kỹ Thuật**:
   - Zero-Trust DLP hiện tại tập trung bảo vệ văn bản (Text Modality). Các định dạng nhị phân chưa hỗ trợ quét bảo mật (`image_url`, `input_audio`, `file`) sẽ bị từ chối rõ ràng bằng mã lỗi `HTTP 400 Bad Request`.
   - Hệ thống được thiết kế tối ưu cho môi trường Single-Worker để duy trì trạng thái approval atomic và tránh race condition.

---

## 🧪 Chạy Kiểm Thử (Automated Tests)

Chạy toàn bộ 60 automated tests hoàn toàn offline:

```bash
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

Kết quả: **60 passed in ~25s** (100% offline, không chạm DB thật, không gọi mạng ngoài, cô lập cấu hình và test database tạm).
