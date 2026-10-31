# ĐẶC TẢ KỸ THUẬT: ALL-IN-ONE AI GATEWAY & AGENT HUB (MCP-LITE + EVENT BUS)

**Phiên bản:** 2.0  
**Mục tiêu:** Nâng cấp `ai-free-gateway` thành Trung tâm Điều hành AI Agent hợp nhất (All-in-One Hub) theo xu thế hiện đại nhất (Chuẩn cắm công cụ MCP-Lite + Kiến trúc Bảng tin chung Event-Driven), giúp tích hợp mọi dự án (`smart-money-agent`, bot cào tin, bot Telegram...) chỉ bằng vài dòng khai báo mà không cần tạo thêm dự án quản lý rời rạc.

---

## 1. Kiến trúc tổng thể (All-in-One Hub Architecture)

```
┌────────────────────────────────────────────────────────────────────────────┐
│                     AI FREE GATEWAY & AGENT HUB (:8000)                    │
│                                                                            │
│  ┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────┐  │
│  │ 1. Free AI Pool      │  │ 2. MCP Tool Registry │  │ 3. Shared Event  │  │
│  │ (Gemini/Groq/R1/     │  │ (Cổng cắm tự động    │  │    Bus (Bảng tin │  │
│  │  Cache/Quota Guard)  │  │  nhận diện dự án)    │  │    liên dự án)   │  │
│  └──────────────────────┘  └──────────────────────┘  └──────────────────┘  │
│                                      ▲                         ▲           │
│  ┌───────────────────────────────────┴─────────────────────────┴────────┐  │
│  │ 4. AI Auto-Orchestrator & Web Dashboard                              │  │
│  │ (Ra lệnh bằng Tiếng Việt trên Web -> Tự động gọi đúng dự án thực thi)│  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└──────────────────▲──────────────────────────────────────▲──────────────────┘
                   │ Cắm qua SDK (1 dòng)                 │ Cắm qua SDK (1 dòng)
       ┌───────────┴───────────┐              ┌───────────┴───────────┐
       │   smart-money-agent   │              │  Dự án mới (Scraper,  │
       │ (Soi ví, Chấm rủi ro) │              │  Telegram, Bán hàng)  │
       └───────────────────────┘              └───────────────────────┘
```

---

## 2. Chi tiết 4 Hạng mục Nâng cấp (H01 – H04)

### H01: Plug-and-Play Project & Tool Registry (Chuẩn cổng cắm MCP-Lite)
**Mục tiêu:** Bất kỳ dự án nào khi bật lên sẽ tự động báo danh với Gateway: *"Tôi là dự án X, tôi đang Online, và tôi có các công cụ A, B, C"*.

* **File triển khai:** `src/hub/registry.py`
* **Cơ chế hoạt động:**
  * Lưu trữ danh sách các dự án kết nối (`project_id`, `display_name`, `description`, `status: online/offline`, `last_heartbeat`, `tools: List[ToolSchema]`).
  * Tự động chuyển trạng thái sang `offline` nếu dự án không gửi tín hiệu heartbeat sau 60 giây.
  * Hàng đợi lệnh (`command_queue`) cho từng dự án: Khi Gateway hoặc AI muốn bấm nút gọi 1 hàm trong dự án con, lệnh được đẩy vào queue, dự án con lấy về thực thi và trả kết quả lên Gateway (không cần mở port phức tạp ở từng dự án con).
* **API Endpoints:**
  * `POST /api/hub/register`: Dự án con đăng ký thông tin và danh sách công cụ (Tools).
  * `POST /api/hub/heartbeat`: Dự án con cập nhật trạng thái sống và lấy các lệnh được giao (Long-poll / Poll).
  * `POST /api/hub/command/result`: Dự án con trả kết quả sau khi chạy xong công cụ.
  * `GET /api/hub/projects`: Trả về toàn bộ danh sách dự án, trạng thái Online/Offline và các công cụ hiện có.

---

### H02: Shared Event Bus – "Bảng Tin Chung" Liên Dự Án (Event-Driven Pub/Sub)
**Mục tiêu:** Cho phép các dự án tự động phối hợp với nhau như một nhóm chat mà không cần viết code nối cứng giữa từng dự án.

