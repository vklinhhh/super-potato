# Flowchart high-level — dùng cho slide report

> Bản rút gọn của `ENGINE_FLOWCHARTS.md` (giữ nguyên để trace/debug) — chỉ còn khối
> concept chính, không có tên field/function/tham số, phù hợp đưa vào slide.

---

## 1. Flow tổng thể

```mermaid
flowchart LR
    A["Ảnh xe"] --> B["Nhận diện\n(Make/Model/Plate)"]
    B --> C["Tra cứu cache\n(Redis)"]
    C --> D["Enrichment Engine\n(chấm điểm & quyết định)"]
    D --> E["Kết quả\nMake/Model đã enrich"]
    D --> F[("Cập nhật lại cache")]
```

---

## 2. Flow logic Enrichment

```mermaid
flowchart LR
    A["Nhận 1 lần detect mới"] --> B["Lấy danh sách ứng viên\ncủa biển số (cache hoặc VBIo)"]
    B --> C["So điểm các ứng viên\n(bằng chứng cũ + bằng chứng mới)"]
    C --> D["Chọn ứng viên đáng tin nhất"]
    D --> E["Phân loại độ tin cậy\n(chắc chắn / khá chắc / không rõ)"]
    E --> F["Trả kết quả"]
```

---

## 3. Flow cơ chế tính điểm (Redis scoring)

```mermaid
flowchart LR
    A["Bằng chứng cũ\n(điểm tích lũy)"] --> B["Giảm dần theo thời gian\n(càng lâu không thấy, càng mất tin)"]
    B --> C["Kết hợp với\nbằng chứng mới (Bayes)"]
    C --> D["Cập nhật điểm\nứng viên khớp"]
    D --> E["Trừ điểm nhẹ\ncác ứng viên còn lại"]
```

---

## 4. Flow đồng bộ định kỳ Redis ↔ VBIo

```mermaid
flowchart LR
    A["Ứng viên mới\n(chỉ ghi nhận qua camera)"] --> B{"Được xác nhận\nnhiều lần?"}
    B -- Có --> C["Trở thành chính thức\n(lưu vĩnh viễn)"]
    B -- Không, lâu không thấy lại --> D["Tự động dọn khỏi cache"]
    C --> E["[Định kỳ, tương lai]\nĐồng bộ vào VBIo"]
```
