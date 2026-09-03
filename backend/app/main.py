"""FastAPI layer — REST /api/v1"""
from __future__ import annotations

import json
import os
from datetime import date

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .db import engine
from .etl import ingest_cot, ingest_price, rebuild_derived, resync_price, snap_weekly_price
from .signal_engine import RuleParams, WeekInput, evaluate_series

app = FastAPI(title="GoldCOT Signal API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"], allow_headers=["*"],
)

CONTRACT_ID = 1
ADMIN_KEY = os.getenv("ADMIN_API_KEY", "")


def _rows(sql: str, **p) -> list[dict]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), p)]


# ---------------------------------------------------------------------
@app.get("/api/v1/health")
def health():
    r = _rows("SELECT MAX(report_date) d FROM cot_raw WHERE contract_id=:c",
              c=CONTRACT_ID)
    return {"status": "ok", "latest_report_date": r[0]["d"] if r else None}


@app.get("/api/v1/cot/latest")
def cot_latest():
    r = _rows("""
        SELECT * FROM cot_raw WHERE contract_id=:c
        ORDER BY report_date DESC LIMIT 1""", c=CONTRACT_ID)
    if not r:
        raise HTTPException(404, "ยังไม่มีข้อมูล COT — สั่ง ingest ก่อน")
    return r[0]


@app.get("/api/v1/timeline")
def timeline(weeks: int = Query(104, ge=8, le=520)):
    """
    ชุดข้อมูลรวม 5 มิติบนเส้นเวลาเดียว ตาม Unified Timeline Blueprint
    frontend เรียกครั้งเดียวได้ครบ ไม่ต้อง join เองฝั่ง client
    """
    rows = _rows("""
        SELECT * FROM v_timeline WHERE contract_id=:c
        ORDER BY report_date DESC LIMIT :n""", c=CONTRACT_ID, n=weeks)
    rows.reverse()
    return {"contract_id": CONTRACT_ID, "weeks": len(rows), "series": rows}


@app.get("/api/v1/signals")
def signals(date_from: date | None = None, date_to: date | None = None,
            limit: int = Query(52, ge=1, le=520)):
    return _rows("""
        SELECT report_date, signal_code, direction, bias_score, confidence,
               price_ref, price_zone, price_state, rationale
        FROM `signal`
        WHERE contract_id=:c AND is_primary=1
          AND (:f IS NULL OR report_date >= :f)
          AND (:t IS NULL OR report_date <= :t)
        ORDER BY report_date DESC LIMIT :n
    """, c=CONTRACT_ID, f=date_from, t=date_to, n=limit)


@app.get("/api/v1/signals/latest")
def signal_latest():
    r = signals(limit=1)
    if not r:
        raise HTTPException(404, "ยังไม่มีสัญญาณ")
    return r[0]


@app.get("/api/v1/options/summary")
def options_summary(top: int = Query(10, ge=3, le=50)):
    """
    สรุปทิศทางจาก Gold Option — Put/Call OI ratio และ strike ที่ OI หนาแน่นที่สุด
    ต้องนำเข้าข้อมูลด้วย etl.ingest_option_oi_from_csv() ก่อน (ไม่มีแหล่งอัตโนมัติ
    เพราะ Barchart ไม่มี API ฟรี) คืนค่า has_data=false ถ้ายังไม่เคยนำเข้า
    """
    latest = _rows("""
        SELECT trade_date, MAX(expiry_date) AS expiry_max
        FROM option_oi WHERE contract_id=:c
        GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1
    """, c=CONTRACT_ID)
    if not latest:
        return {"has_data": False}

    trade_date = latest[0]["trade_date"]
    totals = _rows("""
        SELECT
          SUM(CASE WHEN option_type='C' THEN open_interest ELSE 0 END) AS call_oi,
          SUM(CASE WHEN option_type='P' THEN open_interest ELSE 0 END) AS put_oi
        FROM option_oi WHERE contract_id=:c AND trade_date=:d
    """, c=CONTRACT_ID, d=trade_date)[0]

    strikes = _rows("""
        SELECT strike,
               SUM(CASE WHEN option_type='C' THEN open_interest ELSE 0 END) call_oi,
               SUM(CASE WHEN option_type='P' THEN open_interest ELSE 0 END) put_oi
        FROM option_oi WHERE contract_id=:c AND trade_date=:d
        GROUP BY strike ORDER BY (call_oi + put_oi) DESC LIMIT :n
    """, c=CONTRACT_ID, d=trade_date, n=top)

    call_oi = int(totals["call_oi"] or 0)
    put_oi = int(totals["put_oi"] or 0)
    return {
        "has_data": True,
        "trade_date": trade_date,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "put_call_ratio": round(put_oi / call_oi, 3) if call_oi else None,
        "strikes": strikes,
    }


