# Hướng dẫn Deploy Toàn bộ Hệ sinh thái 3 Dự án (All-in-One) Lên Hugging Face Spaces 24/7

Tài liệu này hướng dẫn bạn đưa **toàn bộ cụm 3 dự án**:
1. **AI Free Gateway & All-in-One Hub** (Web Dashboard + Cấp AI 0đ)
2. **Airdrop Hunter Agent** (Tự động săn Airdrop 0đ, giữ chuỗi Active Days & chống Sybil)
3. **Smart Money Agent** (Săn Meme Token, theo dõi cá mập & nhận 80% vốn Airdrop)

Lên **Hugging Face Spaces** để chạy **24/7 vĩnh viễn và hoàn toàn MIỄN PHÍ 100% (2 vCPU, 16GB RAM)**. Bạn có thể tắt máy tính đi ngủ, bot vẫn tự cày trên Cloud!

---

## 🚀 Bước 1: Tạo Space Mới Trên Hugging Face (Chỉ 1 phút)

1. Đăng nhập hoặc tạo tài khoản miễn phí tại [huggingface.co](https://huggingface.co).
2. Nhấn vào ảnh đại diện góc trên bên phải ➔ Chọn **New Space** (hoặc truy cập trực tiếp: [huggingface.co/new-space](https://huggingface.co/new-space)).
3. Điền các trường:
   - **Space name**: `ai-agent-hub` (hoặc tên tùy ý bạn thích, ví dụ `phong-crypto-hub`).
   - **License**: `mit` hoặc `apache-2.0`.
   - **Select the Space SDK**: Chọn **Docker** ➔ Chọn **Blank**.
   - **Space hardware**: Giữ nguyên `Free (2 vCPU, 16GB RAM)`.
   - **Visibility**: Chọn **Public** (để vào Web UI bằng điện thoại và nhận webhook on-chain).
4. Nhấn **Create Space**.

---

## 🔗 Bước 2: Đẩy Mã Nguồn Lên Space

Có 2 cách cực kỳ nhanh:

### Cách A: Dùng GitHub Sync (Khuyên dùng nhất - Tự động cập nhật mỗi khi commit)
Vì repo của bạn đã có trên GitHub: `https://github.com/PhongNguyen618/ai-free-gateway`
1. Trong Hugging Face Space vừa tạo ➔ Nhấn vào tab **Settings** (Bánh răng cưa).
2. Cuộn xuống phần **GitHub repository**:
   - Chọn **Connect GitHub repository**.
   - Chọn repo `PhongNguyen618/ai-free-gateway` và nhánh `main`.
3. Nhấn **Save**. Hugging Face sẽ tự động kéo toàn bộ mã nguồn về, build Docker và khởi động cả 3 con bot!

---

### Cách B: Đẩy trực tiếp bằng Git từ máy tính của bạn
Sau khi tạo Space, Hugging Face sẽ cấp link Git dạng: `https://huggingface.co/spaces/<USERNAME>/<SPACE_NAME>`

Mở PowerShell tại máy tính:
```powershell
cd C:\Users\PC\Desktop\ai-free-gateway

# 1. Thêm remote của Hugging Face
git remote add hf https://huggingface.co/spaces/<TÊN_USER_CỦA_BẠN>/<TÊN_SPACE>

# 2. Đẩy lên Hugging Face
git push hf main:main
```
*(Khi được hỏi mật khẩu, nhập Access Token Hugging Face quyền `Write` tại: [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens))*.

---

## 🔑 Bước 3: Cấu hình Khóa Bí Mật (Variables and Secrets)

Trong Hugging Face Space ➔ tab **Settings** ➔ mục **Variables and secrets**:
- Nhấn **New secret** để thêm các khóa AI miễn phí (nếu muốn mở rộng nhiều model):
  - `GROQ_API_KEY`: Lấy tại `console.groq.com` (tốc độ siêu nhanh 800 tokens/giây).
  - `OPENROUTER_API_KEY`: Lấy tại `openrouter.ai`.
  - `GEMINI_API_KEY`: Lấy tại `aistudio.google.com`.
  - `GATEWAY_API_KEY`: (Tùy chọn) Đặt mật khẩu khóa nếu bạn muốn bảo mật Hub riêng tư.

---

## 🎉 Bước 4: Tận Hưởng Thành Quả!

Sau khi quá trình Build hoàn tất (khoảng 1-2 phút) và hiện chữ **Running** màu xanh lá cây:
1. Bạn sẽ có một đường link Web vĩnh viễn dạng:
   ```
   https://<TÊN_USER>-<TÊN_SPACE>.hf.space
   ```
2. Mở đường link này trên **Điện thoại** hoặc **Máy tính** bất cứ lúc nào:
   - **Tab Chat AI**: Cấp AI 0đ với mọi mô hình hàng đầu.
   - **Tab 🤖 Agent Hub**: Thấy cả **Bot Săn Airdrop** và **Bot Săn Cá Mập** đang chạy Online song song 24/7!
   - Bạn có thể bấm nút ra lệnh cày streak, quét DeFiLlama, hoặc xem ví cá mập gom hàng ngay từ trình duyệt điện thoại khi đang đi ngoài đường!
