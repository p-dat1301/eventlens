# T3/G1 independent re-review 2 — 2026-10-16

Reviewer: Codex; độc lập với Claude, agent sửa Makefile.
Phạm vi: đóng R1/P2 (URL transport) và xác nhận R2/P3 vẫn đã được xử lý.

**Kết luận: PASS về kỹ thuật cho các bản sửa review. R1 và R2 CLOSED.**
Không phát hiện lỗi mới trong phạm vi thay đổi này. G1 acceptance vẫn OPEN vì
reviewer không thay thế quyết định của chủ dự án về D41 và nghiệm thu G1.

## Source và phạm vi kiểm tra

- So sánh SHA-256 với snapshot re-review trước: chỉ Makefile thay đổi.
- So với review gốc: Makefile và plan thay đổi; product Python/SQL/harness không đổi.
- Các bằng chứng full suite trước vẫn được giữ đúng ngày/phạm vi; không tuyên bố
  chạy lại `make check`, full DB suite hoặc aggregate gate trong lượt hẹp này.
- Chỉ thêm evidence. Không sửa product, plan, báo cáo cũ, migration SQL hoặc DB/volume hiện có.

## R1/P2 — CLOSED

Makefile dùng target-specific export:

    rag-migrate rag-load-fixtures rag-rebuild: export EVENTLENS_RAG_DATABASE_URL = $(RAG_DATABASE_URL)

Ba recipe chỉ gọi CLI, không ghép URL vào shell command. Đã xác minh độc lập
giá trị URL với cả `&` và `'` được truyền nguyên từng byte tới tiến trình recipe,
sau đó chạy cả ba target trên PostgreSQL/pgvector thật trong container riêng.
Biến EVENTLENS_RAG_DATABASE_URL từ harness đã được bỏ khỏi environment subprocess
Make để tránh kết quả PASS do vô tình dùng lại cấu hình DB kế thừa.

URL kiểm tra: `postgresql://…/rag_test?sslmode=disable&connect_timeout=2&application_name=g1_review'quoted`.

| Check mới | Exit / kết quả |
| --- | --- |
| Env shim cho cả ba target | 0 mỗi target; URL khớp byte-for-byte |
| Make migrate x2, URL có cả & và ' | 0 / 0 |
| Make load-fixtures, cùng URL | 0; 30 bài |
| SQL trước rebuild | 30 lexical rows, 0 current vectors |
| Make rebuild x2, cùng URL | 0 / 0; 30 published rồi reuse 30/30 |
| SQL sau rebuild | 30 current vectors, 30 generations, 5 migration ledger rows |
| Cả ba Make target với URL có &/' trỏ port không kết nối được | Make 2, CLI 3; `rag_storage_unavailable`, không có missing-option/shell syntax error |
| `rtk git diff --check` | 0 |
| Container eventlens-rag-test-* sau lượt chạy | Không có container sót |

Command kiểm tra: `rtk proxy sh apps/rag/with-test-db.sh uv run --package eventlens-rag python .omo/evidence/G1/rereview2-20261016-codex/verify.py`.
Exit: 0. Reproducer: `verify.py`. Raw log: `verification.log`.

## R2/P3 — CLOSED

Plan vẫn giữ nhãn Historical cho trạng thái T1/T2 cũ và có dòng Current phù hợp
checklist. Nội dung này không đổi kể từ re-review đã xác nhận R2 PASS.

## Trạng thái F1–F4 cập nhật

| Review | Kết luận hiện tại |
| --- | --- |
| F1 Plan compliance | Technical requirements đã kiểm chứng; R2 đóng. D41 procedural/history gap vẫn OPEN |
| F2 Code quality | PASS trong phạm vi review; R1 và follow-up đã đóng |
| F3 Manual QA | PASS cho luồng mặc định và URL override có &/' trên DB thật |
| F4 Scope fidelity | Product boundaries PASS; D41 historical clearance vẫn OPEN |

Review độc lập đã thực hiện và các code/documentation findings đã xử lý. Không
cần một reviewer mới chỉ để lặp lại các bản sửa này. Nếu có thay đổi sản phẩm mới,
phải đánh giá lại phạm vi bị ảnh hưởng.

## Điều kiện còn lại để nghiệm thu G1

1. Chủ dự án cung cấp căn cứ approval D41 lịch sử hoặc ghi rõ chấp nhận khoảng
   trống bằng chứng đó. Không tạo hoặc suy diễn approval ngày 2026-10-03.
2. Chủ dự án ghi nhận quyết định nghiệm thu G1 trong phạm vi T1–T3 fixture/fake,
   local DB eventlens_rag; không suy ra live provider/shared use/deployment approval.

Không cần drop/archive các bảng legacy để dùng đường DB riêng eventlens_rag.
Fresh-DB PASS không phải bằng chứng in-place legacy upgrade PASS. Không kết nối
hoặc thay đổi DB legacy thật trong lượt review này.

Các báo cáo REQUEST CHANGES trước được giữ làm lịch sử; báo cáo này đóng R1/R2
cho source snapshot hiện tại. Không có acceptance decision nào được tạo thay người dùng.