@app.get("/api/v1/options/analysis")
def options_analysis():
    """
    ข้อมูลจาก Gold Options Analysis Template — ภาพรวมตลาด, สรุปรายซีรีส์
    พร้อมบทวิเคราะห์ภาษาไทย, และรายละเอียด strike สำคัญพร้อมหมายเหตุกลยุทธ์
    ต้องนำเข้าด้วย python import_option_analysis.py <ไฟล์.xlsx> ก่อน
    คืนค่า has_data=false ถ้ายังไม่เคยนำเข้า
    """
    overview = _rows("""
        SELECT trade_date, call_oi_total, put_oi_total, oi_pc_ratio, macro_sentiment
        FROM option_market_overview WHERE contract_id=:c
        ORDER BY trade_date DESC LIMIT 1
    """, c=CONTRACT_ID)
    if not overview:
        return {"has_data": False}

    trade_date = overview[0]["trade_date"]
    series = _rows("""
        SELECT series_code, futures_ref, put_oi, call_oi, total_oi, oi_pc_ratio,
               put_volume, call_volume, total_volume, vol_pc_ratio,
               market_sentiment, interpretation_th
        FROM option_series_summary
        WHERE contract_id=:c AND trade_date=:d
        ORDER BY oi_pc_ratio ASC
    """, c=CONTRACT_ID, d=trade_date)

    strikes = _rows("""
        SELECT series_code, strike, futures_ref, moneyness, put_oi, call_oi,
               put_volume, call_volume, dominant_side, note_th
        FROM option_strike_detail
        WHERE contract_id=:c AND trade_date=:d
        ORDER BY series_code, strike
    """, c=CONTRACT_ID, d=trade_date)

    return {"has_data": True, "trade_date": trade_date,
            "overview": overview[0], "series": series, "strikes": strikes}


@app.get("/api/v1/options/strike-map")
def strike_map(expiry: date | None = None, top: int = Query(30, ge=5, le=200)):
    """OI แยกตาม strike — ใช้สแกนแนวรับ/แนวต้านที่ซ่อนอยู่ในโครงสร้างออปชัน"""
    return _rows("""
        SELECT strike,
               SUM(CASE WHEN option_type='C' THEN open_interest ELSE 0 END) call_oi,
               SUM(CASE WHEN option_type='P' THEN open_interest ELSE 0 END) put_oi
        FROM option_oi
        WHERE contract_id=:c
          AND trade_date=(SELECT MAX(trade_date) FROM option_oi WHERE contract_id=:c)
          AND (:e IS NULL OR expiry_date=:e)
        GROUP BY strike
        ORDER BY (call_oi + put_oi) DESC
        LIMIT :n
    """, c=CONTRACT_ID, e=expiry, n=top)


@app.get("/api/v1/forecast/price")
def forecast_price(days: int = Query(10, ge=1, le=30)):
    """
    พยากรณ์ราคาทองคำล่วงหน้ารายวันด้วย ARIMAX (SARIMAX + exogenous = สถานะ COT)
    คำนวณสดทุกครั้งที่เรียก ต้องมีข้อมูลราคารายวันและ cot_derived พอสมควรก่อน
    (รัน run_ingest.py ให้ครบ) คืนค่า has_data=false พร้อมเหตุผลถ้าข้อมูลไม่พอ
    """
    from .forecast import ForecastUnavailable, fit_and_forecast
    try:
        return fit_and_forecast(contract_id=CONTRACT_ID, horizon_days=days)
    except ForecastUnavailable as e:
        return {"has_data": False, "reason": str(e)}


@app.get("/api/v1/prices/daily")
def prices_daily(years: int = Query(10, ge=1, le=30), symbol: str = "XAUUSD"):
    """ราคาทองรายวันย้อนหลัง — ใช้วาด line chart ระยะยาวบน dashboard"""
    rows = _rows("""
        SELECT trade_date, close_px FROM price_daily
        WHERE symbol=:s AND trade_date >= DATE_SUB(CURDATE(), INTERVAL :y YEAR)
        ORDER BY trade_date
    """, s=symbol, y=years)
    return {"symbol": symbol, "years": years, "count": len(rows), "series": rows}


@app.get("/api/v1/cot/series")
def cot_series(weeks: int = Query(520, ge=8, le=1040)):
    """
    สถานะ Long/Short ของผู้ซื้อขายทั้ง 4 กลุ่ม + Open Interest รายสัปดาห์
    พร้อมการเปลี่ยนแปลงเทียบสัปดาห์ก่อนหน้า (คำนวณด้วย LAG window function)
    """
    rows = _rows("""
        SELECT * FROM (
          SELECT report_date, open_interest,
                 prod_long, prod_short, swap_long, swap_short,
                 mm_long, mm_short, other_long, other_short,
                 open_interest - LAG(open_interest) OVER w AS d_oi,
                 prod_long   - LAG(prod_long)   OVER w AS d_prod_long,
                 prod_short  - LAG(prod_short)  OVER w AS d_prod_short,
                 swap_long   - LAG(swap_long)   OVER w AS d_swap_long,
                 swap_short  - LAG(swap_short)  OVER w AS d_swap_short,
                 mm_long     - LAG(mm_long)     OVER w AS d_mm_long,
                 mm_short    - LAG(mm_short)    OVER w AS d_mm_short,
                 other_long  - LAG(other_long)  OVER w AS d_other_long,
                 other_short - LAG(other_short) OVER w AS d_other_short
          FROM cot_raw WHERE contract_id=:c
          WINDOW w AS (ORDER BY report_date)
        ) t ORDER BY report_date DESC LIMIT :n
    """, c=CONTRACT_ID, n=weeks)
    rows.reverse()
    return {"weeks": len(rows), "series": rows}


