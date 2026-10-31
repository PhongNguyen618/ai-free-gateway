# ĐẶC TẢ KỸ THUẬT: HỆ THỐNG GIAO DỊCH ON-CHAIN THẬT, TỐI ƯU YIELD & CHIẾN LƯỢC CHỐT LỜI BẬC THANG + MOONBAG X50 (V3.0)

**Trạng thái:** Sẵn sàng triển khai (Ready for Implementation)  
**Mục tiêu cốt lõi:**  
1. Loại bỏ hoàn toàn các hàm giả lập (`mock_tx_hash`), chuyển sang **Tương tác On-Chain thật 100%** trên mạng Base L2 / Solana với chi phí tối thiểu (< \$0.0005/tx).  
2. Kết hợp **Lợi nhuận kép ổn định (DeFi Yield + Airdrop Farming)** để nuôi phí vận hành và tạo dòng tiền nền tảng an toàn.  
3. Áp dụng **Thuật toán Chốt Lời Từng Đoạn (Tiered Scaling-Out) + Giữ Moonbag Gồng x10–x50** cho `smart-money-agent` để tối ưu tỷ lệ Lợi nhuận/Rủi ro (Asymmetric R:R) đặc thù của Meme Coin.

---

## 1. BÀI TOÁN TỶ LỆ LỢI NHUẬN / RỦI RO (ASYMMETRIC PAYOFF) CỦA MEME COIN

### Tại sao không được chốt lời 100% vị thế ở mức +10% hay +15%?
- Meme coin có biến động cực lớn. Nếu chấp nhận rủi ro âm -20% đến -30% mỗi lệnh thua mà lệnh thắng chỉ ăn +10% rồi bán sạch, về dài hạn kỳ vọng toán học ($E$) sẽ âm, và **đánh mất hoàn toàn cơ hội ăn các siêu sóng x10, x50** vốn là nguồn tạo đột phá tài khoản trong thị trường Meme.
- Ngược lại, nếu chỉ gồng x50 mà không chốt lời từng đoạn, 90% các kèo tăng x2 rồi quay đầu về 0 sẽ làm mất cả vốn lẫn lãi.

### Giải pháp chuẩn Quỹ Định Lượng (Quant Tiered Take-Profit + Moonbag Engine)
Chia mỗi vị thế mua ($100\%$ số lượng token) thành **4 phần rõ rệt** theo kịch bản giá:

| Giai đoạn | Mốc Lợi Nhuận (PnL) | Tỷ lệ bán ( trên tổng vị thế ban đầu ) | Tỷ lệ còn giữ lại | Hành động Quản trị Rủi ro (Stop-Loss) | Ý nghĩa Tài chính |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Vào lệnh (Entry)** | $0\%$ ($1.0\times$) | $0\%$ | $100\%$ | Hard Stop-Loss: $-18\%$ (hoặc thoát khẩn cấp nếu Liquidity giảm $>30\%$) | Giới hạn rủi ro tối đa trên mỗi kèo |
| **Đoạn 1 (TP1 - Khóa thế an toàn)** | **$+40\%$ ($1.4\times$)** | Bán **$35\%$** | Còn **$65\%$** | Dời Stop-Loss lên **Hòa vốn ($+2\%$ bù phí gas/slippage)** | Thu về $49\%$ vốn gốc, đẩy rủi ro của lệnh về mức gần bằng $0$ |
| **Đoạn 2 (TP2 - Free Ride 100%)** | **$+100\%$ ($2.0\times$)** | Bán **$30\%$** | Còn **$35\%$** | Dời Trailing Stop lên **$+40\%$** | Đã thu về **$109\%$ vốn gốc** ($0.49 + 0.60$). Toàn bộ $35\%$ token còn lại là **LÃI RÒNG 100% (Vốn 0 đồng)** |
| **Đoạn 3 (TP3 - Thu hoạch sóng lớn)** | **$+300\%$ ($4.0\times$)** | Bán **$15\%$** | Còn **$20\%$** | Kích hoạt **Trailing Stop động $-35\%$ từ đỉnh (High-Water Mark)** | Đút túi thêm $+60\%$ lợi nhuận trên tổng vốn gốc |
| **Đoạn 4 (MOONBAG - Gồng x10 ➔ x50+)** | **$+900\%$ ($10\times$) ➔ $+4900\%$ ($50\times$)** | Chia nhỏ bán **$5\%$** tại $10\times$, **$5\%$** tại $25\times$, giữ **$10\%$** tới $50\times$+ | **$10\% - 20\%$ (Moonbag)** | Chỉ đóng nếu hợp đồng có dấu hiệu rút thanh khoản (Rug Alert) hoặc gãy cấu trúc lớn ($-45\%$ từ ATH sau mốc $10\times$) | **Bắt trọn siêu sóng x10, x50** bằng tiền lãi mà tâm lý hoàn toàn thoải mái vì đã rút hết gốc + lãi từ TP1 & TP2 |

