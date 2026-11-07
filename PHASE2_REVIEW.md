# Soát xét: Code `super-potato` vs. VBIO-RFC-01 Phase 2

> Dựa trên `assets/TECHNICAL PROPOSAL - VBIO-RFC-01.pdf` (phần "Solution Concept (Phase 2)")
> và `assets/V-Bio Plan 20260709.pdf`. Mục tiêu: giải thích đơn giản cơ chế code hiện tại
> đang làm gì, và đối chiếu xem đã khớp với concept Phase 2 chưa.

## 1. Cơ chế vận hành hiện tại (nói nôm na)

Luồng chính nằm ở `vbio/enrichment/` + `vbio/vehicle_lookup_service.py`:

```
Ảnh --> MCAP (nhận diện xe/biển số) --> đọc Plate+State
     --> tra VBIo (parquet/Redis) lấy các "candidate" Make-Model đã biết cho biển số đó
     --> EnrichmentEngine.process_detection() xử lý 1 "detection" --> trả về 1 kết quả đã xếp hạng (tier)
```

Mỗi biển số có thể có **nhiều candidate** (nhiều cặp Make-Model khác nhau từng được ghi nhận),
mỗi candidate lưu trong Redis dưới dạng 1 hash riêng (`vbio/clients/redis_store.py`), gồm:
`score`, `posterior`, `lastSeen`, `lastDecayedAt`, `observationCount`, `status` (PENDING/ACTIVE)...

Mỗi lần có 1 detection mới, `EnrichmentEngine.process_detection()` (file `engine.py`) làm 5 bước,
đúng thứ tự:

1. **Decay (lão hóa điểm)** — `scoring.decay_all()`
   Mỗi candidate tự giảm điểm theo thời gian riêng của nó (không phải thời gian chung của biển số):
   `score *= e^(-λ * số_ngày_trôi_qua)`, với `λ = 0.00385` (half-life ~180 ngày).
   → Khớp đúng mục **2.1 Lazy Exponential Time Decay** trong RFC-01.

2. **Cộng/trừ điểm theo độ tin cậy (confidence)** — `scoring.apply_high_confidence_update()` /
   `apply_low_confidence_update()`
   - Nếu confidence ≥ Th1 (0.90): candidate thắng được **+confidence**, mọi candidate khác trên
     cùng biển số bị **trừ malus** = `malus_rate * confidence * score_hiện_tại` (malus_rate = 0.10).
   - Nếu confidence < Th1 nhưng candidate đó đã tồn tại: chỉ được cộng nhẹ
     `malus_low_rate * confidence`, không ai bị trừ (tránh ảnh đêm/nhiễu phá điểm xe khác).
   → Khớp đúng mục **2.2 Confidence-Driven Scoring & Competitor Malus**.

3. **Tính posterior kiểu Bayes (HMM)** — `posterior.py`
   - `compute_pi_score()`: chuẩn hóa điểm số hiện tại thành phân phối xác suất (bằng chứng tích lũy).
   - `compute_t_same_dynamic()`: tính "độ tin vào trạng thái cũ" — co dần về `1/N` nếu biển số đó
     lâu không xuất hiện (gap ngày lớn).
   - `compute_transition_prior()`: trộn posterior lần trước với bằng chứng tích lũy theo `T_same`.
   - `compute_likelihoods()` + `compute_posteriors()`: nhân với độ tin cậy riêng của MCAP cho từng
     candidate (nếu có top-k), chuẩn hóa lại → ra posterior mới cho mỗi candidate.
   → Đây là phần triển khai ý tưởng ở mục **3. Bayesian HMM Look-Up & Enrichment Logic**.

4. **Phân "tier" kết quả trả về** — `tiering.decide_tier()`
   - `OBSERVED`: MCAP tự tin ≥ Th1 → tin luôn kết quả MCAP, không cần tra cứu.
   - `HIGH`: posterior ≥ Th3 (0.80) và candidate thắng còn "sống" (score > 0).
   - `MEDIUM`: không đạt Th3 → fallback sang candidate có điểm cao nhất **cùng Make** với candidate
     Bayes chọn (giải quyết case nhầm sub-model, ví dụ Mercedes GLB vs GLC Coupe).
   - `LOW`: không có candidate cùng Make nào còn sống → lấy candidate điểm cao nhất trên cả biển số
     (giải quyết case nhầm giữa các mẫu xe nhìn giống nhau khác hãng, ví dụ GMC Acadia vs Terrain).
   - `NONE`: không còn candidate nào có score > 0 → trả "Unknown", không fallback về DMV cũ.
   → Khớp đúng **Four-Tier Output & Rescue Gates** ở mục 3.2.

5. **Lưu lại Redis** (`save_candidates`) — nếu không phải `dry_run`.

Ngoài ra còn có "cold start": nếu biển số chưa từng có trong Redis, engine sẽ nạp (seed) các
candidate ban đầu từ VBIo (file parquet dựng từ DMV export, xem `vbio_schema.py`), với điểm khởi
tạo `score = seed_base * HmmPosterior`.

## 2. Đối chiếu với Phase 2 concept trong RFC-01

