# Review FE remediation, câu hỏi gần nhất và ảnh bài — 04/11/2026

## Kết luận và phạm vi

R1–R7 đã được triển khai đúng hướng qua code, test FE và ảnh evidence đã xem. Chưa nghiệm thu chất lượng chatbot: demo đang dùng model sinh đáp án và embedding giả. Lỗi câu hỏi gần nhất được xác nhận trực tiếp từ DB, không phải suy đoán từ báo cáo agent.

Đã kiểm:

- Đọc report remediation và code feed/grouping/filter/first-question/chat/article.
- Tự chạy `bun test`: **170 pass, 0 fail**; có React warning `act(...)` ở một số test async.
- Tự chạy `bun run typecheck`: exit 0.
- Gọi catalog qua Nginx 18080; danh sách bài trả JSON đúng, chưa có image field.
- Đọc SELECT trên DB demo: run gần nhất, model identity của PG index, khoảng ngày corpus.
- Đọc cấu hình provider được chọn trong worker; không xuất secret.
- Xem ảnh FE sau remediation do agent lưu; không coi đó là kiểm browser độc lập.
- Đã thử mở trực tiếp qua công cụ browser nhưng control/screenshot bị lỗi (trang trắng/xám và lỗi công cụ); chưa hoàn thành kiểm browser độc lập, không kết luận đây là lỗi app.
- Không chạy lại toàn bộ make/e2e/DB gate; không gọi model, không sửa source app hoặc dữ liệu demo.

Code hiện hành nằm ở `.claude/worktrees/rag-search-platform`, branch `feature/rag-search-platform`, có thay đổi chưa commit trên HEAD 3fe5fc0.

## 1. Kết quả R1–R7

| Finding cũ | Kết quả đối chiếu |
|---|---|
| R1 bộ lọc | Date → source trước; topic disclosure, locality tìm kiếm; selected chip vẫn có đường xóa |
| R2/R7 feed | Không lead; grouping theo các run ngày liên tiếp, không sort lại; relevance nguyên ranking |
| R3 preview | ArticleItem không render summary_preview, không tạo N+1 summary request |
| R4 chat | Composer ở trang đầu; create khi gửi; pending question có key được giữ trước chuyển route |
| R5 nav | Nav chính giảm; CTA scope là draft link; không tạo conversation chỉ vì mở |
| R6 trang bài | Nắm nhanh và summary disclosure; facts thu gọn; test revision/codepoint/withheld còn chạy |

Giới hạn create conversation thiếu idempotency server được agent ghi đúng. Không ép task FE giải quyết exactly-once bằng logic client.

Điểm còn cần sửa nhỏ trong FE: `ChatPage.tsx` nút xác nhận xóa vẫn ghi **“Xóa hẳn”** trong khi backend soft-delete, và SessionNote đã giải thích chưa có lịch xóa vật lý. Đổi thành “Xóa hội thoại” hoặc “Xác nhận xóa”, không hứa purge. Đây là finding mức trung bình, không phải nguyên nhân câu trả lời sai.

React test warning về `act` nên được dọn ở test async; không suy ra 170 test đã thất bại, nhưng tránh dùng kết quả xanh làm bằng chứng quá mức về timing.

## 2. Run gần nhất: câu hỏi đúng, hệ thống trả lời sai mục tiêu

```text
run_id:          54c2e3c6-6696-4040-8bc7-18358bb72f88
conversation_id: fcde8f59-0b64-44f2-bb1c-00cbbf1fcb72
question:        hôm nay ngày bao nhiêu
status:          completed
outcome:         answered
mode:            fixture
model_version:   fixture-chat-model.v1
retrieval_mode:  hybrid
```

Server lưu đúng câu hỏi. Answer gồm năm đoạn từ các bài không cùng mục tiêu, có đoạn đề cập ngày xổ số trong tin cũ. Vì vậy không phải FE làm mất câu hỏi hoặc người dùng hỏi không phù hợp về cú pháp. `completed/answered` chỉ là kết quả lifecycle/validator, chưa chứng minh đáp án liên quan.