**Công thức Kỳ vọng Toán học ($EV$):**
$$R_{\text{realized}} = 0.35 \times 1.4 + 0.30 \times 2.0 + 0.15 \times 4.0 + 0.20 \times M_{\text{moonbag}} - 1.0$$
- Ngay cả khi phần Moonbag chưa tới x50 mà dừng ở x4, bạn đã lãi ròng **+149% (+2.49x)**.
- Nếu trúng 1 kèo Moonbag đạt **x50**, riêng $20\%$ Moonbag mang về **$10\times$ (1000%) tổng vốn ban đầu**, bù đắp cho hàng chục lệnh cắt lỗ nhỏ và đẩy tổng tài khoản tăng vọt!

---

## 2. KIẾN TRÚC NÂNG CẤP CHI TIẾT (4 MODULE)

### Module R01: Bộ Quản Lý Vị Thế Bậc Thang & Moonbag (`smart_money/position_manager.py`)
Xây dựng State Machine theo dõi từng vị thế mở (Open Positions) trong SQLite & bộ nhớ thực:
- **Cấu trúc dữ liệu vị thế (`PositionState`):**
  - `position_id`, `token_address`, `token_symbol`, `chain` (`base` / `solana`)
  - `entry_price_usd`, `current_price_usd`, `highest_price_usd` (High-Water Mark ATH)
  - `initial_quantity`, `remaining_quantity`, `remaining_pct` ($100\% \rightarrow 65\% \rightarrow 35\% \rightarrow 20\%$)
  - `invested_usd`, `realized_usd` (Số tiền USD đã thu về ví sau các lần chốt từng đoạn), `unrealized_usd`
  - `tp1_hit` (`bool`), `tp2_hit` (`bool`), `tp3_hit` (`bool`), `moonbag_10x_hit` (`bool`), `moonbag_50x_hit` (`bool`)
  - `stop_loss_price_usd`, `trailing_drawdown_limit_pct`
- **Cơ chế tự động quét giá thật (Live Price Feed từ DexScreener API):**
  - Mỗi 20 giây, hàm `update_positions_live_prices()` gọi trực tiếp `https://api.dexscreener.com/latest/dex/tokens/{token_address}` để lấy giá USD thật, thanh khoản (Liquidity USD) và khối lượng 24h thật.
  - Tự động kích hoạt lệnh bán từng phần khi giá chạm mốc TP1 ($1.4\times$), TP2 ($2.0\times$), TP3 ($4.0\times$), Moonbag ($10\times$, $25\times$, $50\times$), hoặc cắt lỗ bảo vệ vốn khi chạm `stop_loss_price_usd` / phát hiện rút thanh khoản đột ngột.
  - Mỗi lần chốt lời 1 đoạn, tự động phát sự kiện `smart_money.partial_tp_hit` hoặc `smart_money.moonbag_active` lên **Event Bus** để **bắn tin nhắn báo cáo về Telegram ngay lập tức**.

---

### Module R02: Bộ Lọc Thị Trường Thật & Chấm Điểm Hợp Đồng Thật (`smart_money/live_scanner.py`)
Thay thế hoàn toàn dữ liệu mẫu tĩnh trong `smart-money-agent` bằng API On-chain thật 100% (Miễn phí, không tốn phí API):
1. **DexScreener Live Scanner (`https://api.dexscreener.com/token-boosts/top/v1` & Search Pair API):**
   - Quét các cặp token thực tế đang có dòng tiền đột biến trên **Base** và **Solana**.
   - **Bộ lọc bảo vệ vốn (Anti-Rug Filter):**
     - Thanh khoản tối thiểu (`liquidity.usd`): $\ge \$30,000$ USD.
     - Khối lượng 24h (`volume.h24`): $\ge \$50,000$ USD.
     - Tỷ lệ Mua/Bán 1 giờ gần nhất (`txns.h1.buys / txns.h1.sells`): $\ge 1.25$ (Phe mua áp đảo).
     - FDV hợp lý: $\$50,000 \le \text{FDV} \le \$15,000,000$ (Đủ nhỏ để còn dư địa x10–x50, đủ lớn để không phải rác mới tạo 1 phút).
2. **GoPlus Security API (`https://api.gopluslabs.io/api/v1/token_security/{chain_id}`):**
   - Kiểm tra trực tiếp Smart Contract thật:
     - `is_honeypot == "0"` (Không phải bẫy Honeypot).
     - `buy_tax <= 0.05` và `sell_tax <= 0.05` (Thuế mua/bán $\le 5\%$).
     - `is_open_source == "1"` (Hợp đồng đã xác minh mã nguồn).
     - `owner_change_balance == "0"` (Chủ dự án không thể tự sửa số dư ví người dùng).

---

