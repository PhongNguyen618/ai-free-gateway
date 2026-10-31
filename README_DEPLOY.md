# Hướng dẫn Deploy AI Free Gateway 24/7 lên Cloud

Tài liệu này hướng dẫn bạn đưa **AI Free Gateway** lên cloud để chạy **24/7 vĩnh viễn và hoàn toàn miễn phí**, giúp mọi dự án khác (`smart-money-agent`, app web, chatbot, script Python, VS Code,...) có thể gọi đến dùng bất cứ lúc nào mà không cần mở máy tính cá nhân.

---

## Cách 1: Deploy lên Hugging Face Spaces (Khuyên dùng nhất - 24/7 Miễn phí)

Hugging Face Spaces cung cấp hạ tầng Docker miễn phí với **2 vCPU và 16GB RAM**, không bị giới hạn timeout 10-15s như Vercel.

### Bước 1: Tạo Space trên Hugging Face
1. Đăng nhập hoặc tạo tài khoản miễn phí tại [huggingface.co](https://huggingface.co).
2. Nhấn vào ảnh đại diện góc trên bên phải ➔ Chọn **New Space** (hoặc truy cập [huggingface.co/new-space](https://huggingface.co/new-space)).
3. Điền thông tin:
   - **Space name**: `ai-free-gateway` (hoặc tên tùy ý bạn thích).
   - **License**: `mit` hoặc `apache-2.0`.
   - **Select the Space SDK**: Chọn **Docker** ➔ Chọn **Blank**.
   - **Space hardware**: Giữ nguyên `Free (2 vCPU, 16GB RAM)`.
   - **Visibility**: Chọn **Public** (để các app bên ngoài gọi vào được).
4. Nhấn **Create Space**.

### Bước 2: Đẩy mã nguồn lên Space
Có 2 cách đơn giản:

#### Cách A: Dùng Git (Nhanh nhất)
Sau khi tạo Space, Hugging Face sẽ hiển thị URL clone git của Space dạng:
`https://huggingface.co/spaces/<username>/<space-name>`

Tại thư mục `C:\Users\PC\Desktop\ai-free-gateway`, mở PowerShell chạy:
```powershell
# 1. Thêm remote của Hugging Face Space
git remote add hf https://huggingface.co/spaces/<YOUR_USERNAME>/<SPACE_NAME>

# 2. Push mã nguồn lên Hugging Face
git push hf main:main
```
*(Hệ thống sẽ hỏi Username và Access Token Hugging Face của bạn. Bạn lấy token tại: [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) chọn quyền `Write`)*.

#### Cách B: Dùng GitHub Sync (Tự động cập nhật)
Nếu bạn đã có repo GitHub (`https://github.com/PhongNguyen618/ai-free-gateway`):
1. Trong Hugging Face Space ➔ Chọn tab **Settings**.
2. Chọn **Connect GitHub repository** ➔ Chọn repo `PhongNguyen618/ai-free-gateway`.
3. Mỗi lần bạn `git push` lên GitHub, Hugging Face sẽ tự động build và deploy lại phiên bản mới nhất.

### Bước 3: Cấu hình API Keys (Tùy chọn)
Trong Space của bạn trên Hugging Face:
- Vào tab **Settings** ➔ Mục **Variables and secrets**.
- Thêm các Secret (khóa bí mật) nếu muốn bật thêm các nhà cung cấp:
  - `GROQ_API_KEY`: Khóa Groq miễn phí (lấy tại `console.groq.com`).
  - `OPENROUTER_API_KEY`: Khóa OpenRouter miễn phí (lấy tại `openrouter.ai`).
  - `GEMINI_API_KEY`: Khóa Google Gemini (lấy tại `aistudio.google.com`).
  - `GATEWAY_API_KEY`: *(Tùy chọn)* Nếu bạn muốn đặt mật khẩu khóa chỉ riêng bạn mới được gọi gateway này.

### Bước 4: Lấy URL Endpoint
Sau khi build hoàn tất (trạng thái chuyển sang màu xanh **Running**):
- Nhấn vào biểu tượng dấu 3 chấm góc trên bên phải của Space ➔ Chọn **Embed this Space** hoặc nhìn vào URL trực tiếp.
- Endpoint OpenAI tương thích toàn cầu của bạn sẽ có dạng:
  ```
  https://<YOUR_USERNAME>-<SPACE_NAME>.hf.space/v1
  ```
  *(Ví dụ: `https://phongnguyen-ai-free-gateway.hf.space/v1`)*

---

## Cách 2: Deploy lên Render.com (Hoặc Koyeb / Railway)

Nếu bạn quen dùng các nền tảng PaaS:
1. Đăng nhập [render.com](https://render.com) ➔ Chọn **New Web Service**.
2. Kết nối repo GitHub `PhongNguyen618/ai-free-gateway`.
3. Chọn Runtime: **Docker**.
4. Instance Type: **Free**.
5. Nhấn **Deploy**.
6. Render sẽ cung cấp URL dạng: `https://ai-free-gateway.onrender.com/v1`.
*(Lưu ý: Render Free sẽ sleep sau 15 phút không dùng. Để giữ thức 24/7, bạn đăng ký một cron ping miễn phí tại `cron-job.org` để gọi vào `https://ai-free-gateway.onrender.com/health` 10 phút/lần)*.

---

## Cách dùng: Bất kỳ dự án nào cũng gọi được

Sau khi deploy xong, bạn chỉ cần cấu hình ở các dự án khác như sau:

### 1. Cho dự án `smart-money-agent`
Trong file `config/.env` của `smart-money-agent`:
```env
AI_GATEWAY_URL=https://<YOUR_USERNAME>-<SPACE_NAME>.hf.space/v1
LLM_MODEL=auto
```

### 2. Cho dự án Python bất kỳ (Thư viện `openai` chuẩn)
```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<YOUR_USERNAME>-<SPACE_NAME>.hf.space/v1",
    api_key="free", # hoặc GATEWAY_API_KEY của bạn nếu có cài đặt
)

response = client.chat.completions.create(
    model="auto",  # hoặc "gpt-4o-mini", "llama-3.3-70b-versatile", "gemini-2.5-flash"
    messages=[
        {"role": "user", "content": "Xin chào! Bạn là AI nào thế?"}
    ]
)

print(response.choices[0].message.content)
```

### 3. Cho lệnh cURL / Postman / Terminal
```bash
curl https://<YOUR_USERNAME>-<SPACE_NAME>.hf.space/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer free" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### 4. Cho VS Code (Continue / Cline / Roo Code)
- **Provider**: `OpenAI-Compatible`
- **Base URL**: `https://<YOUR_USERNAME>-<SPACE_NAME>.hf.space/v1`
- **API Key**: `free`
- **Model**: `auto`
