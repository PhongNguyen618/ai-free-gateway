# AI Free Gateway — Spec sửa lỗi vòng 2

Ngày: 2026-10-28. Đọc cùng `FIX_SPEC.md`; tài liệu này bổ sung các lỗi còn tồn tại sau đợt sửa đầu. Khi hai tài liệu khác nhau về chi tiết triển khai, ưu tiên hành vi cụ thể trong tài liệu này. Không coi việc đạt spec vòng 2 là bằng chứng mọi yêu cầu vòng 1 đã hoàn thành.

## 1. Mục tiêu và bằng chứng hiện tại

Giữ kiến trúc FastAPI/httpx/SQLite và dashboard hiện có. Sửa các đường rò rỉ DLP, lỗi tương thích tool calling, kiểm tra input, DNS transport và streaming. Không viết lại toàn bộ ứng dụng, không thêm provider hoặc đổi model theo trí nhớ.

Baseline kiểm tra thực tế:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=storage/review_tmp_20261028
# 47 passed in 11.52s
```

Lần chạy không chỉ định basetemp gặp quyền truy cập thư mục Temp của pytest, không phải lỗi logic ứng dụng. Khi chạy lại hãy dùng thư mục basetemp mới, dành riêng cho test: pytest có thể dọn nội dung basetemp. Không trỏ vào `storage` chung hoặc thư mục dữ liệu thật.

Đã tái hiện bằng app factory, DB tạm và upstream giả, không dùng provider thật:

| ID | Kết quả quan sát |
|---|---|
| R2-01 | `functions[].description` chứa AWS key giả vẫn được gửi nguyên ra upstream, HTTP 200 |
| R2-02 | `medium=allow`: email nguyên văn xuất hiện trong `dlp_audit_logs.prompt_snippet` |
| R2-03 | Tool message mất `tool_call_id` trước egress |
| R2-04 | Text part có `text` là dict gây TypeError không xử lý |

Các mục DNS/SSE/policy/cancellation/error leakage dưới đây được xác định qua đọc code; cần viết test tái hiện trước khi sửa, không ghi là đã kiểm chứng end-to-end. Không kiểm tra DNS/provider thật, browser hoặc Windows startup trong vòng review này.

## 2. Ràng buộc chung cho AI triển khai

1. Mỗi ID có test hồi quy kiểm tra hành vi thực qua route/transport phù hợp; không dùng test chỉ tìm chuỗi trong source để chứng minh bảo mật.
2. Test dùng `tmp_path`, config mới hoặc deep copy; không sửa `get_config()` cache, DB thật, credential, script startup hoặc tiến trình thật. Mock mọi outbound network, kể cả harvester và DNS.
3. Payload/credential/raw response nhạy cảm chỉ ở bộ nhớ request. Không ghi raw secret vào SQLite, WAL, log, exception trả client hoặc trạng thái dashboard.
4. Giữ API `/v1/chat/completions`, `/v1/models`, dashboard và single-worker. Giữ các sửa auth, XSS, price filtering, alias dedup, quota và atomic approval đã có.
5. Không hạ test assertion, tắt DLP hoặc cho qua unknown fields để làm test xanh. Thay test cũ chỉ khi expectation mâu thuẫn contract mới; ghi rõ lý do.
6. Tách input validation, DLP decision, outbound assembly và audit projection thành các bước rõ ràng. Không dùng cùng một object cho payload egress có policy allow và audit bắt buộc redact.

## 3. R2-01 — P1: Đóng đường bypass DLP qua trường chưa scan

**Vị trí:** `src/router/openai_proxy.py`: `ChatCompletionRequest`, `outgoing_payload = dict(raw_body)`; `src/security/sanitizer.py`: `sanitize_messages`.

**Nguyên nhân:** Pydantic kiểm tra một phần request nhưng proxy lại forward raw body, chỉ thay `messages` và `tools`. Trường khác như legacy `functions` không được scan hoặc từ chối.

**Payload tái hiện:**

```json
{
  "model": "groq/test",
  "messages": [{"role": "user", "content": "hello"}],
  "functions": [{"name": "lookup", "description": "AKIAIOSFODNN7EXAMPLE"}]
}
```

Setup test: DLP bật, high=reject; seed model active trong DB tạm; mock egress trả completion 200 và capture body. Hiện tại thấy key giả trong captured body.

**Hành vi bắt buộc:**

- Tạo outbound từ schema đã validate và sanitizer output; không copy raw body làm payload nền.
- Có danh sách explicit các field hỗ trợ, phân biệt scalar điều khiển và field mang nội dung. Unknown field trả 400 `unsupported_parameter` trước egress; không âm thầm strip rồi giả vờ đã hỗ trợ.
- Giữ các tham số phổ biến đã được hỗ trợ như model, messages, stream, tools, max_tokens, temperature, stream_options. Bổ sung field điều khiển thường dùng khi cần tương thích: tool_choice, parallel_tool_calls, top_p, stop, n, seed và max_completion_tokens, với validation tương ứng. Không bắt buộc thêm một field nếu không triển khai an toàn; khi đó trả lỗi rõ.
- Quyết định cố định cho legacy `functions`/`function_call`: hỗ trợ đầy đủ và scan hoặc từ chối 400 rõ ràng. Nếu hỗ trợ, cả tool definitions, descriptions, schema/default/example và argument values phải qua DLP.
- `response_format`/JSON schema, metadata/user và extension khác chỉ được gửi khi có contract kiểm tra rõ. Default an toàn là từ chối field chưa hỗ trợ.
- Không bỏ sót dữ liệu nhạy cảm trong dictionary keys hoặc nested extension. Keys/IDs/name là định danh giao thức: không tự sửa tùy tiện vì có thể phá schema; phát hiện dữ liệu nhạy cảm ở vị trí không thể redact an toàn thì từ chối request.
- Text multipart chỉ nhận field hợp lệ; trường phụ kiểu `{type:"text", text:"hello", extra_secret:"..."}` phải bị từ chối, không forward nguyên dict.

**Test nghiệm thu:**

1. Payload trên: 403 nếu hỗ trợ và phát hiện secret, hoặc 400 nếu không hỗ trợ; upstream call count=0.
2. Cùng secret ở response_format/schema, unknown top-level field, tool extension và text-part extension: không có đường forward nguyên văn.
3. Request sạch với tools được hỗ trợ vẫn 200, outbound giữ đúng schema và các tham số điều khiển.
4. Deep nested secret, schema property key chứa secret: bị xử lý theo contract, không mất kiểm soát do chỉ scan dict values.
5. DB/log/error/status không chứa sentinel; raw request không bị mutate khi sanitizer chạy.

## 4. R2-02 — P1: Audit phải redact độc lập với policy allow

**Vị trí:** `sanitizer.py`: bỏ replacement khi action=allow; `openai_proxy.py`: lấy prompt_snippet trực tiếp từ sanitized_messages; `approval_gate.py`: persist sanitized payload.

**Tái hiện:** cấu hình `medium=allow`, gửi `contact alice@example.com`; upstream được phép nhận email theo policy nhưng SQLite hiện cũng lưu email nguyên văn.

**Thiết kế bắt buộc:**

- Tách ít nhất hai projection: `outbound_payload` tuân thủ allow/redact/approve; `audit_payload` luôn redact tất cả findings bất kể outbound action.
- Findings/reverse map dùng chung trong memory được, nhưng không lưu reverse map hoặc raw matched_text. Audit không được scan lại token đã thay rồi mất mapping/nhận diện sai.
- Pending review lưu audit projection. Kể cả HIGH=approve + MEDIUM=allow, pending DB không chứa email hoặc secret nguyên văn.
- Kiểm soát cả metadata do client nhập như requested model, name, IDs, descriptions: không lưu thẳng nếu có thể chứa dữ liệu nhạy cảm. Metadata ngoài contract phải bị từ chối hoặc chuyển thành biểu diễn an toàn.
- `ApprovalGate.create_request` không được chọn `raw_payload` làm safe_payload qua fallback. Bỏ tham số raw không cần thiết hoặc từ chối việc persist raw; cập nhật caller/test. Tên biến `safe_payload` không phải bằng chứng đã sanitize.
- Action audit phản ánh đúng: allow không được ghi REDACTED như thể đã thay token ở egress. Nếu mixed actions, lưu metadata tổng hợp và số redact thực tế; không suy từ tổng findings.
- Không tự xóa log lịch sử. Ghi rõ sửa này ngăn bản ghi mới; việc làm sạch dữ liệu cũ cần quy trình riêng.

**Test nghiệm thu:**

1. medium=allow: upstream có email, mọi record audit không có email.
2. high=approve + medium=allow: pending record và snippet không chứa hai sentinel; sau approve, outbound đúng policy.
3. Test high reject, redact, timeout, admin reject, error paths; kiểm tra toàn bộ field JSON/text trả từ SQLite, không chỉ snippet.
4. Dùng caplog kiểm tra sentinel không xuất hiện. Khi kiểm tra persistence, query DB/WAL theo môi trường test; không tuyên bố forensic wiping dữ liệu cũ.

## 5. R2-03 — P2: Giữ nguyên định danh tool calling

**Vị trí:** `ChatMessage` thiếu `tool_call_id`; `model_dump(exclude_none=True)` làm mất dữ liệu protocol. Sanitizer còn redact `messages[].name` như text thường.

**Payload tái hiện tối thiểu:**

```json
{"model":"groq/test","messages":[{"role":"tool","tool_call_id":"call_123","content":"ok"}]}
```

Payload tối thiểu nhằm bắt mất field, không chứng minh hội thoại hợp lệ với provider. Test thành công thực tế phải có assistant tool_call đi trước và tool response có ID tương ứng.

**Sửa:**

- Schema role rõ: system/developer/user/assistant/tool; legacy function role chỉ khi hỗ trợ rõ. Tool role yêu cầu tool_call_id không rỗng; assistant content=null hợp lệ nếu có tool_calls.
- Giữ nguyên tool_call_id, tool_calls[].id/index/type, function.name và message name hợp lệ. Không pseudonymize ID/name khiến request/response mất liên kết; nếu định danh chứa dữ liệu vi phạm policy thì từ chối.
- Kiểm tra shape tool call/function object và arguments đúng string JSON theo phạm vi hỗ trợ, không để dict tùy ý đi vào pipeline.
- JSON arguments có escaped quote, backslash, newline vẫn hợp lệ sau sanitize và restore. Không dùng replace trực tiếp làm hỏng JSON khi original chứa quote.
- Non-stream output hỗ trợ restoration content và tool/function arguments nhất quán. Không tự sửa tên hàm hoặc tool ID.

**Test nghiệm thu:** hội thoại user -> assistant(tool call) -> tool result -> assistant giữ nguyên IDs; tool message thiếu ID trả 400; n>=2 và nhiều tool không tráo ID; khôi phục argument chứa quote/backslash parse JSON được và bằng giá trị gốc.

## 6. R2-04 — P2: Validation đầy đủ, không lộ input trong lỗi

**Vị trí:** `ChatMessage.content` dùng `List[Any]`, role string không enum; sanitizer gọi regex với text dict. `except Exception as e` hiện đưa `str(e)` validation vào lỗi client, có thể lặp lại input nhạy cảm.

**Tái hiện:**

```json
{"messages":[{"role":"user","content":[{"type":"text","text":{"bad":1}}]}]}
```

**Sửa:**

- Typed nested models cho content part/tool call/function/schema được hỗ trợ; không để `Any` thay cho kiểm tra data path có thể chạy regex.
- stream là boolean strict: từ chối string `"true"`, `"false"`, số 0/1 và null nếu contract boolean không nullable. Không validate rồi lại forward raw stream string.
- Giới hạn số theo contract: n dương có trần hợp lý, max_tokens/max_completion_tokens dương và conflict rõ, temperature/top_p finite trong miền cho phép; không chấp nhận NaN/Infinity/coercion tùy tiện.
- Invalid body/field trả 400 theo một error envelope `{error:{message,type,param,code}}`; không có raw input, traceback, API key hoặc nội dung prompt. Message đủ để biết vị trí lỗi bằng path, ví dụ messages[0].content[0].text.
- Validation chạy trước DB writes, approval creation, candidate lookup và network khi có thể.

**Test matrix:** body null/list/string; messages sai kiểu/rỗng/phần tử sai; invalid role; text part text null/dict/list/number; unknown part type/extension; malformed tools; model null/number; stream string/number; numeric bounds. Dùng ASGITransport `raise_app_exceptions=False` để xác nhận HTTP không phải 500, đồng thời test không có exception uncaught. Sentinel trong input sai không được lặp lại vào error/caplog. Mọi trường hợp invalid: zero upstream calls.

## 7. R2-05 — P1: DNS kiểm tra và kết nối phải dùng cùng địa chỉ

**Bằng chứng:** `PinnedDNSAsyncTransport` chỉ có constructor và chưa gắn vào AsyncClient. `verify_dns()` trả list IP nhưng caller bỏ kết quả; HTTP client vẫn tự resolve hostname.

**Yêu cầu:**

- Kiểm tra URL và size trước DNS/network. Resolve asynchronously, từ chối bất kỳ địa chỉ private/loopback/link-local/reserved/multicast/unspecified; dùng cùng policy cho IPv4/IPv6.
- Transport/network backend kết nối trực tiếp IP đã duyệt; giữ original hostname cho Host header, SNI và certificate verification. Không đơn giản thay URL hostname bằng IP rồi tắt TLS verify.
- DNS không được resolve lại ngoài cơ chế kiểm soát trước connect. Hỗ trợ nhiều public IP qua attempt có giới hạn; không fallback sang IP chưa validate.
- Không dùng shared mutable mapping hostname->IP không có ràng buộc concurrent request. Connection pooling phải giữ host/port/TLS isolation. Quy định cache TTL rõ nếu cache DNS.
- Áp dụng GET discovery, JSON POST và streaming POST. Giữ trust_env=False và redirect disabled.
- Đọc API public của phiên bản httpx/httpcore thực dùng; nếu dùng integration nhạy phiên bản phải pin tương thích và test. Không gọi code rỗng là DNS pinning.

**Test bắt buộc ở transport/connect boundary:**

1. Resolver kiểm tra trả public IP A; resolver thứ hai giả trả private B. Connect chỉ nhận A, không thực hiện resolve ngoài kiểm soát.
2. Record connect_tcp host, TLS server_hostname và HTTP Host: lần lượt là A, original domain, original domain.
3. DNS mixed public/private từ chối toàn bộ; IPv6 và zero answer có lỗi rõ.
4. Hai request concurrent cùng/khác host không tráo IP/SNI; connection reuse không vượt policy.
5. Các test chạy với backend giả, không socket thật. Mock `egress.post` hoặc `verify_dns` riêng lẻ không đủ chứng minh pinning.

## 8. R2-06 — P2: SSE parser và lifecycle phải hoàn chỉnh

**Bằng chứng:** parser hiện bỏ blank lines rồi parse từng data line; trailing buffer bị bọc thành JSON `content` khác protocol. EOF thiếu DONE không fail. Quota vẫn ước lượng bằng số content delta; success ghi tại first probe với latency 150ms. Tool argument delta chưa restore.

**State machine:**

`connecting -> streaming -> completed | failed | cancelled`

- `completed` chỉ khi đã nhận terminal event theo contract adapter (mặc định OpenAI `[DONE]`). EOF trước đó là `upstream_incomplete`, không phải success.
- UTF-8 incremental decoder; SSE event ghép theo blank line, data fields nối bằng newline; hỗ trợ LF/CRLF, `data:` không space. Comment/heartbeat không phải content và không được coi completion thành công.
- Event JSON malformed trả lỗi protocol ổn định; không fallback bằng cách forward raw line có thể chứa dữ liệu chưa restore. Giới hạn buffer line/event để tránh memory tăng vô hạn.
- Không tạo `data:{content:raw_line_buffer}`. EOF xử lý theo parser state và báo incomplete nhất quán; không mất hoặc tự biến đổi fragment thành completion.
- Trước event completion đầu tiên được phát cho client có thể retry provider; sau đó chỉ error event rồi một DONE khi connection còn mở, không ghép provider khác vào cùng stream. Comment/heartbeat có thể bỏ để chưa commit response.
- Restorer độc lập theo `(choice_index, channel, tool_call_index)`; content không dùng chung buffer với tool arguments, legacy function argument có channel riêng nếu hỗ trợ. Giữ metadata id/model/index/type/name; flush trước finish_reason tương ứng.
- Streaming JSON arguments phải giữ escaped-string semantics khi khôi phục; ghép toàn bộ arguments cuối cùng parse JSON được. Nếu cần buffer argument để bảo đảm escaping, ghi rõ và kiểm tra độ trễ/buffer limit.
- Hỗ trợ custom redacted_token_format bằng tập token thực trong reverse_map hoặc parser tương ứng. Prefix cố định `<REDACTED_` không đủ nếu format đổi; validate format uniqueness và chống token collision trước startup/request.
- Dùng try/finally để aclose nested iterator/upstream response; CancelledError/GeneratorExit không bị đổi thành SSE error. Client disconnect không tiếp tục tiêu thụ upstream.
- Success metrics chỉ tăng đúng một lần sau completed; failure/cancel không tăng success. Latency đo bằng clock thực; phân biệt time-to-first-event và total duration nếu lưu cả hai.
- Usage dùng upstream total_tokens nếu có, kể cả 0; thiếu usage thì unknown hoặc estimate có cờ/source rõ. Không ghi chunk count vào số token thực. Failed/cancelled usage có dữ liệu thực cần được ghi nhận theo contract riêng, không làm request thành công.

**Test matrix tối thiểu:**

| Ca | Kỳ vọng |
|---|---|
| Tiếng Việt/emoji chia mọi byte | Text ghép lại bằng bản gốc |
| JSON event chia nhiều TCP chunks | Parse đúng một event |
| SSE multi-line data chứa JSON hợp lệ | Ghép trước parse |
| Placeholder chia event, n=2 interleave | Restore đúng từng choice |
| Hai tool argument stream interleave | Không tráo dữ liệu, JSON cuối hợp lệ |
| Custom token format | Restore đúng dù cắt giữa token |
| EOF trước DONE, empty body | Error, không success |
| Finish_reason rồi DONE | Không content phát muộn sau finish |
| Usage-only final event | Token chính xác, không đếm đôi |
| Lỗi trước/sau event đầu | Retry trước commit; không retry sau commit |
| Client cancel | Upstream đóng, task kết thúc, không giả success |
| Oversized/malformed event | Lỗi có kiểm soát và buffer có trần |

## 9. R2-07 — P1: Không rò raw upstream error qua log/dashboard/client

**Bằng chứng:** JSON branch gắn `resp.text` vào last_error, lưu failed_attempts; exception được str rồi log/trả client. Stream HTTPStatusError có raw response body; stream error phát `str(e)`.

**Sửa:**

- Chuẩn hóa lỗi nội bộ có code, provider, status, retryable và correlation ID. Không lưu/trả body upstream hay arbitrary exception string.
- Client nhận thông báo an toàn, dashboard failed_attempts chỉ có thông tin phân loại. Log không chứa raw prompt/header/body; debug cũng không tự bật raw logging.
- 400/422 do request không tương thích không retry toàn pool vô ích. 429 retry provider khác, 5xx/network retry trong budget; 401/403 credential/provider config xử lý rõ và không thử mọi alias cùng provider.
- JSON response invalid hoặc shape sai là protocol error, không đánh dấu success trước validation.

**Nghiệm thu:** mock upstream 400/429/500 và exception có sentinel trong body/message; capture HTTP body, SSE output, caplog, routing status và DB: sentinel không xuất hiện. Provider error vẫn đủ phân biệt status/correlation ID. Kiểm tra số attempt theo retry policy.

## 10. R2-08 — P2: Policy phối hợp và approval cancellation/race

**Bằng chứng:** medium chỉ xử lý khi `not high_findings`; high=redact có thể che medium=approve. High approval timeout không áp dụng timeout_action=redact. `cancel_request` có nhưng route/wait chưa bảo đảm gọi khi cancellation. Timeout gán memory expired mà không kiểm tra transition DB có thành công khi approve đang diễn ra.

**Contract:**

- Ưu tiên toàn request: có reject -> reject; nếu không reject nhưng có bất kỳ finding cần approve -> một approval; còn lại forward theo allow/redact. High=redact không bỏ qua medium=approve. Approval không gửi original secret; chỉ cho phép forward theo bản outbound an toàn đã xác định.
- Một request chỉ tạo một approval dù nhiều nhóm risk cần approve. UI thấy các findings liên quan bằng audit projection.
- timeout_action=redact áp dụng nhất quán cho high/medium approve: expiry chuyển thành forward bản redact toàn bộ nhóm cần duyệt; reject của admin luôn chặn. Không đánh đồng cancelled với timeout.
- Approval memory và SQLite phải cùng final decision. Timeout/approve/cancel dùng một cơ chế serialize hoặc đọc lại authoritative state khi conditional update thua race; không trả expired nếu DB đã approved.
- Cancellation dọn memory và chuyển pending DB sang cancelled khi thích hợp; giữ nguyên terminal state đã commit. Chỉ disconnect trong lúc đợi HITL cũng phải có cơ chế quan sát và cleanup có giới hạn, không giả định ASGI tự hủy task trong mọi server.
- Lifecycle shutdown phải finalize/cancel waiter và đóng resources bằng try/finally. Startup orphan cleanup chỉ phù hợp single-worker; không tự expire approval của process khác đang sống.

**Test:** high redact + medium approve chờ admin; nhiều finding chỉ một approval; matrix high/medium timeout redact/reject; admin reject không forward; race điều khiển bằng asyncio.Event giữa DB transition và event notification (không dựa sleep ngẫu nhiên); cancellation/disconnect/restart không orphan; DB failure không để memory/DB báo hai kết quả khác nhau.

## 11. R2-09 — P2: Hoàn thiện routing context và test isolation

- Context budget hiện `text //4 + tools //4 + 1000`, bỏ tool arguments/multipart và output yêu cầu. Tính trên payload cuối được phép gửi; gồm toàn lịch sử/tool/schema, output reserve từ max_tokens/max_completion_tokens. Dùng tokenizer nếu sẵn hoặc ước lượng bảo thủ có mô tả, không gọi API đếm token mặc định. Explicit model cũng phải đủ budget.
- Model explicit không tồn tại không được âm thầm route model khác: trả 404 `model_not_found`; explicit model không đủ context trả 400 `context_length_exceeded`. Chỉ auto mode xây fallback tự động theo contract; nếu muốn fallback cho explicit, phải có option rõ và test.
- Một số regression test hiện `config=get_config(); config.db_path=...` vẫn mutate cache. Chuyển mọi test sang config riêng/deep copy; fixture assert cache sau test không đổi. Không chỉ sửa các file cũ rồi bỏ test mới.
- Test app lifecycle phải mock discovery, không ghi `.gateway.pid` thật. PID writer có thể inject/disable cho test hoặc chuyển path vào thư mục instance tạm.

