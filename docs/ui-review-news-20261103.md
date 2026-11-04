# Đánh giá UI/UX EventLens — 03/11/2026

## Kết luận

Chưa nên nghiệm thu về trải nghiệm người dùng. Các luồng có thể chạy đúng nhưng giao diện hiện thiên về trình bày dữ liệu và giải thích hợp đồng backend. Người dùng phải đọc nhiều trước khi biết chọn bài, lọc hoặc hỏi gì. Cần chỉnh phân cấp thông tin, giảm nội dung mặc định và rút ngắn thao tác; thêm animation riêng lẻ không giải quyết được các vấn đề này.

Đây là đánh giá heuristic từ giao diện và code, chưa phải usability test với người dùng độc lập. Không chấm điểm cảm tính hoặc tuyên bố đã kiểm mọi luồng.

## Phạm vi kiểm tra

- Website đang chạy: `http://127.0.0.1:18080`.
- Đã xem trực tiếp: `/news`, drawer bộ lọc, một trang bài, `/chat`, màn hình hội thoại mới.
- Desktop theo viewport trình duyệt hiện có; trang tin kiểm thêm tại 390 × 844.
- Đọc code tại `.claude/worktrees/rag-search-platform/src/frontend/web/`.
- Có tạo một hội thoại rỗng để quan sát màn hình nhập; chưa gửi câu hỏi/model request. Không xóa hội thoại hoặc dữ liệu.
- Không sửa source ứng dụng, không rebuild/restart demo, không stage/commit/push.
- Không chạy lại functional gates, kiểm tải, toàn bộ accessibility hoặc đánh giá chất lượng đáp án trong vòng này.
- Không sao chép ảnh có nội dung bài vào tài liệu công khai.

Skill tham khảo: `/Users/phungdinhdat/.codex/skills/ui-ux-pro-max/SKILL.md`. Hai truy vấn UX về progressive disclosure/cognitive load không trả hướng dẫn đúng trọng tâm; các nhận định bên dưới dựa trên quan sát thực tế và nguyên tắc chung của skill, không được trình bày là kết quả nghiên cứu người dùng từ skill.

## Findings

### R1 — Lựa chọn chưa có dữ liệu chiếm gần hết bộ lọc

**Ưu tiên: cao.**

Drawer hiển thị toàn bộ 11 chủ đề rồi danh sách địa phương rất dài, tất cả 0 bài. Các checkbox vẫn chọn được, dù có cảnh báo chưa phân loại. Nhóm Nguồn có dữ liệu nằm sau hai nhóm này, phải cuộn nhiều mới tới.

Người dùng thấy nhiều chức năng nhưng phần lớn thao tác dẫn tới kết quả rỗng. Cảnh báo giải thích đúng không bù được chi phí dò tìm.

Code: `src/news/FilterDrawer.tsx`, phần fieldsets chủ đề/địa phương/nguồn, khoảng dòng 258–340.

**Sửa:**

- Đưa Nguồn và Thời gian lên đầu; cho truy cập nhanh từ thanh công cụ feed.
- Khi cả corpus chưa có nhãn đáng tin, thu gọn mỗi nhóm chưa sẵn sàng thành một hàng trạng thái; có thể mở để xem giải thích/taxonomy khi cần.
- Không đồng nhất “0 theo bộ lọc hiện hành” với “cả corpus chưa được phân loại”. Khi người dùng đã chọn filter dẫn tới 0, phải giữ chip và đường bỏ lựa chọn.
- Với dữ liệu lớn, locality dùng tìm kiếm trước, không mở sẵn toàn bộ danh sách.
- Không tạo nhãn giả để làm UI trông đầy đủ.

### R2 — Bài đầu bị trình bày như một tin được ưu tiên biên tập

**Ưu tiên: cao.**

Feed mặc định latest lấy phần tử đầu làm lead cực lớn, ba bài tiếp thành cột phải. Không có field biên tập/độ quan trọng nào chứng minh bài đầu đáng chiếm phần lớn màn hình. Ở màn hình được kiểm, một tiêu đề dài chiếm nhiều dòng lớn và để khoảng trắng rộng bên dưới, trong khi cột phải đặc chữ.

