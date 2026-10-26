# G1 F3 — Independent manual QA

Reviewer: Codex, 2026-10-16; độc lập với implementer Claude.
Verdict: **PASS luồng mặc định; FAIL URL override nhiều parameter (R1).**

Đã chạy lại gate và script `qa.py` trên hai container pgvector riêng do harness
tạo và dọn. Không kết nối persistent Compose volume/legacy DB. Docker truy cập
được trong lượt review này; không có sandbox blocker.

- Gate: scoped pytest 303 passed; DB suite 129 passed/0 skip; migrate/load/rebuild
  x2/reuse và unavailable-DB checks đạt. Exit 2 là acceptance OPEN.
- QA trực tiếp: migrate x2; load 30; FTS harbor đúng nguồn 001/002/015; vectors 0
  trước rebuild; excerpt bằng đúng substring; rebuild 30 rồi reuse toàn bộ 30;
  30 current vectors/30 generations/5 ledger rows; self cosine distance 0.
- URL query hợp lệ hoạt động với direct CLI nhưng thất bại qua Make (R1).
- migrate/load-fixtures/rebuild với port không kết nối được đều exit 3, lỗi typed.
- Worker validate/process chạy trong gate; 27/27 receipt digest OK.
- Không có container eventlens-rag-test-* sót sau khi chạy.

Raw evidence: `manual-qa.log`, `qa.py`, `gate-g1.log`; chi tiết [review.md](review.md).
Không có API/web E2E, benchmark performance hoặc real-provider quality claim.
