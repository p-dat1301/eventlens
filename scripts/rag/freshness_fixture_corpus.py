"""Synthetic update scenarios for freshness mechanics (fictional; no publisher text).

Six storylines, each with several dated updates, plus fresh off-topic
distractors that share place names, one update without a publication time
and one dated after the query clock. Used only to test the *mechanism* of
intent freshness; it says nothing about real-data quality.

Query clock: 2026-05-20T00:00:00Z (``QUERY_CLOCK``).
Question tuple: (qid, query, intent, answerability, evidence, claims,
temporal_requirement, extras) with ``extras`` = category, split, filters.
"""

from typing import Final

QUERY_CLOCK: Final = "2026-05-20T00:00:00+00:00"

ARTICLES: Final = (
    # 1. Cầu Sông Lam Mới
    (
        "bridge-0501",
        "giaothong.example",
        "Giao Thông Mẫu",
        "2026-05-01T08:00:00Z",
        "Phát hiện vết nứt trên cầu Sông Lam Mới",
        (
            "Ngày 1/5, đơn vị quản lý phát hiện vết nứt dài 40 cm ở trụ T3 của cầu Sông Lam Mới.",
            "Cầu Sông Lam Mới vẫn cho xe qua nhưng giảm tốc độ xuống 30 km/h trong khi chờ kiểm định.",
        ),
    ),
    (
        "bridge-0508",
        "giaothong.example",
        "Giao Thông Mẫu",
        "2026-05-08T08:00:00Z",
        "Cấm xe tải qua cầu Sông Lam Mới",
        (
            "Từ ngày 8/5, cầu Sông Lam Mới cấm xe tải trên 10 tấn để gia cố trụ T3.",
            "Xe tải được phân luồng qua quốc lộ cũ, dài thêm khoảng 12 km.",
        ),
    ),
    (
        "bridge-0515",
        "doisong.example",
        "Đời Sống Mẫu",
        "2026-05-15T08:00:00Z",
        "Sửa chữa cầu Sông Lam Mới đạt 70%",
        (
            "Đến ngày 15/5, việc sửa chữa cầu Sông Lam Mới đã hoàn thành 70% khối lượng.",
            "Nhà thầu cho biết trụ T3 đã được bơm keo epoxy và bọc thép.",
        ),
    ),
    (
        "bridge-0519",
        "giaothong.example",
        "Giao Thông Mẫu",
        "2026-05-19T08:00:00Z",
        "Cầu Sông Lam Mới thông xe trở lại",
        (
            "Chiều 19/5, cầu Sông Lam Mới thông xe trở lại cho mọi loại phương tiện sau khi sửa xong trụ T3.",
            "Lệnh cấm xe tải trên 10 tấn được bãi bỏ từ cùng ngày.",
        ),
    ),
    (
        "bridge-festival",
        "doisong.example",
        "Đời Sống Mẫu",
        "2026-05-20T00:00:00Z",
        "Lễ hội pháo hoa bên cầu Sông Lam Mới",
        (
            "Lễ hội pháo hoa mùa hè sẽ được tổ chức ở công viên bên bờ cầu Sông Lam Mới vào cuối tháng 6.",
            "Ban tổ chức dự kiến đón 50.000 khách và bố trí bãi đỗ xe miễn phí.",
        ),
    ),
    # 2. Sốt xuất huyết Tân Hòa Đông
    (
        "dengue-0420",
        "suckhoe.example",
        "Sức Khỏe Mẫu",
        "2026-04-20T08:00:00Z",
        "Tân Hòa Đông ghi nhận 120 ca sốt xuất huyết",
        (
            "Tính đến ngày 20/4, huyện Tân Hòa Đông ghi nhận 120 ca sốt xuất huyết, chưa có ca tử vong.",
            "Trung tâm y tế huyện tổ chức phun hóa chất tại ba xã có nhiều ca bệnh.",
        ),
    ),
    (
        "dengue-0505",
        "suckhoe.example",
        "Sức Khỏe Mẫu",
        "2026-05-05T08:00:00Z",
        "Sốt xuất huyết ở Tân Hòa Đông tăng lên 340 ca",
        (
            "Đến ngày 5/5, số ca sốt xuất huyết ở huyện Tân Hòa Đông tăng lên 340 ca.",
            "Bệnh viện huyện kê thêm 40 giường cho bệnh nhân sốt xuất huyết.",
        ),
    ),
    (
        "dengue-0518",
        "doisong.example",
        "Đời Sống Mẫu",
        "2026-05-18T08:00:00Z",
        "Ca sốt xuất huyết ở Tân Hòa Đông giảm",
        (
            "Trong tuần tới ngày 18/5, huyện Tân Hòa Đông chỉ ghi nhận thêm 90 ca sốt xuất huyết, giảm một nửa so với tuần trước.",
            "Ngành y tế đánh giá dịch sốt xuất huyết ở Tân Hòa Đông đã qua đỉnh.",
        ),
    ),
    (
        "dengue-market",
        "kinhte.example",
        "Kinh Tế Mẫu",
        "2026-05-20T00:00:00Z",
        "Chợ đầu mối Tân Hòa Đông mở rộng",
        (
            "Chợ đầu mối huyện Tân Hòa Đông được mở rộng thêm 2 ha để phục vụ nông sản.",
            "Hơn 300 tiểu thương sẽ chuyển vào khu chợ mới từ tháng 7.",
        ),
    ),
    # 3. Giá nước Hà Thành Xanh
    (
        "water-0301",
        "kinhte.example",
        "Kinh Tế Mẫu",
        "2026-03-01T08:00:00Z",
        "Hà Thành Xanh đề xuất tăng giá nước 12%",
        (
            "Công ty Nước sạch Hà Thành Xanh đề xuất tăng giá nước sinh hoạt thêm 12% để bù chi phí xử lý.",
            "Đề xuất được gửi Sở Tài chính thẩm định trong tháng 3.",
        ),
    ),
    (
        "water-0415",
        "kinhte.example",
        "Kinh Tế Mẫu",
        "2026-04-15T08:00:00Z",
        "HĐND duyệt tăng giá nước Hà Thành Xanh 8%",
        (
            "Hội đồng nhân dân tỉnh chỉ chấp thuận mức tăng giá nước 8% thay vì 12% như Hà Thành Xanh đề xuất.",
            "Hộ nghèo được giữ nguyên giá nước cũ.",
        ),
    ),
    (
        "water-0517",
        "doisong.example",
        "Đời Sống Mẫu",
        "2026-05-17T08:00:00Z",
        "Giá nước Hà Thành Xanh mới áp dụng từ 1/6",
        (
            "Hà Thành Xanh thông báo giá nước mới, tăng 8%, áp dụng từ ngày 1/6 trên hóa đơn tháng 6.",
            "Khách hàng có thể tra cứu biểu giá nước mới trên ứng dụng của công ty.",
        ),
    ),
    # 4. Buýt điện 88 Phú Lâm
    (
        "bus-0210",
        "giaothong.example",
        "Giao Thông Mẫu",
        "2026-02-10T08:00:00Z",
        "Phú Lâm thử nghiệm tuyến buýt điện 88",
        (
            "Từ ngày 10/2, thành phố Phú Lâm chạy thử nghiệm tuyến buýt điện số 88 với 6 xe.",
            "Tuyến buýt điện 88 nối bến xe trung tâm với khu đại học.",
        ),
    ),
    (
        "bus-0401",
        "giaothong.example",
        "Giao Thông Mẫu",
        "2026-04-01T08:00:00Z",
        "Tuyến buýt điện 88 chính thức vận hành 20 xe",
        (
            "Ngày 1/4, tuyến buýt điện 88 ở Phú Lâm chính thức vận hành với 20 xe.",
            "Giá vé buýt điện 88 là 7.000 đồng một lượt.",
        ),
    ),
    (
        "bus-0512",
        "doisong.example",
        "Đời Sống Mẫu",
        "2026-05-12T08:00:00Z",
        "Buýt điện 88 tăng lên 45 xe",
        (
            "Từ ngày 12/5, tuyến buýt điện 88 ở Phú Lâm tăng lên 45 xe, giãn cách còn 8 phút.",
            "Sở Giao thông Phú Lâm cho biết lượng khách tăng gấp đôi sau một tháng.",
        ),
    ),
    (
        "bus-realestate",
        "kinhte.example",
        "Kinh Tế Mẫu",
        "2026-05-20T00:00:00Z",
        "Giá căn hộ gần tuyến buýt ở Phú Lâm tăng",
        (
            "Giá căn hộ dọc trục đường có buýt ở Phú Lâm tăng khoảng 5% trong quý II.",
            "Các sàn môi giới cho rằng giao thông công cộng giúp khu đại học hút người mua.",
        ),
    ),
    # 5. Nhà máy thép Hải Đăng (one update dated after the query clock)
    (
        "steel-0510",
        "moitruong.example",
        "Môi Trường Mẫu",
        "2026-05-10T08:00:00Z",
        "Rò rỉ khí tại nhà máy thép Hải Đăng",
        (
            "Tối 10/5, nhà máy thép Hải Đăng xảy ra sự cố rò rỉ khí từ lò cao số 2.",
            "Khoảng 200 hộ dân gần nhà máy được khuyến cáo đóng cửa.",
        ),
    ),
    (
        "steel-0511",
        "moitruong.example",
        "Môi Trường Mẫu",
        "2026-05-11T08:00:00Z",
        "Nhà máy thép Hải Đăng tạm dừng lò cao",
        (
            "Ngày 11/5, nhà máy thép Hải Đăng tạm dừng lò cao số 2 để kiểm tra sau sự cố rò rỉ khí.",
            "Đoàn thanh tra môi trường lấy mẫu không khí tại năm điểm.",
        ),
    ),
    (
        "steel-0516",
        "moitruong.example",
        "Môi Trường Mẫu",
        "2026-05-16T08:00:00Z",
        "Nhà máy thép Hải Đăng bị phạt 600 triệu đồng",
        (
            "Ngày 16/5, nhà máy thép Hải Đăng bị phạt 600 triệu đồng vì để rò rỉ khí ra môi trường.",
            "Nhà máy thép Hải Đăng phải khắc phục hệ thống cảnh báo khí trước khi chạy lại lò cao.",
        ),
    ),
    (
        "steel-future",
        "moitruong.example",
        "Môi Trường Mẫu",
        "2026-05-25T08:00:00Z",
        "Nhà máy thép Hải Đăng dự kiến chạy lại lò cao",
        (
            "Nhà máy thép Hải Đăng dự kiến chạy lại lò cao số 2 sau khi được nghiệm thu hệ thống cảnh báo khí.",
            "Thời điểm cụ thể chưa được công bố.",
        ),
    ),
    # 6. Hồ Đá Bàn Thượng (one update without a publication time)
    (
        "lake-0428",
        "moitruong.example",
        "Môi Trường Mẫu",
        "2026-04-28T08:00:00Z",
        "Hồ Đá Bàn Thượng xuống mực nước chết",
        (
            "Ngày 28/4, hồ Đá Bàn Thượng xuống gần mực nước chết, chỉ còn 9% dung tích.",
            "Nhà máy nước phải bơm vét để duy trì cấp nước.",
        ),
    ),
    (
        "lake-0506",
        "doisong.example",
        "Đời Sống Mẫu",
        "2026-05-06T08:00:00Z",
        "Cấp nước luân phiên do hồ Đá Bàn Thượng cạn",
        (
            "Từ ngày 6/5, các phường phía tây cấp nước luân phiên hai ngày một lần vì hồ Đá Bàn Thượng cạn.",
            "Mực nước hồ Đá Bàn Thượng còn 7% dung tích.",
        ),
    ),
    (
        "lake-undated",
        "doisong.example",
        "Đời Sống Mẫu",
        None,
        "Hồ Đá Bàn Thượng tích nước sau mưa",
        (
            "Sau đợt mưa lớn, hồ Đá Bàn Thượng tích thêm nước, đạt 25% dung tích.",
            "Bài viết không ghi ngày đăng.",
        ),
    ),
)

