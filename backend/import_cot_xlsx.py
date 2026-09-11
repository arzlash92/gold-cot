"""
CLI นำเข้าข้อมูล COT (Commitment of Traders) จากไฟล์ Excel ที่ดาวน์โหลดด้วยมือจาก CFTC.gov

ใช้เป็นทางเลือกสำรองเมื่อ CFTC Socrata API (ingest_cot ใน etl.py) ล่ม โดน
rate-limit หรือเปลี่ยนชื่อฟิลด์กะทันหัน รวมถึงใช้ backfill ข้อมูลย้อนหลังยาว ๆ
ได้ในครั้งเดียว เพราะไฟล์ Excel ที่ CFTC เผยแพร่มักมีข้อมูลหลายปีในชีตเดียว

รองรับไฟล์รูปแบบ "Disaggregated Futures-Only" ที่ดาวน์โหลดจากหน้า
https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalCompressed/index.htm
(หรือไฟล์ที่ export มาจากระบบอื่นที่ใช้ชื่อคอลัมน์เดียวกัน เช่น
'Open_Interest_All', 'M_Money_Positions_Long_ALL' ฯลฯ)

การใช้งาน
---------
    cd backend
    python import_cot_xlsx.py path\\to\\gold.xlsx
    python import_cot_xlsx.py path\\to\\gold.xlsx --market "GOLD" --code 088691

Idempotent — รันซ้ำไฟล์เดิมหรือไฟล์ที่มีข้อมูลทับซ้อนกันได้ผลเท่าเดิม
(ใช้ INSERT ... ON DUPLICATE KEY UPDATE เหมือนการดึงจาก API ปกติ)

หลังนำเข้าแล้ว อย่าลืมรัน rebuild_derived() ต่อ (หรือรัน run_ingest.py ทั้งเส้น)
เพื่อคำนวณ net / Δ / z-score และประเมินสัญญาณ S1–S4 ใหม่จากข้อมูลที่เพิ่งเพิ่ม
"""
from __future__ import annotations

import argparse
import sys
import traceback

from app.etl import GOLD_CFTC_CODE, ingest_cot_from_xlsx, rebuild_derived
from app.main import CONTRACT_ID, evaluate_and_store


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xlsx_path", help="พาธของไฟล์ Excel ข้อมูล COT จาก CFTC")
    ap.add_argument("--market", default="GOLD",
                     help="คำที่ใช้ filter ชื่อตลาด ถ้าหาคอลัมน์รหัสสัญญาไม่เจอ (ค่าเริ่มต้น: GOLD)")
    ap.add_argument("--code", default=GOLD_CFTC_CODE,
                     help=f"CFTC_Contract_Market_Code ที่ต้องการกรอง (ค่าเริ่มต้น: {GOLD_CFTC_CODE})")
    ap.add_argument("--sheet", default=0,
                     help="ชื่อหรือลำดับชีตในไฟล์ (ค่าเริ่มต้น: ชีตแรก)")
    ap.add_argument("--no-recalc", action="store_true",
                     help="ข้ามขั้นตอนคำนวณ derived + ประเมินสัญญาณใหม่หลังนำเข้า")
    a = ap.parse_args()

    sheet = a.sheet
    try:
        sheet = int(sheet)
    except (TypeError, ValueError):
        pass  # ชื่อชีตเป็น string ปกติ ปล่อยผ่าน

    print(f"กำลังนำเข้า {a.xlsx_path} (market={a.market}, code={a.code}) ...")
    try:
        n = ingest_cot_from_xlsx(a.xlsx_path, contract_id=CONTRACT_ID,
                                  market_filter=a.market, cftc_code=str(a.code), sheet_name=sheet)
        print(f"✓ สำเร็จ — บันทึก {n} สัปดาห์")

        if n and not a.no_recalc:
            print("กำลังคำนวณ net / Δ / z-score ใหม่ (rebuild_derived) ...")
            d = rebuild_derived(CONTRACT_ID)
            print(f"✓ คำนวณใหม่ {d} สัปดาห์")

            print("กำลังประเมินสัญญาณ S1–S4 ใหม่ (evaluate_and_store) ...")
            s = evaluate_and_store()
            print(f"✓ ประเมินสัญญาณใหม่ {s} สัปดาห์")

        print("ทดสอบได้ที่ http://127.0.0.1:8000/api/v1/signals/latest")
    except Exception as e:                       # noqa: BLE001
        print(f"✗ ล้มเหลว: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
