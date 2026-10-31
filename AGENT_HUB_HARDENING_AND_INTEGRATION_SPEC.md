# ĐẶC TẢ NÂNG CẤP & HOÀN THIỆN THỰC CHIẾN: AGENT HUB V2.1 (S01 – S05)

**Ngày lập:** 2026-10-31  
**Mục tiêu:** Khắc phục các điểm nghẽn kỹ thuật sau khi rà soát sâu (Deep Audit) trên `ai-free-gateway` và kết nối thực chiến trực tiếp với dự án `smart-money-agent`.

---

## 1. Kết quả Rà soát Hệ thống (Audit Findings)

Qua kiểm tra mã nguồn hiện tại của [`src/main.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/main.py), [`src/hub/orchestrator.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/hub/orchestrator.py) và dự án `smart-money-agent`, có **5 điểm cần fix và nâng cấp** để hệ thống chạy thực tế bền bỉ 24/7:

| Mã | Vấn đề phát hiện | Mức độ | Tác động nếu không xử lý |
| :--- | :--- | :--- | :--- |
| **S01** | Các API `/api/hub/*` trong `src/main.py` chưa gắn khóa bảo mật `Depends(verify_auth)` | **Cao (Bảo mật)** | Khi bật `gateway_api_key` hoặc đưa lên Cloud, người ngoài có thể gọi vào Hub mà không cần mật khẩu. |
| **S02** | Hàm `internal_ai_chat` của AI Trưởng phòng chỉ gọi `candidates[0]`, chưa tự động chuyển model dự phòng khi gặp lỗi 429 | **Cao (Độ ổn định)** | Nếu model đầu tiên hết quota (429), AI Trưởng phòng báo lỗi ngay thay vì tự chuyển sang model miễn phí thứ 2, thứ 3. |
| **S03** | `AIAutoOrchestrator` mới chỉ gọi được 1 công cụ đơn lẻ trong 1 lần ra lệnh | **Trung bình (Tính năng)** | Không xử lý được câu lệnh liên hoàn nhiều bước (Ví dụ: *"Lấy kèo mới ở dự án A rồi đem sang dự án B chấm điểm rủi ro"*). |
| **S04** | Lịch sử Bảng Tin Chung (`SharedEventBus`) lưu hoàn toàn trên RAM | **Trung bình (Dữ liệu)** | Khi khởi động lại Gateway, toàn bộ nhật ký trao đổi trước đó giữa các Agent bị xóa trắng trên giao diện Web. |
| **S05** | Dự án `smart-money-agent` chưa được gắn đầu cắm `AgentHubNode` thực tế | **Quan trọng (Thực chiến)** | Mở giao diện Web của Gateway lên chưa thấy `smart-money-agent` hiện Online và chưa bấm gọi trực tiếp được các hàm soi ví/chấm rủi ro. |

---

## 2. Chi tiết Đặc tả Triển khai (S01 – S05)

### S01: Khoá Bảo mật Zero-Trust cho toàn bộ Endpoint `/api/hub/*`
* **File sửa đổi:** [`src/main.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/main.py)
* **Yêu cầu:**
  * Gắn `dependencies=[Depends(verify_auth)]` vào tất cả 9 endpoints của Agent Hub (`/api/hub/register`, `/api/hub/heartbeat`, `/api/hub/command/result`, `/api/hub/projects`, `/api/hub/tools`, `/api/hub/events/publish`, `/api/hub/events/poll`, `/api/hub/events/history`, `/api/hub/orchestrate`).
  * Đảm bảo `AgentHubNode` trong `sdk/agent_sdk.py` luôn gửi kèm header `Authorization: Bearer <api_key>` hoặc `X-Gateway-Key`.

### S02: Failover đa tầng & Cache cho AI Trưởng Phòng (`internal_ai_chat`)
* **File sửa đổi:** [`src/main.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/main.py)
* **Yêu cầu:**
  * Nâng cấp `internal_ai_chat(messages, model_tier)`:
    1. Kiểm tra `response_cache` trước khi gọi mạng (tiết kiệm quota tuyệt đối nếu câu lệnh điều phối lặp lại).
    2. Duyệt qua toàn bộ danh sách `candidates = await balancer.get_candidate_chain(model_tier)`.
    3. Nếu candidate gặp lỗi HTTP 429 hoặc 5xx, tự động ghi nhận lỗi vào `balancer` / cooldown và thử ngay candidate kế tiếp.
    4. Hỗ trợ chuẩn hóa payload/URL cho cả Gemini, Groq, OpenRouter và GitHub Models.

