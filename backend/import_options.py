"""
CLI นำเข้าข้อมูล Gold Option OI จากไฟล์ CSV ที่ export มาด้วยมือ

Barchart ไม่มี API สาธารณะฟรี (แพ็กเกจเริ่มต้น 500 ดอลลาร์/เดือน) และการดึงข้อมูล
จากหน้าเว็บโดยตรงมีความเสี่ยงโดนบล็อกเช่นเดียวกับที่เคยเจอกับ Stooq มาก่อน
วิธีนี้จึงให้ผู้ใช้คัดลอก/ส่งออกตาราง option chain จากหน้า Barchart มาเป็น CSV เอง
แล้วนำเข้าด้วยสคริปต์นี้ — ทำได้ไม่บ่อย (option chain ไม่ได้เปลี่ยนทุกวันเท่าราคา)

วิธีเตรียมไฟล์ CSV
------------------
เปิด https://www.barchart.com/futures/quotes/GC*0/options?futuresOptionsView=merged
คัดลอกตาราง (หรือใช้ปุ่ม Download ถ้ามีสิทธิ์) แล้วบันทึกเป็น CSV ที่มีคอลัมน์
อย่างน้อย: strike, type (Call/Put หรือ C/P), open interest, expiry (วันหมดอายุ)
ชื่อคอลัมน์ตัวพิมพ์เล็ก-ใหญ่หรือมีช่องว่างไม่เป็นไร สคริปต์จะจับคู่ให้อัตโนมัติ

การใช้งาน
---------
    cd backend
    python import_options.py path\\to\\gold_options.csv
    python import_options.py path\\to\\gold_options.csv --date 2026-08-29
"""
from __future__ import annotations

import argparse
import sys
import traceback

from app.etl import ingest_option_oi_from_csv


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path", help="พาธของไฟล์ CSV ที่ export มาจาก Barchart")
    ap.add_argument("--date", default=None,
                     help="วันที่ของข้อมูล (YYYY-MM-DD) ถ้าไม่ระบุจะใช้วันนี้")
    a = ap.parse_args()

    print(f"กำลังนำเข้า {a.csv_path} ...")
    try:
        n = ingest_option_oi_from_csv(a.csv_path, trade_date=a.date)
        print(f"✓ สำเร็จ — บันทึก {n} แถว")
        print("ทดสอบได้ที่ http://127.0.0.1:8000/api/v1/options/summary")
    except Exception as e:                       # noqa: BLE001
        print(f"✗ ล้มเหลว: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
