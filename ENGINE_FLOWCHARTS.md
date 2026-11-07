# Flowchart tổng hợp — Enrichment Engine (Phase 2)

> Tài liệu này chỉ tập trung vào 4 flowchart, ở các mức zoom khác nhau, để dễ trình bày/report.
> Chi tiết đầy đủ (công thức, tham số, lịch sử các fix, case study thật) nằm ở
> `ENGINE_LOGIC_FLOW.md` — tài liệu này chỉ tóm lại dưới dạng hình vẽ + giải thích ngắn.

---

## 1. Flow tổng thể (high-level)

Góc nhìn toàn cảnh: từ 1 frame ảnh MCAP đến kết quả enrich cuối cùng, không đi vào chi
tiết công thức.

```mermaid
flowchart LR
    A["Ảnh / Detection event"] --> B["MCAP\nnhận diện Make/Model/Plate/State/Confidence"]
    B --> C{"Đọc được\nPlate + State?"}
    C -- Không --> Z["Không enrich được"]
    C -- Có --> D["Redis lookup\n(candidate records của plate này)"]
    D --> E{"Redis có candidate\ncho plate này chưa?"}
    E -- Chưa (cold start) --> F["Query VBIo (parquet/DB)\nseed candidate ban đầu"]
    E -- Đã có --> G["Canonicalize make/model\n(make_model_mapping.json)"]
    F --> G
    G --> H["Enrichment Engine\n(decay + Bayes scoring + tiering)\n-- xem flow #2, #3"]
    H --> I["Enriched Result\nTier + Make + Model + Confidence"]
    H --> J[("Ghi lại Redis:\ncandidate records + TTL/persist\n-- xem flow #4")]
```

