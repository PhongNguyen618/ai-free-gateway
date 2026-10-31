# ĐẶC TẢ KỸ THUẬT: HỆ THỐNG GIÁM SÁT SỐ DƯ VÍ, ĐIỀU KHIỂN 1-CHẠM, BÁO CÁO TELEGRAM & CHỐNG NGỦ RENDER 24/7

**Mục tiêu:** Giải quyết triệt để 4 nhu cầu vận hành thực tế trên đám mây Render (`https://aifreegateway.onrender.com`):
1. **Theo dõi quá trình hoạt động trực quan** ngay trên Web UI (không cần gõ lệnh phức tạp).
2. **Kiểm tra số dư ví (Wallet Balance), Nonce, Gas và Điểm Airdrop** tại một bảng duy nhất + Cho phép thêm ví mới ngay trên Web.
3. **Bắn thông báo Telegram tự động theo thời gian thực** về điện thoại mỗi khi có kèo ngon, cày xong nhiệm vụ, chuyển vốn hoặc có tín hiệu cá mập.
4. **Chống ngủ (Anti-Sleep Keep-Alive) 24/7** cho gói Render Free.

---

## 1. Kiến trúc Luồng Giám Sát & Cảnh Báo Telegram 24/7

```
┌────────────────────────────────────────────────────────────────────────────┐
│                     MÁY CHỦ RENDER (24/7 KEEP-ALIVE)                       │
│                                                                            │
│  ┌─────────────────────────┐          ┌─────────────────────────────────┐  │
│  │  airdrop-hunter-agent   │          │       smart-money-agent         │  │
│  │ • Quét kèo DeFiLlama    │          │ • Soi ví cá mập Solana/Base/BSC │  │
│  │ • Cày Streak & Điểm     │          │ • Nhận 80% vốn từ Airdrop       │  │
│  │ • Đọc Balance ETH/SOL   │          │ • AI Bull/Bear Debate           │  │
│  └────────────┬────────────┘          └────────────────┬────────────────┘  │
│               │                                        │                   │
│               ▼                                        ▼                   │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                SHARED EVENT BUS & CENTRAL DISPATCHER                 │  │
│  └───────────────┬──────────────────────────────────┬───────────────────┘  │
└──────────────────┼──────────────────────────────────┼──────────────────────┘
                   │                                  │
     (1) Hiển thị Trực Tiếp              (2) Bắn Tin Nhắn Tức Thì
         Bảng Balance & 1-Click               Qua Telegram Bot API
                   ▼                                  ▼
   ┌───────────────────────────────┐  ┌───────────────────────────────────┐
   │   WEB DASHBOARD (/ui)         │  │   ĐIỆN THOẠI CỦA BẠN (TELEGRAM)   │
   │ • Bảng Số Dư Ví (ETH/SOL/$)   │  │ • Báo cáo Kèo Airdrop mới         │
   │ • Bảng Điểm & Chuỗi Active    │  │ • Báo cáo Cày xong + Điểm thưởng  │
   │ • Nút thêm ví & Nút bấm 1-Chạm│  │ • Báo cáo Bơm vốn & Tín hiệu Meme │
   └───────────────────────────────┘  └───────────────────────────────────┘
```

---

## 2. Chi tiết 5 Hạng mục Nâng cấp (M01 – M05)

### M01: Central Telegram Hub Notifier (`src/hub/telegram_notifier.py` & `src/hub/event_bus.py`)
**Vấn đề hiện tại:** Các sự kiện từ bot chỉ lưu trong bộ nhớ Hub, chưa gửi tin nhắn về Telegram của bạn.  
**Giải pháp kỹ thuật:**
- Tạo module `src/hub/telegram_notifier.py` (`TelegramHubNotifier`):
  - Sử dụng `TELEGRAM_BOT_TOKEN` và `TELEGRAM_CHAT_ID` từ biến môi trường (Zero-dependency, gọi trực tiếp `https://api.telegram.org/bot<TOKEN>/sendMessage`).
  - Tự động lắng nghe mọi sự kiện quan trọng trên `SharedEventBus`:
    - `system.startup`: *"🚀 Hệ sinh thái 3 Dự án trên Render đã khởi động thành công!"*
    - `airdrop.alpha_opportunity`: *"🎯 Phát hiện kèo Airdrop tiềm năng cao: [Tên dự án] - Điểm: [X]/100"*
    - `airdrop.cycle_completed`: *"⚡ Hoàn thành chu kỳ cày Airdrop: [Số ví] ví | +[Điểm] pts | Phí gas: $[X]"*
    - `airdrop.capital_routed`: *"💸 Đã thu hoạch Airdrop & bơm $[X] (80%) sang Bot Săn Meme!"*
    - `news.hot_token` / `smart_money.*`: *"🐋 Tín hiệu Cá Mập gom hàng mới!"*
- Thêm API Endpoint `POST /api/hub/telegram/test` trên Gateway:
  - Cho phép bấm nút **"📲 Gửi Báo Cáo Về Telegram Ngay"** trên giao diện Web để kiểm tra kết nối Telegram và nhận bản tóm tắt toàn hệ thống về điện thoại bất cứ lúc nào.

---

