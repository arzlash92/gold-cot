"""
CLI นำเข้าข้อมูลจาก "Gold Options Analysis Template" (.xlsx)

ไฟล์นี้เป็นรายงานวิเคราะห์ Gold Option ที่จัดทำไว้แล้ว (มีชีต Executive Summary,
Series Breakdown, Strike Deep-Dive) — Barchart ไม่มี API สาธารณะฟรี จึงนำเข้า
ด้วยมือทุกครั้งที่มีรายงานฉบับใหม่ ไม่ใช่ข้อมูลที่ดึงอัตโนมัติรายวัน

การใช้งาน
---------
    cd backend
    python import_option_analysis.py path\\to\\Gold_Options_Analysis_Template.xlsx
    python import_option_analysis.py path\\to\\file.xlsx --date 2026-08-29
"""
from __future__ import annotations

import argparse
import sys
import traceback

from app.etl import ingest_option_analysis_from_xlsx


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("xlsx_path", help="พาธของไฟล์ Gold Options Analysis Template (.xlsx)")
    ap.add_argument("--date", default=None,
                     help="วันที่ของรายงาน (YYYY-MM-DD) ถ้าไม่ระบุจะใช้วันนี้")
    a = ap.parse_args()

    print(f"กำลังนำเข้า {a.xlsx_path} ...")
    try:
        res = ingest_option_analysis_from_xlsx(a.xlsx_path, trade_date=a.date)
        print(f"✓ สำเร็จ — ภาพรวมตลาด {res['overview']} แถว, "
              f"รายซีรีส์ {res['series']} ซีรีส์, รายละเอียด strike {res['strikes']} แถว")
        print("ทดสอบได้ที่ http://127.0.0.1:8000/api/v1/options/analysis")
    except Exception as e:                       # noqa: BLE001
        print(f"✗ ล้มเหลว: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