| Mục trong RFC-01 Phase 2 | Có trong code? | Ghi chú |
|---|---|---|
| 1. Identification of high-quality detections | ⚠️ Không có trong repo này | Đây là bước lọc ở tầng MCAP/upstream (daytime, 1 xe/frame, đủ plate+state...), repo coi input detection là đã qua lọc sẵn, không tự kiểm tra lại. |
| 2.1 Lazy Exponential Time Decay | ✅ Đúng | `scoring.decay_score()`, cùng λ = 0.00385. |
| 2.2 Confidence-Driven Scoring & Competitor Malus | ✅ Đúng | `apply_high_confidence_update` / `apply_low_confidence_update`, cùng công thức malus `k * C * score`. |
| 2.3 Automated Crossover Resolution (đổi chủ xe trên biển số) | ⚠️ Chỉ ngầm định, chưa tường minh | RFC-01 mô tả 1 **Primary Flag (P)** được lật 0↔1 khi crossover xảy ra, và đây là 1 trong 3 "Trigger" để ghi SQL Server. Code hiện tại **không lưu cờ `IsPrimary`** nào cả — ai đang thắng chỉ được xác định *tạm thời tại thời điểm truy vấn* qua `decide_tier()` (ai điểm cao nhất lúc đó). Hệ quả: crossover "tự xảy ra" nhờ cơ chế decay+malus (xe mới liên tục được cộng, xe cũ liên tục bị decay/trừ), nhưng không có sự kiện rõ ràng nào được *phát hiện và ghi lại* khi điều đó xảy ra. |
| 2.4 SQL Server Write-Gating (Throttling Policy) | ❌ Chưa có | RFC-01 yêu cầu chỉ ghi xuống SQL Server khi: (1) Crossover (P đổi), (2) Candidate mới qua Th1, hoặc (3) Score trôi ≥ 0.5 kèm cooldown 24h. **Repo này không có lớp SQL Server / nguồn chân lý lâu dài nào cả** — chỉ có Redis (cache, có TTL) và file VBIo parquet (dữ liệu seed tĩnh, dựng 1 lần từ DMV export, không được engine viết ngược lại). Nghĩa là mọi điểm số/trạng thái HMM chỉ sống trong Redis; nếu Redis mất dữ liệu (hết TTL, restart không persist...) thì toàn bộ lịch sử học được sẽ mất, không có nơi nào lưu bền như SQL Server được RFC-01 đề xuất. |
| 3. Bayesian HMM Look-Up & Enrichment (4-tier) | ✅ Đúng | `posterior.py` + `tiering.py` triển khai khá sát: OBSERVED/HIGH/MEDIUM/LOW/NONE tương ứng Tier HIGH/MEDIUM/LOW/NONE + nhánh "direct" của RFC. |
| 4. Multi-Candidate Data Persistence (SQL Server 1-to-N) | ❌ Chưa có | VBIo parquet đúng là "1-to-N" về mặt *đọc* (1 Plate có thể map nhiều Make/Model), nhưng đây là dữ liệu **seed tĩnh**, không phải nơi engine ghi cập nhật liên tục theo thời gian thực như RFC-01 mô tả cho SQL Server. Không có cơ chế đồng bộ Redis → kho 1-to-N lâu dài. |
| Redis data layout (packed hex string MMMM:OOOO:CC:SSSS...) | ⚠️ Khác thiết kế | RFC-01 đề xuất 1 chuỗi Redis String đóng gói nhị phân/hex để tối ưu bộ nhớ (~28 bytes/candidate, pipe-delimited cho multi-candidate), kèm 1 Redis List riêng `det:` lưu 3 detection gần nhất/vehicle. Code hiện tại dùng **Redis Hash** với field dạng text dễ đọc (`make`, `model`, `score`...), không dùng List `det:`, không đóng gói nhị phân. Về mặt chức năng tương đương, nhưng tốn bộ nhớ hơn — đổi lại dễ debug/đọc hơn nhiều. Đây là lựa chọn hợp lý cho 1 PoC. |

## 3. Kết luận

Phần "lõi toán học" của Phase 2 — decay mũ, cộng điểm/malus theo confidence, Bayes/HMM posterior,
4-tier rescue gate — **đã được port đúng và khá đầy đủ** vào `vbio/enrichment/` (bản thân code còn
ghi rõ trong docstring là "ported 1:1 from simulation_hmm.html"), và README cũng tự nhận đây là
**PoC** (proof-of-concept) chỉ tập trung vào lớp enrichment logic.

Phần **còn thiếu để gọi là "Phase 2 đầy đủ" theo đúng RFC-01** là toàn bộ lớp *persistence lâu dài*:
- Không có SQL Server (hay tương đương) làm nguồn chân lý multi-candidate 1-to-N.
- Không có write-gating/throttling (3 trigger: crossover / candidate mới / drift+cooldown).
- Không có cờ `IsPrimary` tường minh để đánh dấu & log sự kiện crossover.

Nói cách khác: repo này đang dừng ở **tầng Redis + scoring engine** (tức là "bộ não" tính điểm theo
thời gian thực), còn **tầng ghi xuống kho dữ liệu bền vững theo chính sách throttle của RFC-01**
(phần khiến Phase 2 giải quyết được bài toán "database write saturation" ban đầu) thì **chưa được
xây dựng** trong repo này.