### M02: Bảng Số Dư Ví (Wallet Balance Monitor) & Nút Bấm 1-Chạm Trên Web UI (`ui/index.html`)
**Vấn đề hiện tại:** Muốn xem số dư ví hay chạy công cụ phải gõ chữ vào ô chat; chưa có bảng quản lý ví trực quan.  
**Giải pháp kỹ thuật:**
1. **Thêm khu vực "💰 Bảng Số Dư Ví & Tiến Độ Airdrop / Meme" ngay trong tab `🤖 Agent Hub`:**
   - **4 Thẻ Chỉ Số Nhanh (KPI Cards):**
     - `Tổng Số Ví Đang Cày` & `Tổng Chuỗi Ngày Active`.
     - `Tổng Điểm Airdrop Tích Lũy (pts)`.
     - `Doanh Thu Airdrop ($)` & `Vốn Đã Bơm Sang Bot Meme ($)`.
     - `Phí Gas Mạng Base L2 Hiện Tại (Gwei / $)` & `Trạng Thái Telegram`.
   - **Bảng Chi Tiết Từng Ví (Wallet Balance Table):**
     - Các cột: `Địa Chỉ Ví`, `Mạng`, `Số Dư On-Chain (ETH/SOL)`, `Số Giao Dịch (Nonce)`, `Cá Tính (Persona)`, `Khung Giờ Hoạt Động`, `Trạng Thái`.
   - **Ô Nhập "➕ Thêm Ví Của Bạn":**
     - Cho phép bạn dán địa chỉ ví thật của bạn (`0x...`) ngay trên Web và bấm **Thêm Ví** ➔ Tự động lưu vào DB, phân bổ Persona chống Sybil và kiểm tra số dư on-chain ngay lập tức!
2. **Nút Bấm Công Cụ 1-Chạm (1-Click Tool Execution):**
   - Biến mọi thẻ `🔧 tên_công_cụ` trên danh sách dự án thành **nút bấm tương tác**:
     - Click vào `🔧 check_wallet_onchain_health` ➔ Chạy kiểm tra số dư & gas ngay.
     - Click vào `🔧 run_daily_farming_cycle` ➔ Kích hoạt cày nhiệm vụ & cộng điểm ngay.
     - Click vào `🔧 discover_top_airdrops` ➔ Quét kèo DeFiLlama ngay.
     - Click vào `🔧 get_system_status` / `🔧 get_recent_signals` ➔ Xem ngay tín hiệu cá mập!
3. **Lưu API Key Vĩnh Viễn (`localStorage` kết hợp `sessionStorage`):**
   - Để bạn mở lại trình duyệt không phải nhập lại `sk-local-dev-token` nhiều lần.

---

### M03: Tự Động Hóa Vòng Lặp Daemon & Thêm Công Cụ Quản Lý Ví (`ecosystem/airdrop_hunter/`)
**Vấn đề hiện tại:** Khi container mới khởi tạo trên Render, DB chưa có ví nên daemon chờ rỗng và chưa bắn sự kiện lên Bảng Tin Chung.  
**Giải pháp kỹ thuật:**
- Thêm công cụ thứ 7 vào `airdrop-hunter-agent`: **`add_farming_wallet(wallet_address, chain_type="EVM")`** để thêm ví mới từ Web UI.
- Trong `run_daemon_loop()`:
  - Tự động đọc biến môi trường `FARMING_WALLETS` (nếu có) hoặc khởi tạo sẵn 2 ví mặc định ngay khi khởi chạy.
  - Ngay ở chu kỳ đầu tiên (`cycle_count == 1`), bắt buộc thực thi quét kèo + cày chu kỳ đầu và gọi `bridge_node.publish_event(...)` để **Bảng Tin Chung trên Web UI** và **Telegram** nhận ngay dữ liệu thực tế!

---

### M04: Chống Ngủ Render 24/7 (`ecosystem_supervisor.py`)
**Vấn đề hiện tại:** Render gói Free sẽ tự động đóng băng (spin down) nếu sau 15 phút không có request HTTP từ bên ngoài.  
**Giải pháp kỹ thuật:**
- Thêm luồng nền `render_keep_alive_loop()` trong `ecosystem_supervisor.py`:
  - Tự động lấy địa chỉ `RENDER_EXTERNAL_URL` (biến mặc định Render tự cấp, hoặc `https://aifreegateway.onrender.com`).
  - Định kỳ mỗi **8 phút (480 giây)** tự động gửi 1 gói ping tới `<PUBLIC_URL>/health` để giữ máy chủ thức 24/7 trọn đời!

---

### M05: Đồng Bộ Biến Môi Trường Lên Render (1-Click Bulk Env)
- Chuẩn bị sẵn khối cấu hình Environment Variables (gồm `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`) để bạn bấm nút **"Add from .env"** trên Render dán 1 lần duy nhất là kích hoạt toàn bộ AI và Telegram.

---

## 3. Tiêu chí Nghiệm thu (Acceptance Criteria)
1. Mở Web UI tab **🤖 Agent Hub** thấy ngay **Bảng Số Dư Ví (Balance, Nonce, Gas, Persona, Điểm)** và có thể thêm địa chỉ ví mới trực tiếp trên Web.
2. Bấm vào bất kỳ nút `🔧 công_cụ` nào trên thẻ dự án đều chạy trực tiếp và hiển thị kết quả ngay lập tức.
3. Bảng Tin Chung (`Shared Event Bus`) hiển thị đầy đủ các sự kiện quét kèo và cày nhiệm vụ ngay khi máy chủ khởi động.
4. Bấm nút **"📲 Test Báo Cáo Telegram"** hoặc khi bot chạy xong chu kỳ đều gửi tin nhắn tiếng Việt rõ nét về Telegram của bạn.
5. Toàn bộ bài test tự động (`pytest`) trong cả `ai-free-gateway` và `airdrop-hunter-agent` đạt **100% PASS**.