**Nghiệm thu:** prompt tool arguments dài + output reserve vượt context không route; explicit unknown không gọi upstream; suite chạy lặp/đổi thứ tự không phụ thuộc cache/key/DB thật; lifespan đóng client/background task không leak.

## 12. Phạm vi còn phải kiểm tra trước tuyên bố hoàn tất vòng 1

Không tự đánh dấu F01–F11 hoàn thành chỉ vì sửa R2-01–R2-09. Lập bảng đối chiếu từng yêu cầu ban đầu; đánh dấu implemented/tested/remaining. Đặc biệt kiểm tra browser XSS/auth/offline, single-worker enforcement, PID reuse/start-time verification và static model/free-tier account policy. Những phần chưa chạy thực tế phải ghi rõ chưa xác minh, không suy từ test source-string.

## 13. Thứ tự thực hiện và bàn giao

1. Viết test tái hiện R2-01–R2-04 bằng DB tạm/upstream giả; xác nhận chúng fail trên baseline. Cô lập config trong test trước.
2. Sửa schema/outbound boundary và audit projection; giữ tool protocol. Sau đó sửa DNS transport và redaction error boundary.
3. Sửa policy/approval lifecycle, SSE parser/restorer/metrics; thêm test transport/cancellation/race có điều khiển.
4. Hoàn thiện context/explicit model behavior; cập nhật README nêu giới hạn thật, không dùng các từ bảo đảm tuyệt đối khi còn đường chưa test.
5. Chạy suite offline với basetemp mới trong workspace. Không bắt buộc giữ đúng số 47 test; số test sẽ tăng. Không dùng số lượng test làm tiêu chí an toàn.

