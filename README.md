# GoldCOT Signal

ระบบอ่านสัญญาณเคลื่อนไหวราคาทองคำจากข้อมูล COT ของ CFTC พร้อมพยากรณ์ราคาด้วย ARIMAX
และสรุปทิศทางจาก Gold Option

**Tech stack:** Python (FastAPI) · MySQL 8 · HTML5 + JavaScript + TailwindCSS

---

## โครงสร้างโปรเจกต์

```
db/
  schema.sql               สคีมา MySQL ครบทุกตาราง (รันไฟล์นี้ไฟล์เดียวพอสำหรับ clone ใหม่)
  add_option_tables.sql    migration สำหรับฐานข้อมูลเก่าที่สร้างก่อนมีตาราง Gold Option
                            (ไม่จำเป็นถ้า clone ใหม่ — schema.sql มีให้ครบแล้ว)

backend/
  requirements.txt
  run_ingest.py             ดึงข้อมูล COT + ราคา + คำนวณสัญญาณ ครบทั้งเส้น
  import_option_analysis.py นำเข้ารายงาน Gold Options Analysis Template (.xlsx)
  import_options.py         นำเข้า option OI แบบ CSV ทั่วไป (ทางเลือกสำรอง)
  backtest.py                ทดสอบย้อนหลังว่ากฎสัญญาณ S1–S3 มี edge จริงไหม
  app/
    main.py                 FastAPI — REST /api/v1
    db.py                   การเชื่อมต่อฐานข้อมูล (อ่านจาก DATABASE_URL)
    etl.py                  ดึง COT/ราคาจาก CFTC และ Yahoo Finance
    forecast.py              พยากรณ์ราคาด้วย ARIMAX (SARIMAX + exogenous = สถานะ COT)
    signal_engine.py         กฎ S1–S4 และการให้คะแนน (หัวใจของระบบ)
    scheduler.py              งานอัตโนมัติรายสัปดาห์ (เสาร์ 05:00 ICT)

frontend/
  index.html                Dashboard หน้าเดียว เปิดดูได้ทันทีแม้ไม่มี backend

docs/
  01-system-design.md       เอกสารออกแบบระบบฉบับเต็ม (หลักการ กฎสัญญาณ สถาปัตยกรรม)
  02-backtest.md            ผลการทดสอบย้อนหลังและวิธีอ่านค่า
```

---

## เริ่มใช้งาน

### 1. ฐานข้อมูล

ต้องมี MySQL Server รันอยู่ก่อน (เช็คด้วย `sc query MySQL80` บน Windows หรือ
`systemctl status mysql` บน Linux)

```bash
mysql -u root -p < db/schema.sql
```

ไฟล์นี้ไฟล์เดียวพอ ไม่ต้องรัน `add_option_tables.sql` เพิ่ม (มีไว้สำหรับฐานข้อมูลเก่าเท่านั้น)

### 2. Backend

```bash
cd backend
pip install -r requirements.txt

# ตั้งค่าการเชื่อมต่อฐานข้อมูล — ทำทุกครั้งที่เปิดหน้าต่าง terminal ใหม่
# (หรือใช้ setx บน Windows / เพิ่มใน ~/.bashrc เพื่อตั้งค่าถาวร)
export DATABASE_URL="mysql+pymysql://root:รหัสผ่านจริงของคุณ@localhost:3306/goldcot?charset=utf8mb4"

uvicorn app.main:app --reload
```

บน Windows ใช้ `set` แทน `export`:
```cmd
set DATABASE_URL=mysql+pymysql://root:รหัสผ่านจริงของคุณ@localhost:3306/goldcot?charset=utf8mb4
```

### 3. ดึงข้อมูลจริงเข้าระบบ

รันในหน้าต่างเดียวกับที่ตั้ง `DATABASE_URL` ไว้:

```bash
python run_ingest.py
```

ทำ 5 ขั้นตอนให้ครบ: ดึง COT จาก CFTC → ดึงราคาจาก Yahoo Finance → จับคู่ราคากับสัปดาห์ COT
→ คำนวณ net/Δ/z-score → ประเมินสัญญาณ S1–S4

ถ้าราคาคลาดเคลื่อนจากที่ควรเป็น (เช่นข้อมูลเก่าจากก่อนแก้บั๊ก timezone) ล้างแล้วดึงใหม่ทั้งหมด:
```bash
python run_ingest.py --resync
```

