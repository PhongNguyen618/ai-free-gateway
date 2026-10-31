# STRICT LIVE V2 — ĐẶC TẢ SỬA TOÀN BỘ CHỖ CÒN GIẢ LẬP

- **Phiên bản:** `5.0.0-strict-live-v2`
- **Ngày kiểm toán:** 2026-10-31 (sau commit `d398aad`)
- **Nguyên tắc bắt buộc:**
  1. **Không bao giờ trả `success=True` khi chưa có tx hash thật VÀ receipt `status == 1` (EVM) / `confirmed` (Solana).**
  2. **Không bao giờ trả số liệu hardcode khi API/RPC lỗi.** Lỗi thì trả `live_verified=False` + mã lỗi, và **chặn giao dịch** (fail-closed).
  3. **Bảo mật thiếu dữ liệu = KHÔNG AN TOÀN.** GoPlus lỗi → coi như token rủi ro, không mua.
  4. Số dư/khối lượng/giá vào lệnh phải lấy từ **on-chain thật** (receipt/balance), không tính toán ước lượng.

---

## 1. KẾT QUẢ KIỂM TOÁN (sau commit `d398aad`)

### 🔴 Mức NGHIÊM TRỌNG — ảnh hưởng trực tiếp tới tiền thật

| # | File | Hiện trạng giả lập | Rủi ro |
|---|---|---|---|
| **C1** | [`dex_executor.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/smart_money/dex_executor.py) `execute_sell_pct` | **Trả `success: True` mà KHÔNG gửi lệnh bán nào lên chuỗi.** | Bot báo "đã chốt lời" nhưng token vẫn nằm trong ví → báo cáo lãi ảo. |
| **C2** | [`position_manager.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/smart_money/position_manager.py) `update_price` | TP1/TP2/TP3/Moonbag/Stop-Loss/Rug-exit **chỉ ghi SQLite, không gọi DEX bán**. | Gặp rug pull hoặc chạm SL, bot **không bán thật** → mất vốn. |
| **C3** | [`live_scanner.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/smart_money/live_scanner.py) `check_goplus_security` | Khi GoPlus lỗi trả **`is_safe: True, score: 85, is_honeypot: False`**. | Có thể **mua phải honeypot** khi GoPlus bị rate-limit. |
| **C4** | [`live_scanner.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/smart_money/live_scanner.py) `scan_dexscreener_trending` | Khi DexScreener lỗi trả **danh sách BRETT/DEGEN giá hardcode** (`0.085`, `0.0078`). | Mở lệnh / tính TP theo giá cũ không có thật. |
| **C5** | [`dex_executor.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/smart_money/dex_executor.py) `execute_buy` | **Bỏ qua `amount_usd`**, luôn swap cố định `0.0002 ETH`; chain `solana` vẫn bị gửi qua 0x (sai). | Số vốn trên DB ≠ số vốn thật; Solana không giao dịch được. |
| **C6** | [`dex_executor.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/smart_money/dex_executor.py) approve | Phát hiện cần `approve` nhưng **chỉ log, không gửi tx approve**. | Mọi lệnh bán ERC-20 sẽ revert. |
| **C7** | [`position_manager.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/smart_money/position_manager.py) `open_position` | `initial_quantity = invested_usd / entry_price` (ước lượng), không đọc lượng token thật nhận được. Khi `ENABLE_LIVE_EXECUTION=false` vẫn mở vị thế ảo dù gọi `execute_onchain=True`. | PnL và lượng bán TP sai lệch so với ví. |
| **C8** | [`hub_bridge.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/smart_money/hub_bridge.py) `tool_open_tiered_position` | Không có giá → **dùng `0.05` cố định**; và **không truyền `execute_onchain=True`**. | Nút "Mở vị thế Moonbag" trên UI chỉ tạo vị thế giấy. |

### 🟠 Mức CAO — dữ liệu giả hiển thị cho người dùng