Trên mobile, phần điều khiển chiếm khoảng 350px đầu; phần còn lại của màn hình chủ yếu là một bài có tiêu đề và preview dài. Người đọc khó so sánh nhiều lựa chọn trước khi cuộn.

Code: `src/news/NewsPage.tsx`, `Feed`, phép tách lead/secondary/rest; `src/news.css` phần `.feed-top`, `.story-lead`, `.story-lead .story-title` và `.story-lead .story-preview`.

**Sửa:**

- Feed là luồng khám phá đều nhịp, tiêu đề 20–26px desktop, khoảng 20–22px mobile; điều chỉnh bằng kiểm tra thực tế, không ghim cứng mọi màn hình.
- Desktop dùng vùng feed chính và sidebar tiện ích gọn; không ép bài xếp đầu thành hero mặc định.
- Mobile preview tối đa 2 dòng ở danh sách, title khoảng 3 dòng; nội dung đầy đủ vẫn có ở trang bài và accessible name của link.
- Khi sort latest/oldest, phân nhóm ngày mà vẫn giữ thứ tự. Khi relevance, giữ ranking liên tục.
- Nếu muốn một khối bài mới nhất khác biệt, ghi rõ tiêu chí và giữ kích thước vừa phải. Không gắn “nổi bật” hoặc “tin nóng” không có dữ liệu.

### R3 — Preview AI mô phỏng chưa được nhận diện ngay ở nơi đọc nó

**Ưu tiên: cao trước khi đưa demo cho người khác.**

Feed hiển thị nội dung `summary_preview` với badge “Tóm tắt tự động”; không có nhãn fixture dễ thấy trên trang feed đã kiểm. Chỉ sau khi mở bài mới thấy “Dữ liệu AI mô phỏng”. Người dùng có thể đọc preview và mặc định đó là kết quả model thật.

Code: `src/news/ArticleItem.tsx:48`, card chưa có generator identity trong contract.

**Sửa:**

- Truyền mode demo từ cấu hình server đáng tin hoặc bổ sung metadata origin của preview vào API, rồi hiển thị trạng thái ở đúng nơi tiêu thụ.
- Trong lúc API chưa có provenance per-card, có thể bỏ preview AI khỏi feed, hoặc dùng trạng thái demo toàn trang nếu stack chắc chắn toàn bộ preview thuộc fixture và giá trị đó không bị suy đoán từ máy người dùng.
- Không fetch summary riêng cho mọi card chỉ để lấy generator; tránh N+1 request.
- Không đổi tên thành “sapo” vì đây không phải đoạn giới thiệu được báo cấp.

### R4 — Chat bắt đầu bằng bài hướng dẫn thay vì ô nhập

**Ưu tiên: cao.**

Trang `/chat` có đoạn giải thích scope, ba cột lựa chọn và nút “Bắt đầu hội thoại”. Người dùng muốn hỏi phải bấm thêm mới xuất hiện composer. Hai cột còn lại chủ yếu chỉ cách đi sang trang khác; chúng có trọng lượng thị giác gần giống lựa chọn bắt đầu trực tiếp.

Màn hình conversation mới tiếp tục lặp tiêu đề, scope box và câu “Chưa có câu hỏi nào…” rồi mới đến textarea. Sidebar thường trực có đoạn giải thích session dài.

Code: `src/chat/ChatPage.tsx:247` (`ChatHome`), session note khoảng dòng 155; phần `Thread`/composer.

**Sửa:**