**Đọc nhanh:**
- Mỗi "tick" = 1 detection cho 1 (plate, state).
- "Redis lookup" không đơn thuần là đọc — nếu Redis trống hẳn (plate chưa từng thấy), engine seed thẳng từ VBIo (DMV source of truth).
- Canonicalize chạy **trước** khi vào engine tính điểm, để VBIo và MCAP không bị lệch chính tả (vd "Hyundai" vs "Huyndai") tạo ra 2 candidate khác nhau cho cùng 1 xe.
- Việc ghi lại Redis ở cuối **không chỉ là lưu record** — còn kèm set/refresh TTL (xem flow #4).

---

## 2. Flow logic Enrichment (`EnrichmentEngine.process_detection()`)

Zoom vào đúng 1 lần gọi `process_detection()` — thứ tự các bước quyết định candidate nào
được admit, candidate nào được update, và admit bằng con đường nào (MCAP tự đạt ngưỡng,
hay VBIo xác nhận thẳng).

```mermaid
flowchart TD
    S0["Nhận Detection(make, model, confidence, timestamp)"] --> S1["Canonicalize make/model"]
    S1 --> S2{"Redis có candidate\ncho Plate+State?"}
    S2 -- Chưa có --> S3["Cold start: seed từ VBIo\nscore = seed_base(0.7) * HmmPosterior\nsource = VBIO"]
    S2 -- Đã có --> S4["Dùng candidate hiện có"]
    S3 --> S5
    S4 --> S5["Build top_key từ make+model\ncủa detection hiện tại"]
    S5 --> SR{"top_key CHƯA tồn tại\ntrong records?"}
    SR -- Có --> SR2{"VBIo có đăng ký đúng\nmake/model này không\n(check lại, không chỉ lúc cold-start)?"}
    SR2 -- Có --> SR3["Admit thẳng từ VBIo\nsource=VBIO -- KHÔNG cần qua Th1\n('late-arriving VBIo rescue')"]
    SR2 -- Không --> S6
    SR -- Không --> S6
    SR3 --> S6{"confidence >= Th1 hiệu lực\nVÀ top_key chưa tồn tại?"}
    S6 -- Đúng --> S7["Admit candidate mới\nsource=OBSERVED, status=PENDING\nscore=0"]
    S6 -- Sai --> S8
    S7 --> S8{"Còn candidate nào\ntrên plate không?"}
    S8 -- Không --> S9["Trả tier = NONE"]
    S8 -- Có --> S10["① Decay từng candidate\n-- xem flow #3"]
    S10 --> S11["② Bayes: Prior/Likelihood/Posterior\n-- xem flow #3"]
    S11 --> S12["③ Áp dụng observation\n(EMA update high/low-confidence)\n-- xem flow #3"]
    S12 --> S13["PENDING -> ACTIVE promotion?\n(observationCount >= n_activate)"]
    S13 --> S14["④ decide_tier()\nOBSERVED / HIGH / MEDIUM / LOW / NONE"]
    S14 --> S15["⑤ Persist: save_candidates\n+ set_ttl / persist (Redis TTL)\n-- xem flow #4"]
    S15 --> S16["Trả EnrichmentResult"]
```

**"Th1 hiệu lực"** không phải hằng số cố định: dùng `th1` (0.90) nếu plate **đã từng** có
candidate `source=VBIO`; dùng `th1_no_vbio` (0.80, thấp hơn) nếu plate **chưa từng** có
VBIo backing — tránh trường hợp 1 xe đúng, đọc lặp lại nhất quán ở confidence trung bình
(0.54-0.80) không bao giờ được tin, trong khi 1 lần đọc sai (platform dễ nhầm) vừa đủ
vượt 0.90 lại được tin ngay.

---

## 3. Flow cơ chế tính điểm (Redis scoring mechanism — Bayes/HMM + EMA)

Zoom vào bên trong bước ①②③ ở flow #2 — đây là "trái tim" toán học: decay, Bayes
posterior, và cách `score` của mỗi candidate được cập nhật.

```mermaid
flowchart TD
    D0["records = tất cả candidate\ncủa plate này"] --> D1{"len(records) < 2?\n(chỉ có 1 candidate, solo)"}
    D1 -- Có --> D2["BỎ QUA decay\n(không ai để 'thua điểm')\nchỉ refresh lastDecayedAt"]
    D1 -- Không --> D3["decay_score() từng candidate:\nscore *= e^(-lambda_base * gapDays)\n(gapDays riêng cho từng candidate)"]
    D2 --> P0
    D3 --> P0

    P0["Tính pi_score (prior theo score hiện tại)\n+ t_same_dynamic (độ tin posterior cũ)"] --> P1["compute_transition_prior()\n-- trộn pi_score + posterior tick trước"]
    P1 --> P2["compute_likelihoods()\ntop_k_confidences (exact match +\nsame-make hint nếu không khớp model)\nmọi candidate khác floor ở epsilon"]
    P2 --> P3["compute_posteriors()\nBayes: posterior ∝ likelihood * trans_prior\n(tie-break theo lastSeen gần nhất)"]

    P3 --> U0{"confidence >= Th1\nhiệu lực?"}
    U0 -- Có (HIGH-confidence) --> U1["winner.score = alpha*confidence\n+ (1-alpha)*winner.score  (EMA)\nwinner.observationCount += 1\nwinner.lastSeen = now\nMỌI candidate khác: score -= malus_rate*confidence*score"]
    U0 -- Không, nhưng top_key đã tồn tại (LOW-confidence) --> U2["winner.score = alpha*confidence\n+ (1-alpha)*winner.score  (EMA)\n-- KHÔNG malus, KHÔNG đổi observationCount/lastSeen"]
    U0 -- Không, top_key chưa tồn tại --> U3["Không update gì cả\n(chỉ posterior ở trên vẫn có giá trị\ncho tier MEDIUM/LOW)"]

    U1 --> T0
    U2 --> T0
    U3 --> T0
    T0["decide_tier():\nOBSERVED (bỏ qua score, tin thẳng MCAP)\n> HIGH (posterior >= Th3)\n> MEDIUM (cùng make, score > 0)\n> LOW (bất kỳ, score > 0)\n> NONE"]
```

**Vì sao EMA, không phải `score += confidence`:** cộng dồn không có chặn trên — 1 plate bị
detect nhiều sẽ có `score` tăng vô hạn, vượt ngân sách field 16-bit (0.00-655.35) RFC-01
cấp cho `CredibilityScore`. EMA (`score = alpha*confidence + (1-alpha)*score`) tự nhiên bị
chặn trong khoảng gần [0,1] vì là trung bình có trọng số của 2 giá trị đều trong [0,1].

---

## 4. Flow Redis ↔ VBIo (Periodical sync / TTL lifecycle)

Zoom vào bước ⑤ (persist) ở flow #2 — cách Redis tự dọn rác và cách 1 candidate "được
công nhận" lâu dài. Lưu ý rõ phần nào **đã code**, phần nào **chưa** (open item).

```mermaid
flowchart TD
    E0["Sau bước ③④ (observation + tier),\nxét từng record trong `records`"] --> E1{"source == VBIO?"}
    E1 -- Có --> E2["store.persist(key)\n-- bỏ TTL hẳn, VĨNH VIỄN trong Redis\n(VBIo đã có backing thật ở vbio.parquet)"]
    E1 -- Không (OBSERVED) --> E3{"status == ACTIVE?\n(đã promote, xem flow #2 bước S13)"}
    E3 -- Có --> E2
    E3 -- Không (PENDING) --> E4{"record.key ==\ntop_key (vừa bị tick\nnày chạm tới)?"}
    E4 -- Có --> E5["store.set_ttl(key, now, gc_days*86400)\n-- Redis tự xóa key nếu KHÔNG ai chạm\nlại nó trong gc_days (180) ngày,\nKỂ CẢ KHI plate không bao giờ\nđược detect lại"]
    E4 -- Không --> E6["Không làm gì --\nTTL cũ (nếu có) tiếp tục đếm ngược"]

    E5 --> F0["[Redis tự xử lý, không cần job]\nKey OBSERVED+PENDING hết TTL\n-> Redis tự xóa (reclaim RAM)"]
    E2 --> F1["Candidate ACTIVE/VBIo nằm lại\nRedis vĩnh viễn (không tự expire)"]

    F1 -. "CHƯA code -- open item\n(xem ENGINE_LOGIC_FLOW.md mục 10)" .-> F2["[Ý tưởng] Job batch riêng,\nđộc lập với process_detection():\nquét source=OBSERVED AND status=ACTIVE,\nghi thành record VBIo thật (parquet/DB)"]
```

**Đã implement (production-ready):**
- `set_ttl`/`persist` ở tầng storage (`vbio/clients/redis_store.py`) — map thẳng ra lệnh
  Redis `EXPIRE`/`PERSIST` thật, không cần job quét nào cả.
- Promotion PENDING→ACTIVE (`cleanup.maybe_promote()`) dựa trên `observationCount >= n_activate`.
- Test trên 97-profile: accuracy 96.74% (so với 96.07% nếu không có TTL/promotion) — xem
  `ENGINE_LOGIC_FLOW.md` mục 9 cho toàn bộ lịch sử (kể cả 1 bản thiết kế cũ — scan thủ công
  mỗi tick — đã bị bỏ vì không dọn được rác của plate "chết hẳn").

**CHƯA implement (open item, nhánh chấm-chấm trong hình):**
- Việc đưa 1 candidate `OBSERVED+ACTIVE` (đã "mồ côi" vĩnh viễn trong Redis) thành 1 record
  VBIo thật — hiện tại nó chỉ nằm lại Redis mãi mãi, không tự động đồng bộ ra `vbio.parquet`/DB.
  Cần 1 job batch độc lập, hoàn toàn tách khỏi hot path `process_detection()`.
