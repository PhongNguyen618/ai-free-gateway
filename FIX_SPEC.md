# Spec sửa lỗi AI Free Gateway

Ngày review: 2026-10-28. Tài liệu này là đầu vào độc lập cho AI triển khai. Đọc `SPEC.md` để hiểu mục tiêu ban đầu; dùng tài liệu này cho hành vi sửa lỗi cụ thể. Không thay thế toàn bộ ứng dụng hoặc đổi framework.

## Kết quả review và giới hạn

Đã đọc toàn bộ module backend, dashboard, các test và script dừng server. Chạy `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`: **26 passed in 6.08s**. Các phát hiện dưới đây dựa trên mã nguồn; không đồng nghĩa mọi tình huống đã được tái hiện end-to-end. Chưa gọi provider thật, chưa kiểm chứng model/giá hiện tại, chưa chạy script autostart/stop. Không sửa mã chạy trong lần review này.

Ưu tiên: P1 = bảo mật/chi phí/tính đúng cốt lõi, P2 = độ ổn định và vận hành. Không cần đổi model cụ thể theo trí nhớ: khi triển khai phải kiểm chứng tài liệu chính thức và quyền tài khoản nếu cập nhật catalog.

## Quy tắc triển khai chung

- Giữ FastAPI, httpx, SQLite, API `/v1`, dashboard và hỗ trợ Windows.
- Mỗi mục cần test hồi quy chứng minh lỗi trước sửa và pass sau sửa. Dùng upstream giả qua httpx transport; không cần API key thật, không gọi Internet trong test.
- Không ghi raw prompt, secret, reverse map, Authorization hoặc raw provider error vào log/SQLite. Audit chỉ lưu dữ liệu đã xử lý. Không đọc/in file `.env` để debug.
- Không xóa DB thật, không chạy script kill/autostart trong test. Migration phải giữ dữ liệu và chạy lặp an toàn.
- Không tự ý chuyển sang model tính phí. Không coi HTTP 200 hoặc tên model là bằng chứng giá bằng 0.
- Không thêm yêu cầu phê duyệt mới cho request sạch; dashboard vẫn dùng được khi bật authentication.

## F01 — P1: Dashboard API bỏ qua authentication, CORS quá rộng

**Bằng chứng:** `src/main.py` khai báo `/api/*` không có dependency kiểm tra key. `verify_gateway_auth` nằm riêng trong `create_openai_router` nên chỉ bảo vệ `/v1`. CORS cho mọi origin và credentials.

**Tác động:** Dù cấu hình `gateway_api_key`, bên gọi dashboard API vẫn đọc audit/pending hoặc gửi quyết định duyệt. Website ngoài có thể tương tác gateway local theo chính sách CORS hiện tại.

**Sửa:** Tách auth dùng chung, áp dụng toàn bộ `/api/*` và `/v1/*` khi có key; `/health` chỉ trả trạng thái tối thiểu. Dùng so sánh constant-time. Dashboard có nhập key vào bộ nhớ phiên và gắn header cho tất cả fetch; không nhúng key trong HTML/URL hoặc lưu bền mặc định. CORS chỉ cho origin cấu hình rõ, mặc định same-origin; kiểm tra Origin cho API thay đổi trạng thái để chặn cross-site ngay cả chế độ local không key. Giữ hỗ trợ CLI không có Origin theo cấu hình local. Khi bind ngoài loopback phải yêu cầu auth hoặc explicit insecure opt-in có mô tả rõ.

**Nghiệm thu:** Với key bật, missing/wrong key trả 401 cho read và POST decision/harvest; đúng key hoạt động qua UI. Origin lạ không đọc được API và không thay đổi trạng thái; test cả request không có Origin và preflight.

## F02 — P1: Stored XSS trong dashboard

**Bằng chứng:** `ui/index.html` chèn `${item.model_requested}`, `${m.display_name || m.model_name}`, `${m.id}` và dữ liệu routing vào `innerHTML`. Model request do client nhập; tên model đến từ upstream.