### 4. (ทางเลือก) นำเข้าข้อมูล Gold Option

Barchart ไม่มี API สาธารณะฟรี จึงต้องนำเข้าด้วยมือจากรายงานที่ export มา:

```bash
python import_option_analysis.py path/to/Gold_Options_Analysis_Template.xlsx
```

### 5. Frontend

เปิด `frontend/index.html` ในเบราว์เซอร์ได้โดยตรง ไม่ต้อง build อะไร

- ถ้าเชื่อมต่อ backend ที่ `http://127.0.0.1:8000` ได้ จะแสดงข้อมูลจริงทั้งหมด
- ถ้าเชื่อมต่อไม่ได้ จะ fallback ไปแสดงข้อมูล COT จริงที่ฝังไว้ในไฟล์ (ไม่มีราคา/สัญญาณ)
  เพื่อให้ยังเห็นโครงสร้างหน้าจอได้แม้ backend ยังไม่พร้อม

---

## REST API (`/api/v1`)

| Endpoint | คืนอะไร |
|---|---|
| `GET /health` | สถานะระบบ + วันที่ข้อมูลล่าสุด |
| `GET /cot/latest` | ข้อมูล COT ดิบสัปดาห์ล่าสุด |
| `GET /cot/series?weeks=` | Long/Short ครบ 4 กลุ่ม + Δ รายสัปดาห์ |
| `GET /timeline?weeks=` | ราคา + COT รวมบนเส้นเวลาเดียว |
| `GET /signals?limit=` | ประวัติสัญญาณ S1–S4 |
| `GET /signals/latest` | สัญญาณล่าสุด |
| `GET /prices/daily?years=` | ราคาทองรายวันจาก Yahoo Finance |
| `GET /forecast/price?days=` | พยากรณ์ราคาด้วย ARIMAX (คำนวณสดทุกครั้ง) |
| `GET /options/analysis` | สรุป Gold Option จาก Analysis Template |
| `GET /options/summary` / `/options/strike-map` | สรุป Option OI แบบ CSV (ทางเลือกสำรอง) |
| `GET /rules` | threshold ของกฎสัญญาณปัจจุบัน |
| `POST /admin/ingest` | สั่ง ingest ทันทีผ่าน API (ต้องมี header `x-api-key`) |
| `POST /admin/resync-price` | ล้างราคาเก่าแล้วดึงใหม่ผ่าน API |

Endpoint ที่ขึ้นต้นด้วย `/admin` ต้องตั้ง `ADMIN_API_KEY` ไว้ก่อน:
```bash
export ADMIN_API_KEY="ตั้งคีย์ของคุณเอง"
```

---

## ทดสอบโดยไม่ต้องมีฐานข้อมูล

```bash
# ทดสอบกฎสัญญาณด้วยสัปดาห์สังเคราะห์
python backend/app/signal_engine.py

# ทดสอบย้อนหลังว่ากฎสัญญาณมี edge จริงไหม (อ่านผลที่ docs/02-backtest.md)
python backend/backtest.py --synthetic
python backend/backtest.py --csv data/gold_cot_weekly.csv
```

---

## ข้อจำกัดที่ควรรู้ก่อนใช้งานจริง

1. **สัญญาณไม่ใช่คำแนะนำการลงทุน** — COT เป็นข้อมูลล่าช้า 3 วันทำการ สัญญาณสะท้อนบริบท
   เชิงโครงสร้างของตลาด ไม่ใช่จุดเข้าออกคำสั่งซื้อขาย
2. **การพยากรณ์ราคา (ARIMAX)** ตรึงค่า COT ไว้เท่าค่าล่าสุดตลอดช่วงพยากรณ์ เพราะไม่มีทาง
   ทราบค่า COT ในอนาคตจริง ความแม่นยำจึงลดลงเมื่อพยากรณ์ไกลขึ้น
3. **Threshold ของกฎสัญญาณ** (`signal_rule` table) เป็นค่าเริ่มต้นที่ยังไม่ผ่านการ optimize
   จากข้อมูลจริง ควรรัน `backtest.py` แล้วปรับก่อนใช้ตัดสินใจใดๆ
4. **ราคาอ้างอิงจาก Yahoo Finance (GC=F)** เป็นสัญญา futures ต่อเนื่อง ไม่ใช่ spot price —
   อาจมี gap เวลาเปลี่ยนสัญญา (contract roll)