@app.get("/api/v1/rules")
def rules():
    return _rows("SELECT signal_code, version, is_active, params, note "
                 "FROM signal_rule WHERE is_active=1")


# ---------------------------------------------------------------------
def _require_admin(key: str | None):
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(401, "ต้องใช้คีย์ผู้ดูแลระบบ")


@app.post("/api/v1/admin/ingest")
def admin_ingest(x_api_key: str | None = Header(None)):
    _require_admin(x_api_key)
    n_cot = ingest_cot(CONTRACT_ID)
    n_daily = ingest_price()
    n_px = snap_weekly_price()
    n_dv = rebuild_derived(CONTRACT_ID)
    n_sig = evaluate_and_store()
    return {"cot_rows": n_cot, "price_daily_rows": n_daily, "price_weeks": n_px,
            "derived_rows": n_dv, "signals_written": n_sig}


@app.post("/api/v1/admin/resync-price")
def admin_resync_price(x_api_key: str | None = Header(None)):
    """
    ล้างราคาเก่าทั้งหมดแล้วดึงใหม่ — ใช้ครั้งเดียวหลังแก้บั๊กวันที่เพี้ยนจาก
    Yahoo Finance (gmtoffset) เพื่อกันแถวเก่าที่บันทึกวันที่ผิดพลาดค้างอยู่
    """
    _require_admin(x_api_key)
    n_daily = resync_price()
    n_px = snap_weekly_price()
    n_dv = rebuild_derived(CONTRACT_ID)
    n_sig = evaluate_and_store()
    return {"price_daily_rows": n_daily, "price_weeks": n_px,
            "derived_rows": n_dv, "signals_written": n_sig}


# ---------------------------------------------------------------------
def _load_params() -> RuleParams:
    rows = _rows("SELECT params FROM signal_rule WHERE is_active=1")
    merged: dict = {}
    for r in rows:
        p = r["params"]
        merged.update(json.loads(p) if isinstance(p, str) else p)
    known = {f for f in RuleParams.__dataclass_fields__}
    return RuleParams(**{k: v for k, v in merged.items() if k in known})


def evaluate_and_store(weeks: int = 156) -> int:
    """อ่าน cot_derived + ราคา แล้วเขียนผลสัญญาณลงตาราง signal"""
    rows = _rows("""
        SELECT * FROM v_timeline WHERE contract_id=:c
        ORDER BY report_date DESC LIMIT :n""", c=CONTRACT_ID, n=weeks)
    rows.reverse()

    inputs = [
        WeekInput(
            report_date=str(r["report_date"]),
            close_px=float(r["close_px"] or 0),
            ret_4w=float(r["ret_4w"] or 0),
            pct_52w=float(r["pct_52w"] or 50),
            d_mm_net=int(r["d_mm_net"] or 0),
            d_mm_long=int(r["d_mm_long"] or 0),
            d_swap_net=int(r["d_swap_net"] or 0),
            d_swap_short=int(r["d_swap_short"] or 0),
            d_prod_short=int(r["d_prod_short"] or 0),
            z_mm_net=float(r["z_mm_net"] or 0),
            z_mm_long=float(r.get("z_mm_long") or 0),
            z_swap_net=float(r["z_swap_net"] or 0),
            z_swap_short=float(r.get("z_swap_short") or 0),
            z_prod_short=float(r.get("z_prod_short") or 0),
            mm_net=int(r["mm_net"] or 0),
            swap_net=int(r["swap_net"] or 0),
            prod_net=int(r["prod_net"] or 0),
        )
        for r in rows if r["close_px"] is not None
    ]

    results = evaluate_series(inputs, _load_params())
    with engine.begin() as conn:
        for res in results:
            conn.execute(text("""
              INSERT INTO `signal` (contract_id, report_date, signal_code, is_primary,
                direction, bias_score, confidence, price_ref, price_zone,
                price_state, rationale)
              VALUES (:c,:d,:code,1,:dir,:b,:conf,:px,:zone,:state,:r)
              ON DUPLICATE KEY UPDATE
                direction=VALUES(direction), bias_score=VALUES(bias_score),
                confidence=VALUES(confidence), price_zone=VALUES(price_zone),
                price_state=VALUES(price_state), rationale=VALUES(rationale)
            """), {
                "c": CONTRACT_ID, "d": res.report_date, "code": res.signal_code,
                "dir": res.direction, "b": res.bias_score, "conf": res.confidence,
                "px": res.price_ref, "zone": res.price_zone,
                "state": res.price_state,
                "r": json.dumps({"items": res.rationale, "overlay": res.overlay},
                                ensure_ascii=False),
            })
    return len(results)