| # | File | Hiện trạng giả lập |
|---|---|---|
| **H1** | [`rpc_client.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/airdrop_hunter/src/executor/rpc_client.py) | RPC lỗi → trả **balance `0.05`, nonce `7`, gas `0.02`** giả; Solana luôn nonce `12`. |
| **H2** | [`airdrop_hunter/src/hub_bridge.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/airdrop_hunter/src/hub_bridge.py) | Ví trống → tự chèn **2 ví demo** `0x71c6…0123`, `0x89d2…0456` (không ai sở hữu key) vào DB và chạy farming trên chúng. |
| **H3** | [`airdrop_hunter/main.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/airdrop_hunter/main.py) `run_daemon_loop` | Tự seed 3 ví mẫu (cùng 2 ví demo trên). |
| **H4** | [`farming_executor.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/airdrop_hunter/src/executor/farming_executor.py) `execute_faucet_claim` | Ghi **`SUCCESS` + cộng 10 điểm** mà không có giao dịch nào. |
| **H5** | [`farming_executor.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/airdrop_hunter/src/executor/farming_executor.py) farming nói chung | Tất cả ví farming đều gọi `BaseWethSigner` ký bằng **một `EVM_PRIVATE_KEY` duy nhất** → `wallet_address` hiển thị khác với ví thực ký tx. Daily check-in unwrap `0.00005 WETH` sẽ revert nếu ví chưa có WETH. |
| **H6** | [`reward_harvester.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/airdrop_hunter/src/executor/reward_harvester.py) `evaluate_wallet_eligibility` | Vẫn dùng công thức `120 + days*15.5 + points*0.25` USD → **phần thưởng ảo**; `route_capital_to_smart_money` chuyển "vốn" ảo sang bot meme. |
| **H7** | [`airdrop_radar.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/airdrop_hunter/src/radar/airdrop_radar.py) `SEED_CAMPAIGNS` | Danh sách campaign tĩnh được trộn vào kết quả scan như dữ liệu live. |
| **H8** | [`ecosystem/smart_money/core/hub_bridge.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/smart_money/core/hub_bridge.py) | **File bản cũ trùng lặp** vẫn còn `RUNTIME_PROFILE=PAPER`, tín hiệu mock `sig_sol_01`, điểm giả. |
| **H9** | [`live_scanner.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/smart_money/live_scanner.py) + `analyze_token_risk` | Thiếu kiểm tra Solana (`freeze_authority`, `mint_authority`) — chỉ có EVM. |

### 🟡 Mức TRUNG BÌNH — kỹ thuật

| # | Vấn đề |
|---|---|
| **M1** | `dex_executor` vòng chờ receipt dùng `time.sleep` trong hàm async → chặn event loop của Gateway. |
| **M2** | `farming_executor.solve_quiz_with_ai` AI lỗi → **chọn đáp án 0** và báo như thật. |
| **M3** | `onchain_signer._get_gas_price_gwei` RPC lỗi → trả `0.006` gwei giả rồi vẫn ký tx. |
| **M4** | Docstring `onchain_signer.py` vẫn ghi "Dual Mode ... safe simulation"; comment `config.py` vẫn nhắc `SIMULATION`. |
| **M5** | UI bảng ví hiển thị badge `FALLBACK` thay vì báo lỗi rõ ràng. |
| **M6** | Không có giới hạn rủi ro khi chạy thật: chưa có `MAX_LIVE_ORDER_USD`, `MAX_LIVE_EXPOSURE_USD`, `LIVE_PAUSE_BUYS`, kill-switch, chống mua trùng (idempotency). |

---

## 2. THIẾT KẾ SỬA

### Module V1 — Fail-Closed Data Layer (sửa C3, C4, H1, H9, M3)

- `check_goplus_security`: lỗi/timeout/không có dữ liệu → `{"status": "UNVERIFIED", "is_safe": False, "score": 0, "live_verified": False}`. Có retry 2 lần (backoff 1s, 3s).
- Thêm kiểm tra Solana: `mintable.status`, `freezable.status`, `transfer_fee` từ GoPlus Solana API → có cờ là `is_safe=False`.
- `scan_dexscreener_trending`: **xóa danh sách hardcode**; lỗi → `[]` + `live_verified=False`.
- `rpc_client`: đổi chữ ký trả `Tuple[Optional[float], bool]`; lỗi → `(None, False)`. UI hiển thị `"RPC LỖI"`, không hiển thị số giả.
- `_get_gas_price_gwei`: lỗi → raise `RpcUnavailable`; `execute_weth_cycle` trả `RPC_UNAVAILABLE`, **không ký tx**.

### Module V2 — DEX Engine hoàn chỉnh (sửa C1, C5, C6, M1)

File: [`dex_executor.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/smart_money/dex_executor.py)

1. **`execute_buy(chain, token, amount_usd)`**
   - Lấy giá native thật (DexScreener cặp WETH/WSOL) → `sell_amount = amount_usd / native_price`.
   - Kiểm tra `amount_usd <= MAX_LIVE_ORDER_USD` và tổng exposure `<= MAX_LIVE_EXPOSURE_USD`; `LIVE_PAUSE_BUYS=true` → chặn mua.
   - Giữ lại gas reserve (EVM `0.0003 ETH`, Solana `0.01 SOL`).
   - Đọc `token_balance` **trước & sau** swap → `received_qty` thật (raw units / `decimals()`).
   - Trả về `{success, tx_hash, received_qty, spent_native, effective_price_usd, explorer_url}`.
