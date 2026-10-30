# AI Free Gateway — Spec củng cố độ ổn định và nghiệm thu thực tế

Ngày: 2026-10-29.

## 1. Định hướng

Ưu tiên tiếp theo là giới hạn tài nguyên, tính đúng của tool/SSE, quản lý vòng đời và kiểm chứng an toàn trình duyệt. Chưa thêm provider hoặc làm lại dashboard. Giữ FastAPI, httpx, SQLite, giao diện hiện tại và Windows.

Tài liệu này bổ sung `FIX_SPEC.md` và `FIX_SPEC_ROUND_2.md`. Không thay thế các yêu cầu chưa hoàn thành của hai spec trước.

Baseline lần chạy gần nhất: **73 test pass trong 21.11s**, gồm 11 test mới cho DLP control fields/schema keys, raw error logging, tool index và success count. Đây là kết quả lần sửa ngay trước khi viết tài liệu; chưa chạy lại suite trong lần viết spec này.

Phân biệt bằng chứng:

- **Đã sửa và test:** bốn nhóm lỗi nêu trên. Phải giữ các test này.
- **Quan sát từ code:** SSE chưa có trần buffer rõ; tool arguments đang restore bằng string replacement; timeout approval dùng điều kiện unexpired khi expire; startup chưa có instance lock; dependencies có lower bounds và có dùng API nội bộ httpcore/httpx.
- **Đề xuất cần test xác nhận:** các race/cancellation, browser Host/Origin, PID reuse, hành vi provider lỗi trong danh sách dưới đây. Không gọi tất cả là lỗ hổng đã khai thác.

## 2. Quy tắc thực hiện

1. Mỗi mục có test hành vi tại boundary liên quan. Không dùng grep source thay cho test bảo mật.
2. Test mặc định offline, config deep copy, DB/PID/lock/log tạm; không dùng credential hay storage thật. Dùng fake transport/socket backend, fake clocks và asyncio.Event cho race.
3. Không xóa dữ liệu runtime hiện có, không kill process thật, không thay autostart hoặc deploy trong quá trình triển khai/test.
4. Không ghi prompt, reverse map, Authorization hay raw upstream body vào audit/log/error. Giữ auth và các bản sửa đã có.
5. Mỗi mục là một thay đổi có thể review độc lập. Không làm migration dữ liệu lớn hoặc chuyển framework để giải quyết lỗi nhỏ.
6. Khi cần chọn API transport/SDK hoặc model catalog hiện hành, kiểm tra source/package đang cài và tài liệu chính thức; không dựa trên trí nhớ. Các giá trị resource limit dưới đây là mặc định đề xuất, không phải số đo hiệu năng đã đạt.

## 3. N01 — P1: Giới hạn tài nguyên trước khi đọc/giải mã dữ liệu

**Mục tiêu:** một client/upstream không làm gateway tăng RAM vô hạn hoặc giữ toàn bộ kết nối.

**Phạm vi:** `main.py`, `openai_proxy.py`, `egress_guard.py`, `config_loader.py`.

### Contract

- Thêm `server.max_body_bytes`, mặc định 2 MiB, cấu hình được và phải >0. Đếm bytes ASGI thực nhận trước JSON parse; không tin Content-Length. Khi quá trần trả 413 OpenAI error, không scan, không tạo approval, không gọi upstream.
- Với body không Content-Length hoặc chia nhiều chunks, vẫn áp dụng cùng trần. Content-Length hợp lệ có thể giúp từ chối sớm; không dùng nó làm kiểm tra duy nhất.
- Thêm admission limit: mặc định tối đa 8 completion đang được xử lý và 32 approval đang chờ. Dashboard/health/decision phải còn phục vụ được khi completion đầy. Request vượt giới hạn trả 429 `gateway_overloaded` với Retry-After ngắn; không âm thầm enqueue vô hạn.
- Approval đang chờ không giữ slot network upstream; phải có slot riêng để người duyệt không bị deadlock. Mọi slot release trong finally khi reject, timeout, lỗi và client disconnect.
- Trần upstream JSON response 16 MiB; đọc incrementally và dừng trước khi load toàn bộ JSON vượt trần. Áp dụng cả discovery catalog với limit riêng nếu catalog cần lớn hơn.
- SSE có trần bytes line 256 KiB và event 1 MiB; đếm trước khi append/gộp JSON. Giới hạn buffer cho tool argument/restorer; khi vượt trả protocol error an toàn rồi đóng upstream.
- Cấu hình `first_event_timeout_seconds=30`, `idle_timeout_seconds=60`, `max_stream_duration_seconds=600`. Định nghĩa first event là completion data hợp lệ, heartbeat không kéo dài vô hạn deadline tổng. Cho phép người vận hành tăng các giá trị; không retry sau khi đã phát completion đầu tiên.
- Completion non-stream cũng có deadline tổng qua mọi attempt; không nhân timeout theo kích thước model pool.

