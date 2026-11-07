# Schema thiết kế — Redis (cache) và VBIo (permanent store)

> Tài liệu tra cứu nhanh field/type/nguồn gốc của 2 schema đang dùng trong repo, để
> thiết kế lại hoặc review hạ tầng. Chi tiết logic nghiệp vụ nằm ở `ENGINE_LOGIC_FLOW.md`;
> flowchart nằm ở `ENGINE_FLOWCHARTS.md` / `ENGINE_FLOWCHARTS_SLIDES.md`.

---

## 1. Quan hệ 2 tầng

```
VBIo (vbio.parquet / DB)          Redis (cache)
  "permanent store"                 "hot cache, có TTL cho dữ liệu chưa chắc"
  1 row / (Plate,State,Make,Model)  1 hash key / (Plate,State,Make,Model)
  không bao giờ tự mất              OBSERVED+PENDING: tự hết hạn nếu bỏ rơi
                                     OBSERVED+ACTIVE / VBIO: vĩnh viễn (persist)
```
- **Cold start**: Redis trống cho 1 plate → query VBIo, seed candidate (`source=VBIO`).
- **Camera tự phát hiện** (chưa có trong VBIo) → tạo candidate mới trong Redis
  (`source=OBSERVED`, `status=PENDING`), **không** ghi vào VBIo ngay.
- **Đồng bộ ngược OBSERVED → VBIo thật**: hiện **chưa code** (xem mục 4, open item).

---

## 2. Redis — Candidate hash

**Key:** `vbio:{PLATE_NUMBER}:{PLATE_STATE}:{make}_{model}`
(`make`/`model` đã qua `normalize_model_key()`: bỏ dấu, khoảng trắng, hoa/thường, dấu câu —
KHÔNG áp dụng alias ngữ nghĩa, vd "Hyundai"≠"Huyndai" ở cấp key, phải canonicalize alias
**trước** khi build key).

| Field | Type (Redis string) | Type thật | Nguồn / cách set | Ghi chú |
|---|---|---|---|---|
| `key` | string | — | tính từ Plate+State+Make+Model | Không lưu riêng, chính là tên hash key |
| `plateNumber` | string | string | Detection / VBIo | Giữ nguyên case gốc |
| `plateState` | string | string | Detection / VBIo | |
| `make` | string | string | Detection (canonicalized) / VBIo (canonicalized) | |
| `model` | string | string | Detection (canonicalized) / VBIo (canonicalized) | |
| `firstSeen` | ISO datetime string | datetime | VBIo `FirstSeen`, hoặc `now` lúc admit (nếu OBSERVED) | |
| `lastSeen` | ISO datetime string | datetime | VBIo `LastSeen`, hoặc `now` **chỉ** khi có detect **high-confidence** khớp đúng candidate | KHÔNG đổi khi chỉ có detect low-confidence |
| `lastDecayedAt` | ISO datetime string | datetime | `now`, refresh **mỗi tick** cho mọi candidate của plate | Mốc decay riêng từng candidate, không phải field nghiệp vụ |
| `observationCount` | string (int) | int | VBIo `ObservationCount`, hoặc `+1` mỗi lần có detect high-confidence khớp đúng candidate | Dùng để xét promotion PENDING→ACTIVE |
| `score` | string (float) | float ~[0,1] | VBIo `CredibilityScore` × hệ số seed, hoặc EMA update mỗi tick | Điểm tích lũy, decay dần theo thời gian |
| `posterior` | string (float) | float [0,1] | Tính lại **mỗi tick** (Bayes) | Không persist ý nghĩa lâu dài — chỉ là kết quả tick gần nhất |
| `hmmPosterior` | string (float) | float [0,1] hoặc rỗng | VBIo `HmmPosterior` | Chỉ để truy vết/debug, không dùng trong logic runtime |
| `status` | string | `PENDING` \| `ACTIVE` | VBIo `Status` (seed), hoặc `PENDING` lúc admit mới → `ACTIVE` khi đủ `n_activate` lần confirm | Quyết định có bị TTL quét hay không (mục 4) |
| `source` | string | `VBIO` \| `OBSERVED` | Cố định theo nguồn admit | `VBIO` = có backing DMV; `OBSERVED` = camera tự phát hiện |
| *(TTL ở cấp key, không phải field)* | Redis `EXPIRE` | — | Set/refresh khi `source=OBSERVED AND status=PENDING` và vừa bị detect chạm tới | Xem mục 4 |

**Lưu ý kỹ thuật:** Redis hash chỉ lưu string — mọi field numeric/datetime đều serialize
ra string lúc ghi (`HSET`) và parse lại lúc đọc (`HGETALL`). `None` → chuỗi rỗng `""`.

---

## 3. Redis — Meta key (mốc thời gian cấp plate)

**Key:** `vbio:meta:{PLATE_NUMBER}:{PLATE_STATE}:lastUpdate`