2. **`execute_sell_pct(chain, token, pct)`**
   - Đọc `balanceOf` thật × `pct` → `sell_amount` raw units.
   - EVM: quote 0x (`sellToken=token`, `buyToken=0xEeee…` native) → nếu `issues.allowance` có `spender` thì **ký & gửi `approve(spender, sell_amount)`**, chờ receipt `0x1`, rồi mới swap.
   - Solana: Jupiter `quote` → `swap` → ký `VersionedTransaction` bằng `solders` → `sendTransaction` → poll `getSignatureStatuses` tới `confirmed`.
   - Đọc native balance trước/sau → `received_native` thật.
   - **Chỉ `success=True` khi receipt thành công.**
3. **Solana buy** qua Jupiter (`inputMint = So111…112`), header `x-api-key: JUPITER_API_KEY`.
4. Toàn bộ chờ dùng `await asyncio.sleep()`; timeout receipt 60s. Hết timeout → `status: "PENDING_UNKNOWN"`, lưu hash vào journal, **không gửi lại tx mới**.
5. **Journal ghi trước (write-ahead)**: bảng `live_tx_journal(id, position_id, side, chain, tx_hash, status, created_at)`; lần chạy sau tiếp tục kiểm tra hash `PENDING` thay vì gửi lại.
6. Slippage mặc định 150 bps, meme tối đa 500 bps (`MAX_SLIPPAGE_BPS`).

### Module V3 — Position Manager khớp lệnh thật (sửa C2, C7, C8)

- `open_position(..., execute_onchain=True)` là **mặc định khi `ENABLE_LIVE_EXECUTION=true`**.
  - `ENABLE_LIVE_EXECUTION=false` → **từ chối mở vị thế** với lỗi `LIVE_EXECUTION_DISABLED` (không còn vị thế giấy). Chỉ cho phép vị thế giấy nếu đặt rõ `PAPER_MODE=true` và gắn nhãn `mode="PAPER"` trên DB/UI/Telegram.
  - `initial_quantity = received_qty`, `entry_price = effective_price_usd`, `invested_usd = spent_native × native_price`.
  - Thêm cột `mode`, `entry_tx_hash`, `exit_tx_hashes (JSON)`.
- `update_price`: tách **quyết định** và **thực thi**:
  1. Tính trigger (TP1 35% / TP2 30% / TP3 15% / Moonbag 10x, 25x, 50x / SL / Rug).
  2. Gọi `execute_sell_pct` với tỷ lệ **so với số dư hiện tại** (vd TP1: 35% của ban đầu = 35% số dư; TP2: 30/65 ≈ 46.15% số dư; TP3: 15/35 ≈ 42.86%…).
  3. **Chỉ khi bán thành công** mới cập nhật `remaining_quantity`, `realized_usd = received_native × native_price`, cờ `tpX_hit`, dời SL, và bắn Telegram kèm link BaseScan/Solscan.
  4. Bán thất bại → giữ nguyên trạng thái, ghi event `SELL_FAILED`, Telegram cảnh báo đỏ, thử lại ở chu kỳ sau (tối đa 3 lần, sau đó `NEEDS_MANUAL`).
- `tool_open_tiered_position`: không có giá thật → trả lỗi `PRICE_UNAVAILABLE`, **xóa fallback `0.05`**; truyền `execute_onchain=True`.
- `close_position_manual`: bán 100% số dư thật.

### Module V4 — Airdrop thật (sửa H2–H7, M2)

- **Xóa ví demo** ở `hub_bridge.py` và `main.py`. Ví trống → trả `NO_WALLETS_CONFIGURED`.
- **Ví farming = ví suy ra từ key thật.** Hỗ trợ nhiều key: `EVM_PRIVATE_KEYS=0xkey1,0xkey2,...` (giữ tương thích `EVM_PRIVATE_KEY`). Lúc khởi động tự `upsert_wallet(Account.from_key(k).address)`. `BaseWethSigner` nhận `wallet_address` → tra key tương ứng; không có key cho ví đó → `NO_KEY_FOR_WALLET`, không ký bằng key khác.
- **Daily check-in**: đọc WETH thật; có WETH → unwrap; không có → wrap số nhỏ (jitter). Không bao giờ unwrap khi WETH = 0.
- **Faucet claim**: không có hợp đồng faucet thật đã cấu hình → trả `NOT_SUPPORTED`, **không cộng điểm, không ghi SUCCESS**.
- **Điểm/active days chỉ cộng khi tx thật thành công.**
- **Reward harvester**: xóa công thức USD ước lượng. `evaluate_wallet_eligibility` chỉ trả tiêu chí hoạt động (`active_days`, `tx_count` thật từ `eth_getTransactionCount`, volume) và `estimated_value_usd = None`. Phần thưởng chỉ ghi nhận khi phát hiện **số dư token thật tăng** (theo danh sách token của campaign) qua `balanceOf`. `route_capital_to_smart_money` chỉ chuyển số đã xác minh on-chain.
- **Radar**: `SEED_CAMPAIGNS` gắn nhãn `source="CURATED_STATIC"` riêng, không trộn với `source="DEFILLAMA_LIVE"`; UI hiển thị rõ nguồn.
- **Quiz AI** lỗi → trả `AI_UNAVAILABLE`, không tự chọn đáp án 0.

