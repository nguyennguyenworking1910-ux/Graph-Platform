"""Recognizable names for mock data: films in Vietnamese cinemas 2025-2026, cinema sites, destinations, shops, products."""

from __future__ import annotations

MOVIES = {
    "action": [
        "Tử Chiến Trên Không", "Mưa Đỏ", "Địa Đạo: Mặt Trời Trong Bóng Tối", "Thám Tử Kiên: Kỳ Án Không Đầu",
        "Nhiệm Vụ Bất Khả Thi: Nghiệp Báo Cuối Cùng", "Truy Tìm Long Diên Hương",
    ],
    "comedy": [
        "Bộ Tứ Báo Thủ", "Lật Mặt 8: Vòng Tay Nắng", "Thỏ Ơi!!", "Nhà Ba Tôi Một Phòng", "Báu Vật Trời Cho", "Cục Vàng Của Ngoại",
    ],
    "drama": ["Nhà Gia Tiên", "Mang Mẹ Đi Bỏ", "Điều Ước Cuối Cùng", "Mùi Phở", "Quán Kỳ Nam", "Chị Ngã Em Nâng"],
    "horror": ["Quỷ Nhập Tràng", "Đèn Âm Hồn", "Âm Dương Lộ", "Tìm Xác: Ma Không Đầu", "Dưới Đáy Hồ", "Út Lan: Oán Linh Giữ Của"],
    "romance": [
        "Yêu Nhầm Bạn Thân", "Tay Anh Giữ Một Vì Sao", "Nụ Hôn Bạc Tỷ", "Em Xinh Tinh Quái", "Wicked: Phần 2", "Ai Thương Ai Mến",
    ],
    "animation": [
        "Thám Tử Lừng Danh Conan: Dư Ảnh Của Độc Nhãn", "Doraemon: Nobita và Cuộc Phiêu Lưu Vào Thế Giới Trong Tranh",
        "Thanh Gươm Diệt Quỷ: Vô Hạn Thành", "Zootopia 2", "Na Tra: Ma Đồng Náo Hải", "Trạng Quỳnh Nhí: Truyền Thuyết Kim Ngưu",
    ],
    "scifi": [
        "Avatar: Lửa và Tro Tàn", "Thế Giới Khủng Long: Tái Sinh", "Superman", "Bộ Tứ Siêu Đẳng: Bước Đi Đầu Tiên",
        "Tron: Ares", "Mickey 17",
    ],
}  # fmt: skip
GENRES = list(MOVIES)
MOVIE_TITLES = [title for titles in MOVIES.values() for title in titles]
MOVIES_PER_GENRE = len(MOVIE_TITLES) // len(GENRES)

CINEMAS = [
    "CGV Vincom Bà Triệu", "Galaxy Nguyễn Du", "Beta Mỹ Đình", "Lotte Cinema Gò Vấp", "BHD Star Thảo Điền", "Cinestar Quốc Thanh",
    "CGV Landmark 81", "Galaxy Tân Bình", "Beta Thanh Xuân", "Lotte Cinema Hà Đông", "BHD Star Phạm Ngọc Thạch", "Cinestar Hai Bà Trưng",
    "CGV Aeon Long Biên", "Galaxy Kinh Dương Vương", "Beta Thái Nguyên", "Lotte Cinema Nha Trang", "Trung tâm Chiếu phim Quốc gia",
    "CGV Vincom Đà Nẵng", "Galaxy Đà Nẵng", "Beta Biên Hòa", "CGV Sư Vạn Hạnh", "Galaxy Huế", "Beta Quy Nhơn", "Lotte Cinema Cần Thơ",
]  # fmt: skip

DESTINATIONS = [
    "Đà Nẵng", "Phú Quốc", "Đà Lạt", "Nha Trang", "Hạ Long", "Sa Pa", "Hội An", "Huế", "Quy Nhơn", "Côn Đảo",
    "Phú Yên", "Mũi Né", "Vũng Tàu", "Ninh Bình", "Hà Giang", "Cát Bà", "Mộc Châu", "Tam Đảo", "Lý Sơn", "Cần Thơ",
    "Bangkok", "Singapore", "Seoul", "Tokyo", "Osaka", "Đài Bắc", "Bali", "Kuala Lumpur", "Hồng Kông", "Thượng Hải",
    "Phuket", "Siem Reap", "Luang Prabang", "Busan", "Dubai", "Paris", "Sydney", "Maldives", "Lệ Giang", "Phượng Hoàng Cổ Trấn",
]  # fmt: skip
TRIP_KINDS = [
    "2 ngày 1 đêm", "3 ngày 2 đêm", "4 ngày 3 đêm", "5 ngày 4 đêm", "vé máy bay và khách sạn", "tour trọn gói", "nghỉ dưỡng cuối tuần",
]  # fmt: skip

SHOP_KINDS = {
    "grocery": ["Tạp hóa", "Bách hóa", "Siêu thị mini"],
    "restaurant": ["Quán cơm", "Nhà hàng", "Quán phở"],
    "fashion": ["Thời trang", "Shop quần áo", "Giày dép"],
    "electronics": ["Điện máy", "Điện thoại", "Máy tính"],
    "pharmacy": ["Nhà thuốc", "Quầy thuốc", "Hiệu thuốc"],
    "fuel": ["Cửa hàng xăng dầu", "Trạm xăng", "Cây xăng"],
    "cafe": ["Cà phê", "Trà sữa", "Tiệm bánh"],
}
# invented shop names, none of them a known chain
SHOP_NAMES = [
    "Sông Hồng", "Minh Châu", "Hòa Bình", "Phương Nam", "Thành Công", "Cô Ba", "Chú Tư", "Bình Minh", "Hoa Sen", "Tân Tiến",
    "Góc Phố", "Hương Quê", "Ngọc Lan", "Đại Phát", "Kim Liên", "Thanh Xuân", "An Lạc", "Phú Quý", "Hồng Hà", "Mai Vàng",
    "Trường Sơn", "Bến Thành", "Cửu Long", "Tây Đô", "Nam Phong", "Đông Á", "Lộc Phát", "Vạn Phúc", "Thiên Ân", "Gia Hưng",
]  # fmt: skip

INSURANCE_PRODUCTS = {"life": "Bảo hiểm nhân thọ", "health": "Bảo hiểm sức khỏe", "motor": "Bảo hiểm xe", "travel": "Bảo hiểm du lịch"}
SAVINGS_PRODUCTS = {"term_deposit": "Tiền gửi có kỳ hạn", "flexi": "Tiết kiệm linh hoạt", "fund": "Chứng chỉ quỹ"}


def shop_name(category: str, nth: int) -> str:
    """The nth shop of a category: every kind x name pair once, then numbered repeats."""
    kinds = SHOP_KINDS[category]
    pairs = len(kinds) * len(SHOP_NAMES)
    name = f"{kinds[nth % len(kinds)]} {SHOP_NAMES[nth // len(kinds) % len(SHOP_NAMES)]}"
    return name if nth < pairs else f"{name} {nth // pairs + 1}"
