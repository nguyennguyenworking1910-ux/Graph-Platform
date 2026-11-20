# Graph Platform

Gom dữ liệu của mọi đơn vị trong một doanh nghiệp về một graph chung, rồi trả lời những câu hỏi mà từng bảng riêng lẻ không trả lời được: ai phối hợp với ai, tiền đi vòng qua đâu, nên gợi ý gì cho ai.

![Kiến trúc hệ thống](assets/architecture.png)

Bản trình bày cho người mới: mở [`presentation/index.html`](presentation/index.html) bằng trình duyệt. Sơ đồ sửa được ở [excalidraw.com](https://excalidraw.com) từ các file trong `assets/`.

## Vì sao cần graph

Một khách hàng xuất hiện ở tín dụng, ví, bảo hiểm, vé xem phim dưới bốn mã khác nhau. Mỗi đơn vị chỉ thấy phần của mình. Nối các bản ghi đó lại thành một người, rồi nối người với thiết bị, cửa hàng và giao dịch, thì những thứ sau hiện ra:

- tám tài khoản đăng nhập từ cùng một điện thoại và chuyển tiền vòng cho nhau;
- một cửa hàng chỉ có vài người trả, mỗi người vài trăm triệu, toàn số tròn;
- những người xem cùng phim với bạn đã xem thêm phim gì.

## Kiến trúc

| Tầng | Việc | Thư mục |
|---|---|---|
| Extract | Kéo bảng từ PostgreSQL, BigQuery, Oracle về landing; toàn bộ hoặc tăng dần theo cột mốc | `src/graph_platform/trunk/extract.py` |
| Trunk | Profile, data dictionary, mapping về ontology chung, gate chất lượng | `src/graph_platform/trunk/` |
| Nối danh tính | Nhận ra cùng một người qua số điện thoại, email, giấy tờ; chỉ trong một doanh nghiệp | `src/graph_platform/resolution/` |
| Graph | Parquet là nguồn sự thật, Neo4j để truy vấn và vẽ | `src/graph_platform/graph/` |
| Feature store | Feature theo thời điểm (`as_of`), không nhìn trước tương lai | `src/graph_platform/features/` |
| Bài toán | 27 bài toán, mỗi bài một file cấu hình, 16 plugin, 7 họ | `src/graph_platform/branches/`, `conf/tenants/*/branches/` |
| Agents | Đề xuất dictionary và mapping; engine tất định thực thi sau khi qua gate | `src/graph_platform/agents/`, `src/graph_platform/control/` |
| Giao diện | Web tiếng Việt cho người dùng nghiệp vụ, có hình vẽ graph | `src/graph_platform/app.py`, `src/graph_platform/ui/` |

Ontology lõi có 7 loại node (Party, Account, Device, Identifier, Item, Event, Location) và 9 loại quan hệ. Mỗi doanh nghiệp thêm thuộc tính riêng trong `conf/tenants/<tenant>/ontology.yaml`; bảng rộng theo khách hàng được giữ nguyên bên cạnh graph. Cột nào của nguồn chưa đưa vào platform đều được liệt kê, không bị bỏ im lặng.

## Ba use case đưa lên production trước

| Use case | Câu hỏi | Cách làm | Trên dữ liệu mô phỏng |
|---|---|---|---|
| Merchant quasi-cash | Cửa hàng nào bị dùng để rút tiền mặt qua giao dịch khống? | Ít người trả, số tiền tròn, lặp lại | 10 cửa hàng cài sẵn chiếm trọn 10 vị trí đầu |
| Fraud detection | Nhóm tài khoản nào phối hợp bất thường? | Dùng chung thiết bị, tiền chuyển nội bộ | Tìm ra 10 trên 10 nhóm cài sẵn, trong 11 nhóm báo lên |
| Movie recommendation | Nên gợi ý phim nào cho từng khách? | Người xem giống nhau xem gì tiếp | hit@10 0,61 so với 0,33 của gợi ý phim phổ biến |

Số trên chứng minh đường ống chạy đúng, chưa chứng minh giá trị kinh doanh.

![Phục vụ realtime](assets/serving.png)

Hiện platform tính theo đợt. API realtime ở hình trên là việc của giai đoạn tới.

## Chạy thử

Bằng Docker:

```bash
cp .env.example .env                 # đặt NEO4J_PASSWORD
docker compose up -d neo4j
docker compose run --rm setup        # dữ liệu mô phỏng -> graph -> 27 bài toán, khoảng 15 phút
docker compose run --rm app user admin --name "Admin" --role admin --tenant alpha_group
docker compose up -d app             # http://localhost:8501
```

Không dùng Docker (Python 3.12, [uv](https://docs.astral.sh/uv/), một Neo4j 5 đang chạy):

```bash
uv sync
export GP_NEO4J_PASSWORD=...
uv run ./scripts/demo-data.sh alpha_group
uv run gp user admin --name "Admin" --role admin --tenant alpha_group
uv run gp ui
uv run pytest
```

## Nối dữ liệu thật

Khai kết nối trong `conf/tenants/<tenant>/domains/<domain>.yaml`. Mật khẩu chỉ nằm trong biến môi trường.

```yaml
sources:
  - name: loyalty_db
    connection:
      url_env: GP_LOYALTY_DB_URL
      tables:
        - {table: members, relation: crm.members}
        - {table: redemptions, relation: crm.redemptions, watermark: redeemed_at}
```

| Hệ thống | URL | Trạng thái |
|---|---|---|
| PostgreSQL | `postgresql+psycopg://user:pass@host:5432/db` | Đã kiểm trên Postgres thật |
| BigQuery | `bigquery://<project>` | Đã kiểm trên bảng mẫu công khai |
| Oracle | `oracle+oracledb://...` | Chưa kiểm |

```bash
uv run gp extract <tenant>      # kéo dữ liệu mới
uv run gp supervise <tenant>    # agents lập dictionary và mapping, engine kiểm và nạp
```

## Trạng thái

| Đã có | Chưa có |
|---|---|
| Đường ống từ nguồn tới graph, 27 bài toán, giao diện có hình vẽ | API realtime |
| Kết nối PostgreSQL, BigQuery | Agents chạy với model thật trên cloud |
| Nhiều doanh nghiệp, cô lập dữ liệu theo doanh nghiệp | Triển khai AWS (Terraform đã viết và qua validate, chưa apply) |
| 115 test tự động | SSO, giám sát vận hành; Docker Compose chưa chạy thử |

## Nhóm

| Thành viên | Phụ trách |
|---|---|
| Nguyên | PM; agents, data engineering, data science |
| Thảo | Agents, data engineering, data science, data analysis |
| Duy | Product owner; UX/UI, growth, marketing |
| Khang | Hạ tầng, deploy, production engineering |
