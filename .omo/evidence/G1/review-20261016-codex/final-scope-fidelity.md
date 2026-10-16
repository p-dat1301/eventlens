# G1 F4 — Independent scope fidelity

Reviewer: Codex, 2026-10-16; độc lập với implementer Claude.
Verdict: **Product boundaries PASS; D41 historical clearance OPEN.**

- Thay đổi selfcheck giới hạn trong worker path cleanup, migration guard, local
  DB Make targets, gate checks, tests và documentation/evidence.
- RAG rebuild vẫn sử dụng FakeEmbeddings. Graph hiện là persistence-only, không
  model/tool/checkpointer. Không thấy crawler, provider client, canonical event
  mutation hoặc G4–G6 runtime trong các thay đổi này.
- Existing FakeEventExtractor/InMemoryEventWriter là demo worker có sẵn, không
  phải canonical writer mới của RAG.
- Không phát hiện sửa migration SQL đã áp dụng trong thay đổi selfcheck; byte/set
  identity của hai thư mục SQL được kiểm tra trong các suite mới chạy.
- Receipt worker khớp 27/27, technical correctness đã kiểm chứng độc lập.
- Approval lịch sử cho lần chuyển ngày 2026-10-03 vẫn chưa được xác minh; không
  tự tạo, suy diễn hoặc coi chỉ dẫn giữ đường dẫn hiện tại là approval quá khứ.
- Legacy fresh DB path được chấp nhận về mặt kỹ thuật; không chứng nhận in-place
  upgrade. Không cần thao tác xóa DB/bảng để tiếp tục dùng DB riêng.

Reviewer không sửa product/plan/SQL hoặc DB hiện có. [Chi tiết](review.md).