### Module V5 — Dọn dẹp & Kiểm soát rủi ro (sửa H8, M4, M5, M6)

- **Xóa** [`ecosystem/smart_money/core/hub_bridge.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/ecosystem/smart_money/core/hub_bridge.py) (bản cũ trùng lặp, sau khi xác nhận không còn nơi nào import).
- Sửa docstring/comment còn nhắc "simulation".
- UI: badge `FALLBACK` → `RPC LỖI` (đỏ); cột mới **Tx Hash** link BaseScan/Solscan trong bảng vị thế; cột **Mode** (`LIVE`/`PAPER`).
- Biến môi trường quản trị rủi ro (mặc định an toàn):

  | Biến | Mặc định | Ý nghĩa |
  |---|---|---|
  | `ENABLE_LIVE_EXECUTION` | `false` | Công tắc tổng |
  | `MAX_LIVE_ORDER_USD` | `2` | Tối đa mỗi lệnh mua |
  | `MAX_LIVE_EXPOSURE_USD` | `10` | Tổng vốn đang mở |
  | `LIVE_PAUSE_BUYS` | `false` | Chỉ cho bán (chốt lời/SL), cấm mua mới |
  | `MAX_SLIPPAGE_BPS` | `500` | Trượt giá tối đa |
  | `MAX_DAILY_GAS_USD` | `0.5` | Trần gas/ngày cho farming |
  | `EVM_PRIVATE_KEYS` | — | Nhiều key ví farming (phân tách dấu phẩy) |

- **Kill-switch**: endpoint `POST /api/hub/live/pause` (bật `LIVE_PAUSE_BUYS` lúc chạy) và `POST /api/hub/live/panic-sell` (bán toàn bộ vị thế mở).
- **Chống mua trùng**: khóa theo `(chain, token)` — đã có vị thế mở thì từ chối mở thêm.

---

## 3. KẾ HOẠCH KIỂM THỬ

File mới `tests/test_strict_live_v2.py` (dùng `unittest.mock` để giả lập **phản hồi RPC/API trong test**, code sản phẩm không có nhánh giả):

1. GoPlus lỗi → `is_safe False`, `get_safe_trending_gems` trả `[]`.
2. DexScreener lỗi → `[]`, không có BRETT/DEGEN hardcode.
3. RPC lỗi → `get_native_balance` trả `(None, False)`.
4. `execute_sell_pct` khi receipt `status=0x0` → `success False`; khi `0x1` → `success True` có `tx_hash`.
5. Có `issues.allowance` → tx approve được gửi **trước** tx swap.
6. `update_price` chạm TP1 mà bán thất bại → `tp1_hit False`, `remaining_quantity` không đổi.
7. `update_price` chạm TP1 bán thành công → lượng bán = 35% số dư, `realized_usd` theo `received_native`.
8. `ENABLE_LIVE_EXECUTION=false` → `open_position` bị từ chối.
9. Vượt `MAX_LIVE_ORDER_USD` → từ chối mua.
10. Farming không có ví → `NO_WALLETS_CONFIGURED`; faucet → `NOT_SUPPORTED`, không cộng điểm.
11. Grep tĩnh: không còn chuỗi `0x71c6bf00…`, `SIMULATION_VERIFIED`, `FALLBACK_ASSESSMENT`, `"price_usd": 0.085`, `return 0.05, False`.
12. Toàn bộ test cũ (155) vẫn PASS (cập nhật các test phụ thuộc dữ liệu hardcode).

## 4. TRIỂN KHAI

1. Code V1 → V5, chạy `pytest`.
2. Commit & push `main` → Render tự build.
3. Kiểm tra `GET /api/hub/wallets/audit` trên Render để xác nhận địa chỉ ví & số dư thật.
4. Chạy thử **1 lệnh mua \$1 + bán 100%** trên Base với ví phụ, đối chiếu tx trên BaseScan trước khi bật bot tự động.