**Definition of Done:**

- Mọi test hồi quy đã mô tả chạy được và pass; test cũ pass với thay đổi expectation được giải thích.
- Các lỗi đã tái hiện không còn tồn tại; có bằng chứng egress capture, audit query, transport boundary và lifecycle phù hợp.
- Không dùng key/network/DB thật, không thay startup Windows, không deploy trong quá trình sửa.
- Báo cáo bàn giao từng ID: trạng thái, file/hàm thay đổi, tên test, lệnh và kết quả chạy thật, giới hạn chưa giải quyết. DNS pinning/SSE correctness không được đánh dấu xong bằng comment hoặc class chưa dùng.
- Nếu chưa xử lý hết, ghi rõ các ID còn mở và tác động; không gọi bản sửa là hoàn tất toàn bộ spec.

## Prompt giao cho AI triển khai

> Đọc `FIX_SPEC_ROUND_2.md` và `FIX_SPEC.md`, kiểm tra code hiện tại trước khi sửa. Triển khai lần lượt các mục R2-01 đến R2-09 bằng code hoàn chỉnh và test hồi quy chạy offline. Giữ auth/XSS/free-price/quota/atomic transition đã sửa. Không sử dụng credentials, provider thật hay DB gateway thật trong test. Không chỉ sửa để 47 test cũ pass: phải tái hiện và loại bỏ các đường bypass, mất tool_call_id, audit raw allow, input gây 500, DNS resolve lại và SSE/lifecycle sai. Chạy test bằng basetemp mới dành riêng trong workspace. Bàn giao bảng từng ID với bằng chứng test và mục còn thiếu; không deploy hoặc thao tác Windows startup.
