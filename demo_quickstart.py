"""
Ví dụ mẫu nhanh (Quickstart Demo) - Dễ hiểu, chạy được ngay!
Cách dùng AI Free Gateway với SDK trong các dự án của bạn.
"""

from sdk.agent_sdk import UniversalAIClient, BatchAIAgent, ReActToolAgent


def main():
    print("=" * 60)
    print("HƯỚNG DẪN DÙNG AI FREE GATEWAY - CỰC KỲ ĐƠN GIẢN")
    print("=" * 60)

    # 1. Khởi tạo Client (Kết nối tới Gateway AI Free của bạn)
    # Lưu ý: Đảm bảo bạn đã bật Gateway bằng lệnh: python run.py
    client = UniversalAIClient(
        base_url="http://127.0.0.1:8000/v1",
        project_id="my-first-app",   # Tên dự án của bạn
        priority="normal",          # Mức ưu tiên: critical, normal, background
    )

    # -------------------------------------------------------------
    # VÍ DỤ 1: Nhận kết quả dạng JSON chuẩn 100% (Không sợ lỗi parse)
    # -------------------------------------------------------------
    print("\n[VÍ DỤ 1] Yêu cầu AI phân tích dữ liệu trả về JSON:")
    try:
        data = client.ask_json(
            prompt="Chấm điểm tiềm năng bài viết: 'Công nghệ AI Agent mã nguồn mở bùng nổ năm 2026'",
            model="fast",  # Tự động chọn mô hình nhanh nhất (Gemini Flash, Groq...)
            schema_hint='{"score": int (1-10), "sentiment": "Tích cực"|"Tiêu cực", "summary": str}',
        )
        print("Kết quả JSON nhận được trực tiếp vào code:")
        print(f"- Điểm số: {data.get('score')}")
        print(f"- Cảm xúc: {data.get('sentiment')}")
        print(f"- Tóm tắt: {data.get('summary')}")
    except Exception as e:
        print(f"Lưu ý: Cần bật gateway trước khi chạy (python run.py). Chi tiết lỗi: {e}")

    # -------------------------------------------------------------
    # VÍ DỤ 2: Gom lô (Batching) để KHÔNG BAO GIỜ bị hết lượt (Rate Limit)
    # -------------------------------------------------------------
    print("\n[VÍ DỤ 2] Gom 6 tiêu đề tin tức gửi 1 lần (tiết kiệm quota):")
    news_titles = [
        "Bitcoin vượt đỉnh 100,000 USD",
        "Fed cân nhắc giảm lãi suất vào tháng tới",
        "Sàn giao dịch X bị điều tra bảo mật",
        "Solana ra mắt bản cập nhật tăng tốc độ",
        "Một dự án DeFi bị tấn công flash loan",
        "Vốn đầu tư mạo hiểm đổ mạnh vào AI Agent",
    ]

    batch_agent = BatchAIAgent(client=client, default_batch_size=3)
    # Thay vì gọi 6 lần tốn 6 lượt request, agent sẽ tự chia thành 2 mẻ (mỗi mẻ 3 tin)
    print(f"Đang gom {len(news_titles)} tin thành từng mẻ 3 tin để xử lý...")
    # results = batch_agent.process_batch(
    #     items=news_titles,
    #     task_instruction="Phân loại tin thành 'Tốt' hoặc 'Xấu' cho thị trường",
    #     model="fast"
    # )
    # print("Kết quả:", results)

    # -------------------------------------------------------------
    # VÍ DỤ 3: AI Agent tự động suy nghĩ và gọi Công cụ (Tool)
    # -------------------------------------------------------------
    print("\n[VÍ DỤ 3] Tạo AI Agent tự biết tra cứu số dư và giá coin:")

    # Định nghĩa các hàm Python bình thường trong dự án của bạn:
    def tra_cuu_gia(coin: str) -> str:
        """Tra cứu giá của một đồng coin (ví dụ: BTC, ETH, SOL)."""
        coin_clean = coin.strip().upper()
        prices = {"BTC": "$98,000", "ETH": "$3,400", "SOL": "$195"}
        return prices.get(coin_clean, "Không tìm thấy giá")

    def xem_so_du_vi(loai_tien: str) -> str:
        """Xem số dư khả dụng trong ví."""
        return "1,500 USDT khả dụng"

    # Gắn các hàm trên vào Agent:
    tools = {
        "tra_cuu_gia": tra_cuu_gia,
        "xem_so_du_vi": xem_so_du_vi,
    }

    agent = ReActToolAgent(
        client=client,
        tools=tools,
        model="reasoning",  # Tự động chọn mô hình tư duy logic (DeepSeek R1...)
        max_steps=3,
    )

    print("Agent đã sẵn sàng nhận câu hỏi phức tạp:")
    cau_hoi = "Tôi đang có bao nhiêu USDT trong ví và giá đồng SOL hiện tại là bao nhiêu? Tôi có đủ tiền mua 5 SOL không?"
    print(f"Câu hỏi: {cau_hoi}")
    # decision = agent.run(cau_hoi)
    # print("\nCâu trả lời cuối cùng của Agent:")
    # print(decision["final_answer"])


if __name__ == "__main__":
    main()