### Nguyên nhân có bằng chứng

1. **Fake synthesis không diễn giải câu hỏi.** `src/backend/rag/infra/llm/fake_synthesis.py` lấy sources từ payload, mỗi bài chọn span đầu, cắt khoảng 300 ký tự rồi dùng chính đoạn ấy làm claim. Có claim là đánh dấu sufficient. Đó là nguyên nhân trực tiếp của câu trả lời chắp vá/cụt chữ.
2. **Embedding demo giả.** Worker chọn provider fixture; DB generation ghi `fixture-embedding / fixture-embedding.v1`. `FakeEmbeddings` băm text để sinh vector, không phải embedding ngữ nghĩa. Dòng hybrid không chứng minh semantic model thật đang hoạt động; cũng không đủ dữ liệu trace để quy kết từng candidate sai đến từ nhánh nào.
3. **Không có route trả lời từ đồng hồ hệ thống.** Câu hỏi ngày hiện tại bị đưa vào news retrieval; classifier intent hiện có không phải bộ phân loại general-chat/system-time/news đầy đủ.
4. **Corpus cũ so với thời điểm hỏi.** DB có 200 bài, published_at từ 09/09/2026 đến 18/10/2026. Không thể lấy ngày “hôm nay” từ việc tìm chữ hôm nay/ngày trong các bài cũ.

### Thứ tự xử lý đề xuất

- Trước retrieval, phân loại phạm vi hỗ trợ: câu hỏi ngày/giờ hiện tại → clock server và timezone rõ; câu hỏi news → retrieval; ngoài phạm vi → thông báo/hỏi lại.
- Không gán citation báo cho kết quả clock. Nếu thêm loại answer hệ thống phải thiết kế contract/persistence rõ; không bỏ luật grounding của news answer.
- Phân biệt “hôm nay ngày bao nhiêu” với “hôm nay có tin giao thông nào”: câu sau vẫn cần news retrieval và time filter theo clock chốt lúc nhận request.
- Cho người thử biết chế độ fixture ngay trước khi gửi. Đáp án fixture có thể phục vụ kiểm dây nối nhưng không được dùng để đánh giá khả năng hiểu/ngữ nghĩa.
- Để đánh giá chất lượng: nối synthesis model thật theo activation/transport hiện có, dùng semantic embedding thật hoặc lexical-only có nhãn rõ. Không chỉ đổi env live và coi đã có model.
- Thêm case: ngày hiện tại; tin hôm nay không có dữ liệu; câu không liên quan; câu về một bài; follow-up; câu nhiều nguồn. Kiểm answer relevance/correct abstention, không chỉ schema và status.

## 3. Có lấy ảnh từ URL bài được không?

**Có, đã thử thành công trên hai bài của catalog hiện tại.**

| Trang gốc | HTTP trang | Metadata tìm được |
|---|---:|---|
| Bài CafeF đầu feed | 200 | og:image + twitter:image, khai báo 1200 × 630 |
| Bài Thanh Niên kế tiếp | 200 | og:image + twitter:image, khai báo 1200 × 630 |

Đã tải thử phần đầu ảnh CafeF: HTTP 200, Content-Type image/jpeg, signature JPEG `ffd8ff`. Không ghi ảnh vào repo. Chưa đo độ phủ toàn bộ 200 bài và chưa kiểm mọi ảnh có đúng chủ thể hay bị hotlink block.

Nguồn giao thức:

- [Open Graph](https://ogp.me/) định nghĩa og:image, width/height/alt.
- [MDN CORS](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CORS) giải thích giới hạn fetch khác origin từ JavaScript. Display img khác origin không đồng nghĩa script được đọc HTML trang đó.

### Khoảng trống hiện tại

- API ArticleCard/Detail không có thumbnail/image.
- Handoff articles.jsonl hiện không có trường ảnh.
- Repo đã có shared `ArticleMedia` với URL, role, caption/alt/credit, width/height, download_status, nhưng đường handoff/catalog chưa lưu/serve nó thành ảnh bài.
- Do vậy FE hiện chưa thể hiển thị ảnh thật chỉ bằng sửa CSS.

### Cách bổ sung phù hợp

```text
Crawler gửi ảnh cùng bài (ưu tiên)
             hoặc
Worker backfill theo article_id → đọc URL canonical đã biết
       → chọn ảnh đại diện → kiểm/ghi metadata → cache thumbnail
       → catalog API trả thumbnail → FE hiển thị
```

Ưu tiên ảnh do crawler cấp vì đã tải trang tại thời điểm crawl. Với 200 bài đã nhập, dùng worker backfill đọc trang gốc, không sửa trực tiếp các file Downloads hoặc revision nội dung để thêm ảnh.

Thứ tự lựa chọn: lead image từ parser → og:image → twitter:image → schema.org Article.image → ảnh nội dung phù hợp. Không lấy ảnh đầu tiên bất kỳ vì có thể là logo, quảng cáo hoặc pixel theo dõi.

Tách metadata ảnh khỏi text revision/embedding. Chỉ cập nhật thumbnail không nên làm mọi citation/source ref stale hoặc re-embed bài.

### Contract đề xuất, chưa triển khai

```json
{
  "thumbnail": {
    "url": "/media/article-images/example.webp",
    "width": 640,
    "height": 360,
    "alt": null,
    "caption": null,
    "credit": null,
    "source_page_url": "https://publisher.example/article",
    "origin": "og_image"
  },
  "image_status": "ready"
}
```

`thumbnail=null` khi không có; status có thể pending/ready/unavailable/failed theo contract được chốt. URL media trên chỉ minh họa, chưa có endpoint này.

- Card trả thumbnail gọn, detail có thể thêm ảnh lớn/caption/credit.
- Vì DTO hai phía đang strict, phải cập nhật RAG → shared contracts → CRUD → OpenAPI → FE cùng lúc, không chỉ thêm field ở một service.
- Worker fetch chỉ dùng article canonical URL đã kiểm soát, không tạo public arbitrary-URL fetcher. Giới hạn redirect, private-network destinations, timeout, dung lượng và loại nội dung để tránh SSRF/tải file bất kỳ.
- Thu thập/caching theo chính sách nguồn; lưu provenance/credit nếu có. Không bịa alt/caption. Không bypass trang yêu cầu đăng nhập hoặc chặn truy cập.
- Lỗi ảnh không làm list/detail thất bại. Retry/backoff và cache tránh fetch publisher trên mỗi GET.
- Có thể hotlink URL nguồn cho pilot nếu được phép, nhưng nên cache/thumbnail phục vụ từ storage/CDN để ổn định; URL gốc có thể hết hạn/chặn hotlink.
- FE dùng ảnh nhỏ đúng tỉ lệ, có reserved space, lazy-load ảnh dưới màn hình và fallback khi hỏng; bài không có ảnh vẫn đọc được.

## 4. Quyết định sau review

- Có thể tiếp tục vòng tích hợp FE; sửa wording “Xóa hẳn” và hoàn thành kiểm browser độc lập trước nghiệm thu FE cuối.
- Chưa nghiệm thu chatbot về hiểu câu hỏi hoặc chất lượng đáp án. Ưu tiên route câu hỏi và model thật, không chỉnh trọng số retrieval để chữa câu hỏi ngày hiện tại.
- Có đủ bằng chứng khả thi cho task thumbnail ingestion/backfill + API + FE; chưa triển khai trong vòng review.
- Không đổi mode/container/DB/retrieval default hoặc ghi model output mới trong review này.