### S03: Điều phối Đa Bước Liên Dự Án (Multi-Step Chaining Orchestrator)
* **File sửa đổi:** [`src/hub/orchestrator.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/hub/orchestrator.py)
* **Yêu cầu:**
  * Nâng cấp `orchestrate(user_query, model_tier, max_steps=3)`:
    * Cho phép AI Trưởng phòng thực hiện chuỗi tối đa `max_steps` bước (Step 1 gọi công cụ dự án A ➔ Nhận kết quả ➔ Step 2 gọi công cụ dự án B hoặc phát sự kiện lên Bảng tin ➔ Step 3 tổng hợp báo cáo cuối cùng).
    * Chống lặp (Loop Guard): Nếu AI gọi lại cùng 1 `(project_id, tool_name, arguments)` trong cùng 1 phiên điều phối, tự động ngắt và tổng hợp kết quả ngay.
    * Trả về danh sách `steps_executed` chi tiết để hiển thị rõ từng bước trên Web UI.

### S04: Lưu trữ bền vững Bảng Tin Chung & Dự Án vào SQLite
* **File sửa đổi:** [`src/storage/db.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/storage/db.py), [`src/hub/event_bus.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/hub/event_bus.py)
* **Yêu cầu:**
  * Tạo bảng `hub_events` trong SQLite (`sequence`, `event_id`, `topic`, `source_project`, `payload_json`, `priority`, `timestamp`).
  * Khi Gateway khởi động (`lifespan`), tự động nạp lại 100 sự kiện gần nhất từ SQLite vào `SharedEventBus` để giao diện Web không bao giờ bị mất dữ liệu khi restart.

### S05: Tích hợp Thực chiến `smart-money-agent` vào Agent Hub
* **File tạo mới / cập nhật tại `C:\Users\PC\Desktop\smart-money-agent`:**
  * Tạo [`core/hub_bridge.py`](file:///c:/Users/PC/Desktop/smart-money-agent/core/hub_bridge.py) (sử dụng `AgentHubNode` kết nối tới `http://127.0.0.1:8000`).
  * Đăng ký sẵn 4 công cụ thực chiến lên Hub:
    1. `get_system_status`: Xem trạng thái bot, các chain đang quét (Solana, Base, BSC) và chế độ giao dịch.
    2. `analyze_token_risk`: Kiểm tra nhanh điểm rủi ro và thanh khoản của một token.
    3. `run_bull_bear_debate`: Kích hoạt 2 AI (Phe Bò vs Phe Gấu) tranh luận về một kèo bất kỳ.
    4. `get_recent_signals`: Lấy danh sách các tín hiệu ví cá mập mới nhất từ database.
  * Cập nhật `.env` của `smart-money-agent` trỏ `LLM_BASE_URL=http://127.0.0.1:8000/v1` và `LLM_MODEL=reasoning` để dùng nguồn AI miễn phí của Gateway.

---

## 3. Tiêu chí Nghiệm thu
1. Toàn bộ bài test của `ai-free-gateway` (139 tests hiện tại + bộ test nâng cấp S01–S04) đạt **100% PASSED**.
2. Toàn bộ bài test của `smart-money-agent` (339+ tests) đạt **100% PASSED**.
3. Khi bật Gateway và `smart-money-agent`, vào tab **🤖 Agent Hub** trên Web thấy ngay **"Bot Săn Cá Mập (smart-money-agent) – ONLINE"** với đầy đủ 4 công cụ sẵn sàng nhận lệnh tiếng Việt.