### Nghiệm thu

- Test body vượt trần có/không Content-Length và chia nhiều chunks; transport call count=0, không DB approval/audit raw.
- Test SSE không có newline, event nhiều data lines và tool buffer vượt trần: kết thúc có kiểm soát, memory buffer có cận.
- Test admission limit và slot release sau success/reject/error/cancel. Khi đầy, API approval vẫn quyết định được request đang chờ.
- Fake clock/timeout kiểm tra heartbeat-only stream, stalled stream, many-candidate fallback và deadline tổng. Không cần test tải kéo dài hoặc mạng thật.

## 4. N02 — P1: Hoàn thiện tool arguments và terminal SSE

**Mục tiêu:** mọi tool arguments ghép cuối cùng parse JSON đúng và trả đúng nguyên bản theo reverse map.

**Bằng chứng code:** hiện restore function arguments bằng `restore_text`, có thể đưa quote/backslash/newline trực tiếp vào chuỗi JSON arguments. SSE đã tách tool index nhưng còn flush tool buffer tại DONE thay vì bảo đảm trước finish_reason.

### Contract

- Với JSON response, parse arguments thành JSON, restore string values đệ quy, rồi json.dumps lại; không sửa dict keys/identifier tùy tiện. Arguments upstream invalid JSON là protocol error theo contract, không được báo thành công rồi trả output hỏng.
- Với SSE, chọn và document một chiến lược: buffer đầy đủ arguments theo `(choice_index, tool_index)` đến khi tool/choice kết thúc, hoặc parser JSON incremental có escaping đúng. Khuyến nghị buffer có trần từ N01 vì dễ kiểm chứng; metadata id/type/name vẫn được chuyển đúng.
- Content, reasoning_content và từng tool argument có state độc lập. Giữ index thật trong delta; không dùng vị trí trong list.
- Flush mọi channel trước finish_reason của choice. Sau finish_reason không phát thêm content/arguments cho choice đó. `[DONE]` phát đúng một lần; dừng đọc upstream sau terminal event theo adapter contract.
- Nếu event JSON malformed hoặc sai shape, kết thúc failed; không phát lỗi rồi tiếp tục đọc đến DONE để ghi success.
- Empty stream hoặc EOF thiếu DONE: trước khi commit response có thể fallback, sau commit thì lỗi stream rồi đóng. Không ghi success cho stream này.
- Success/usage không đếm đôi và không biến chunk count thành token thực. Thiếu usage thì đánh dấu unknown/estimated rõ; dashboard không trình bày là số token chính xác.

### Test matrix

1. Giá trị gốc có `"`, `\\`, newline, Unicode và chuỗi trông giống placeholder; arguments khôi phục phải parse được và bằng dữ liệu gốc.
2. Hai choices, hai tools interleave, delta chỉ chứa tool index 1, token chia tại mọi vị trí; không trộn dữ liệu.
3. Finish_reason đến khi restorer còn tail: tail phải xuất hiện trước/tại finish event, không sau đó.
4. Event malformed rồi upstream DONE không tăng success. DONE lặp không phát hai terminal marker và không tiếp tục tiêu thụ dữ liệu.
5. Empty/heartbeat-only/EOF và lỗi trước/sau first completion event có đúng fallback boundary.
6. Usage 0, usage-only final event, thiếu usage: model metrics/quota đúng contract, một success tối đa.

## 5. N03 — P1: HITL cancellation và trạng thái DB nhất quán

**Phạm vi:** `approval_gate.py`, `openai_proxy.py`, `db.py`.

### Contract

