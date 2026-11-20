# Graph Platform

Gom dữ liệu của mọi đơn vị trong một doanh nghiệp về một graph chung, rồi trả lời những câu hỏi mà từng bảng riêng lẻ không trả lời được: ai phối hợp với ai, tiền đi vòng qua đâu, nên gợi ý gì cho ai.

![Kiến trúc hệ thống](assets/architecture.png)

- Bản trình bày có animation cho người mới: mở [`presentation/index.html`](presentation/index.html) bằng trình duyệt.
- Checklist và timeline cho nhóm: [`checklist/graph-platform-checklist.xlsx`](checklist/graph-platform-checklist.xlsx).
- Mọi sơ đồ sửa được ở [excalidraw.com](https://excalidraw.com) từ file `.excalidraw` trong `assets/`.

Mục lục: [Câu chuyện](#câu-chuyện) · [Graph trong một phút](#graph-trong-một-phút) · [Kiến trúc](#kiến-trúc) · [Ba use case](#ba-use-case) · [Realtime](#phục-vụ-realtime) · [Triển khai](#triển-khai) · [Chạy thử](#chạy-thử) · [Nối dữ liệu thật](#nối-dữ-liệu-thật) · [Quyết định thiết kế](#quyết-định-thiết-kế) · [Trạng thái](#trạng-thái) · [Nhóm](#nhóm)

## Câu chuyện

Một tập đoàn có bảy đơn vị: tín dụng, bảo hiểm, tiết kiệm, ví thanh toán, merchant, du lịch, vé xem phim. Mỗi đơn vị một hệ thống, một cách đặt tên cột, một mã khách hàng.

| Bước | Chuyện gì xảy ra | Ai thấy |
|---|---|---|
| 1 | Một người vay tiêu dùng. Hệ thống tín dụng gọi họ là `C000123`. | Tín dụng |
| 2 | Họ mở ví, mã `U88-4471`. Mua bảo hiểm, mã `H-45210`. Mua vé phim, mã `MB70093`. | Mỗi đơn vị thấy một mảnh |
| 3 | Platform kéo dữ liệu bốn hệ thống về, nhận ra cùng số điện thoại, email, giấy tờ. Bốn bản ghi thành một khách hàng. | Mọi đơn vị thấy cùng một người |
| 4 | Khách hàng đó dùng chung một điện thoại với bảy tài khoản khác. Tám tài khoản chuyển tiền vòng cho nhau. | Chỉ graph thấy |
| 5 | Một trong tám tài khoản trả tiền đều đặn cho một tiệm bánh: mỗi lần 10 hoặc 20 triệu, chín lần. Tiệm bánh chỉ có năm người trả như vậy. | Chỉ graph thấy |
| 6 | Đội chống gian lận nhận một danh sách đã xếp hạng, kèm hình vẽ và lý do. Tín dụng biết người vay này có quan hệ với ai. | Người ra quyết định |

Bước 3 là chỗ khó nhất, và là nền của mọi thứ phía sau:

![Nối danh tính](assets/identity.png)

## Graph trong một phút

| Từ | Nghĩa | Ví dụ |
|---|---|---|
| Node | Một thứ có thật | một người, một cửa hàng, một điện thoại, một bộ phim |
| Edge | Quan hệ giữa hai node, có thời điểm | A chuyển tiền cho B lúc 10:02 ngày 3/5 |
| Path | Chuỗi edge nối tiếp | A chuyển cho B, B chuyển cho C, C chuyển lại A |
| Hỏi theo quan hệ | Điều bảng làm chậm và graph làm nhanh | "Những ai dùng chung thiết bị với người này, và họ đã từng nợ xấu chưa?" |

Với bảng, câu hỏi cuối cần join nhiều bảng của nhiều đơn vị, mỗi bảng một mã khách hàng khác nhau. Với graph, đó là đi hai bước từ một node.

![Ontology](assets/ontology.png)

| Node | Là gì | Ví dụ subtype |
|---|---|---|
| Party | Người hoặc tổ chức | person, organization, merchant |
| Account | Tài khoản, hợp đồng | wallet, bank, loan, savings, policy, loyalty |
| Device | Thiết bị dùng để giao dịch | mobile, web, pos |
| Identifier | Thứ dùng để nhận ra một Party | phone, email, national_id, address, ip, merchant_code, tax_id |
| Item | Thứ được mua | loan_product, insurance_product, savings_product, trip, movie, voucher, goods |
| Event | Việc xảy ra, có trạng thái | application, loan, loan_performance, claim, deposit, churn, fraud_case, chargeback |
| Location | Nơi chốn | city, country, geo_cell |

| Edge | Từ | Tới | Mang thông tin |
|---|---|---|---|
| OWNS | Party | Account | |
| USES | Party | Device | |
| HAS_IDENTIFIER | Party | Identifier | |
| TRANSACTS | Party, Account | Party, Account | amount, currency, channel, status, cross_border, corridor |
| APPLIES_FOR | Party | Event | |
| PURCHASES | Party | Item | amount, currency, quantity, voucher_code, status |
| REFERS | Party | Party | relation |
| LOCATED_AT | Party | Location | |
| RELATES_TO | Event | Event, Account | |

Khai báo đầy đủ ở [`conf/ontology/core.yaml`](conf/ontology/core.yaml). Tên riêng của người được đánh dấu là dữ liệu cá nhân và không bao giờ vào graph.

## Kiến trúc

### Các tầng

| Tầng | Vào | Ra | Thư mục code |
|---|---|---|---|
| Extract | Bảng trong PostgreSQL, BigQuery, Oracle | `landing/<domain>/<source>/<table>/load_ts=.../` | `trunk/extract.py` |
| Landing | File thô | Bản gốc, không sửa | `trunk/landing.py` |
| Profile | Landing | Thống kê cột, mẫu đã che | `trunk/profiler.py` |
| Dictionary | Profile | Ý nghĩa từng cột, cờ dữ liệu cá nhân | `trunk/dictionary.py` |
| Mapping | Dictionary, ontology | File khai báo: cột nào thành node, quan hệ nào | `contracts/mapping_spec.py`, `trunk/compiler.py` |
| Gate | Kết quả chạy thử | Đạt thì ghi, không đạt thì giữ bản cũ | `trunk/pipeline.py` |
| Canonical | Mapping đã qua gate | Node và quan hệ chuẩn, theo từng đơn vị | `data/<tenant>/canonical/` |
| Nối danh tính | Canonical mọi đơn vị | Mỗi người một mã chung | `resolution/` |
| Graph | Canonical, danh tính | Parquet (nguồn sự thật), Neo4j (truy vấn) | `graph/` |
| Feature store | Graph tại một ngày | Feature theo thời điểm | `features/` |
| Bài toán | Feature, nhãn | Danh sách, điểm số, báo cáo | `branches/` |
| Phục vụ | Kết quả | Web UI, SDK Python, API (kế hoạch) | `app.py`, `ui/`, `sdk.py` |

Thư mục code nằm dưới `src/graph_platform/`.

### Trunk: từ database tới graph

![Trunk](assets/trunk.png)

Một mapping spec trông như sau. Không có SQL, không có code: chỉ khai báo, và chỉ được dùng 14 phép biến đổi có sẵn.

```yaml
source: movie_tix
domain: movies
tables:
  - table: tickets
    nodes:
      - {type: Item, subtype: movie, key: {col: movie_code}, attrs: {title: {col: movie_title}}}
      - {type: Party, subtype: merchant, key: {col: cinema_code}, attrs: {trade_name: {col: cinema_name}}}
    edges:
      - type: PURCHASES
        src: {type: Party, key: {col: member_id}}
        dst: {type: Item, key: {col: movie_code}}
        key: {col: ticket_id}
        time: {col: purchased_at, tf: [epoch_ms]}
        attrs: {amount: {col: price}, quantity: {col: seats}}
```

| Gate | Kiểm gì | Ngưỡng mặc định |
|---|---|---|
| `spec_valid` | Spec đúng ontology, cột có thật, dictionary phủ đủ cột | phải đúng |
| `key_null_rate` | Tỷ lệ khóa rỗng | tối đa 1% |
| `duplicate_rate` | Tỷ lệ trùng khóa | tối đa 5% |
| `time_parse_rate` | Tỷ lệ đọc được thời gian | tối thiểu 99% |
| `endpoint_coverage` | Hai đầu của quan hệ có tồn tại | tối thiểu 99% |
| `reconciliation` | Không bảng đầu ra nào rỗng | phải đúng |

Ngưỡng do chủ platform đặt cho từng doanh nghiệp (`gates.yaml`). Mapping spec không tự nới được ngưỡng của chính nó.

### Nối danh tính

| Luật | Chi tiết |
|---|---|
| Nối chắc | Trùng giấy tờ tùy thân, mã merchant, hoặc mã số thuế |
| Email | Nối nếu hai bên không có giấy tờ mâu thuẫn |
| Số điện thoại | Chỉ nối khi tên giống nhau từ 0,88 trở lên (hai người trong một nhà hay dùng chung số) |
| Bỏ qua định danh đông người | Một số điện thoại dùng bởi hơn 6 bên thì không còn là bằng chứng |
| Chặn cụm lớn | Cụm hơn 12 bản ghi chỉ giữ lại bằng chứng chắc |
| Không bao giờ | Nối người với cửa hàng; nối giữa hai doanh nghiệp |

Trên dữ liệu mô phỏng: precision 0,9997, recall 0,979; 51.481 bản ghi thành 20.636 khách hàng và cửa hàng.

### Graph

| | Parquet | Neo4j |
|---|---|---|
| Vai trò | Nguồn sự thật | Bản chiếu để truy vấn và vẽ |
| Ai đọc | Feature store, mô hình | Giao diện, người điều tra, API |
| Hỏng thì | Dựng lại từ canonical | Nạp lại từ Parquet |

Graph không lấy toàn bộ lịch sử và toàn bộ hàng xóm. Mỗi doanh nghiệp có một phạm vi tính (`graph_scope.yaml`):

| Tham số | Mặc định | Tác dụng |
|---|---|---|
| `lookback_days` | 365 | Giao dịch cũ hơn mốc này không tính |
| `max_degree` | 50 | Mỗi người giữ tối đa 50 quan hệ mạnh nhất |
| `device_fanout` | 20 | Thiết bị dùng bởi hơn 20 người (máy công cộng) bị bỏ qua |
| `model_hops` | 2 | Mô hình graph chỉ chạy trong 2 bước quanh nhóm đang xét |

### Feature store

Mọi feature nhận một ngày `as_of` và chỉ nhìn dữ liệu trước ngày đó. Tên feature theo một khuôn duy nhất, sai khuôn thì không đăng ký được:

```
<entity>__<family>__<metric>__<qualifier>__<window>
party__degree__txn_cnt__transacts_out__90d
```

| Họ | Đo cái gì | Ví dụ |
|---|---|---|
| `profile` | Thuộc tính của chính khách hàng | `party__profile__income_val__self__all` |
| `degree` | Số lượng và giá trị quan hệ trực tiếp | `party__degree__amt_sum__transacts_in__30d` |
| `shared` | Dùng chung thiết bị, định danh | `party__shared__party_nuniq__device__all` |
| `khop` | Chuyện của những người xung quanh | `party__khop__default_rate__refers_any_h1__all` |
| `centrality` | Vị trí trong mạng | điểm trung tâm của dòng tiền |
| `community` | Đặc điểm của cụm mình thuộc về | tỷ lệ dùng chung thiết bị trong cụm |
| `embedding` | Vị trí trong mạng dưới dạng vector | `party__embedding__dim007__all__all` |

### Bài toán

Mỗi bài toán là một file trong `conf/tenants/<tenant>/branches/`. Thêm bài toán cùng họ: thêm một file. Thêm họ mới: thêm một plugin. Trunk không đổi.

```yaml
name: fraud_ring
owner_domain: payment
title: "Nhóm gian lận"
question: "Những nhóm tài khoản nào đang phối hợp với nhau một cách bất thường?"
family: community
plugin: community_rings
as_of: 2025-06-01
params: {min_size: 3, max_size: 30}
```

| Họ | Bản chất | Bài toán đã có |
|---|---|---|
| `node_level` | Chấm điểm một người hoặc một cửa hàng | chấm điểm tín dụng, churn, tài khoản trung gian, chiếm đoạt tài khoản, rủi ro bồi thường, merchant quasi-cash, rủi ro hoàn tiền, giá trị khách hàng, đầu cơ vé, rà soát hạn mức, vay vốn merchant |
| `edge_level` | Dự đoán hoặc phân loại một quan hệ | gợi ý phim, gợi ý chuyến đi, cross-sell bảo hiểm, gợi ý merchant, giao dịch đáng ngờ |
| `community` | Tìm cụm bất thường | nhóm gian lận, lạm dụng khuyến mãi, nhóm đặt chỗ chung |
| `path_flow` | Lần theo dòng chảy | dòng tiền quay vòng, tuyến chuyển tiền bất thường, đầu mối chuỗi cung ứng, ảnh hưởng giới thiệu |
| `temporal` | Lan truyền theo thời gian | cảnh báo sớm nợ xấu, ưu tiên thu hồi nợ |
| `entity_resolution` | Hợp nhất danh tính | hồ sơ khách hàng xuyên đơn vị |
| `knowledge` | Hỏi đáp, giải thích | tài liệu ngữ cảnh theo khách hàng |

27 bài toán, 16 plugin, 7 họ.

### Agents và control plane

Agents không đọc dữ liệu thật. Chúng nhận thống kê cột và mẫu đã che (chữ số thành 9, chữ cái thành a), rồi viết ra file khai báo. Engine chạy file đó và quyết định.

| Vai trò | Agent | Nhận | Viết ra |
|---|---|---|---|
| Phân tích dữ liệu | `profiler` | Thống kê cột, ghi chú của đơn vị | Data dictionary |
| Kỹ sư dữ liệu | `mapper` | Ontology, dictionary | Mapping spec |
| Kỹ sư dữ liệu | `maintainer` | Spec cũ, gate lỗi, schema đổi | Mapping spec đã sửa |
| Nhà khoa học dữ liệu | `experiment` | Yêu cầu, danh mục bài toán, danh sách feature | Cấu hình bài toán |
| Vận hành | `triage` | Sự cố | Chẩn đoán |

| Luật của control plane | |
|---|---|
| Mọi đề xuất là một change proposal | Có tác giả, lý do, kết quả gate |
| Qua gate thì áp dụng | Không qua thì sửa, tối đa 3 vòng, rồi chờ người |
| Thay đổi phá hủy luôn chờ người duyệt | Xóa bảng, bỏ loại đang dùng, đổi luật nối danh tính |
| Có trần chi phí | Vượt số token trong ngày thì ngừng gọi model, phần tất định vẫn chạy |
| Mọi thứ ghi audit | Mỗi lần gọi model, mỗi hành động, kèm người làm |

Hiện agents mới được kiểm bằng model kịch bản. Chất lượng với model thật chưa đo.

### Nhiều doanh nghiệp, một platform

| Lớp | Cách tách |
|---|---|
| Cấu hình | `conf/tenants/<tenant>/` riêng cho từng doanh nghiệp |
| Dữ liệu | `data/<tenant>/` riêng; không đường đọc ghi nào xuyên doanh nghiệp |
| Danh tính | Chỉ nối trong một doanh nghiệp |
| Đơn vị trong doanh nghiệp | Đơn vị chỉ đọc đơn vị khác khi được khai trong `readers` |
| Neo4j | Hiện tách bằng thuộc tính trên mỗi node; tách cứng cần mỗi doanh nghiệp một instance hoặc bản Enterprise |
| Cloud | Một IAM role cho mỗi doanh nghiệp và mỗi đơn vị |
| Giao diện | Ba vai trò (viewer, analyst, admin), giới hạn theo doanh nghiệp và đơn vị |

Dữ liệu chỉ một đơn vị có vẫn có chỗ: doanh nghiệp khai thêm thuộc tính trong `ontology.yaml` của mình, hoặc giữ nguyên cả bảng bên cạnh graph bằng `attached`. Cột nào của nguồn chưa đưa vào platform đều được liệt kê sau mỗi lần xử lý.

### Dữ liệu nằm ở đâu

```
conf/ontology/core.yaml                 ontology lõi
conf/tenants/<tenant>/domains/          khai báo đơn vị, nguồn, kết nối database
conf/tenants/<tenant>/dictionaries/     data dictionary
conf/tenants/<tenant>/mappings/         mapping spec
conf/tenants/<tenant>/branches/         cấu hình bài toán
data/<tenant>/landing/                  bản gốc từ nguồn
data/<tenant>/canonical/                node và quan hệ chuẩn, theo đơn vị
data/<tenant>/shared/                   danh tính đã nối, graph
data/<tenant>/features/                 feature theo phạm vi và ngày
data/<tenant>/branches/                 kết quả từng lần chạy bài toán
data/<tenant>/control/                  đề xuất, audit
```

## Ba use case

![Ba hình dạng](assets/patterns.png)

### Merchant quasi-cash

| | |
|---|---|
| Câu hỏi | Cửa hàng nào đang bị dùng để rút tiền mặt qua giao dịch khống? |
| Vì sao khó | Từng giao dịch hợp lệ. Chỉ khi nhìn tất cả người trả của một cửa hàng mới thấy bất thường. |
| Tín hiệu | Tỷ lệ số tiền tròn; tỷ trọng của 5 người trả nhiều nhất; tỷ lệ lặp lại (ít người, nhiều giao dịch) |
| Cách tính | Nhân ba tín hiệu thành một điểm cho mỗi cửa hàng có từ 20 giao dịch, rồi xếp hạng |
| Kết quả mô phỏng | 10 cửa hàng cài sẵn chiếm trọn 10 vị trí đầu trong 177 cửa hàng được chấm |
| Code | [`plugins/merchant_quasi_cash.py`](src/graph_platform/branches/plugins/merchant_quasi_cash.py) |

### Fraud detection

| | |
|---|---|
| Câu hỏi | Nhóm tài khoản nào đang phối hợp với nhau một cách bất thường? |
| Vì sao khó | Mỗi tài khoản trông bình thường. Dấu hiệu nằm ở quan hệ giữa chúng. |
| Tín hiệu | Dùng chung thiết bị; tiền chuyển qua lại trong nhóm; tiền đi thành vòng khép kín |
| Cách tính | Gom các tài khoản dùng chung thiết bị thành cụm từ 3 tới 30 thành viên, chấm theo lượng giao dịch nội bộ |
| Kết quả mô phỏng | Tìm ra 10 trên 10 nhóm cài sẵn, trong 11 nhóm báo lên |
| Code | [`plugins/community_rings.py`](src/graph_platform/branches/plugins/community_rings.py), [`circular_flow.py`](src/graph_platform/branches/plugins/circular_flow.py) |

### Movie recommendation

| | |
|---|---|
| Câu hỏi | Nên gợi ý phim nào cho từng khách? |
| Vì sao graph | Người xem giống bạn là hàng xóm hai bước của bạn trong graph: bạn, phim, họ. |
| Tín hiệu | Phim mà những người cùng gu đã xem; cộng thêm phim mà người quen của bạn đã xem |
| Cách tính | Giữ lại 90 ngày cuối để kiểm; gợi ý 10 phim cho mỗi khách; đo có trúng phim khách thật sự xem không |
| Kết quả mô phỏng | hit@10 0,61 (cùng gu) và 0,64 (thêm người quen), so với 0,33 của gợi ý phim phổ biến; 3.478 khách được kiểm |
| Code | [`plugins/link_prediction.py`](src/graph_platform/branches/plugins/link_prediction.py) |

Dữ liệu mô phỏng dùng 42 phim chiếu ở Việt Nam 2025-2026. Rạp là một merchant trong cùng mạng với mọi cửa hàng khác.

Số trên chứng minh đường ống chạy đúng trên dữ liệu có cài sẵn tín hiệu. Chúng chưa chứng minh giá trị kinh doanh.

## Phục vụ realtime

![Phục vụ realtime](assets/serving.png)

Hiện platform tính theo đợt. API realtime là việc của giai đoạn tới: sản phẩm gọi, platform trả mức rủi ro hoặc danh sách gợi ý kèm lý do.

## Triển khai

![Triển khai](assets/deployment.png)

Terraform ở [`infra/terraform/`](infra/terraform/) đã qua `terraform validate`, chưa apply. Chưa có tài nguyên cloud nào được tạo.

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

| Lệnh | Làm gì |
|---|---|
| `gp simulate <tenant>` | Sinh dữ liệu mô phỏng |
| `gp extract <tenant>` | Kéo dữ liệu mới từ database nguồn |
| `gp onboard <tenant>` | Chạy trunk với mapping có sẵn |
| `gp supervise <tenant>` | Một lượt đầy đủ có agents |
| `gp resolve <tenant>` | Nối danh tính |
| `gp graph build <tenant>` | Dựng graph và nạp Neo4j |
| `gp branch run <tenant> <tên>` | Chạy một bài toán |
| `gp proposal list`, `gp proposal decide` | Xem và duyệt đề xuất |
| `gp audit <tenant>` | Xem audit log |
| `gp ui` | Mở giao diện web |

## Nối dữ liệu thật

Khai kết nối trong `conf/tenants/<tenant>/domains/<domain>.yaml`. Mật khẩu chỉ nằm trong biến môi trường.

```yaml
sources:
  - name: loyalty_db
    connection:
      url_env: GP_LOYALTY_DB_URL
      tables:
        - {table: members, relation: crm.members}                                   # trích toàn bộ mỗi lần
        - {table: redemptions, relation: crm.redemptions, watermark: redeemed_at}   # chỉ kéo dòng mới
```

| Hệ thống | URL | Trạng thái |
|---|---|---|
| PostgreSQL | `postgresql+psycopg://user:pass@host:5432/db` | Đã kiểm trên Postgres thật |
| BigQuery | `bigquery://<project>` | Đã kiểm trên bảng mẫu công khai |
| Oracle | `oracle+oracledb://...` | Chưa kiểm |

Dữ liệu đặc thù của một đơn vị:

| Dữ liệu | Cách đưa vào |
|---|---|
| Một trường mô tả khách hàng, tài khoản, sự kiện | Khai thuộc tính trong `conf/tenants/<tenant>/ontology.yaml` |
| Một bảng rộng theo khách hàng (điểm số, số dư theo ngày) | `attached` trong mapping spec; đọc bằng `sdk.party_table` |
| Một bảng trộn nhiều loại bản ghi | `when: {col, is_in}` trên từng node hoặc quan hệ |
| Ghi chú cho agents | `conf/tenants/<tenant>/context/<source>.md` |

## Quyết định thiết kế

| Chọn | Thay vì | Vì |
|---|---|---|
| Parquet là nguồn sự thật, Neo4j là bản chiếu | Mọi thứ trong graph database | Mô hình cần đọc hàng loạt và dựng lại quá khứ; graph database giỏi truy vấn, không giỏi hai việc đó |
| Ontology lõi đóng, doanh nghiệp thêm thuộc tính | Mỗi doanh nghiệp tự đặt loại node | Feature và bài toán dùng lại được giữa các doanh nghiệp |
| Agents viết khai báo, engine thực thi | Agents tự chạy code trên dữ liệu | Kiểm được, lặp lại được, agents không chạm dữ liệu thật |
| Mỗi bài toán một file cấu hình | Mỗi bài toán một đoạn code riêng | Đơn vị nghiệp vụ tự chỉnh định nghĩa và chạy lại |
| Mọi thứ theo thời điểm | Tính trên dữ liệu mới nhất | Không nhìn trước tương lai khi huấn luyện; kết quả kiểm lại được |

## Trạng thái

| Đã có | Chưa có |
|---|---|
| Đường ống từ nguồn tới graph, 27 bài toán, giao diện có hình vẽ | API realtime |
| Kết nối PostgreSQL, BigQuery | Agents chạy với model thật trên cloud |
| Nhiều doanh nghiệp, cô lập dữ liệu theo doanh nghiệp | Triển khai cloud |
| 115 test tự động | SSO, giám sát vận hành; Docker Compose chưa chạy thử |

| Giai đoạn | Nội dung | Xong khi |
|---|---|---|
| 1 Nền chung | Docker, CI, nguồn dữ liệu thật đầu tiên, agents chạy model thật | Một nguồn thật đã vào graph |
| 2 Ba use case | Chạy theo đợt trên dữ liệu thật, có số đo và giao diện | Người nghiệp vụ xác nhận kết quả |
| 3 Realtime | API service cho ba use case | Ba endpoint đạt độ trễ mục tiêu |
| 4 Production | Giám sát, bảo mật, khách hàng pilot | Một khách hàng dùng thật |

Việc của từng người, lý do, các bước và timeline 12 tuần nằm trong [checklist](checklist/graph-platform-checklist.xlsx).

Công nghệ: Python 3.12, DuckDB, Polars, Parquet, Neo4j 5, scikit-learn, PyTorch, MLflow, Streamlit, Cytoscape.js, SQLAlchemy, Terraform.

## Nhóm

| Thành viên | Phụ trách |
|---|---|
| Nguyên | PM; agents, data engineering, data science |
| Thảo | Agents, data engineering, data science, data analysis |
| Duy | Product owner; UX/UI, growth, marketing |
| Khang | Hạ tầng, deploy, production engineering |
