# AI Free Gateway 🛡️⚡

> **Local Zero-Trust OpenAI-Compatible Proxy & Free-Tier AI Harvester**  
> Tự động quét và định tuyến yêu cầu tới các API LLM miễn phí (OpenRouter `:free`, Groq, Gemini Free, GitHub Models) kết hợp hệ thống ngăn chặn thất thoát dữ liệu cục bộ (Zero-Trust DLP), phê duyệt Human-in-the-Loop và hỗ trợ streaming SSE.

---

## 🌟 Tính Năng Nổi Bật

1. **Zero External Telemetry DLP**: Quét regex chuyên sâu kết hợp tính toán Shannon Entropy cục bộ 100% offline. Phát hiện và xử lý:
   - AWS Keys, OpenAI Keys, GitHub Tokens, Google API Keys, Slack Tokens, JWT.
   - Private Keys (RSA/EC/DSA/PGP), Passwords trong Connection String.
   - Thông tin định danh cá nhân (Email, Số điện thoại Việt Nam & Quốc tế, Số CCCD 12 số, Thẻ tín dụng kiểm tra chuẩn thuật toán Luhn).
   - Địa chỉ IP nội bộ (10.x.x.x, 172.16-31.x.x, 192.168.x.x, 127.0.0.1).
2. **Reversible Pseudonymization**: Thay thế tự động các dữ liệu nhạy cảm bằng token định danh `<REDACTED_TYPE_N>` và khôi phục nguyên trạng khi nhận kết quả từ LLM.
3. **SSE Streaming Support**: Khôi phục token trực tiếp theo thời gian thực ngay trên Server-Sent Events stream (`stream=True`) với bộ đệm chống đứt đoạn token (`StreamingRestorer`).
4. **Human-in-the-Loop (HITL) Gate**: Hàng đợi bất đồng bộ với `asyncio.Event` cho phép quản trị viên phê duyệt hoặc từ chối các request chứa dữ liệu rủi ro trung bình/cao ngay trên Web UI Dashboard.
5. **Egress Guard**: Chặn tuyệt đối các domain ngoài whitelist, ngăn chặn HTTP redirect SSRF (`follow_redirects=False`) và kiểm soát dung lượng outbound.
6. **Free-Tier Harvester**: Tự động tìm kiếm các model $0 trên OpenRouter và quản lý model pool từ Groq, Gemini, GitHub Models.
7. **Smart Fallback Router**: Tự động chuyển vùng provider khi gặp lỗi 429 Rate Limit hoặc lỗi 5xx mà không làm gián đoạn ứng dụng người dùng.
8. **Modern Dark Dashboard**: Giao diện TailwindCSS quản lý model pool, quota cooldowns, DLP audit logs và duyệt request pending.

---

## 📁 Cấu Trúc Dự Án

```text
ai-free-gateway/
├── config/
│   ├── settings.yaml            # Cấu hình providers, rate limits, whitelist domains, DLP blocklist
│   └── .env.example             # Template API keys (OPENROUTER_API_KEY, GROQ_API_KEY, ...)
├── src/
│   ├── __init__.py
│   ├── main.py                  # Khởi tạo FastAPI app, CORS, mount UI static, background tasks
│   ├── config_loader.py         # Pydantic models load & validate settings.yaml và .env
│   ├── storage/
│   │   └── db.py                # SQLite bất đồng bộ (aiosqlite): models_pool, quota_state, dlp_audit_logs
│   ├── security/
│   │   ├── sanitizer.py         # Regex + Shannon Entropy scanner, reversible pseudonymization (<ENTITY_ID>)
│   │   ├── approval_gate.py     # Hàng đợi bất đồng bộ Human-in-the-Loop approval
│   │   └── egress_guard.py      # httpx wrapper chặn domain ngoài whitelist & log byte size
│   ├── discovery/
│   │   └── harvester.py         # Polling OpenRouter $0 models & health check quota
│   └── router/
│       ├── balancer.py          # Chọn free model tối ưu theo task, latency và 429 cooldown
│       └── openai_proxy.py      # Endpoints chuẩn OpenAI: /v1/chat/completions & /v1/models
├── ui/
│   └── index.html               # Clean Dark-Mode Dashboard (Quotas, Audit Logs, Approvals)
├── tests/                       # Bộ kiểm thử tự động pytest
├── requirements.txt
└── pytest.ini
```

---

## 🚀 Hướng Dẫn Cài Đặt & Sử Dụng

### 1. Cài đặt môi trường
Yêu cầu Python 3.11+.

```bash
# Tạo môi trường ảo
python -m venv .venv

# Kích hoạt môi trường (Windows PowerShell)
.\.venv\Scripts\Activate.ps1

# Cài đặt thư viện phụ thuộc
pip install -r requirements.txt
```

### 2. Cấu hình API Keys (Tùy chọn)
Sao chép file `.env.example` thành `.env` và nhập các API key miễn phí bạn sở hữu:
```bash
cp config/.env.example config/.env
```
*(Nếu chưa có API Key, hệ thống vẫn khởi động bình thường với các model được cấu hình sẵn trong pool).*

### 3. Khởi chạy Gateway Server
```bash
python -m src.main
```
Hoặc dùng Uvicorn:
```bash
uvicorn src.main:app --host 127.0.0.1 --port 8000 --reload
```

- **Web Dashboard**: Truy cập `http://127.0.0.1:8000/` hoặc `http://127.0.0.1:8000/ui`
- **OpenAI Endpoint Base URL**: `http://127.0.0.1:8000/v1`

---

## 🧪 Ví Dụ Sử Dụng Với OpenAI Client (Python / Curl)

### Gửi yêu cầu qua cURL
```bash
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [
      {"role": "user", "content": "Viết cho tôi một hàm Python tính Fibonacci"}
    ],
    "stream": false
  }'
```

### Sử dụng với thư viện `openai` trong Python
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="not-needed"  # Hoặc điền master key nếu cấu hình gateway_api_key
)

response = client.chat.completions.create(
    model="auto",  # hoặc chỉ định model cụ thể: "llama-3.3-70b-versatile"
    messages=[
        {"role": "user", "content": "Server nội bộ của tôi là 192.168.1.50, hãy viết script ping kiểm tra."}
    ]
)

print(response.choices[0].message.content)
```

---

## 🛡️ Cơ Chế Bảo Vệ Zero-Trust DLP

- Nếu bạn gửi prompt có chứa **API Key** (ví dụ `AKIAIOSFODNN7EXAMPLE`) hoặc **Private Key**:
  - Request lập tức bị chặn với mã lỗi `403 Forbidden` (`dlp_violation`).
  - Ghi nhận chi tiết vào **DLP Audit Logs** cục bộ.
- Nếu bạn gửi prompt chứa thông tin nhạy cảm mức trung bình (Email, IP nội bộ, CCCD):
  - Request chuyển sang trạng thái chờ duyệt **Pending Approval**.
  - Mở Web Dashboard tại `http://127.0.0.1:8000/` để xem chi tiết và bấm **Approve** hoặc **Reject**.

---

## 🧪 Chạy Kiểm Thử (Automated Tests)

```bash
.\.venv\Scripts\pytest -v
```
Toàn bộ 11/11 bài test (Entropy Scanner, Luhn Checksum, Reversible Pseudonymization, Streaming Restorer, Egress Guard, Storage DB, Approval Gate, API Endpoints) đều vượt qua thành công!