**Sửa:** Dùng DOM `textContent`, tạo node và gắn event listener cho dữ liệu động; không ghép dữ liệu không tin cậy vào HTML, attribute hoặc inline handler. Có thể dùng escape đúng ngữ cảnh nếu thực sự cần template, nhưng ưu tiên DOM API. Rà tất cả sink `innerHTML`, không chỉ ví dụ trên.

**Nghiệm thu:** Seed approval/model/routing có `<img src=x onerror=...>` và chuỗi phá attribute; dashboard hiển thị nguyên văn, không tạo phần tử hoặc chạy handler từ payload. Kiểm thử browser phù hợp hoặc ghi rõ bước kiểm tra thủ công.

## F03 — P1: DLP bỏ qua nhiều đường truyền dữ liệu

**Bằng chứng:** `sanitize_messages` chỉ scan `content` string và part `type=text`. Proxy copy toàn bộ `body` rồi chỉ thay `messages`. `messages[].tool_calls[].function.arguments`, `function_call.arguments`, `tools[].function.description/parameters`, và phần mở rộng khác đi nguyên dạng. Pending lưu toàn bộ sanitized_messages nên secret ở trường chưa scan cũng có thể xuống SQLite.

**Sửa:** Xác định schema outbound được hỗ trợ; duyệt mọi trường văn bản có thể mang nội dung người dùng, bao gồm tool calls/schema/description. Giữ nguyên tên field, role, ID và cấu trúc giao thức; xử lý JSON arguments mà không làm hỏng JSON. Trường chứa dữ liệu mà chưa có cách scan an toàn phải bị từ chối rõ ràng hoặc có chính sách opt-in; không âm thầm cho qua. Quy định image URL/data URL/audio/file: không được tuyên bố scan nội dung nhị phân bằng regex; mặc định chặn modality chưa hỗ trợ DLP, nêu lỗi rõ. Dùng cùng payload đã kiểm tra cho egress và bản audit an toàn. Khôi phục token trong tool/function arguments trả về khi hỗ trợ, giữ JSON hợp lệ.

**Nghiệm thu:** Đặt AWS key giả `AKIAIOSFODNN7EXAMPLE` trong từng đường tool arguments/description/schema và content; high=reject phải không gọi upstream, DB/log không chứa key. Medium=redact thay dữ liệu nhất quán cả tool và text; JSON arguments vẫn parse được. Thêm test payload đa phương thức và extension chưa hỗ trợ.

## F04 — P1: Egress policy không đồng nhất, DNS chưa được bảo vệ

**Bằng chứng:** `stream_post` không kiểm tra `max_request_bytes` và không cộng outbound bytes. `validate_url` kiểm tra hostname/IP literal, không kiểm tra DNS; cho HTTP và mọi subdomain của whitelist. Client mặc định có thể nhận proxy từ môi trường.

**Sửa:** Chuẩn hóa build/serialize request một lần cho JSON/stream; đo đúng bytes thực gửi, vượt limit thì chặn trước network. Mặc định HTTPS, host chính xác, port cho phép, từ chối URL userinfo; subdomain/HTTP chỉ qua cấu hình explicit. DNS resolve bất đồng bộ, từ chối nếu bất kỳ địa chỉ không public; bảo đảm địa chỉ socket kết nối cũng là địa chỉ đã kiểm tra để chống DNS rebinding (chỉ resolve trước rồi để httpx resolve lại là chưa đủ). Giữ TLS hostname verification/SNI, `follow_redirects=False`, tắt proxy môi trường mặc định hoặc cấu hình proxy tin cậy rõ ràng. Nếu chưa triển khai transport pinning, ghi rõ giới hạn và không tuyên bố SSRF đã giải quyết hoàn toàn.