- Transition duy nhất từ pending đến approved/rejected/expired/cancelled. Thời hạn hết phải dùng `expires_at <= now`, không dùng predicate còn hạn để expire.
- Khi conditional transition thua race, đọc authoritative state và trả đúng kết quả đó. Một getter tự expire bản ghi không được là điều kiện để waiter hoạt động đúng.
- Client disconnect trong lúc đợi duyệt phải dọn memory, finalize pending thành cancelled nếu vẫn pending, và không gọi upstream sau đó. Không giả định mọi ASGI server tự cancel handler khi client rời đi.
- Watcher disconnect và waiter có vòng đời chung, task thua cuộc phải cancel/await. Không chạy polling loop không giới hạn hoặc tạo task mồ côi.
- Cancellation không được ghi đè quyết định admin đã commit. Nếu đã approved nhưng client rời trước egress, dừng request, ghi delivery cancellation riêng nếu cần; không sửa lịch sử quyết định.
- Startup/shutdown cancel waiter và đóng tài nguyên trong finally, kể cả lỗi discovery hoặc lỗi sau yield lifespan.
- Không lưu payload gốc vào pending. Metadata như requested model phải qua boundary an toàn trước persistence.

### Nghiệm thu

- Điều khiển race bằng Event: approve commit trước notification, timeout xảy ra đúng lúc; memory/DB phải thống nhất.
- Test expire trực tiếp bằng query SQL không qua getter có auto-expiry; DB không còn pending sau timeout.
- ASGI disconnect lúc pending: gate count về 0, không outbound, không watcher còn sống.
- Cancel/error DB/forced shutdown không làm mất slot từ N01 và không để client/task leak.

## 6. N04 — P1: Kiểm chứng Origin/Host và trình duyệt local

**Quan sát:** middleware so Origin host/port nhưng chưa so đầy đủ scheme; request hostname đến từ Host client. Cần kiểm tra cả DNS rebinding phía browser, độc lập với egress DNS pinning.

### Contract

- Validate Host bằng allowlist từ địa chỉ gateway cấu hình và `allowed_hosts` explicit. Mặc định loopback chỉ nhận localhost/127.0.0.1/[::1] với port hợp lệ; từ chối Host domain lạ trước route xử lý state. Không tự tin forwarded headers từ Internet.
- Same-origin so đầy đủ scheme/host/effective port. Origin null, URL malformed, port không hợp lệ trả 403/400 có kiểm soát, không 500.
- Origin được allow phải match cấu hình chính xác; không mở wildcard credentials. CLI không Origin vẫn theo auth/local policy hiện có.
- Request có Authorization header Unicode/sai encoding phải trả lỗi auth có kiểm soát; normalize key bytes hoặc validate trước constant-time compare để không gây 500.
- Dùng browser test cho auth, API decision, XSS seed và offline assets. Không chèn thật API key vào screenshot/log/browser storage lâu dài trong test.

### Nghiệm thu

- Evil Host + Origin trùng evil Host vẫn bị chặn; IP socket local không đủ để coi Host tin cậy.
- Same host/port khác scheme không được coi same-origin. Origin malformed/null không gây exception.
- Key bật: read/write thiếu key bị 401, đúng key hoạt động; cross-origin bị chặn trước thao tác state.
- Seed model/request có HTML/attribute payload: không chạy script và hiển thị như text. Browser network khi mở dashboard không gọi CDN/telemetry.
- Nếu chưa có môi trường browser, ghi rõ kiểm tra nào chưa chạy; không dùng test tìm `innerHTML` để thay kết quả browser.

## 7. N05 — P2: Vòng đời instance, single-worker và Windows stop

**Quan sát:** `.gateway.pid` hiện chứa PID đơn; stop script match command line khá rộng và phụ thuộc cwd. Startup cleanup có thể tác động instance khác nếu chạy nhiều worker.

### Contract

- Acquire OS-backed lock theo đường DB/instance trước cleanup orphan/start harvester. Instance thứ hai cùng DB fail startup rõ; không ghi đè PID, không expire approval của process còn sống. Khác DB được chạy độc lập.
- Lock tự release khi process chết; không dựa hoàn toàn vào file tồn tại để kết luận instance đang sống.
- Instance metadata có PID, process creation time, executable path, project root và instance UUID; ghi atomic. Không chứa credential.
- Start/stop scripts resolve theo `%~dp0`, đường có khoảng trắng vẫn chạy đúng.
- Stop xác minh PID + creation time + executable/project identity; nếu metadata stale/malformed hoặc identity không khớp thì không kill. Không kill mọi process theo port hoặc substring chung `src.main`.
- Ưu tiên shutdown có giới hạn nếu có kênh phù hợp, sau đó chỉ force-stop đúng instance đã xác minh. Không thêm unauthenticated HTTP shutdown endpoint.
- Lifespan try/finally đóng harvester/client/waiter và chỉ xóa metadata do instance đó sở hữu.

