# T3/G1 independent re-review — 2026-10-16

Reviewer: Codex, độc lập với Claude là agent sửa Makefile và plan.
Scope: follow-up của R1/P2 và R2/P3 trong `../review-20261016-codex/review.md`.
Không sửa product code, plan, báo cáo cũ, migration SQL hoặc persistent DB.

**Verdict: REQUEST CHANGES — R2 đóng; R1 sửa được dấu & nhưng còn lỗi quoting.**

So sánh SHA-256 với snapshot source của review trước: chỉ `Makefile` và
`.omo/plans/eventlens-ai-rag-mvp.md` thay đổi. Vì vậy không chạy lại toàn bộ suite
Python/web/storage đã đạt ở review trước; chạy kiểm tra trực tiếp cho thay đổi này
trên container pgvector riêng bằng harness hiện có.

## Finding còn lại

### R1 follow-up — P2: Nháy đơn trong URL hợp lệ vẫn phá recipe shell

Vị trí: `Makefile:47`, `Makefile:50`, `Makefile:53`.

Recipe hiện dùng `EVENTLENS_RAG_DATABASE_URL='$(RAG_DATABASE_URL)'`. Cách này giữ
được `&`, nhưng không escape nháy đơn có sẵn trong giá trị URL. Đã tái hiện với
URL hợp lệ `postgresql://…/rag_test?sslmode=disable&application_name=g1_review'quoted`.
Direct CLI `migrate --database-url <URL>` exit 0 trên DB thật. Cùng URL qua
`make rag-migrate RAG_DATABASE_URL=<URL>` exit 2 với `unexpected EOF while looking
for matching ...` và `syntax error: unexpected end of file`, trước khi gọi CLI.

Đây vẫn là lỗi truyền cấu hình URL qua shell, không phải lỗi kết nối DB. Ba
target có cùng cách ghép chuỗi nên đều cần xử lý. Không có shell payload hoặc
lệnh tùy ý nào được chạy trong probe; chỉ dùng một nháy đơn trong application_name.

Khắc phục: export URL qua environment của Make rồi gọi CLI mà không ghép giá trị
URL vào source shell; hoặc dùng cơ chế shell escaping đầy đủ, bao gồm nháy đơn.
Giữ kiểm tra URL nhiều parameter và thêm case chứa nháy đơn. Không chỉ đổi sang
nháy kép vì cách đó vẫn để shell diễn giải một số ký tự trong cấu hình.

## Kết quả xác minh mới

| Check | Kết quả |
| --- | --- |
| Migrate bằng Make với `?sslmode=disable&connect_timeout=2&application_name=g1_rereview`, chạy hai lần | 0 / 0 |
| Load fixtures bằng Make, cùng URL | 0; 30 bài, lexical 30/vector 0 trước rebuild |
| Rebuild bằng Make, cùng URL, chạy hai lần | 0 / 0; 30 published rồi reuse 30/30 |
| SQL sau rebuild | 30 current vectors, 30 generations |
| Direct CLI với nháy đơn trong application_name | 0 |
| Make với chính URL chứa nháy đơn đó | 2; shell syntax error (R1 follow-up) |
| Make migrate với URL có `&` trỏ port không có DB | Make 2, CLI 3; `rag_storage_unavailable`, không còn thiếu database-url |
| R2/P3 — plan trạng thái T1/T2 | PASS: đoạn cũ có nhãn Historical, đoạn Current khớp checklist và giữ G1 OPEN |
| `rtk git diff --check` | 0 |
| Container `eventlens-rag-test-*` sau khi chạy | Không còn |

Reproducer: `verify.py`. Raw log: `verification.log`.
Command chạy: `rtk proxy sh apps/rag/with-test-db.sh uv run --package eventlens-rag python .omo/evidence/G1/rereview-20261016-codex/verify.py`.
Script exit 0 chứng minh các assertion kiểm tra đã đạt; probe nháy đơn được ghi
riêng là failure của Make, không phải test chứng nhận fix hoàn tất.

## Ảnh hưởng F1–F4 và acceptance

- F1: R2 về tài liệu đã đóng; khoảng trống D41 lịch sử giữ nguyên.
- F2: REQUEST CHANGES do R1 follow-up.
- F3: PASS URL có `&` cho cả ba Make target trên DB thật; FAIL URL chứa nháy đơn.
- F4: Không có thay đổi scope mới ngoài hai file; D41 historical clearance OPEN.

G1 acceptance tiếp tục OPEN. Không có quyết định chấp nhận D41 hoặc nghiệm thu
G1 được tạo/suy diễn trong review. Không cần drop bảng legacy để dùng DB riêng
eventlens_rag; reviewer không đụng vào DB legacy thật.
