# AI Free Gateway — Đặc Tả Kỹ Thuật Nâng Cấp: Chấm Dứt Vòng Lặp Tool Calling & Xử Lý Yêu Cầu Agent (ANTI_LOOP_UPGRADE_SPEC)

**Ngày lập:** 2026-10-30  
**Trạng thái:** Sẵn sàng triển khai (Actionable & Self-Contained)  
**Đối tượng thực thi:** Bất kỳ AI Coding Agent hoặc Kỹ sư phần mềm nào.  
**Mục tiêu cốt lõi:** Xử lý triệt để hiện tượng **AI Agent (Cline / Roo Code / Cursor / Continue / OpenAI SDK) bị kẹt vòng lặp vô tận (infinite loop)**, gọi lại một tool nhiều lần, hoặc báo lỗi không xử lý được yêu cầu khi đi qua AI Free Gateway.

---

## 1. CHẨN ĐOÁN NGUYÊN NHÂN GỐC RỄ (TẠI SAO AGENT BỊ VÒNG LẶP?)

Qua phân tích trực tiếp mã nguồn hiện tại (`src/router/openai_proxy.py`, `src/router/balancer.py`, `src/security/sanitizer.py`, `config/settings.yaml`, `ui/index.html`) và dữ liệu thực tế trong `storage/gateway.db`, có **7 nguyên nhân kỹ thuật trực tiếp** khiến AI Agent bị lặp vô tận khi gọi qua Gateway:

### Nguyên nhân 1 (Nghiêm trọng nhất — P0): Đứt gãy giao thức Streaming `tool_calls` trong `_handle_streaming_proxy`
- **Vị trí lỗi:** [`src/router/openai_proxy.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/openai_proxy.py#L1424-L1498)
- **Cơ chế gây lỗi:**
  1. Ở chunk đầu tiên của tool call, upstream gửi:
     `{"index": 0, "id": "call_abc123", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":"}}`
  2. Tại dòng 1435–1443, Gateway gom `arguments` vào `tool_args_buffer` và gán `fn["arguments"] = ""`, rồi phát chunk đầu tiên đi (có `id` và `name`, nhưng `arguments` rỗng).
  3. Tại các chunk tiếp theo, upstream chỉ gửi phần tiếp của `arguments`:
     `{"index": 0, "function": {"arguments": "\"src/main.py\"}"}}`
     Gateway lại xóa `fn["arguments"] = ""` và **vẫn phát các chunk rỗng `{"index": 0, "function": {"arguments": ""}}` xuống client!**
  4. Đến khi gặp `finish_reason: "tool_calls"` (dòng 1446–1498), Gateway phát một chunk `flush_chunk` chỉ có:
     `{"index": 0, "function": {"arguments": "{\"path\": \"src/main.py\"}"}}` — **nhưng chunk này KHÔNG CÓ `id`, KHÔNG CÓ `type: "function"`, KHÔNG CÓ `function.name`**, và các chunk rỗng trước đó đã làm hỏng bộ ghép (accumulator) của nhiều SDK/Agent.
  5. Tệ hơn: Nếu upstream gửi `tool_calls` hoàn chỉnh trong cùng một chunk có `finish_reason="tool_calls"`, Gateway emit `flush_chunk` (không có `id`/`name`) **TRƯỚC** `chunk_obj` (có `id`/`name` nhưng `arguments` đã bị xóa thành `""` ở dòng 1443!). Kết quả là Agent nhận được `arguments` trước khi biết `id` và `function.name`, hoặc `chunk_obj` đè `arguments` về `""`!
- **Hậu quả:** Agent (như Cline) nhận được một tool call bị thiếu `name`, thiếu `id`, hoặc `arguments = ""` (rỗng). Agent báo lỗi *"Missing tool parameters / Invalid tool call"* về lại cho LLM -> LLM lại cố gọi lại đúng tool đó -> **Vòng lặp vô tận (Infinite Loop)!**

### Nguyên nhân 2 (P0): `prune_messages_for_groq` tự ý cắt lịch sử hội thoại và chèn `role: "system"` vào giữa mảng `messages`
- **Vị trí lỗi:** [`src/router/balancer.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/balancer.py#L82-L171) và [`resolve_provider_request`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/balancer.py#L610-L626)
- **Cơ chế gây lỗi:**
  1. System prompt của các Agent như Cline/Roo Code dài khoảng `12,000 – 25,000` ký tự.
  2. `prune_messages_for_groq` đặt trần cứng `max_chars = 15000 - tools_len` (chỉ còn khoảng `6,000 – 10,000` ký tự).
  3. Tại dòng 110–114: Nếu `system_msg` dài hơn `50% max_chars` (tức `> 3,000 - 5,000` ký tự), Gateway **cắt cụt 60% cuối của System Prompt** (`c_text[:int(max_chars * 0.4)]`)! Phần cuối của System Prompt trong Cline/Roo Code chính là nơi định nghĩa **quy tắc định dạng công cụ (Tool Use Rules), thông tin môi trường (cwd, danh sách file) và hướng dẫn dừng (`attempt_completion`)**. Khi bị cắt mất, mô hình bị "mù" quy tắc và lặp vô định!
  4. Tại dòng 165–169: Gateway chèn một message `{"role": "system", "content": "[Previous conversation history compressed...]"}` vào **vị trí thứ 2** (ngay sau system prompt đầu tiên hoặc giữa hội thoại). Nhiều model trên Groq/OpenRouter chỉ chấp nhận duy nhất 1 message `system` ở index `0`, hoặc bị rối loạn ngữ cảnh khi mất các bước suy luận trước đó -> Agent quên mất mình vừa đọc file nào, cứ đọc đi đọc lại cùng một file (**Action Loop**).
  5. Tại dòng 617–622: Ép cứng `max_tokens = min(2048, ...)` trên Groq khiến các phản hồi viết file dài hoặc sinh tool call lớn bị cắt ngang giữa chừng (`finish_reason: "length"`), làm hỏng JSON/XML của Agent -> Agent thử lại liên tục.