* **File triển khai:** `src/hub/event_bus.py`
* **Cơ chế hoạt động:**
  * Bất kỳ dự án nào cũng có thể phát một thông điệp (Event) lên một chủ đề (`topic`), ví dụ:
    * `market.hot_token`: Phát hiện đồng coin có dòng tiền đột biến.
    * `risk.audit_passed`: Kết quả kiểm tra an toàn đạt chuẩn.
    * `alert.telegram`: Yêu cầu gửi thông báo khẩn cho người dùng.
  * Hỗ trợ **AI Event Rule (Luật phản xạ tự động bằng AI)**: Có thể cài đặt để khi có sự kiện A xuất hiện trên Bảng tin, AI của Gateway tự động đọc, đánh giá và kích hoạt công cụ của Dự án B.
* **API Endpoints:**
  * `POST /api/hub/events/publish`: Bắn sự kiện mới lên Bảng tin chung (`topic`, `source_project`, `payload`, `priority`).
  * `GET /api/hub/events/poll`: Các dự án con lấy sự kiện mới theo `topic` và `since_id`.
  * `GET /api/hub/events/history`: Xem lịch sử trao đổi giữa các dự án trên Dashboard.

---

### H03: AI Auto-Orchestrator (Điều phối viên AI tự động bằng Tiếng Việt)
**Mục tiêu:** Bạn chỉ cần gõ yêu cầu bằng ngôn ngữ tự nhiên (ví dụ: *"Kiểm tra rủi ro đồng SOL và báo cáo tình trạng ví cá mập"*), Gateway sẽ tự động nhìn vào danh sách công cụ của tất cả các dự án đang cắm vào và điều phối thực hiện.

* **File triển khai:** `src/hub/orchestrator.py`
* **Cơ chế hoạt động:**
  1. Nhận câu lệnh tiếng Việt của bạn từ giao diện Web hoặc API.
  2. Tổng hợp toàn bộ các công cụ (MCP Tools) từ các dự án đang `online` trong `H01`.
  3. Dùng mô hình AI miễn phí trong Gateway phân tích xem cần gọi công cụ nào của dự án nào, hoặc phát sự kiện gì lên Bảng tin.
  4. Giao việc cho dự án tương ứng, thu thập kết quả và tổng hợp thành câu trả lời cuối cùng cho bạn.
* **API Endpoint:**
  * `POST /api/hub/orchestrate`: Gửi yêu cầu bằng ngôn ngữ tự nhiên, trả về kế hoạch điều phối và kết quả thực thi.

---

### H04: Đầu cắm `AgentHubNode` siêu gọn & Giao diện Web Điều Hành
**Mục tiêu:** Biến việc tích hợp một dự án bất kỳ về mức tối giản (chỉ 3–5 dòng code) và quản lý trực quan trên Web UI.

* **Nâng cấp `sdk/agent_sdk.py`:** Bổ sung class `AgentHubNode`:
  ```python
  from sdk.agent_sdk import AgentHubNode

  hub = AgentHubNode(project_id="smart-money-agent", name="Bot Săn Cá Mập")

  # 1. Cắm công cụ lên Trung tâm (MCP Tool)
  @hub.tool(description="Kiểm tra điểm rủi ro của một token trên chuỗi")
  def check_token_risk(token_address: str) -> dict:
      return {"token": token_address, "risk_score": 15, "verdict": "SAFE"}

  # 2. Lắng nghe Bảng tin chung
  @hub.on_event("news.hot_token")
  def handle_hot_news(event_data: dict):
      print("Nhận tín hiệu từ bot tin tức:", event_data)

  # 3. Chạy ngầm tự động kết nối về Gateway
  hub.start_background()
  ```
* **Nâng cấp Giao diện Web (`ui/index.html`):**
  * Thêm tab/khu vực **"Agent Hub (Trung Tâm Dự Án)"**:
    * Hiển thị danh sách các dự án đang cắm vào (Online/Offline, số công cụ).
    * Hiển thị **Bảng Tin Chung (Live Event Stream)** xem các Agent đang báo tin gì cho nhau.
    * Ô **Ra lệnh nhanh cho AI Điều Phối** ngay trên Web.

---

## 3. Tiêu chí nghiệm thu (Acceptance Criteria)
1. **Đơn giản tối đa:** Chỉ cần bật 1 server duy nhất là `ai-free-gateway`.
2. **Tự động nhận diện (Zero-Config):** Dự án mới dùng `@hub.tool` và `hub.start_background()` là ngay lập tức xuất hiện trên Gateway.
3. **Giao tiếp liên dự án:** Dự án A bắn sự kiện lên Bảng tin, Dự án B nhận được ngay lập tức (< 1 giây).
4. **Kiểm thử tự động:** 100% bài test hiện có (131 tests) + bộ test mới cho Agent Hub (`tests/test_agent_hub.py`) vượt qua hoàn toàn.