**Nghiệm thu:** Cả JSON/stream oversized đều bị chặn và transport không được gọi; metrics bằng bytes thực. Mock DNS host whitelist về loopback/private/link-local IPv4/IPv6 phải bị chặn. Test DNS thay đổi giữa kiểm tra/kết nối, HTTPS SNI, redirect, userinfo, port và subdomain không cấp phép. Không dùng DNS thật.

## F05 — P1: Catalog/fallback có thể dùng model không chứng minh miễn phí

**Bằng chứng:** `get_candidate_chain` tự tạo fallback từ config với `is_free=1`, gồm `openrouter/auto`; không kiểm chứng giá/catalog. `harvest_openrouter` coi suffix `:free` là đủ và dùng `float(value or -1)` làm số 0 numeric thành -1. Catalog không loại model đã biến mất/tăng giá. `harvest_groq` không kiểm tra enabled.

**Sửa:** Route chỉ candidate active, provider enabled, credentials hợp lệ khi cần, có chính sách free rõ. Không tự tạo model miễn phí từ tên fallback. Với OpenRouter yêu cầu giá prompt/completion hợp lệ bằng 0; thiếu/null/âm/NaN/Infinity/invalid hoặc giá dương phải loại, kể cả suffix free. Parse giá từng model, một record xấu không hủy toàn bộ harvest. Với free-tier provider khác hiển thị đúng là quota free theo tài khoản, không cam kết miễn phí khi tài khoản billing có thể tính tiền; policy phải explicit. Harvest snapshot thành công mới reconcile active; lỗi mạng giữ snapshot trước và đánh dấu stale. Không gọi provider disabled. Loại mặc định auto chưa chứng minh free.

**Nghiệm thu:** Test `0`, `0.0`, `"0"`, null, thiếu, NaN, giá dương và record hỏng; model mất/tăng giá không route sau harvest thành công. Snapshot không bị xóa khi harvest fail. DB rỗng/không có key/provider disabled => 503 và zero outbound completion calls.

## F06 — P2: Fallback lặp alias và tiếp tục gọi provider đã cooldown

**Bằng chứng:** Harvester lưu namespaced ID và alias riêng; comment dedup nói provider+model nhưng code chỉ so `id`. Chain được tính trước vòng request; 429 chỉ cập nhật DB, các candidate cùng provider còn lại vẫn được thử. Context check chỉ tính text //4, bỏ output/tools và bỏ qua matched model.

**Sửa:** Dedup bằng `(provider, model_name)`; alias chỉ để tra cứu, metrics dùng canonical identity. Skip mọi candidate của provider vừa 429 ngay trong request, đồng thời recheck cooldown trước attempt. Xử lý Retry-After có giới hạn hợp lý. Tính budget đầu vào gồm tools/multipart được hỗ trợ và output reserve với ước lượng bảo thủ; matched model cũng phải đủ context. Phân biệt lỗi client 400/422 với lỗi retryable 429/5xx/network; không retry vô hạn. Unknown explicit model trả lỗi model rõ; fallback cho model cụ thể phải giữ chính sách hiện có hoặc cấu hình/document rõ.

**Nghiệm thu:** Hai alias cùng model chỉ gọi một lần; provider A nhiều model trả 429 chỉ nhận một call rồi chuyển B. Context không đủ không được chọn kể cả request explicit. Test toàn chain fail, không ứng viên và lỗi client không bị retry bừa.

## F07 — P1: SSE có thể mất Unicode và trộn nhiều choices

**Bằng chứng:** `_handle_streaming_proxy` decode từng TCP chunk bằng `errors=ignore`, dùng một `StreamingRestorer` cho mọi choice, chỉ flush khi `[DONE]`, bỏ phần `line_buffer` ở EOF. Parser chỉ nhận `data: `, xử lý từng dòng như một event. Stream success ghi trước khi kết thúc, latency cố định 150ms; quota dùng số delta có content.