### Nguyên nhân 3 (P0): DLP Sanitizer làm biến dạng dữ liệu bên trong `messages` và `tool_calls` của Agent
- **Vị trí lỗi:** [`src/security/sanitizer.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/security/sanitizer.py#L194-L222) và [`src/router/openai_proxy.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/openai_proxy.py#L462-L485)
- **Cơ chế gây lỗi:**
  1. **Rule `CCCD` (`\b(0\d{11})\b`) và `HIGH_ENTROPY`:** Trong quá trình làm việc của Agent (đọc log, git commit hash, ID container, timestamp, số thập phân, chuỗi base64/path dài không có dấu `_`), DLP Sanitizer nhận diện nhầm là `CCCD`, `PHONE`, `INTERNAL_IP` (`127.0.0.1`), hoặc `HIGH_ENTROPY` và thay bằng `<REDACTED_INTERNAL_IP_1>`, `<REDACTED_HIGH_ENTROPY_1>`.
  2. Khi Agent gửi lịch sử hội thoại ở lượt tiếp theo (chứa kết quả tool hoặc lệnh kiểm tra `http://127.0.0.1:8000`), LLM nhìn thấy `<REDACTED_...>` hoặc khi LLM sinh ra lệnh gọi tool mới, token bị lệch hoặc khi khôi phục (`restore_text`) làm thay đổi độ dài/cấu trúc code khiến lệnh `edit_file` / `replace_in_file` của Agent không khớp (`Target not found`) -> Agent lặp lại việc đọc và sửa file vô tận!
  3. **Kiểm tra `inspect_boundary` quá gắt trên schema:** Tại [`src/router/openai_proxy.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/openai_proxy.py#L462-L485), hàm `inspect_boundary` quét toàn bộ dictionary keys của `tools` và toàn bộ giá trị của `stop`, `response_format`. Nếu tên thuộc tính trong JSON Schema của một MCP tool dài `>= 20` ký tự có chữ số và không có 2 dấu gạch dưới (ví dụ: `projectId2026Oct30Config`), `entropy_scan` sẽ coi key đó là `HIGH_ENTROPY` và trả về **HTTP 403 `dlp_violation`** ngay lập tức!

### Nguyên nhân 4 (P1): Định tuyến sai Model (Routing) — Chọn Model không hỗ trợ Tool Calling hoặc đã chết (`error_count` cao)
- **Vị trí lỗi:** [`src/router/balancer.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/balancer.py#L434-L556) và dữ liệu thực tế trong `storage/gateway.db`.
- **Bằng chứng từ Database (`storage/gateway.db`):**
  - `github/Meta-Llama-3.1-70B-Instruct`: `error_count = 23`, `success_count = 0`
  - `github/gpt-4o-mini`: `error_count = 20`, `success_count = 0`
  - `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`: `latency_ms = 4361ms`
- **Cơ chế gây lỗi:**
  1. Trong `evaluate_model_intelligence` và `get_candidate_chain`, một model nằm trong `priority_models` được cộng tới `+560` đến `+640` điểm, cộng thêm `+500` điểm credential, cộng `+250` điểm tên `gpt-4o` -> Tổng điểm `~1,500` điểm.
  2. Trong khi đó, mỗi lần lỗi chỉ bị trừ `errors * 20.0` điểm (`20 lỗi * 20 = 400` điểm) và provider trả lỗi `401/403/400/404` **không hề được đưa vào cooldown trong DB**!
  3. Do đó, ở **mọi request mới**, `github/gpt-4o-mini` và `github/Meta-Llama-3.1-70B-Instruct` (vốn đang lỗi liên tục 100% với `success_count = 0`) vẫn luôn đứng trong Top đầu chuỗi fallback, làm tiêu tốn mất `attempt_count` (trong giới hạn `max_attempts = 3`)!
  4. Hơn nữa, khi request có `tools` (`has_tools = True`), Balancer chưa hề lọc bỏ các model không hỗ trợ native function calling (`tool_calls`). Nếu route sang một model free trên OpenRouter không hỗ trợ `tools`, upstream trả về `400 Bad Request`. Và tại [`src/router/openai_proxy.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/openai_proxy.py#L928-L932) & [`L1034-L1041`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/openai_proxy.py#L1034-L1041):
     ```python
     if resp.status_code in (400, 422):
         break  # DỪNG NGAY LẬP TỨC TOÀN BỘ VÒNG LẶP FALLBACK!
     ```
     Khi candidate #1 trả về `400` (do model đó không hỗ trợ `tools`, `reasoning_effort`, hoặc `stream_options`), Gateway `break` thoát khỏi vòng lặp và trả về **502 Bad Gateway** cho Client mà không thèm thử candidate #2!

### Nguyên nhân 5 (P1): Chuẩn hóa `messages` khi có `tool_calls` không tương thích giữa các Provider (Groq / Gemini / OpenRouter / GitHub)
- **Vị trí lỗi:** [`src/router/openai_proxy.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/openai_proxy.py#L492-L512) và [`src/router/balancer.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/balancer.py#L598-L627)
- **Cơ chế gây lỗi:**
  1. Khi Agent gửi message `role: "assistant"` có `tool_calls`, trường `content` có thể là `None` hoặc `""`. Một số provider (như הגדרות Gemini OpenAI-compatible hoặc một số model trên OpenRouter/GitHub) yêu cầu `content` trong assistant tool_call message phải là `""` hoặc `null` tùy provider, và `tool` message bắt buộc `content` phải là `str` (nếu `content` là `list` các `ContentPart` như Cline hay gửi `[{"type": "text", "text": "..."}]` cho `role: "tool"`, Groq và GitHub Models sẽ trả lỗi `400 Bad Request: tool message content must be a string`!).
  2. Kiểm tra [`ChatMessage`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/openai_proxy.py#L165-L173): `content: Optional[Union[str, List[ContentPart]]] = None`. Khi client gửi `role: "tool"` với `content: [{"type": "text", "text": "result"}]`, Gateway giữ nguyên dạng `list` gửi lên Groq -> Groq trả `400` -> Gateway gặp `400` liền `break` vòng lặp -> trả `502 Bad Gateway`!

### Nguyên nhân 6 (P1): Cơ chế Phát hiện Vòng lặp (Loop Detection & Recovery) ở tầng Gateway chưa tồn tại
- Khi một model miễn phí (nhất là các model nhỏ hoặc bị quantize) trả về đi trả về lại **cùng một tool call với cùng tham số** hoặc **cùng một nội dung văn bản** ở 2–3 lượt liên tiếp trong lịch sử `messages`, Gateway hiện tại vẫn gửi lại vào chính model đó với cùng `temperature` -> Model tiếp tục sinh ra đúng output cũ (Deterministic Repetition Loop).

### Nguyên nhân 7 (P2): Lỗi cú pháp JavaScript trong `ui/index.html`
- **Vị trí lỗi:** [`ui/index.html`](file:///c:/Users/PC/Desktop/ai-free-gateway/ui/index.html#L377-L561)
- **Cơ chế gây lỗi:** Hàm `setApiKey(key)` mở ngoặc `{` ở dòng 377 nhưng chưa đóng `}` mà chứa lồng luôn các hàm `setPlaygroundPrompt`, `clearPlayground`, `pingProviders`, `sendPlaygroundMessage` đến tận dòng 561 mới đóng `}`, và các hàm này lại bị viết lặp lại một lần nữa ở dòng 1326–1500. Đồng thời có 2 thẻ `<div id="tab-quotas">` trùng ID ở dòng 255 và 284.

---

## 2. QUY TẮC BẮT BUỘC CHO AI KHI TRIỂN KHAI SPEC NÀY

1. **Không phá vỡ các bài test hiện có:** Toàn bộ các bài test bảo mật và regression hiện tại (`tests/test_*.py`) phải tiếp tục PASS. Nếu một bài test cũ kiểm tra hành vi cũ đã được thay đổi có chủ đích trong Spec này (ví dụ: xử lý lỗi 400 của một candidate trước khi sang candidate khác, hoặc cấu trúc chunk SSE của tool_calls), hãy cập nhật cả code và test sao cho thỏa mãn cả yêu cầu bảo mật lẫn yêu cầu chống lặp.
2. **Không dùng mạng thật hay đụng chạm tiến trình thật khi chạy test:** Mọi bài test dùng `tmp_path`, `config.deep_copy()`, `record_pid = False`, và mock `egress_guard`.
3. **Thực hiện tuần tự từng hạng mục từ `U01` đến `U07`:** Đọc kỹ hướng dẫn sửa từng file/hàm bên dưới, không bỏ sót bất kỳ mục nào.

---

## 3. CHI TIẾT KỸ THUẬT CÁC HẠNG MỤC NÂNG CẤP (U01 – U07)

---

### HẠNG MỤC U01 (P0): Sửa triệt để Streaming `tool_calls` trong `src/router/openai_proxy.py`

#### Vấn đề cần giải quyết
Trong `_handle_streaming_proxy` ([`src/router/openai_proxy.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/openai_proxy.py#L1181-L1572)):
- Khi nhận các delta của `tool_calls`, nếu Gateway cần buffer `arguments` để chạy `restore_tool_arguments_json` (khôi phục DLP placeholder an toàn trong JSON), Gateway **PHẢI giữ đầy đủ metadata (`id`, `type`, `function.name`)** và **KHÔNG ĐƯỢC** phát các chunk `tool_calls` rỗng hoặc phát `arguments` sau chunk `finish_reason`, cũng không được để `chunk_obj` có `arguments: ""` ghi đè sau khi đã flush.

#### Thiết kế chuẩn xác cần triển khai trong `_handle_streaming_proxy`:
1. **Cấu trúc lưu trạng thái Tool Call theo `(choice_idx, tool_idx)`:**
   Thay vì chỉ dùng `tool_args_buffer: Dict[Tuple[int, int], str]`, hãy dùng một dict lưu đầy đủ thông tin của từng tool call đang được gom trong choice đó:
   ```python
   # Key: (choice_idx, tool_idx)
   # Value: {"id": Optional[str], "type": str, "name": Optional[str], "arguments": str}
   tool_calls_state: Dict[Tuple[int, int], Dict[str, Any]] = {}
   ```
2. **Khi nhận một event có `delta["tool_calls"]` (tại vòng lặp xử lý `choices`):**
   - Với mỗi `tc` trong `delta["tool_calls"]`:
     - Kiểm tra `t_idx = tc.get("index")`: phải là `int` và `>= 0` (nếu upstream thiếu `index`, mặc định `0`).
     - Lấy hoặc khởi tạo state:
       ```python
       key = (c_idx, t_idx)
       state = tool_calls_state.setdefault(key, {
           "id": None,
           "type": "function",
           "name": None,
           "arguments": "",
       })
       if tc.get("id"):
           state["id"] = tc["id"]
       if tc.get("type"):
           state["type"] = tc["type"]
       fn = tc.get("function") or {}
       if fn.get("name"):
           state["name"] = fn["name"]
       args_piece = fn.get("arguments")
       if isinstance(args_piece, str):
           state["arguments"] += args_piece
       ```
     - Kiểm tra trần bộ nhớ `max_tool_buffer_bytes`:
       ```python
       total_tool_bytes = sum(len(v["arguments"].encode("utf-8")) for v in tool_calls_state.values())
       if total_tool_bytes > config.server.max_tool_buffer_bytes:
           # phát lỗi stream_error và return như hiện tại
       ```
   - **Quan trọng:** Xóa `"tool_calls"` khỏi `delta` của `chunk_obj` đang xét (`delta.pop("tool_calls", None)`) trong lúc đang gom, để **không phát các mảnh `tool_calls` rỗng/cụt** xuống client.
   - Nếu sau khi `delta.pop("tool_calls", None)`, một choice có `delta == {}` và `finish_reason is None` (và `chunk_obj` không chứa `usage`), thì **không cần yield chunk rỗng đó** (tránh spam hàng chục chunk `delta: {}` vô nghĩa làm rối client). Lưu ý: Để vượt qua bước probe `first_chunk` ở đầu stream (dòng 899), nếu `not has_yielded` và `has_received_valid_completion`, hãy emit một chunk khởi tạo role chuẩn OpenAI:
     ```python
     {
         "id": chunk_obj.get("id", "chatcmpl-stream"),
         "object": "chat.completion.chunk",
         "created": chunk_obj.get("created", int(time.time())),
         "model": chunk_obj.get("model", candidate["model_name"]),
         "choices": [{"index": c_idx, "delta": {"role": "assistant"}, "finish_reason": None}],
     }
     ```
3. **Khi gặp `finish_reason` (ví dụ `"tool_calls"` hoặc `"stop"`) hoặc khi flush tại `[DONE]`:**
   - Trước khi emit chunk có `finish_reason`, kiểm tra tất cả `key = (c_idx, t_idx)` trong `tool_calls_state` của `c_idx` đó:
   - Với mỗi `(c_idx, t_idx)` theo thứ tự `t_idx` tăng dần:
     - Lấy `raw_args = state["arguments"]`.
     - Nếu `raw_args` rỗng (`""`), chuẩn hóa thành `"{}"` nếu có `state["name"]` (hoặc giữ `""` nếu không có). Nếu `raw_args` khác rỗng, gọi `restore_tool_arguments_json(raw_args, reverse_map, allow_raw_string=False)`.
     - Đóng gói `tool_delta` có **ĐẦY ĐỦ** `index`, `id` (nếu có), `type` (`"function"`), và `function` (gồm cả `name` nếu có và `arguments` đã khôi phục):
       ```python
       tc_entry: Dict[str, Any] = {
           "index": t_idx,
           "type": state.get("type") or "function",
           "function": {"arguments": restored_args},
       }
       if state.get("id"):
           tc_entry["id"] = state["id"]
       if state.get("name"):
           tc_entry["function"]["name"] = state["name"]
       tool_deltas.append(tc_entry)
       ```
   - Emit chunk chứa `delta: {"tool_calls": tool_deltas}` với `finish_reason: None` **TRƯỚC**, sau đó mới emit chunk `chunk_obj` có `finish_reason` (với `delta` chỉ còn `content`/`reasoning_content` đã flush hoặc `{}`).
   - Tương tự trong hàm `flush_all_remaining()` (khi gặp `[DONE]` hoặc EOF mà upstream quên gửi `finish_reason`): cũng phải xuất đầy đủ `id`, `type`, `function.name` và `function.arguments` từ `tool_calls_state`.

---

### HẠNG MỤC U02 (P0): Chuẩn hóa lịch sử `messages` Đa Provider & Bỏ cơ chế cắt cụt System Prompt gây mù Agent

#### Vấn đề cần giải quyết
1. `prune_messages_for_groq` trong [`src/router/balancer.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/balancer.py#L82-L171) đang cắt cụt 60% System Prompt khi dài quá 7,500 ký tự, chèn thêm message `role: "system"` vào giữa hội thoại, và ép `max_tokens <= 2048`.
2. Khi Agent gửi `role: "tool"` có `content` là mảng `[{"type": "text", "text": "..."}]`, hoặc `role: "assistant"` có `tool_calls` và `content: None`, một số provider từ chối với HTTP 400.

#### Thiết kế chuẩn xác cần triển khai trong `src/router/balancer.py`:
1. **Viết hàm chuẩn hóa `normalize_messages_for_provider(messages: List[Dict[str, Any]], provider: str) -> List[Dict[str, Any]]`:**
   - Với mọi message trong `messages`:
     - Tạo bản sao `m_copy = dict(m)`.
     - **Xử lý `content` dạng `list` (multimodal text parts):**
       Nếu `isinstance(m_copy.get("content"), list)`:
       Gộp tất cả các phần tử `{"type": "text", "text": "..."}` thành một chuỗi `str` duy nhất nối bởi `"\n"`:
       ```python
       parts_text = [
           p.get("text", "") for p in m_copy["content"]
           if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
       ]
       m_copy["content"] = "\n".join(parts_text)
       ```
       *(Điều này giúp 100% các model trên Groq, GitHub Models, Gemini và OpenRouter không bao giờ báo lỗi 400 khi Cline/Roo Code gửi array content trong `user` hoặc `tool` message!)*
     - **Xử lý `role == "tool"`:**
       Đảm bảo `m_copy["content"]` luôn là một `str` (nếu `None` thì đặt thành `""`).
     - **Xử lý `role == "assistant"`:**
       - Nếu provider là `"groq"`, `"gemini"`, `"github"`:
         - Nếu không có `content` mà có `reasoning_content`: nếu message **không có** `tool_calls`, chuyển `reasoning_content` sang `content`; nếu **có** `tool_calls`, bỏ `reasoning_content` và giữ `content = None` (hoặc `""`).
         - Luôn `.pop("reasoning_content", None)` vì Groq/Gemini/GitHub báo lỗi 400 với field lạ trong message.
2. **Sửa hàm `prune_messages_for_groq(messages: List[Dict[str, Any]], max_chars: int = 28000) -> List[Dict[str, Any]]`:**
   - Tăng `max_chars` mặc định từ `15000` lên `28000` (tương đương ~7,000–8,000 tokens thực tế cho code/text).
   - **TUYỆT ĐỐI KHÔNG cắt cụt `system_msg["content"]`** trừ khi bản thân `system_msg` vượt quá `24,000` ký tự; và nếu buộc phải rút gọn (khi `sys_len > max_chars * 0.85`), phải **giữ cả phần đầu VÀ phần cuối** của System Prompt (`head + "\n...[middle omitted]...\n" + tail`), vì phần cuối chứa Tool Use Formatting và hoàn thành tác vụ của Agent!
   - **TUYỆT ĐỐI KHÔNG chèn thêm một message `{"role": "system", ...}` thứ hai** vào mảng `result`! Nếu có lượt hội thoại cũ bị lược bỏ (`len(kept_forward) < len(working_msgs)`), hãy gắn dòng ghi chú `[Note: Earlier conversation turns were trimmed to fit context window]` vào đầu message `user` đầu tiên trong `kept_forward` hoặc cuối `system_msg` đầu tiên, không tạo thêm phần tử `role: "system"` mới làm hỏng cấu trúc hội thoại!
   - Đảm bảo một cặp `[user_message] -> [assistant with tool_calls] -> [tool results...]` được giữ nguyên khối, không để chuỗi sau khi cắt bắt đầu bằng một message `assistant` có `tool_calls` mồ côi (không có `user` đứng trước) vì Gemini/Groq sẽ từ chối nếu sau `system` là `assistant` `tool_calls`. Nếu `kept_forward[0].get("role") in ("tool", "assistant")` và có `tool_calls`, hãy giữ thêm message `user` ngay trước đó hoặc nếu không có thì chèn `{"role": "user", "content": "Continue with the task."}` đứng trước.
3. **Bỏ giới hạn cứng `2048` cho `max_tokens` trên Groq:**
   - Trong `resolve_provider_request` (dòng 615–624), nâng trần `max_tokens` / `max_completion_tokens` của Groq từ `2048` lên `4096` (hoặc `8192`) để câu trả lời chứa tool call viết file không bị cắt cụt giữa chừng.

---

### HẠNG MỤC U03 (P0): Không dừng toàn bộ chuỗi Fallback khi 1 Provider trả về 400/422 & Tự động lọc Model hỏng

#### Vấn đề cần giải quyết
1. Trong [`src/router/openai_proxy.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/openai_proxy.py#L928-L932) và [`L1034-L1041`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/openai_proxy.py#L1034-L1041):
   Khi candidate đầu tiên trả về HTTP `400` hoặc `422` (ví dụ: một model trên OpenRouter hoặc GitHub không hỗ trợ `tools`, không hỗ trợ `stream_options`, hoặc quá giới hạn TPM của riêng provider đó trả về 400/413), Gateway hiện tại gọi `break` và kết thúc luôn với lỗi `502 Bad Gateway`!
2. Trong [`src/router/balancer.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/balancer.py#L434-L556):
   Các model có `error_count` cao liên tiếp và `success_count == 0` (như `github/gpt-4o-mini` bị lỗi 20 lần liên tiếp) vẫn được xếp hạng đầu.

#### Thiết kế chuẩn xác cần triển khai:
1. **Trong `src/router/openai_proxy.py` (cả nhánh Streaming dòng 928 và Non-streaming dòng 1034):**
   - Khi một upstream candidate trả về `400`, `413`, hoặc `422`:
     - Nếu `is_auto_mode` là `False` (người dùng chỉ định đích danh 1 model cụ thể và không còn candidate từ provider khác): có thể dừng (`break`).
     - Nhưng nếu đang có nhiều candidates trong chuỗi fallback (`len(candidates) > 1`): **KHÔNG ĐƯỢC `break` ngay!** Thay vào đó:
       - Nếu request có `stream_options` hoặc `reasoning_effort` hoặc `parallel_tool_calls`, ở lần fallback sang candidate kế tiếp, `resolve_provider_request` chỉ gửi các trường chuẩn.
       - Dùng `continue` để thử candidate kế tiếp (tối đa trong phạm vi `max_attempts`).
   - Khi một upstream candidate trả về `401` hoặc `403` (Key hỏng/hết hạn hoặc không có quyền truy cập model đó):
     - Ngoài việc thêm vào `failed_auth_providers`, hãy gọi `await storage_db.set_provider_cooldown(config.db_path, provider, 300, resp.status_code, "Authentication or permission error")` để các request kế tiếp trong 5 phút không mất thời gian gọi lại vào provider đang hỏng key (như `github` khi token không có quyền Models)!
   - Tăng giá trị mặc định của `max_attempts` trong `ServerConfig` ([`src/config_loader.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/config_loader.py#L31)) từ `3` lên `4` (hoặc giữ `3` nếu test N06 yêu cầu mặc định là `3` — **Lưu ý kiểm tra `tests/test_spec_n05_n06.py` trước khi đổi default! Nếu test assert `config.server.max_attempts == 3`, hãy giữ default là `3` trong `ServerConfig` và chỉ đặt `max_attempts: 5` trong `config/settings.yaml` hoặc khi loại bỏ sớm các model hỏng ở bước lọc**).
2. **Trong `src/router/balancer.py` (`get_candidate_chain` và `evaluate_model_intelligence`):**
   - **Phạt nặng các model chết (`success_count == 0` và `error_count >= 3`):**
     Trong vòng lặp chấm điểm `scored_candidates` (dòng 543–545):
     ```python
     errors = int(m.get("error_count", 0) or 0)
     successes = int(m.get("success_count", 0) or 0)
     if errors >= 3 and successes == 0:
         score -= 1200.0  # Đẩy xuống cuối bảng xếp hạng để các model sống chạy trước
     else:
         score -= (errors * 35.0)
     ```
   - **Ưu tiên model đã chứng minh hoạt động tốt (`success_count > 0`) và hỗ trợ Tool Calling khi `has_tools=True`:**
     - Nếu `successes > 0`: cộng điểm thưởng độ tin cậy `score += min(200.0, successes * 10.0)`.
     - Nếu `has_tools=True`:
       - Các model thuộc dòng không hỗ trợ hoặc hỗ trợ kém tool calling (ví dụ tên chứa `"deepseek-r1-distill"`, `"gemma"`, `"phi-3"`, `"nemotron"`, `"vision"`, `"vl"`) bị trừ `-300.0` điểm khi `has_tools=True`.
       - Các model hỗ trợ tool calling tốt (`"gpt-4"`, `"gpt-oss"`, `"qwen"`, `"llama-3.3"`, `"llama-3.1"`, `"claude"`, `"gemini-2"`) được cộng `+150.0` điểm khi `has_tools=True`.
   - **Trong `resolve_provider_request` (`src/router/balancer.py`):**
     - Với `provider in ("groq", "github", "gemini")`: loại bỏ các tham số dễ gây lỗi 400 không tương thích nếu model không hỗ trợ, cụ thể:
       - Nếu `provider in ("groq", "github")`: `.pop("stream_options", None)` nếu `not req_payload.get("stream")`, và `.pop("parallel_tool_calls", None)` nếu không có `tools`.

---

### HẠNG MỤC U04 (P1): Cơ Chế Phát Hiện và Phá Vỡ Vòng Lặp Của Agent (Agent Loop Breaker)

#### Vấn đề cần giải quyết
Khi AI Agent (Cline / Roo Code / Cursor) bị kẹt trong vòng lặp, lịch sử `messages` gửi lên Gateway sẽ có dấu hiệu rất rõ ràng:
- **Dạng 1 (Tool Call Loop):** 2 hoặc 3 message `role == "assistant"` gần nhất gọi **cùng một tên hàm (`function.name`) với cùng chuỗi `function.arguments`**.
- **Dạng 2 (Content Repetition Loop):** 2 hoặc 3 message `role == "assistant"` gần nhất có cùng nội dung `content` hệt nhau (độ dài `> 20` ký tự).

Nếu Gateway cứ chuyển nguyên xi request đó lên model với `temperature` cũ, model sẽ tiếp tục trả về y hệt lần trước.

#### Thiết kế chuẩn xác cần triển khai trong `src/router/balancer.py`:
1. Viết hàm `detect_and_mitigate_agent_loop(messages: List[Dict[str, Any]], payload: Dict[str, Any]) -> Tuple[bool, Optional[str]]`:
   - Trích xuất danh sách các message có `role == "assistant"` từ `messages`.
   - Lấy 2 message `assistant` gần nhất (`last_a1`, `last_a2`).
   - Kiểm tra xem chúng có trùng lặp không:
     - **Trùng `tool_calls`:** Cả hai đều có `tool_calls` không rỗng, và danh sách `[(tc.get("function", {}).get("name"), tc.get("function", {}).get("arguments"))]` của `last_a1` giống hệt `last_a2`.
     - **Hoặc trùng `content`:** Cả hai đều không có `tool_calls`, có `content` dạng `str` dài `>= 30` ký tự và `last_a1["content"].strip() == last_a2["content"].strip()`.
   - Nếu phát hiện trùng lặp (`is_looping = True`):
     1. **Inject Loop-Recovery Hint:** Thêm một nhắc nhở ngắn gọn vào message cuối cùng trong `messages` (nếu message cuối cùng là `user` hoặc `tool`) hoặc điều chỉnh nhẹ bản sao `messages` gửi đi:
        - Nếu message cuối cùng có `role in ("user", "tool")` và `isinstance(msg.get("content"), str)`:
          Nối thêm vào cuối `content` của message cuối cùng đó:
          `"\n\n[SYSTEM NOTICE: You have repeated the exact same action/output multiple times. Do NOT repeat the exact same tool call with identical arguments again. Analyze the previous result carefully, try a different approach, or conclude your response.]"`
     2. **Điều chỉnh `temperature` chống kẹt:** Nếu `payload.get("temperature")` là `None` hoặc `< 0.4`, đặt `payload["temperature"] = 0.5` để model thoát khỏi điểm cực tiểu lặp (greedy decoding trap).
     3. Trả về `(True, repeated_signature)`.
2. **Đổi thứ tự Model khi đang bị lặp:**
   - Trong `get_candidate_chain`, nếu phát hiện request đang ở trạng thái lặp (`is_looping = True`) và `requested_model` là `"auto"` (hoặc tương đương), nếu `self.last_active_model` vừa phục vụ lượt trước, hãy **cộng ưu tiên (+300 điểm) cho candidate đứng thứ 2** (hoặc trừ `-250` điểm của `self.last_active_model["id"]`) để một model khác thông minh tương đương xử lý và phá vỡ vòng lặp!

---

### HẠNG MỤC U05 (P1): Tinh chỉnh DLP Sanitizer & Boundary Check để không làm hỏng Tool Calls / Code của Agent

#### Vấn đề cần giải quyết
1. Trong [`src/router/openai_proxy.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/openai_proxy.py#L462-L485), hàm `inspect_boundary` đang quét cả dictionary keys trong `tools` (tức tên các tham số của hàm trong JSON Schema như `parameters.properties.<param_name>`) bằng `sanitizer.sanitize_text(key)`. Khi gặp một tên biến camelCase dài có số (ví dụ `maxRetryCountForHttp2Client`), `entropy_scan` tưởng nhầm là secret `HIGH_ENTROPY` và trả về **HTTP 403 `dlp_violation`**!
2. Trong [`src/security/sanitizer.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/security/sanitizer.py#L194-L201), rule `INTERNAL_IP` đang bắt cả `127.0.0.1` (địa chỉ loopback mà các lập trình viên luôn nhắc tới trong code/test), và `entropy_scan` bắt các đường dẫn file hoặc tên hàm dài có dấu `/` hoặc `.`.

#### Thiết kế chuẩn xác cần triển khai:
1. **Kiểm tra lại các test hiện có trước khi sửa (`tests/test_review_fixes.py`, `tests/test_round2_regressions.py`, `tests/test_fix_spec_regressions.py`):**
   - Trong `tests/test_review_fixes.py` dòng 44–46:
     `{"tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {KEY: {"type": "string"}}}}}]}`, với `KEY = "AKIAIOSFODNN7EXAMPLE"`.
     Test này yêu cầu khi key chứa AWS Key (`AKIA...`) thì phải trả về 403!
   - Vì vậy, **GIỮ NGUYÊN** logic `inspect_boundary` trong `src/router/openai_proxy.py`! Thay vào đó, cải thiện độ chính xác của `entropy_scan` trong `src/security/sanitizer.py`:
     - Trong [`src/security/sanitizer.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/security/sanitizer.py#L298-L320):
       Khi quét `entropy_candidate_pattern`:
       - Bỏ qua các chuỗi có dạng đường dẫn file hoặc URL thông thường (ví dụ chứa `http://`, `https://`, hoặc bắt đầu bằng `/`, `./`, `../`, hoặc kết thúc bằng đuôi file phổ biến `.py`, `.js`, `.ts`, `.json`, `.md`, `.html`, `.yaml`, `.txt`, `.bat`, `.sh`).
       - Bỏ qua các định danh thuần camelCase / PascalCase không có ký tự đặc biệt mà chỉ có chữ và 1–2 chữ số nhỏ.
2. **Bảo toàn tuyệt đối cấu trúc JSON khi Sanitize & Restore `tool_calls[].function.arguments`:**
   - Trong `restore_tool_arguments_json` ([`src/router/openai_proxy.py`](file:///c:/Users/PC/Desktop/ai-free-gateway/src/router/openai_proxy.py#L121-L143)):
     - Nếu `raw_args.strip() == ""` (một số model trả về chuỗi rỗng khi hàm không có tham số), hãy trả về `"{}"` thay vì ném `InvalidUpstreamToolArgumentsError`! (Vì trong `json.loads("")` sẽ ném `JSONDecodeError`, làm hỏng các lời gọi tool không có tham số như `list_files()` hoặc `get_status()`!).
     - Kiểm tra cụ thể:
       ```python
       if not raw_args or not raw_args.strip():
           return "{}"
       ```
       *(Lưu ý: Kiểm tra xem có bài test nào truyền `""` và kỳ vọng `""` hay không; nếu `raw_args == ""` thì trả `"{}"` khi flush tool call hoặc giữ `""` nếu hàm test gọi trực tiếp `restore_tool_arguments_json("", ...)` -> Tốt nhất: trong `restore_tool_arguments_json`, giữ `if not raw_args: return raw_args`, còn nếu `raw_args.strip() == ""` thì `return "{}"`. Khi chuẩn bị gửi tool call hoặc flush trong `_handle_streaming_proxy`, nếu `raw_args == ""` thì truyền `"{}"` vào!)*

---

### HẠNG MỤC U06 (P2): Sửa lỗi JavaScript & Duplicate DOM ID trong `ui/index.html`

#### Vấn đề cần giải quyết
Trong [`ui/index.html`](file:///c:/Users/PC/Desktop/ai-free-gateway/ui/index.html):
1. Dòng 255 và dòng 284 có **2 thẻ `<div id="tab-quotas">`** bị trùng lặp ID! Khi bấm vào tab "Provider Quotas", `document.getElementById('tab-quotas')` chỉ mở thẻ đầu tiên nhưng CSS grid nằm ở thẻ thứ hai.
2. Dòng 377–561: Hàm `setApiKey(key)` chưa đóng dấu `}` mà bị chèn nhầm toàn bộ cụm hàm `setPlaygroundPrompt`, `clearPlayground`, `pingProviders`, `sendPlaygroundMessage` vào bên trong thân hàm `setApiKey`, đồng thời cụm hàm này đã có sẵn ở dòng 1326–1500!

#### Thiết kế chuẩn xác cần triển khai trong `ui/index.html`:
1. Xóa thẻ `<div id="tab-quotas">` bị thừa ở dòng 255–257 (chỉ giữ lại một `<div id="tab-quotas" class="hidden">` duy nhất có `style="display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 1rem;"`).
2. Tại dòng 377–561: Đóng hàm `setApiKey(key)` đúng chỗ:
   ```javascript
   function setApiKey(key) {
     if (key) {
       sessionStorage.setItem('gateway_api_key', key.trim());
     } else {
       sessionStorage.removeItem('gateway_api_key');
     }
     updateAuthStatusUI();
   }
   ```
   Và xóa đoạn code bị nhân đôi từ dòng 383 đến dòng 561 (vì các hàm `setPlaygroundPrompt`, `clearPlayground`, `pingProviders`, `sendPlaygroundMessage` đã được định nghĩa đầy đủ ở dòng 1325–1500).

---

### HẠNG MỤC U07 (P1): Bộ Bài Test Kiểm Chứng Chống Lặp & Tool Calling (Regression Tests)

Tạo file test mới `tests/test_anti_loop_and_agent_tools.py` kiểm tra đầy đủ 6 kịch bản thực tế sau (tất cả chạy offline với `tmp_path` và mock `egress_guard`):

1. **`test_u01_streaming_tool_calls_preserves_id_name_and_valid_json_arguments`:**
   - Giả lập upstream gửi stream SSE gồm 4 chunks:
     - Chunk 1: `delta: {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_read_1", "type": "function", "function": {"name": "read_file", "arguments": ""}}]}`
     - Chunk 2: `delta: {"tool_calls": [{"index": 0, "function": {"arguments": "{\"path\": \""}}]}`
     - Chunk 3: `delta: {"tool_calls": [{"index": 0, "function": {"arguments": "<REDACTED_EMAIL_1>/config.py\"}"}}]}`
     - Chunk 4: `delta: {}, "finish_reason": "tool_calls"`
     - Chunk 5: `[DONE]`
   - Client ghép các SSE chunks nhận được theo chuẩn OpenAI SDK:
     - Phải nhận được đúng `id == "call_read_1"`, `type == "function"`, `function.name == "read_file"`.
     - Ghép `function.arguments` phải parse được thành JSON `{"path": "alice@example.com/config.py"}`.
     - Tuyệt đối không có bất kỳ chunk `tool_calls` nào phát sau chunk có `finish_reason == "tool_calls"`.

2. **`test_u01_streaming_single_chunk_tool_call_with_finish_reason`:**
   - Giả lập một số provider (như Gemini / Groq) trả về toàn bộ `tool_calls` (gồm cả `id`, `name`, `arguments`) ngay trong cùng 1 chunk có `finish_reason: "tool_calls"`.
   - Kiểm tra client nhận được đầy đủ `id`, `name` và `arguments` đã khôi phục DLP, không bị ghi đè bởi `arguments: ""`.

3. **`test_u02_multimodal_tool_and_user_messages_normalized_and_system_prompt_not_truncated`:**
   - Gửi một System Prompt dài `18,000` ký tự (có đoạn kết thúc `"CRITICAL_TOOL_FORMAT_FOOTER_END"` ở cuối) kèm theo message `role: "tool"` có `content: [{"type": "text", "text": "File content ok"}]` tới provider `groq`.
   - Kiểm tra payload gửi tới `egress_guard.post`:
     - `messages[0]["content"]` vẫn giữ nguyên `"CRITICAL_TOOL_FORMAT_FOOTER_END"` ở cuối (không bị cắt mất phần đuôi).
     - Không có message thứ hai nào có `role == "system"` chèn vào giữa mảng.
     - Message `role == "tool"` có `content` đã được chuyển thành chuỗi `"File content ok"` (kiểu `str`, không phải `list`).

4. **`test_u03_candidate_400_falls_back_to_next_provider_in_auto_mode`:**
   - Cấu hình 2 models trong pool (`github/gpt-4o-mini` và `groq/openai/gpt-oss-120b`).
   - Giả lập `github/gpt-4o-mini` trả về HTTP `400 Bad Request` (do không hỗ trợ tham số hoặc lỗi model).
   - Giả lập `groq/openai/gpt-oss-120b` trả về HTTP `200 OK` với câu trả lời hợp lệ.
   - Gửi request với `model: "auto"`: Gateway phải tự động chuyển tiếp sang `groq/openai/gpt-oss-120b` và trả về `200 OK` thay vì dừng ngay ở lỗi 400 đầu tiên.

5. **`test_u03_dead_models_with_high_errors_ranked_below_healthy_models`:**
   - Seed 2 models: Model A nằm trong `priority_models` nhưng có `error_count = 15, success_count = 0`; Model B có `error_count = 0, success_count = 5`.
   - Gọi `balancer.get_candidate_chain("auto")`: Model B phải được xếp đứng **trước** Model A.

6. **`test_u04_agent_loop_detection_injects_recovery_notice_and_adjusts_temperature`:**
   - Gửi lịch sử `messages` trong đó Assistant đã gọi `read_file({"path": "a.py"})` 2 lần liên tiếp với cùng kết quả `tool`.
   - Kiểm tra payload gửi lên upstream đã được tự động gắn `[SYSTEM NOTICE: You have repeated the exact same action/output multiple times...]` và `temperature >= 0.5`.

---

## 4. TIÊU CHÍ NGHIỆM THU (DEFINITION OF DONE)

1. Triển khai hoàn chỉnh cả 7 hạng mục **U01 -> U07** không bỏ sót.
2. Chạy toàn bộ test suite bằng lệnh:
   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=temp_test_run/anti_loop_verify
   ```
   Tất cả các bài test cũ và 6 bài test mới trong `tests/test_anti_loop_and_agent_tools.py` đều phải **100% PASS**.
3. Dọn dẹp các bảng lỗi cũ trong `storage/gateway.db` (hoặc reset `error_count` / cập nhật `settings.yaml` nếu cần) khi người dùng khởi động lại dịch vụ.
4. Báo cáo kết quả rõ ràng từng mục `U01` – `U07`, tên file đã sửa, và kết quả chạy pytest thực tế.

---

## 5. PROMPT GIAO VIỆC CHO AI (COPY & PASTE ĐỂ CHẠY NGAY)

> Đọc toàn bộ file `ANTI_LOOP_UPGRADE_SPEC.md`. Thực hiện tuần tự và đầy đủ tất cả các hạng mục từ **U01 đến U07** trong `src/router/openai_proxy.py`, `src/router/balancer.py`, `src/security/sanitizer.py`, `ui/index.html` và tạo file test `tests/test_anti_loop_and_agent_tools.py`. Đảm bảo giữ nguyên toàn bộ các tính năng bảo mật DLP/Egress/Auth hiện có và làm cho tất cả các bài test cũ lẫn mới đều PASS 100%. Chạy pytest kiểm chứng thực tế rồi báo cáo kết quả.