UPDATE = {"category": "update", "split": "mechanics"}
HISTORY = {"category": "historical", "split": "mechanics"}

QUESTIONS: Final = (
    (
        "f-01",
        "Tình hình cầu Sông Lam Mới hiện nay ra sao?",
        "update",
        "answerable",
        (
            ("bridge-0519", "cầu Sông Lam Mới thông xe trở lại", 2),
            ("bridge-0515", "đã hoàn thành 70% khối lượng", 1),
            ("bridge-0508", "cấm xe tải trên 10 tấn", 1),
        ),
        ("thông xe trở lại",),
        "latest",
        UPDATE,
    ),
    (
        "f-02",
        "Số ca sốt xuất huyết mới nhất ở Tân Hòa Đông là bao nhiêu?",
        "update",
        "answerable",
        (
            ("dengue-0518", "chỉ ghi nhận thêm 90 ca sốt xuất huyết", 2),
            ("dengue-0505", "tăng lên 340 ca", 1),
            ("dengue-0420", "ghi nhận 120 ca sốt xuất huyết", 1),
        ),
        ("90 ca",),
        "latest",
        UPDATE,
    ),
    (
        "f-03",
        "Giá nước Hà Thành Xanh được cập nhật thế nào?",
        "update",
        "answerable",
        (
            ("water-0517", "tăng 8%, áp dụng từ ngày 1/6", 2),
            ("water-0415", "chỉ chấp thuận mức tăng giá nước 8%", 1),
            ("water-0301", "tăng giá nước sinh hoạt thêm 12%", 1),
        ),
        ("8% từ 1/6",),
        "latest",
        UPDATE,
    ),
    (
        "f-04",
        "Tuyến buýt điện 88 ở Phú Lâm hiện nay có bao nhiêu xe?",
        "update",
        "answerable",
        (
            ("bus-0512", "tăng lên 45 xe", 2),
            ("bus-0401", "chính thức vận hành với 20 xe", 1),
            ("bus-0210", "với 6 xe", 1),
        ),
        ("45 xe",),
        "latest",
        UPDATE,
    ),
    (
        "f-05",
        "Nhà máy thép Hải Đăng hiện tại bị xử lý thế nào sau sự cố?",
        "update",
        "answerable",
        (
            ("steel-0516", "bị phạt 600 triệu đồng", 2),
            ("steel-0511", "tạm dừng lò cao số 2", 1),
            ("steel-future", "dự kiến chạy lại lò cao số 2", 1),
        ),
        ("phạt 600 triệu",),
        "latest",
        UPDATE,
    ),
    (
        "f-06",
        "Mực nước hồ Đá Bàn Thượng mới nhất là bao nhiêu?",
        "update",
        "answerable",
        (
            ("lake-0506", "còn 7% dung tích", 2),
            ("lake-0428", "chỉ còn 9% dung tích", 1),
            ("lake-undated", "đạt 25% dung tích", 1),
        ),
        ("7% dung tích",),
        "latest",
        UPDATE,
    ),
    (
        "f-07",
        "Vết nứt trên cầu Sông Lam Mới được phát hiện ở trụ nào và dài bao nhiêu?",
        "factual",
        "answerable",
        (("bridge-0501", "vết nứt dài 40 cm ở trụ T3", 2),),
        ("40 cm, trụ T3",),
        None,
        HISTORY,
    ),
    (
        "f-08",
        "Ban đầu Hà Thành Xanh đề xuất tăng giá nước bao nhiêu phần trăm?",
        "numeric",
        "answerable",
        (("water-0301", "tăng giá nước sinh hoạt thêm 12%", 2),),
        ("12%",),
        None,
        HISTORY,
    ),
    (
        "f-09",
        "Tuyến buýt điện 88 chạy thử nghiệm với bao nhiêu xe?",
        "numeric",
        "answerable",
        (("bus-0210", "chạy thử nghiệm tuyến buýt điện số 88 với 6 xe", 2),),
        ("6 xe",),
        None,
        HISTORY,
    ),
    (
        "f-10",
        "Sự cố rò rỉ khí ở nhà máy thép Hải Đăng xuất phát từ lò nào?",
        "factual",
        "answerable",
        (("steel-0510", "rò rỉ khí từ lò cao số 2", 2),),
        ("lò cao số 2",),
        None,
        HISTORY,
    ),
    (
        "f-11",
        "Diễn biến sốt xuất huyết ở Tân Hòa Đông trước tháng 5?",
        "update",
        "answerable",
        (("dengue-0420", "ghi nhận 120 ca sốt xuất huyết", 2),),
        ("120 ca",),
        None,
        {
            "category": "explicit_window",
            "split": "mechanics",
            "filters": {"published_before": "2026-05-01T00:00:00+00:00"},
        },
    ),
    (
        "f-12",
        "Tiến độ cầu Sông Lam Mới trong tuần từ 5/5 đến 10/5?",
        "update",
        "answerable",
        (("bridge-0508", "cấm xe tải trên 10 tấn", 2),),
        ("cấm xe tải",),
        None,
        {
            "category": "explicit_window",
            "split": "mechanics",
            "filters": {
                "published_after": "2026-05-05T00:00:00+00:00",
                "published_before": "2026-05-10T00:00:00+00:00",
            },
        },
    ),
)