### Nghiệm thu

- Tests process API/metadata dùng fake: PID reuse, process khác cùng tên module, stale metadata, project path có space, cwd khác.
- Integration lock có thể dùng process con test ngắn với DB tạm; không chạm gateway thật hoặc autostart registry.
- Startup fail không xóa PID instance khác, không để client/network task leak. Một lần shutdown sạch xóa đúng metadata.

## 8. N06 — P2: Retry budget và provider capability rõ ràng

### Contract

- Phân loại lỗi: request/schema 400/422 kết thúc có lỗi an toàn hoặc capability mismatch đã xác định; không thử mọi model mù quáng. 401/403 provider loại provider đó trong request; 429 skip provider + cooldown; 5xx/network chỉ retry trong deadline/attempt budget.
- Đề xuất `max_attempts=3`; không retry provider đã fail auth hoặc cooldown bằng alias khác. Retry-After hỗ trợ số giây và HTTP date, clamp giới hạn và không vượt deadline tổng.
- Candidate phải có enabled/credential/capability phù hợp. Thiếu key trả unavailable reason rõ trước network; không chỉ hạ điểm rồi vẫn gửi request không auth.
- Capability metadata tối thiểu: text, tools, JSON schema và stream. Unknown capability không được quảng cáo là verified; fallback không tự bỏ tools/response_format để ép request chạy.
- Explicit model không tự đổi provider/model trừ option được document. Auto mode ghi lựa chọn thực, số attempt và lý do fallback an toàn.
- Giới hạn lịch sử không được âm thầm cắt một tool call khỏi tool result. Nếu prune đang có, chỉ cắt theo nhóm hội thoại hoàn chỉnh và thông báo contract; mặc định từ chối context overflow rõ ràng khi không bảo toàn cấu trúc.

### Nghiệm thu

- Matrix 400/401/403/429/5xx/network với nhiều alias/model: đúng số attempt, đúng provider skip và không raw error leak.
- tools/json_schema không route sang candidate không hỗ trợ hoặc tự bỏ tham số.
- Missing key không gọi upstream. Explicit unknown/overflow giữ đúng error codes đã có.
- Test Retry-After date, clock skew và bounded deadline bằng fake clock; không sleep dài.

## 9. N07 — P2: Dependency, DNS transport và dữ liệu runtime có thể quản lý

### Contract

- Tách runtime và dev/test dependencies; tạo constraints/lock với phiên bản đã test, ghi Python version hỗ trợ và lệnh cài tái lập. Không tự nâng major khi chưa test.
- Vì transport đang dùng `_pool`, `_backends`, `_exceptions` nội bộ, ghi rõ phiên bản tương thích; test socket boundary, TLS SNI/Host và cleanup cho từng phiên bản được hỗ trợ. Ưu tiên API công khai khi có thể, không dùng try/except nuốt lỗi để giả tương thích.
- Pinned backend phải fail closed nếu domain chưa có pin khi policy bật; không fallback kết nối hostname chưa validate. Concurrent requests không tráo endpoint; mọi IP connect phải public và đã được kiểm tra. Định nghĩa cache TTL/multi-IP fallback rõ.
- .gitignore bao phủ DB-WAL/SHM/journal, runtime log, temp test artifacts và instance metadata. Không ignore toàn bộ fixture test cần commit.
- Retention configurable, đề xuất audit/pending finalized 30 ngày. Chỉ purge record hết retention, batch có giới hạn, không xóa pending sống. Không hứa xóa forensic secret khỏi bản backup lịch sử.
- Thêm backup/restore hướng dẫn dùng SQLite backup API nhất quán khi WAL đang hoạt động, tránh chỉ copy file DB. Restore không chạy trên DB đang mở; không tự ghi đè dữ liệu người dùng trong test.
- Migration có schema version, transaction và test DB cũ -> mới; chạy lặp không phá dữ liệu.

### Nghiệm thu

