"""
CLI ดึงข้อมูลจริงเข้าระบบทั้งเส้น — COT + ราคา + คำนวณ + ประเมินสัญญาณ

ใช้แทนการยิง curl ไปที่ /api/v1/admin/ingest เพราะบน Windows cmd
การส่ง header (-H "x-api-key: ...") มักพิมพ์ผิดง่าย ตัวนี้รันตรงในเครื่องเดียวกับ
ที่ตั้ง DATABASE_URL ไว้แล้ว จึงไม่ต้องผ่าน HTTP เลย

การใช้งาน
---------
    cd backend
    python run_ingest.py            # ดึงข้อมูลตามรอบปกติ
    python run_ingest.py --resync   # ล้างราคาเก่าทั้งหมดแล้วดึงใหม่ (ใช้ครั้งเดียว
                                     # หลังอัปเดต etl.py ที่แก้บั๊กวันที่เพี้ยนจาก Yahoo)

ต้องตั้ง DATABASE_URL ในหน้าต่างเดียวกันก่อนรัน (เหมือนตอนรัน uvicorn)
ถ้าล้มเหลวขั้นไหน สคริปต์จะหยุดทันทีและพิมพ์ข้อความอธิบายเป็นภาษาไทย
"""
from __future__ import annotations

import logging
import sys
import traceback

from app.etl import ingest_cot, ingest_price, rebuild_derived, resync_price, snap_weekly_price
from app.main import CONTRACT_ID, evaluate_and_store

logging.basicConfig(level=logging.INFO, format="    %(message)s")


def run(resync: bool = False):
    price_step = (
        ("ล้างราคาเก่าแล้วดึงใหม่จาก Yahoo Finance (resync)", lambda: resync_price())
        if resync else
        ("ดึงราคาทองคำรายวันจาก Yahoo Finance", lambda: ingest_price())
    )
    steps = [
        ("ดึงข้อมูล COT จาก CFTC (Disaggregated Combined, GOLD 088691)",
         lambda: ingest_cot(CONTRACT_ID)),
        price_step,
        ("จับคู่ราคาปิดวันอังคารกับสัปดาห์ COT",
         lambda: snap_weekly_price()),
        ("คำนวณ net / Δ / z-score (cot_derived)",
         lambda: rebuild_derived(CONTRACT_ID)),
        ("ประเมินสัญญาณ S1–S4",
         lambda: evaluate_and_store()),
    ]

    print("=" * 60)
    print(" GoldCOT Signal — ดึงข้อมูลจริงเข้าระบบ" + (" (โหมด resync)" if resync else ""))
    print("=" * 60)

    for i, (label, fn) in enumerate(steps, 1):
        print(f"\n[{i}/{len(steps)}] {label}")
        try:
            n = fn()
            print(f"    ✓ สำเร็จ — {n} แถว")
        except Exception as e:                          # noqa: BLE001
            print(f"    ✗ ล้มเหลว: {e}")
            print("\n--- รายละเอียดข้อผิดพลาด (ส่งข้อความนี้กลับมาถ้าต้องการความช่วยเหลือ) ---")
            traceback.print_exc()
            print("-" * 60)
            print(f"\nหยุดที่ขั้นตอน [{i}/{len(steps)}] — แก้ปัญหานี้ก่อนแล้วรันใหม่")
            sys.exit(1)

    print("\n" + "=" * 60)
    print(" เสร็จสมบูรณ์ทุกขั้นตอน")
    print(" ทดสอบได้ที่ http://127.0.0.1:8000/api/v1/signals/latest")
    print("=" * 60)


if __name__ == "__main__":
    run(resync="--resync" in sys.argv)