### Module R03: Giao Dịch On-Chain Thật 0% Phí Sàn & Tối Ưu Yield Base L2 (`airdrop_hunter/src/executor/onchain_signer.py`)
Nâng cấp `airdrop-hunter-agent` để tương tác thật trên mạng **Base Mainnet (Chain ID: 8453)**:
1. **Hỗ trợ cả 2 chế độ: `LIVE_ONCHAIN` (Khi có Private Key + ETH trên Base) và `LIVE_READ_SIMULATION` (An toàn khi ví chưa nạp gas):**
   - Tự động kiểm tra biến môi trường `EVM_PRIVATE_KEY` (hoặc `WALLET_PRIVATE_KEY`) và số dư ETH thật của ví trên mạng Base L2.
   - Nếu số dư ví $\ge 0.0002\text{ ETH}$ ($\sim \$0.50$) và phí gas mạng Base $< 0.05\text{ gwei}$ ($< \$0.001$/tx):
     - Thực hiện ký giao dịch thật bằng `eth_account` + gửi qua JSON-RPC `eth_sendRawTransaction`:
       - **Chiến thuật 1 (Zero-Slippage Volume & Active Days):** Gọi hàm `deposit()` (Wrap ETH $\rightarrow$ WETH) và `withdraw()` (Unwrap WETH $\rightarrow$ ETH) trên hợp đồng chuẩn **Canonical WETH của Base (`0x4200000000000000000000000000000000000006`)**.
         - **Ưu điểm tuyệt đối:** Không bao giờ mất phí sàn (0% DEX fee), không trượt giá (0% slippage), 100% tiền gốc quay về ví, chỉ mất ~\$0.0003 phí gas mạng Base, nhưng tạo ra **Transaction Hash thật trên BaseScan**, tăng Nonce thật, tăng Volume thật và giữ chuỗi Active Days thật!
       - **Chiến thuật 2 (DeFi Real Yield Scanner - Aave V3 / Moonwell / Aerodrome trên Base):**
         - Quét lãi suất APY thực tế của USDC/WETH từ DefiLlama Yield API (`https://yields.llama.fi/pools`) trên mạng Base để đề xuất và phân bổ vốn nhận lãi kép ổn định 10%–25%/năm.

---

### Module R04: Nâng Cấp Giao Diện Dashboard & Thông Báo Telegram Bậc Thang
1. **Trên Dashboard (`ui/index.html` - Tab Agent Hub):**
   - Thêm bảng **"🚀 Quản Lý Vị Thế Meme & Thang Chốt Lời Moonbag x50"**:
     - Hiển thị từng đồng coin đang nắm giữ, Giá vào (Entry), Giá hiện tại (Live), Mức lời/lỗ (`+125%`), Trạng thái bậc chốt lời (`[✅ TP1] [✅ TP2 - Free Ride] [⏳ TP3] [🌙 Moonbag 20%]`), Điểm dừng lỗ (Trailing Stop) và nút **"⚡ Chốt Lời / Đóng Vị Thế Ngay"**.
   - Cập nhật công cụ 1-chạm của `smart-money-agent`:
     - `⚡ scan_live_meme_gems`: Quét token thật từ DexScreener + GoPlus.
     - `⚡ open_tiered_position`: Mở vị thế với kế hoạch chốt lời 4 bậc + Moonbag x50.
     - `⚡ check_positions_and_tp`: Cập nhật giá thật và thực thi chốt lời bậc thang.
2. **Trên Telegram (`telegram_notifier.py`):**
   - Báo cáo chi tiết mỗi khi khớp từng bậc chốt lời:
     - `🎯 [CHỐT LỜI ĐOẠN 1 - TP1 +40%] Đã bán 35% token $BRETT, thu hồi vốn an toàn! Đã dời Stop-Loss về hòa vốn.`
     - `🔥 [FREE RIDE - TP2 X2] Đã bán tiếp 30% token, rút 100% gốc + lãi về két! Giữ 35% còn lại gồng x5 ➔ x50!`
     - `🌙 [MOONBAG ALERT - X10 / X50] Vị thế Moonbag đang đạt lợi nhuận +950%!`

---

## 3. KẾ HOẠCH KIỂM THỬ & XÁC MINH (VERIFICATION PLAN)
1. **Unit & Integration Tests (`pytest`):**
   - Kiểm tra thuật toán `TieredPositionManager` qua đủ các kịch bản giá:
     - Kịch bản 1: Giá tăng lên $+45\%$ (khớp TP1, bán $35\%$, SL dời về hòa vốn) $\rightarrow$ Giá tăng lên $+110\%$ (khớp TP2, bán $30\%$) $\rightarrow$ Giá tăng lên $+420\%$ (khớp TP3, bán $15\%$) $\rightarrow$ Giữ $20\%$ Moonbag lên $50\times$.
     - Kịch bản 2: Giá tăng lên $+50\%$ (khớp TP1) rồi quay đầu giảm về điểm hòa vốn $\rightarrow$ Đóng $65\%$ còn lại ở mức hòa vốn, tổng kết lệnh vẫn **LÃI RÒNG $+14\%$** thay vì lỗ!
   - Đảm bảo 100% bài test bảo mật Gateway (143+ tests, không dùng `innerHTML =`) đều PASS.
2. **Triển khai trực tiếp lên Render (`https://aifreegateway.onrender.com`):**
   - Push tự động lên GitHub `main`, kiểm tra trực tiếp các công cụ quét DexScreener/GoPlus thật và bảng vị thế Moonbag trên Cloud.