**Sửa:** Decoder incremental UTF-8 hoặc parser SSE dựa trên dòng chuẩn; ghép event theo blank line, hỗ trợ CRLF, `data:` không space, comment/heartbeat và multi-line data. Restorer riêng theo choice index và từng tool-call stream nếu hỗ trợ. Flush dữ liệu trước finish_reason của choice; quy định EOF thiếu DONE là lỗi stream, không âm thầm đánh dấu thành công; không bỏ phần buffer. Trước event client đầu tiên có thể fallback; sau đó không nối output provider khác. Đóng upstream khi disconnect/cancel. Dùng usage upstream nếu có; giá trị ước lượng phải được gắn nhãn, không coi số chunk là token thực. Ghi success sau hoàn tất, latency đo thực.

**Nghiệm thu:** Chia từng byte chuỗi tiếng Việt/emoji; output khôi phục chính xác. Test placeholder chia nhiều event, n=2 interleave không lẫn nội dung, tool delta, trailing buffer/EOF, finish trước DONE, upstream lỗi trước/sau event đầu và disconnect đóng response. Test usage-only final event và stream_options include_usage.

## F08 — P2: Input và config sai kiểu gây lỗi 500/hành vi âm thầm

**Bằng chứng:** Proxy dùng `body.get`, `requested_model.strip`, `dict(msg)` mà không validate schema. `stream="false"` bị bool thành True. Risk actions là string không giới hạn; allow vẫn bị sanitize, low action không được xử lý, token format cấu hình không được dùng. Timeout redact áp dụng medium nhưng không áp dụng high.

**Sửa:** Pydantic request/config schemas: object body, model string, messages list không rỗng, role/content/tool fields hợp lệ, stream boolean strict; trả OpenAI-style error 400/422 nhất quán thay vì 500. Ràng buộc số port/timeout/size/entropy, enum action; typo fail startup. Định nghĩa allow=cho phép nguyên văn ở nhóm entity được policy cho phép, redact=thay token, approve=duyệt rồi forward sanitized như nhãn UI hiện có. Áp dụng policy theo finding, tránh một risk cao che policy risk thấp; audit luôn sanitized kể cả allow. Thống nhất timeout redact cho high/medium hoặc bỏ lựa chọn không hỗ trợ bằng validation. Implement token format kèm parser/restorer tương ứng hoặc bỏ field và migrate/document rõ, không nhận cấu hình rồi bỏ qua.

**Nghiệm thu:** Body null/list, messages sai kiểu/phần tử, content sai, model null, stream string không làm 500 và không gọi upstream. Matrix risk/action/timeout/reversible đúng hành vi; cấu hình sai bị từ chối trước network. Quy định rõ output trả lỗi tương thích SDK.

## F09 — P2: Approval có thể bị ghi đè, expired/restart không nhất quán

**Bằng chứng:** `submit_decision` ghi lại decision dù item đã có quyết định; SQL update chỉ WHERE id nên đổi cả approved/rejected/expired. wait timeout tính từ lúc wait thay vì expires_at; restart còn pending trong DB nhưng không có waiter. create_request thêm memory trước khi persist; cancellation không finalize DB.

**Sửa:** State machine pending -> approved/rejected/expired/cancelled đúng một lần; conditional SQL update `WHERE status='pending' AND expires_at > now` và kiểm tra rowcount. Serialize quyết định/timeout bằng lock/transaction phù hợp; persist trước khi đánh thức waiter. Dùng deadline tuyệt đối. Restart expire/cancel orphan pending; cancellation dọn memory và DB; insert DB thất bại rollback memory. Chỉ hỗ trợ một process worker cho in-memory gate và document/startup guard, hoặc triển khai phối hợp đa process thực sự.

**Nghiệm thu:** Hai quyết định đồng thời chỉ một thành công, quyết định sau expiry không đổi trạng thái; race timeout/approve có một kết quả nhất quán. Cancel/restart/DB insert failure không để pending hoặc memory gate mồ côi. Invalid decision trả 422, missing/already decided trả 404/409 có quy định nhất quán.