- Mở `/chat` thấy ngay composer: “Bạn muốn tìm hiểu tin gì?”; scope mặc định “Tất cả tin”.
- Khi gửi câu đầu mới tạo conversation rồi submit run, bảo toàn key/retry khi lỗi mạng. Không tạo conversation khi chỉ gõ hoặc render.
- Scope filter/article được mang sang từ điểm vào tương ứng và hiện thành chip ngắn.
- Đưa giải thích đầy đủ scope vào popover/help, vẫn truy cập được bằng keyboard.
- Session note rút thành “Phiên ẩn danh · lưu tối đa 7 ngày”, mở chi tiết khi cần; không hứa dữ liệu bị xóa vật lý sau 7 ngày.
- Có thể gợi ý cách hỏi ngắn; không hứa những câu về tin hôm nay nếu corpus chỉ là batch cũ.

### R5 — Điều hướng có quá nhiều điểm vào cùng chức năng

**Ưu tiên: trung bình.**

Header có tab Hỏi đáp và nút Hỏi AI cùng trỏ `/chat`; feed còn có “Hỏi AI về mọi tin”. Cùng lúc tab “Phân tích” mở luồng analyst cũ nhưng tên không cho biết khác gì hỏi đáp.

Code: `src/App.tsx:88`, nút header khoảng dòng 128; `NewsPage.tsx` phần `ask-feed`.

**Sửa:**

- Điều hướng chính: Tin tức / Hỏi đáp.
- Trên feed chỉ giữ một CTA chat có ngữ cảnh, chẳng hạn “Hỏi về 200 bài” hoặc “Hỏi về 20 bài đang lọc”.
- Analyst chuyển thành lối vào nâng cao có mô tả rõ, vẫn giữ route và khả năng truy cập; không xóa chức năng tương thích.
- Cùng một kiểu CTA nên có cùng thuật ngữ và kiểu nút.

### R6 — Trang bài lặp nội dung, thông tin kỹ thuật cạnh tranh với việc đọc

**Ưu tiên: trung bình.**

Trên bài được kiểm, summary fixture gần như ghép ba câu điểm chính; sidebar lặp lại các câu ấy; thân bài lại bắt đầu bằng chính đoạn đó. Người đọc phải phân biệt ba lớp gần như giống nhau. Title rất lớn, metadata/version/provider và nhiều nhãn làm đầu trang dài.

Code: `src/article/ArticlePage.tsx`, Summary/KeyPoints/Facts và header. Một phần trùng lặp đến từ fixture summarizer; FE nên xử lý cách trình bày, còn nội dung summary thật là nhiệm vụ model/backend.

**Sửa:**

- Một khối “Nắm nhanh” mặc định, ưu tiên key points có căn cứ; text summary dài mở khi cần.
- Không đồng thời trình bày hai dạng tóm tắt trùng lặp trên hai cột.
- Nội dung bài là vùng đọc chính có chiều rộng khoảng 65–75 ký tự; sidebar chỉ giữ công cụ/ngữ cảnh cần thiết.
- Provider identity, thời gian nhập, version hash nằm trong “Thông tin bài”; vẫn hiển thị trạng thái fixture dễ thấy.
- Tách rõ “Tóm tắt” và “Nội dung bài”; giữ highlight offset và điều hướng căn cứ.

### R7 — Nhiều nhãn phụ và kiểu chữ khiến quét tin tốn sức

**Ưu tiên: trung bình.**

Mỗi card lặp publisher, “Đăng …”, title serif, badge summary, preview. Trang còn có “BẢN TIN”, “Tin tức”, timestamp “Danh sách lấy lúc …”. Các đường kẻ, màu nền và nhiều đoạn chữ tương tự nhau tạo cảm giác đơn điệu; khác biệt kích thước lead lại quá mạnh.

**Sửa:**

- Dùng serif cho tiêu đề cấp trang/tiêu đề quan trọng, sans cho UI và đoạn preview ngắn; kiểm bằng font tiếng Việt thực tế.
- Metadata gộp thành một dòng, giảm label lặp, date grouping giúp giảm ngày giờ lặp trên mỗi bài.
- Timestamp lấy danh sách chỉ là thông tin phụ; tuyệt đối không đổi thành “Tin cập nhật lúc…” vì nó không phải thời gian crawl/news freshness.
- Khác biệt thị giác bằng vùng nội dung, nguồn, nhịp khoảng cách và màu nhấn có vai trò. Không thêm gradient, badge, shadow vào mọi khối.
- Không lấy widget nổi của extension Chrome xuất hiện trong ảnh làm finding của EventLens.

