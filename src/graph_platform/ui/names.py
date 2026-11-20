"""Readable names for everything shown to business users.

A person never appears under a real name: the display name is made up from the party id and stays the same everywhere.
A business appears under its public trade name. Devices and identifiers show what they are, never their value.
"""

from __future__ import annotations

import hashlib
import unicodedata

FAMILY = ["Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Phan", "Vũ", "Đặng", "Bùi", "Đỗ", "Hồ", "Ngô", "Dương", "Lý", "Đinh", "Mai"]
MIDDLE = ["Văn", "Thị", "Minh", "Đức", "Ngọc", "Quang", "Thanh", "Hồng", "Xuân", "Gia", "Bảo", "Hữu", "Thu", "Anh", "Khánh", "Tuấn"]
GIVEN = [
    "An", "Bình", "Châu", "Dũng", "Giang", "Hà", "Hiếu", "Hùng", "Khánh", "Lan", "Linh", "Long", "Mai", "Nam", "Nga", "Phúc",
    "Quân", "Sơn", "Tâm", "Thảo", "Trang", "Tuấn", "Vy", "Yến", "Khoa", "Hạnh", "Đạt", "Huy", "Ngân", "Oanh", "Phương", "Thắng",
]  # fmt: skip
DEVICE = {"mobile": "Điện thoại", "web": "Máy tính", "pos": "Máy quẹt thẻ"}
IDENTIFIER = {
    "phone": "Số điện thoại", "email": "Email", "national_id": "Giấy tờ tùy thân", "tax_id": "Mã số thuế",
    "merchant_code": "Mã cửa hàng", "address": "Địa chỉ", "ip": "Địa chỉ mạng",
}  # fmt: skip
ACCOUNT = {
    "savings": "Tài khoản tiết kiệm", "policy": "Hợp đồng bảo hiểm", "wallet": "Ví", "bank": "Tài khoản ngân hàng",
    "loan": "Khoản vay", "loyalty": "Thẻ thành viên",
}  # fmt: skip
EVENT = {
    "loan": "khoản vay", "merchant_loan": "khoản vay vốn", "claim": "lần yêu cầu bồi thường", "chargeback": "lần bị đòi hoàn tiền",
    "fraud_case": "lần bị báo gian lận", "churn": "lần đóng tài khoản", "application": "hồ sơ vay", "deposit": "lần gửi tiền",
}  # fmt: skip
CITY = {
    "hanoi": "Hà Nội", "ho_chi_minh": "TP. Hồ Chí Minh", "da_nang": "Đà Nẵng", "hai_phong": "Hải Phòng", "can_tho": "Cần Thơ",
    "hue": "Huế", "nha_trang": "Nha Trang", "vung_tau": "Vũng Tàu", "bien_hoa": "Biên Hòa", "da_lat": "Đà Lạt", "quy_nhon": "Quy Nhơn",
    "vinh": "Vinh", "thai_nguyen": "Thái Nguyên", "nam_dinh": "Nam Định", "buon_ma_thuot": "Buôn Ma Thuột",
}  # fmt: skip
OCCUPATION = {
    "office": "Nhân viên văn phòng", "worker": "Công nhân", "driver": "Tài xế", "trader": "Buôn bán", "teacher": "Giáo viên",
    "engineer": "Kỹ sư", "freelancer": "Làm tự do", "farmer": "Nông dân",
}  # fmt: skip


def _digest(node_id: str) -> int:
    return int.from_bytes(hashlib.sha1(node_id.encode()).digest()[:8], "big")


def person_name(party_id: str) -> str:
    h = _digest(party_id)
    return f"{FAMILY[h % 16]} {MIDDLE[(h >> 4) % 16]} {GIVEN[(h >> 8) % 32]} {(h >> 13) % 9000 + 1000}"


def party_name(party_id: str, subtype: str | None, trade_name: str | None) -> str:
    if subtype == "person":
        return person_name(party_id)
    return trade_name or f"Cửa hàng {_digest(party_id) % 9000 + 1000}"


def node_label(node_type: str, subtype: str | None, node_id: str, name: str | None) -> str:
    """`name` is the node's public name: trade name of a business, title of an item."""
    if node_type == "Party":
        return party_name(node_id, subtype, name)
    if node_type == "Device":
        return f"{DEVICE.get(subtype or '', 'Thiết bị')} {_digest(node_id) % 900 + 100}"
    if node_type == "Identifier":
        return IDENTIFIER.get(subtype or "", "Định danh")
    if node_type == "Account":
        return ACCOUNT.get(subtype or "", "Tài khoản")
    return f"Mã giảm giá {name}" if subtype == "voucher" else name or node_id.rsplit(":", 1)[-1]


def money(amount: float) -> str:
    for unit, word in ((1e9, "tỷ"), (1e6, "triệu"), (1e3, "nghìn")):
        if amount >= unit:
            return f"{amount / unit:.1f}".rstrip("0").rstrip(".").replace(".", ",") + f" {word}"
    return f"{amount:.0f} đồng"


def plain(text: str) -> str:
    """Lower case without Vietnamese accents, for matching what people type."""
    folded = unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
    return "".join(ch for ch in folded if not unicodedata.combining(ch))