| Field | Type | Ý nghĩa |
|---|---|---|
| *(giá trị string đơn, không phải hash)* | ISO datetime string | Lần detect gần nhất của PLATE này (không phải của 1 candidate riêng) — dùng tính `gapDays` cho Bayes transition prior |

---

## 4. Redis — TTL / lifecycle (không phải field, là thuộc tính của key)

| Khi nào | Lệnh | Hiệu lực |
|---|---|---|
| Candidate `OBSERVED`+`PENDING` vừa bị 1 detection (bất kỳ confidence) chạm tới | `EXPIRE key gc_days*86400` (gc_days=180) | Nếu không ai chạm lại trong 180 ngày, Redis tự xóa key — kể cả khi plate không bao giờ được detect lại |
| Candidate chuyển `PENDING`→`ACTIVE` (đủ `n_activate`=3 lần confirm) | `PERSIST key` | Bỏ TTL hẳn, vĩnh viễn trong cache |
| Candidate `source=VBIO` | `PERSIST key` (luôn luôn) | Không bao giờ tự hết hạn — đã có backing permanent ở VBIo |

---

## 5. VBIo — parquet schema (`vbio.parquet` / DB thật)

**Grain:** 1 row / `(PlateNumber, PlateState, Make, Model)` — đây là **permanent store**,
khác hẳn Redis (cache có TTL).

| Field | Type (pyarrow) | Mô tả |
|---|---|---|
| `Id` | `int64` | Surrogate key (sinh sau khi dedupe, không phải id gốc của nguồn) |
| `PlateNumber` | `string` | Biển số. Anchor key cùng `PlateState` |
| `PlateState` | `string` | Bang đăng ký (2 ký tự). Anchor key cùng `PlateNumber` |
| `Make` | `string` | Hãng xe, vd `TOYOTA` |
| `Model` | `string` | Model xe, vd `CAMRY` |
| `FirstSeen` | `timestamp(us)` | Suy ra từ `VehicleYear` mới nhất trong nhóm nguồn (biểu diễn ngày 01/01 của năm đó) |
| `LastSeen` | `timestamp(us)` | Giống cách suy ra `FirstSeen` lúc seed; pipeline sống nên tự update độc lập khi có detect thật |
| `ObservationCount` | `int32` | Seed từ số dòng `VehicleYear` gộp lại trong nhóm (proxy cho số lần DMV xác nhận) |
| `CredibilityScore` | `float64` | Điểm tích lũy (0.0–100.0) — **thang khác** Redis `score` (~[0,1]); map qua `seed_base` lúc seed vào Redis |
| `HmmPosterior` | `float64` | Posterior seed ban đầu [0.0, 1.0] — nhân với `seed_base` để ra `score` lúc seed vào Redis |
| `Status` | `string` | `PENDING` (đang tích lũy xác nhận) hoặc `ACTIVE` (đã qua ngưỡng) |

**Thứ tự cột chuẩn:** `Id, PlateNumber, PlateState, Make, Model, FirstSeen, LastSeen, ObservationCount, CredibilityScore, HmmPosterior, Status`.

---

## 6. Mapping VBIo → Redis (lúc seed/cold-start)

| VBIo (PascalCase) | → | Redis (camelCase) | Biến đổi |
|---|---|---|---|
| `PlateNumber` | → | `plateNumber` | giữ nguyên |
| `PlateState` | → | `plateState` | giữ nguyên |
| `Make` | → | `make` | canonicalize qua `make_model_mapping.json` |
| `Model` | → | `model` | canonicalize qua `make_model_mapping.json` |
| `FirstSeen` | → | `firstSeen` | giữ nguyên (hoặc `now` nếu thiếu) |
| `LastSeen` | → | `lastSeen` | giữ nguyên (hoặc `now` nếu thiếu) |
| `ObservationCount` | → | `observationCount` | giữ nguyên |
| `CredibilityScore` **hoặc** `HmmPosterior` | → | `score` | `score = seed_base (0.7) * HmmPosterior` — **không** dùng trực tiếp `CredibilityScore` (thang điểm khác nhau, xem mục 5) |
| `HmmPosterior` | → | `hmmPosterior` | giữ nguyên, chỉ để debug |
| `Status` | → | `status` | giữ nguyên |
| *(không có)* | → | `posterior` | luôn seed = `epsilon` (0.01), không lấy từ VBIo |
| *(không có)* | → | `lastDecayedAt` | luôn = `now` lúc seed (không bao giờ lấy từ VBIo — tránh decay lùi về quá khứ) |
| *(không có)* | → | `source` | luôn = `VBIO` |

**Chiều ngược lại (Redis `OBSERVED` → VBIo thật):** hiện **chưa có job nào làm việc này**
(xem `ENGINE_LOGIC_FLOW.md` mục 10 — open item). 1 candidate `OBSERVED` dù đã `ACTIVE`
(persist, vĩnh viễn trong Redis) vẫn chưa tự động trở thành 1 row VBIo thật.