## F10 — P2: Test đang chạm DB thật và chia sẻ config

**Bằng chứng:** `tests/test_api_endpoints.py` import global app và gọi `init_db(config.db_path)` rồi upsert model vào đường mặc định; nhiều test `config.model_copy()` shallow rồi đổi `dlp.risk_action.medium`, làm đổi nested object dùng chung từ cache.

**Sửa:** App factory nhận AppConfig và dependency inject; mỗi test dùng tmp_path DB và `model_copy(deep=True)` hoặc config mới. Lifespan quản lý client/task riêng cho app; test startup/shutdown thực sự, mock toàn bộ discovery network. Không dùng tên DB cố định trong storage. Đặt isolation trước khi thêm test F01–F09.

**Nghiệm thu:** Suite chạy lặp, đổi thứ tự vẫn pass; DB gateway thật không bị sửa, config cache không đổi; không network, không client/task leak. Existing 26 test vẫn pass sau cập nhật expectation hợp lý (ví dụ bỏ fallback không xác minh free), không giữ assertion nguy hiểm chỉ để xanh.

## F11 — P2: Vận hành và dashboard chưa đúng cam kết

- `quota_state.requests_today/tokens_today` tăng mãi, không reset ngày. Thêm usage_date UTC và reset atomic theo ngày; định nghĩa counts attempts/success rõ, tránh cooldown insert tự tạo một success. Test qua nửa đêm và concurrent increments.
- `stop.bat` kill mọi OwningProcess liên quan port 8000 mà không xác minh là gateway, không đọc port config. Dùng PID/instance metadata do launcher tạo, xác minh executable + command line/path + thời điểm start trước stop, tránh PID reuse; chỉ dừng gateway. Test bằng mock process, không kill tiến trình thật.
- UI tải script `https://cdn.tailwindcss.com`, gây kết nối ngoài và phụ thuộc CDN. Bundle CSS tĩnh local; dashboard offline hiển thị đầy đủ, browser không phát request ngoài. Đây là phụ thuộc frontend hiện tại, không phải bằng chứng backend có telemetry.
- Config/env/UI dùng đường dẫn theo cwd. Resolve tài nguyên mặc định theo project root; explicit CONFIG_PATH sai phải báo lỗi thay vì âm thầm dùng mặc định. Test khởi chạy từ thư mục khác.
- Cập nhật README: số test thực tế, auth dashboard, giới hạn DLP/modality/free-tier, single-worker, behavior khi thiếu key. `health_check_interval_seconds` chưa có health checker: triển khai kiểm tra metadata an toàn hoặc bỏ cấu hình/tuyên bố không có thật; không tạo completion tốn tiền để health-check mặc định.

## Trình tự bàn giao và Definition of Done

1. Làm F10 isolation/app factory trước; thêm test tái hiện cho từng mục, dùng synthetic secrets.
2. F01–F05 và F07 để khóa rủi ro bảo mật/chi phí, sau đó F06/F08/F09/F11. Có thể chia commit theo mục nhưng giữ API migration nhất quán.
3. Chạy toàn suite offline bằng lệnh ở đầu tài liệu; chạy test browser cho XSS/auth/offline và test lifecycle/cancellation. Nếu chưa có công cụ browser, ghi rõ kiểm tra còn thiếu thay vì báo đã pass.
4. Bàn giao bảng từng ID: đã sửa/chưa sửa, file thay đổi, test hồi quy, kết quả thực chạy, giới hạn còn lại. Không đánh dấu hoàn thành DNS rebinding, free-only hoặc multiworker khi chỉ mới kiểm tra sơ bộ.
5. Không deploy, thay credential hoặc thao tác Windows startup trong phạm vi spec này. Chỉ review/spec đã được yêu cầu; AI triển khai tiếp theo thực hiện code khi nhận chỉ dẫn sửa.