- Fresh install từ lock chạy suite offline khi package cache sẵn; nếu cần network tải package, báo riêng điều kiện và kết quả.
- DNS test chứng minh connect IP đã duyệt, SNI/Host gốc, unknown pin fail closed; không chỉ assert dict pin tồn tại.
- `git status` không hiện WAL/log/temp mới. Migration giữ model metrics/audit cần thiết; backup restore DB tạm cho dữ liệu bằng nhau.
- Retention không xóa pending sống và không giữ reverse map/raw secret để phục vụ observability.

## 10. N08 — P2: Trạng thái vận hành phản ánh sự thật

### Contract

- `/health` chỉ liveness, không dùng key provider hay gọi mạng. Readiness riêng trả initialized/catalog usable/pending startup theo thông tin an toàn; chi tiết provider lỗi nằm ở dashboard có auth.
- Phân biệt active đang phục vụ, last_success và preferred candidate. Không hiển thị vừa probe stream là request đã thành công.
- Hiển thị latency đo thực với nhãn rõ TTFT/total, quota source actual/estimated/unknown, catalog fresh/stale và thời điểm cập nhật.
- Mỗi request có correlation ID ngẫu nhiên; lưu status/provider/model/attempt/duration và mã lỗi an toàn. Không log body hay response text.
- README có bảng supported parameters/modalities, allow vs audit policy, limitation single-worker, free model vs account free-tier, retry/pruning và cấu hình giới hạn mới.
- Không ghi cam kết an toàn tuyệt đối hoặc miễn phí tuyệt đối chỉ dựa trên model name, test count hoặc catalog snapshot.

### Nghiệm thu

- Streaming fail/cancel không hiện last_success mới; completed cập nhật một lần.
- Thiếu usage hiển thị unknown/estimate, không giả chính xác. Catalog harvest fail vẫn hiển thị stale cùng snapshot theo policy.
- Correlation ID liên kết được client safe error với log/status nhưng không chứa dữ liệu prompt.

## 11. Lộ trình đề xuất

| Đợt | Nội dung | Điều kiện hoàn tất |
|---|---|---|
| A | N01 + N02 | Input/stream bounded, arguments JSON đúng, terminal/metrics đúng |
| B | N03 + N04 | Không orphan waiter, Host/Origin/auth có test hành vi |
| C | N05 + N06 | Instance ownership rõ, retry/capability/deadline ổn định |
| D | N07 + N08 | Cài đặt tái lập, runtime sạch, trạng thái và tài liệu đúng |

Không cần triển khai tất cả trong một commit. Hoàn thành từng đợt và giữ baseline trước khi chuyển tiếp; không bỏ P1 để làm đẹp UI.

## 12. Definition of Done và bàn giao

- Baseline 73 test tiếp tục pass; bổ sung test có ý nghĩa cho từng ID. Không đặt mục tiêu số test thay mục tiêu hành vi.
- Chạy toàn suite với basetemp mới dành riêng trong workspace; không dùng thư mục storage chung làm basetemp. Ghi lệnh và kết quả thực chạy.
- Có bảng N01–N08: implemented/tested/remaining, file thay đổi, tên test, giới hạn còn lại. Ghi rõ browser/process/real-provider checks nào chưa chạy.
- Mọi cấu hình mới có default, validation, config example và migration/compatibility note nếu đổi behavior.
- Không gọi là production-ready nếu còn terminal SSE sai, resource unbounded, instance không xác minh được hoặc browser boundary chưa kiểm chứng.
- Không deploy, đổi credential, xóa dữ liệu người dùng hoặc thao tác autostart trong phạm vi triển khai/test spec này.

## Prompt giao AI

> Đọc `NEXT_ITERATION_SPEC.md`, `FIX_SPEC_ROUND_2.md` và code hiện tại. Bắt đầu đợt A (N01–N02), viết test tái hiện trước, triển khai code hoàn chỉnh và chạy toàn suite. Giữ 73 test baseline và các bản sửa bảo mật hiện có. Dùng DB/config/network/process giả trong test, không gọi provider thật hoặc chạm DB/PID gateway thật. Khi bàn giao, báo từng ID cùng test và giới hạn chưa xử lý; không tự tuyên bố hoàn tất các đợt chưa làm. Chỉ tiếp tục B–D khi phạm vi người dùng yêu cầu bao gồm chúng.