## Hướng bố cục đề xuất

### Trang tin desktop

```text
EventLens                         Tin tức | Hỏi đáp
---------------------------------------------------
Khám phá tin tức        [ Tìm bài theo từ khóa... ]
[Nguồn] [Thời gian] [Bộ lọc khác]     [Mới nhất v]
[chip đang chọn...]                    200 bài
---------------------------------------------------
FEED CHÍNH                          TRỢ LÝ TIN TỨC
18/10/2026                          Hỏi về tập đang xem
Nguồn · giờ                         [Đặt câu hỏi...]
Tiêu đề rõ, kích thước vừa           [Mở hỏi đáp]
Preview ngắn nếu provenance rõ
--------------------------------    NGUỒN BÁO
Bài kế tiếp                         danh sách gọn có count
...
```

Sidebar là tùy chọn theo chỗ trống; không tạo thêm một panel cạnh tranh với feed trên tablet/mobile. Tổng 200 bài và source count phải lấy từ API, không hardcode. Nếu mở hỏi đáp từ sidebar, giữ scope đúng filter.

### Mobile

- Header một hàng gọn, hai điểm điều hướng chính.
- Search + filter nhanh; thanh công cụ tối đa hai hàng có nhãn rõ.
- Feed một cột đồng nhất, phần đầu không bị một title quá lớn chiếm hết.
- Chủ đề/địa phương chưa sẵn sàng thu gọn; drawer ưu tiên Nguồn/Thời gian.
- CTA chat dễ tìm nhưng không che tin hoặc ô nhập.

### Chat

- Composer hiện ngay ở trang mới.
- Một chip scope, một giải thích ngắn về trích dẫn, phần còn lại là nội dung hội thoại.
- Lịch sử ở sidebar desktop hoặc drawer mobile.
- Citation mở nguồn như hiện có; giữ ownership, source stale, keyboard focus và polling.

## Thứ tự thực hiện

1. R1 + R3: giảm lựa chọn vô dụng và giải quyết nguồn gốc preview fixture.
2. R2 + R7: làm feed dễ quét, thu gọn mobile, giữ nguyên ranking/filter/cursor.
3. R4 + R5: cho hỏi trực tiếp và thống nhất navigation.
4. R6: gọn trang đọc, gom thông tin kỹ thuật vào disclosure.
5. Cuối cùng mới thêm motion nhẹ: reveal một lần, reading progress, highlight nguồn, sticky toolbar có giới hạn. Motion phải tắt được bằng reduced-motion.

## Tiêu chí kiểm tra lại

- Desktop và mobile nhận ra ngay hai việc chính: tìm/đọc bài và đặt câu hỏi.
- Các filter có dữ liệu dùng được mà không phải cuộn qua hàng chục lựa chọn 0.
- Không ẩn chip đã chọn hoặc làm người dùng mất đường thoát khi filter trả 0.
- Trang chat mới có ô nhập ngay; gửi câu đầu không tạo trùng conversation/run.
- Mỗi preview mô phỏng được nhận diện trước khi người dùng mở bài, hoặc không được hiển thị khi chưa rõ provenance.
- Feed so sánh được nhiều bài với title dài; danh sách không phụ thuộc có ảnh.
- Summary/key points/full text có vai trò rõ; source highlight vẫn đúng code point/revision.
- Kiểm 390px, tablet, desktop và zoom 200%; keyboard/reduced-motion/focus hoạt động.
- Test lại luồng thật feed → detail → chat → citation, URL filters/back/scroll restoration và reload active run.
- Functional tests xanh là bằng chứng hành vi; bổ sung quan sát nhiệm vụ với người dùng thật để đánh giá họ có tìm/lọc/hỏi được dễ dàng hay không.
