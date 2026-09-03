"""
ETL + Derivation layer

ทุกฟังก์ชันเป็น idempotent — รันซ้ำวันเดิมได้ผลเท่าเดิม เพราะใช้
INSERT ... ON DUPLICATE KEY UPDATE ทั้งหมด

หมายเหตุ: ชื่อฟิลด์ของ CFTC Socrata อาจเปลี่ยนได้ตามเวอร์ชันชุดข้อมูล
ควรยิง /resource/kh3c-gbw2.json?$limit=1 ดูโครงสร้างจริงก่อนใช้งานครั้งแรก
"""
from __future__ import annotations

import io
import logging
from datetime import date, datetime, timedelta, timezone

import httpx
import numpy as np
import pandas as pd
from sqlalchemy import text

from .db import engine

log = logging.getLogger(__name__)

CFTC_ENDPOINT = "https://publicreporting.cftc.gov/resource/kh3c-gbw2.json"
GOLD_CFTC_CODE = "088691"
LOOKBACK = 52          # กรอบคำนวณ stdev
REBUILD_WEEKS = 156    # คำนวณ derived ใหม่ย้อนหลังเท่านี้ทุกสัปดาห์


# ---------------------------------------------------------------------
# 1. ดึง COT
# ---------------------------------------------------------------------
FIELD_MAP = {
    "open_interest_all":            "open_interest",
    "prod_merc_positions_long":     "prod_long",
    "prod_merc_positions_short":    "prod_short",
    "swap_positions_long_all":      "swap_long",
    "swap__positions_short_all":    "swap_short",
    "swap__positions_spread_all":   "swap_spread",
    "m_money_positions_long_all":   "mm_long",
    "m_money_positions_short_all":  "mm_short",
    "m_money_positions_spread":     "mm_spread",
    "other_rept_positions_long":    "other_long",
    "other_rept_positions_short":   "other_short",
    "other_rept_positions_spread":  "other_spread",
    "nonrept_positions_long_all":   "nonrept_long",
    "nonrept_positions_short_all":  "nonrept_short",
}


def fetch_cot(since: date, cftc_code: str = GOLD_CFTC_CODE) -> pd.DataFrame:
    params = {
        "$where": f"cftc_contract_market_code='{cftc_code}' "
                  f"AND report_date_as_yyyy_mm_dd >= '{since.isoformat()}T00:00:00'",
        "$order": "report_date_as_yyyy_mm_dd ASC",
        "$limit": 5000,
    }
    with httpx.Client(timeout=60) as c:
        rows = c.get(CFTC_ENDPOINT, params=params).raise_for_status().json()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # ป้องกันการได้ข้อมูล 0 แบบเงียบ ๆ — CFTC อาจเปลี่ยนชื่อฟิลด์ตามเวอร์ชันชุดข้อมูล
    # ถ้าชื่อที่คาดไว้ไม่ตรง ให้ฟ้อง error พร้อมชื่อฟิลด์จริงทั้งหมด
    # เพื่อให้แก้ FIELD_MAP ได้ถูกจุดในครั้งเดียว แทนที่จะเดาแล้วได้ข้อมูลผิด
    missing = [src for src in FIELD_MAP if src not in df.columns]
    if missing:
        raise RuntimeError(
            "ชื่อฟิลด์จาก CFTC ไม่ตรงกับที่ระบบคาดไว้ ระบบไม่ ingest ต่อเพื่อกันข้อมูลผิด\n"
            f"ฟิลด์ที่หาไม่เจอ: {', '.join(missing)}\n"
            f"ฟิลด์จริงที่ CFTC ส่งมาทั้งหมด ({len(df.columns)} ฟิลด์):\n"
            f"{', '.join(sorted(df.columns))}\n"
            "→ คัดลอกรายการฟิลด์จริงด้านบนไปแก้ไข FIELD_MAP ใน etl.py ให้ตรง"
        )

    if "report_date_as_yyyy_mm_dd" not in df.columns:
        raise RuntimeError(
            "ไม่พบคอลัมน์วันที่ 'report_date_as_yyyy_mm_dd' — "
            f"ฟิลด์จริงที่ได้: {', '.join(sorted(df.columns))}"
        )

    out = pd.DataFrame({
        "report_date": pd.to_datetime(df["report_date_as_yyyy_mm_dd"]).dt.date
    })
    for src, dst in FIELD_MAP.items():
        out[dst] = pd.to_numeric(df[src], errors="coerce").fillna(0).astype(int)
    return out


def validate_cot(df: pd.DataFrame) -> list[str]:
    """ยอด long รวมทุกกลุ่มต้องเท่ากับ short รวม และเท่ากับ open interest"""
    problems = []
    long_cols = ["prod_long", "swap_long", "mm_long", "other_long", "nonrept_long"]
    short_cols = ["prod_short", "swap_short", "mm_short", "other_short", "nonrept_short"]
    spread_cols = ["swap_spread", "mm_spread", "other_spread"]

    tot_long = df[long_cols].sum(axis=1) + df[spread_cols].sum(axis=1)
    tot_short = df[short_cols].sum(axis=1) + df[spread_cols].sum(axis=1)

    for i, row in df.iterrows():
        if abs(tot_long[i] - row["open_interest"]) > 5:
            problems.append(f"{row['report_date']}: long รวมไม่ตรง open interest")
        if abs(tot_long[i] - tot_short[i]) > 5:
            problems.append(f"{row['report_date']}: long รวมไม่ตรง short รวม")
    return problems


UPSERT_RAW = text("""
INSERT INTO cot_raw (contract_id, report_date, open_interest,
  prod_long, prod_short, swap_long, swap_short, swap_spread,
  mm_long, mm_short, mm_spread, other_long, other_short, other_spread,
  nonrept_long, nonrept_short)
VALUES (:cid, :report_date, :open_interest,
  :prod_long, :prod_short, :swap_long, :swap_short, :swap_spread,
  :mm_long, :mm_short, :mm_spread, :other_long, :other_short, :other_spread,
  :nonrept_long, :nonrept_short)
ON DUPLICATE KEY UPDATE
  open_interest=VALUES(open_interest),
  prod_long=VALUES(prod_long), prod_short=VALUES(prod_short),
  swap_long=VALUES(swap_long), swap_short=VALUES(swap_short),
  swap_spread=VALUES(swap_spread),
  mm_long=VALUES(mm_long), mm_short=VALUES(mm_short), mm_spread=VALUES(mm_spread),
  other_long=VALUES(other_long), other_short=VALUES(other_short),
  other_spread=VALUES(other_spread),
  nonrept_long=VALUES(nonrept_long), nonrept_short=VALUES(nonrept_short)
""")


def ingest_cot(contract_id: int = 1, weeks_back: int = 260) -> int:
    since = date.today() - timedelta(weeks=weeks_back)
    log.info("ดึง COT จาก CFTC ตั้งแต่ %s เป็นต้นมา (contract %s)", since, GOLD_CFTC_CODE)
    df = fetch_cot(since)
    if df.empty:
        log.warning("CFTC ไม่คืนข้อมูล — ตรวจว่า cftc_contract_market_code ถูกต้องหรือไม่")
        return 0

    for p in validate_cot(df):
        log.warning("ตรวจข้อมูล: %s", p)

    records = df.to_dict("records")
    with engine.begin() as conn:
        for r in records:
            conn.execute(UPSERT_RAW, {"cid": contract_id, **r})
    log.info("บันทึก COT สำเร็จ %d สัปดาห์", len(records))
    return len(records)


# ---------------------------------------------------------------------
# 2. ราคา — ใช้ Yahoo Finance chart API (ไม่ต้องขอ API key)
#
#    เดิมใช้ Stooq แต่ Stooq ปิดกั้นการเข้าถึงแบบอัตโนมัติผ่าน robots.txt
#    เปลี่ยนมาใช้ Yahoo Finance ซึ่งเป็น endpoint สาธารณะที่โปรแกรมจำนวนมาก
#    ใช้ดึงข้อมูลราคาโดยไม่ต้องมีคีย์ — ไม่มีการปิดกั้นลักษณะเดียวกัน
#
#    เปลี่ยนผู้ให้บริการได้ในอนาคตโดยแก้เฉพาะฟังก์ชัน ingest_price() นี้ฟังก์ชันเดียว
# ---------------------------------------------------------------------
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

# ใช้ Gold Futures ต่อเนื่อง (COMEX) แทน spot XAUUSD=X เพราะมีประวัติยาวและช่องว่างข้อมูลน้อยกว่า
YAHOO_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
}


def ingest_price(symbol: str = "XAUUSD", yahoo_ticker: str = "GC=F",
                  range_: str = "15y") -> int:
    """
    ดึงราคาทองคำรายวันจาก Yahoo Finance chart API เขียนลง price_daily

    ใช้ Gold Futures ต่อเนื่อง (GC=F) เป็นค่าเริ่มต้น ถ้าต้องการ spot จริง
    เปลี่ยนเป็น yahoo_ticker="XAUUSD=X" ได้ (ประวัติสั้นกว่าและมีช่องว่างมากกว่า)
    """
    url = YAHOO_CHART_URL.format(ticker=yahoo_ticker)
    with httpx.Client(timeout=30, headers=YAHOO_HEADERS, follow_redirects=True) as c:
        r = c.get(url, params={"range": range_, "interval": "1d"})
        r.raise_for_status()
        data = r.json()

    chart = data.get("chart") or {}
    result = chart.get("result")
    if not result:
        raise RuntimeError(
            f"Yahoo Finance ไม่คืนข้อมูลให้สัญลักษณ์ '{yahoo_ticker}' "
            f"— error จาก Yahoo: {chart.get('error')}\n"
            "ถ้าเจอปัญหานี้ซ้ำ ให้ใช้ ingest_price_from_csv() แทน "
            "(ดาวน์โหลด CSV ราคาทองด้วยมือจากเว็บแล้วโหลดเข้าระบบ)"
        )

    res = result[0]
    meta = res.get("meta") or {}
    # ชดเชย offset เวลาตลาดก่อนตัดเป็นวันที่ — จำเป็นสำหรับ futures ที่ session
    # ยาวเกือบ 24 ชม. (Globex) ถ้าตัด .date() จาก UTC ตรง ๆ วันที่จะเพี้ยนไป 1 วัน
    # เป็นระบบ ทำให้ราคาที่ผูกกับ report_date ของ COT (วันอังคาร) คลาดเคลื่อน
    gmtoffset = meta.get("gmtoffset", 0) or 0

    timestamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    opens, highs = quote.get("open") or [], quote.get("high") or []
    lows, closes = quote.get("low") or [], quote.get("close") or []
    volumes = quote.get("volume") or []

    if not timestamps or not closes:
        raise RuntimeError("Yahoo Finance คืนโครงสร้างข้อมูลว่างเปล่า ลองใหม่อีกครั้ง")

    # diagnostic: โชว์แท่งดิบ 5 แท่งสุดท้ายจาก Yahoo ก่อนกรอง เพื่อวินิจฉัยว่า
    # ถ้าวันล่าสุดหาย เป็นเพราะ Yahoo ยังไม่ส่งมาเลย หรือส่งมาแต่ close เป็น null
    # (แท่งที่ยังไม่ finalize) — เห็นชัดกว่าการเดาจากปลายทาง
    tail_n = min(5, len(timestamps))
    log.info("Yahoo ส่งแท่งราคาล่าสุดมาทั้งหมด %d แท่ง — 5 แท่งท้ายสุด:", len(timestamps))
    for i in range(len(timestamps) - tail_n, len(timestamps)):
        ts = timestamps[i]
        d_raw = datetime.fromtimestamp(ts + gmtoffset, tz=timezone.utc).date()
        c_raw = closes[i] if i < len(closes) else None
        log.info("  %s  close=%s", d_raw, c_raw if c_raw is not None else "null (ยังไม่ finalize)")

    n = 0
    with engine.begin() as conn:
        for i, ts in enumerate(timestamps):
            c_px = closes[i] if i < len(closes) else None
            if c_px is None:          # วันที่ตลาดปิด Yahoo คืน null ไว้
                continue
            d = datetime.fromtimestamp(ts + gmtoffset, tz=timezone.utc).date()
            conn.execute(text("""
              INSERT INTO price_daily (symbol, trade_date, open_px, high_px, low_px, close_px, volume)
              VALUES (:s,:d,:o,:h,:l,:c,:v)
              ON DUPLICATE KEY UPDATE
                open_px=VALUES(open_px), high_px=VALUES(high_px),
                low_px=VALUES(low_px), close_px=VALUES(close_px), volume=VALUES(volume)
            """), {
                "s": symbol, "d": d,
                "o": _nn(opens[i] if i < len(opens) else None),
                "h": _nn(highs[i] if i < len(highs) else None),
                "l": _nn(lows[i] if i < len(lows) else None),
                "c": float(c_px),
                "v": _i(volumes[i]) if i < len(volumes) and volumes[i] is not None else None,
            })
            n += 1

    log.info("บันทึกราคา %s สำเร็จ %d วัน (Yahoo Finance, ticker=%s)", symbol, n, yahoo_ticker)
    return n


def resync_price(symbol: str = "XAUUSD", yahoo_ticker: str = "GC=F",
                  range_: str = "15y") -> int:
    """
    ล้างราคาเก่าของ symbol นี้ทั้งหมดแล้วดึงใหม่จาก Yahoo

    ใช้ครั้งเดียวหลังแก้บั๊กวันที่เพี้ยน (gmtoffset) เพื่อกันไม่ให้แถวเก่าที่
    บันทึกวันที่ผิดพลาดค้างอยู่ปนกับแถวใหม่ที่ถูกต้อง — ปกติใช้ ingest_price()
    ตามรอบสัปดาห์พอ ไม่ต้อง resync ทุกครั้ง
    """
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM price_daily WHERE symbol=:s"), {"s": symbol})
    log.info("ล้างราคาเก่าของ %s แล้ว กำลังดึงใหม่", symbol)
    return ingest_price(symbol, yahoo_ticker, range_)


def ingest_price_from_csv(path: str, symbol: str = "XAUUSD") -> int:
    """
    ทางเลือกสำรอง — โหลดราคาจากไฟล์ CSV ที่ดาวน์โหลดด้วยมือ (กรณี Yahoo ใช้งานไม่ได้)

    ใช้ได้กับ CSV ที่ดาวน์โหลดจากหน้าเว็บ Yahoo Finance โดยตรง (ปุ่ม "Download"
    บนหน้า finance.yahoo.com/quote/GC=F/history) หรือแหล่งอื่นที่มีคอลัมน์
    Date, Open, High, Low, Close อย่างน้อย — ต่างจาก ingest_price() ตรงที่
    ดาวน์โหลดผ่านเบราว์เซอร์เอง ไม่ผ่านโปรแกรมอัตโนมัติ จึงไม่โดนบล็อกแบบ Stooq
    """
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    required = {"date", "close"}
    if not required.issubset(df.columns):
        raise RuntimeError(f"ไฟล์ CSV ต้องมีคอลัมน์ date, close อย่างน้อย — คอลัมน์ที่ได้: {list(df.columns)}")

    df["trade_date"] = pd.to_datetime(df["date"]).dt.date
    df = df[pd.to_numeric(df["close"], errors="coerce").notna()]  # ตัดแถว "null" ที่ Yahoo ใส่ไว้
    has_volume = "volume" in df.columns

    with engine.begin() as conn:
        for _, r in df.iterrows():
            conn.execute(text("""
              INSERT INTO price_daily (symbol, trade_date, open_px, high_px, low_px, close_px, volume)
              VALUES (:s,:d,:o,:h,:l,:c,:v)
              ON DUPLICATE KEY UPDATE
                open_px=VALUES(open_px), high_px=VALUES(high_px),
                low_px=VALUES(low_px), close_px=VALUES(close_px), volume=VALUES(volume)
            """), {
                "s": symbol, "d": r["trade_date"],
                "o": _nn(r.get("open")), "h": _nn(r.get("high")),
                "l": _nn(r.get("low")), "c": float(r["close"]),
                "v": _i(r.get("volume")) if has_volume else None,
            })
    log.info("บันทึกราคา %s จาก CSV สำเร็จ %d แถว", symbol, len(df))
    return len(df)


def ingest_option_oi_from_csv(path: str, contract_id: int = 1,
                               trade_date: str | None = None) -> int:
    """
    นำเข้าข้อมูล Open Interest ของ Gold Option ด้วยมือจากไฟล์ CSV

    Barchart ไม่มี API สาธารณะฟรี (ราคาเริ่มต้น 500 ดอลลาร์/เดือน) และการดึงข้อมูล
    จากหน้าเว็บโดยตรงมีความเสี่ยงโดนปิดกั้นเช่นเดียวกับที่เคยเจอกับ Stooq จึงใช้วิธี
    ให้ผู้ใช้ export/copy ตารางจาก Barchart แล้วบันทึกเป็น CSV ก่อนนำเข้าด้วยฟังก์ชันนี้

    CSV ต้องมีคอลัมน์อย่างน้อย: strike, type (C หรือ P), open_interest, expiry
    (expiry รูปแบบ YYYY-MM-DD) — ชื่อคอลัมน์ไม่สนตัวพิมพ์เล็กใหญ่และเว้นวรรครอบ ๆ
    """
    df = pd.read_csv(path)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    col_map = {
        "strike": ["strike", "strike_price"],
        "type": ["type", "option_type", "put/call", "putcall"],
        "open_interest": ["open_interest", "openinterest", "oi"],
        "expiry": ["expiry", "exp_date", "expiration", "expiry_date"],
    }
    resolved = {}
    for need, aliases in col_map.items():
        found = next((a for a in aliases if a in df.columns), None)
        if not found:
            raise RuntimeError(
                f"ไม่พบคอลัมน์ที่ตรงกับ '{need}' — คอลัมน์ที่มีในไฟล์: {list(df.columns)}"
            )
        resolved[need] = found

    d = trade_date or date.today().isoformat()
    n = 0
    with engine.begin() as conn:
        for _, r in df.iterrows():
            raw_type = str(r[resolved["type"]]).strip().upper()
            opt_type = "C" if raw_type.startswith("C") else "P" if raw_type.startswith("P") else None
            if opt_type is None:
                continue
            try:
                strike = float(str(r[resolved["strike"]]).replace(",", ""))
                oi = int(float(str(r[resolved["open_interest"]]).replace(",", "") or 0))
                expiry = str(pd.to_datetime(r[resolved["expiry"]]).date())
            except (ValueError, TypeError):
                continue

            conn.execute(text("""
              INSERT INTO option_oi (contract_id, trade_date, expiry_date, strike, option_type, open_interest)
              VALUES (:c,:d,:e,:s,:t,:oi)
              ON DUPLICATE KEY UPDATE open_interest=VALUES(open_interest)
            """), {"c": contract_id, "d": d, "e": expiry, "s": strike, "t": opt_type, "oi": oi})
            n += 1

    log.info("บันทึก option OI สำเร็จ %d แถว (trade_date=%s)", n, d)
    return n


def snap_weekly_price(symbol: str = "XAUUSD") -> int:
    """
    ดึงราคาปิดวันอังคาร map เข้ากับ report_date ของ COT
    ถ้าวันอังคารนั้นตลาดปิด ใช้ราคาปิดวันทำการก่อนหน้าที่ใกล้ที่สุด
    """
    with engine.begin() as conn:
        dates = [r[0] for r in conn.execute(
            text("SELECT DISTINCT report_date FROM cot_raw ORDER BY report_date"))]

        px = pd.read_sql(
            text("SELECT trade_date, close_px FROM price_daily WHERE symbol=:s"),
            conn, params={"s": symbol}, parse_dates=["trade_date"])

        if px.empty:
            return 0
        px = px.set_index("trade_date")["close_px"].astype(float).sort_index()

        rows = []
        for d in dates:
            ts = pd.Timestamp(d)
            window = px.loc[:ts]
            if window.empty:
                continue
            rows.append({"report_date": d, "close_px": float(window.iloc[-1])})

        s = pd.DataFrame(rows).set_index("report_date")["close_px"]
        ret_1w = s.pct_change(1)
        ret_4w = s.pct_change(4)
        pct_52w = s.rolling(LOOKBACK, min_periods=12).apply(
            lambda x: (x < x.iloc[-1]).mean() * 100, raw=False)

        for d in s.index:
            conn.execute(text("""
              INSERT INTO price_weekly (symbol, report_date, close_px, ret_1w, ret_4w, pct_52w)
              VALUES (:s,:d,:c,:r1,:r4,:p)
              ON DUPLICATE KEY UPDATE close_px=VALUES(close_px),
                ret_1w=VALUES(ret_1w), ret_4w=VALUES(ret_4w), pct_52w=VALUES(pct_52w)
            """), {"s": symbol, "d": d, "c": float(s[d]),
                   "r1": _nn(ret_1w.get(d)), "r4": _nn(ret_4w.get(d)),
                   "p": _nn(pct_52w.get(d))})
    return len(s)


def _nn(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)


# ---------------------------------------------------------------------
# 3. ชั้นคำนวณ — net, delta, z-score
# ---------------------------------------------------------------------
def rebuild_derived(contract_id: int = 1, weeks: int = REBUILD_WEEKS) -> int:
    """
    คำนวณใหม่ทั้งช่วง ไม่ใช่เฉพาะสัปดาห์ล่าสุด เพราะ CFTC แก้ตัวเลขย้อนหลังได้
    และ stdev แบบ rolling ก็เปลี่ยนตามไปด้วย
    """
    with engine.begin() as conn:
        df = pd.read_sql(text("""
            SELECT report_date, open_interest,
                   prod_long, prod_short, swap_long, swap_short,
                   mm_long, mm_short, other_long, other_short
            FROM cot_raw WHERE contract_id=:cid
            ORDER BY report_date
        """), conn, params={"cid": contract_id})

        if len(df) < 8:
            return 0

        df["mm_net"] = df.mm_long - df.mm_short
        df["swap_net"] = df.swap_long - df.swap_short
        df["prod_net"] = df.prod_long - df.prod_short
        df["other_net"] = df.other_long - df.other_short

        for col in ["mm_net", "mm_long", "mm_short", "swap_net", "swap_long",
                    "swap_short", "prod_short", "open_interest"]:
            df["d_" + col] = df[col].diff()

        # z-score ของ Δ เทียบ stdev 52 สัปดาห์ (ปิดหน้าต่างที่สัปดาห์ปัจจุบัน)
        for col in ["d_mm_net", "d_mm_long", "d_swap_net", "d_swap_short", "d_prod_short"]:
            sd = df[col].rolling(LOOKBACK, min_periods=12).std()
            df["z_" + col[2:]] = (df[col] / sd).replace([np.inf, -np.inf], np.nan)

        df["mm_net_pct_3y"] = df.mm_net.rolling(156, min_periods=26).apply(
            lambda x: (x < x.iloc[-1]).mean() * 100, raw=False)
        df["mm_net_over_oi"] = df.mm_net / df.open_interest.replace(0, np.nan)

        tail = df.tail(weeks)
        for _, r in tail.iterrows():
            conn.execute(text("""
              INSERT INTO cot_derived (contract_id, report_date, mm_net, swap_net,
                prod_net, other_net, d_mm_net, d_mm_long, d_mm_short, d_swap_net,
                d_swap_long, d_swap_short, d_prod_short, d_oi,
                z_mm_net, z_mm_long, z_swap_net, z_swap_short, z_prod_short,
                mm_net_pct_3y, mm_net_over_oi)
              VALUES (:cid,:d,:mm,:sw,:pr,:ot,:dmm,:dmml,:dmms,:dsw,:dswl,:dsws,
                      :dps,:doi,:zmm,:zmml,:zsw,:zsws,:zps,:pct,:ooi)
              ON DUPLICATE KEY UPDATE
                mm_net=VALUES(mm_net), swap_net=VALUES(swap_net),
                prod_net=VALUES(prod_net), other_net=VALUES(other_net),
                d_mm_net=VALUES(d_mm_net), d_mm_long=VALUES(d_mm_long),
                d_mm_short=VALUES(d_mm_short), d_swap_net=VALUES(d_swap_net),
                d_swap_long=VALUES(d_swap_long), d_swap_short=VALUES(d_swap_short),
                d_prod_short=VALUES(d_prod_short), d_oi=VALUES(d_oi),
                z_mm_net=VALUES(z_mm_net), z_mm_long=VALUES(z_mm_long),
                z_swap_net=VALUES(z_swap_net), z_swap_short=VALUES(z_swap_short),
                z_prod_short=VALUES(z_prod_short),
                mm_net_pct_3y=VALUES(mm_net_pct_3y), mm_net_over_oi=VALUES(mm_net_over_oi)
            """), {
                "cid": contract_id, "d": r.report_date,
                "mm": int(r.mm_net), "sw": int(r.swap_net),
                "pr": int(r.prod_net), "ot": int(r.other_net),
                "dmm": _i(r.d_mm_net), "dmml": _i(r.d_mm_long), "dmms": _i(r.d_mm_short),
                "dsw": _i(r.d_swap_net), "dswl": _i(r.d_swap_long), "dsws": _i(r.d_swap_short),
                "dps": _i(r.d_prod_short), "doi": _i(r.d_open_interest),
                "zmm": _nn(r.z_mm_net), "zmml": _nn(r.z_mm_long),
                "zsw": _nn(r.z_swap_net), "zsws": _nn(r.z_swap_short),
                "zps": _nn(r.z_prod_short),
                "pct": _nn(r.mm_net_pct_3y), "ooi": _nn(r.mm_net_over_oi),
            })
    return len(tail)


def _i(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else int(v)


# ---------------------------------------------------------------------
# 4. Gold Options Analysis — นำเข้าจากไฟล์ "Gold Options Analysis Template"
#    (Barchart ไม่มี API ฟรี จึงนำเข้าด้วยมือจากไฟล์ xlsx ที่วิเคราะห์ไว้แล้ว)
# ---------------------------------------------------------------------
def _find_row(df: pd.DataFrame, text: str, col: int = 0) -> int | None:
    """หาแถวแรกที่คอลัมน์ที่ระบุมีข้อความนี้อยู่ (ใช้หาหัวข้อ section)"""
    for i in range(len(df)):
        cell = df.iat[i, col]
        if cell is not None and text in str(cell):
            return i
    return None


def _read_block(df: pd.DataFrame, header_row: int) -> pd.DataFrame:
    """
    อ่านตารางที่เริ่มจากแถว header_row (แถวชื่อคอลัมน์) ไปจนกว่าจะเจอแถวว่าง
    ใช้กับไฟล์เทมเพลตที่วางหลายตารางต่อกันในชีตเดียว โดยไม่มีเส้นแบ่งชัดเจน
    """
    cols = [str(c).strip() if pd.notna(c) else f"col{i}"
            for i, c in enumerate(df.iloc[header_row])]
    rows = []
    i = header_row + 1
    while i < len(df) and pd.notna(df.iat[i, 0]):
        rows.append(df.iloc[i].tolist())
        i += 1
    return pd.DataFrame(rows, columns=cols)


def _num(v):
    """แปลงเป็นตัวเลข ปลอดภัยจากค่าว่าง/NaN/ข้อความ"""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def ingest_option_analysis_from_xlsx(path: str, contract_id: int = 1,
                                      trade_date: str | None = None) -> dict:
    """
    นำเข้าข้อมูลจาก "Gold Options Analysis Template" (.xlsx) ทั้ง 3 ชีต:
    Executive Summary (ภาพรวม + บทวิเคราะห์รายซีรีส์), Series Breakdown
    (ครบทุกซีรีส์), Strike Deep-Dive (รายละเอียดราย strike)

    ไฟล์นี้เป็นรายงานวิเคราะห์ที่จัดทำไว้แล้ว (ไม่ใช่ raw feed อัตโนมัติ)
    เพราะ Barchart ไม่มี API สาธารณะฟรี — นำเข้าด้วยมือทุกครั้งที่มีรายงานใหม่
    """
    d = trade_date or date.today().isoformat()
    result = {"overview": 0, "series": 0, "strikes": 0}

    xl = pd.ExcelFile(path)
    with engine.begin() as conn:

        # ── 1) ภาพรวมตลาดจากชีต Executive Summary ──────────────────
        if "Executive Summary" in xl.sheet_names:
            exec_df = pd.read_excel(path, sheet_name="Executive Summary", header=None)

            r = _find_row(exec_df, "Total Market Call OI")
            if r is not None:
                call_oi, put_oi, ratio, sentiment = exec_df.iloc[r + 1, :4].tolist()
                conn.execute(text("""
                  INSERT INTO option_market_overview
                    (contract_id, trade_date, call_oi_total, put_oi_total, oi_pc_ratio, macro_sentiment)
                  VALUES (:c,:d,:call,:put,:ratio,:sent)
                  ON DUPLICATE KEY UPDATE
                    call_oi_total=VALUES(call_oi_total), put_oi_total=VALUES(put_oi_total),
                    oi_pc_ratio=VALUES(oi_pc_ratio), macro_sentiment=VALUES(macro_sentiment)
                """), {"c": contract_id, "d": d, "call": _i(call_oi), "put": _i(put_oi),
                       "ratio": _num(ratio), "sent": str(sentiment) if pd.notna(sentiment) else None})
                result["overview"] = 1

            # ── 2) บทวิเคราะห์ + ตัวเลขแม่นยำของ 5 ซีรีส์หลัก (ถ้ามี) ──
            interp_map: dict[str, str] = {}
            main_series_map: dict[str, dict] = {}

            r_main = _find_row(exec_df, "Main Contract Series")
            if r_main is not None:
                header_row = r_main + 1
                main_block = _read_block(exec_df, header_row)
                for _, row in main_block.iterrows():
                    code = str(row.get("Contract Series", "")).strip()
                    if code:
                        main_series_map[code] = {
                            "oi_pc_ratio": _num(row.get("OI P/C Ratio")),
                            "vol_pc_ratio": _num(row.get("Vol P/C Ratio")),
                            "market_sentiment": (str(row.get("Market Sentiment"))
                                                  if pd.notna(row.get("Market Sentiment")) else None),
                        }

            r2 = _find_row(exec_df, "Comprehensive Meaning")
            if r2 is not None:
                block = _read_block(exec_df, r2 + 1)
                # คอลัมน์ข้อความยาวอาจถูก pandas แยกเป็นหลายคอลัมน์ว่างต่อกัน
                # หาคอลัมน์ที่ชื่อขึ้นต้นด้วย 'Detailed Interpretation'
                note_col = next((c for c in block.columns
                                  if str(c).startswith("Detailed Interpretation")), None)
                code_col = block.columns[0]
                if note_col:
                    for _, row in block.iterrows():
                        code = str(row[code_col]).strip()
                        note = row[note_col]
                        if code and pd.notna(note):
                            interp_map[code] = str(note)

        # ── 3) รายซีรีส์ครบทุกตัวจากชีต Series Breakdown ───────────
        if "Series Breakdown" in xl.sheet_names:
            sb_df = pd.read_excel(path, sheet_name="Series Breakdown", header=None)
            r3 = None
            for i in range(len(sb_df)):
                if str(sb_df.iat[i, 0]).strip() == "Series Code":
                    r3 = i
                    break
            if r3 is not None:
                block = _read_block(sb_df, r3)
                for _, row in block.iterrows():
                    code = str(row.get("Series Code", "")).strip()
                    if not code or code.upper().startswith("TOTAL"):
                        continue

                    put_oi, call_oi = _num(row.get("Put OI")), _num(row.get("Call OI"))
                    put_vol, call_vol = _num(row.get("Put Volume")), _num(row.get("Call Volume"))
                    main = main_series_map.get(code, {})

                    oi_ratio = _num(row.get("OI P/C Ratio")) or main.get("oi_pc_ratio")
                    if oi_ratio is None and call_oi:
                        oi_ratio = round(put_oi / call_oi, 3) if put_oi is not None else None
                    vol_ratio = _num(row.get("Vol P/C Ratio")) or main.get("vol_pc_ratio")
                    if vol_ratio is None and call_vol:
                        vol_ratio = round(put_vol / call_vol, 3) if put_vol is not None else None

                    sentiment = row.get("Market Sentiment")
                    sentiment = str(sentiment) if pd.notna(sentiment) else main.get("market_sentiment")
                    if sentiment is None and oi_ratio is not None:
                        # ประมาณการจาก OI P/C ratio ตามเกณฑ์ใน Interpretation Guide
                        # (< 0.70 = Bullish, > 1.00 = Bearish/Hedging) — ระบุ (ประมาณการ)
                        # ชัดเจนเพื่อไม่ให้ปนกับป้ายที่นักวิเคราะห์ระบุไว้จริงสำหรับ 5 ซีรีส์หลัก
                        if oi_ratio < 0.35: sentiment = "Extreme Bullish (ประมาณการ)"
                        elif oi_ratio < 0.70: sentiment = "Bullish (ประมาณการ)"
                        elif oi_ratio <= 1.00: sentiment = "Neutral (ประมาณการ)"
                        elif oi_ratio <= 1.50: sentiment = "Bearish/Hedging (ประมาณการ)"
                        else: sentiment = "Strong Bearish (ประมาณการ)"

                    conn.execute(text("""
                      INSERT INTO option_series_summary
                        (contract_id, trade_date, series_code, futures_ref, put_oi, call_oi,
                         total_oi, oi_pc_ratio, put_volume, call_volume, total_volume,
                         vol_pc_ratio, market_sentiment, interpretation_th)
                      VALUES (:c,:d,:code,:fref,:put,:call,:tot,:oipc,:pv,:cv,:tv,:volpc,:sent,:interp)
                      ON DUPLICATE KEY UPDATE
                        futures_ref=VALUES(futures_ref), put_oi=VALUES(put_oi), call_oi=VALUES(call_oi),
                        total_oi=VALUES(total_oi), oi_pc_ratio=VALUES(oi_pc_ratio),
                        put_volume=VALUES(put_volume), call_volume=VALUES(call_volume),
                        total_volume=VALUES(total_volume), vol_pc_ratio=VALUES(vol_pc_ratio),
                        market_sentiment=VALUES(market_sentiment),
                        interpretation_th=COALESCE(VALUES(interpretation_th), interpretation_th)
                    """), {
                        "c": contract_id, "d": d, "code": code,
                        "fref": _num(row.get("Futures Ref")),
                        "put": _i(put_oi), "call": _i(call_oi),
                        "tot": _i(_num(row.get("Total OI"))) or (_i(put_oi) or 0) + (_i(call_oi) or 0),
                        "oipc": oi_ratio,
                        "pv": _i(put_vol), "cv": _i(call_vol),
                        "tv": _i(_num(row.get("Total Volume"))) or (_i(put_vol) or 0) + (_i(call_vol) or 0),
                        "volpc": vol_ratio,
                        "sent": sentiment,
                        "interp": interp_map.get(code),
                    })
                    result["series"] += 1

        # ── 4) รายละเอียด strike จากชีต Strike Deep-Dive ───────────
        if "Strike Deep-Dive" in xl.sheet_names:
            sd_df = pd.read_excel(path, sheet_name="Strike Deep-Dive", header=None)
            r4 = None
            for i in range(len(sd_df)):
                if str(sd_df.iat[i, 0]).strip() == "Strike Price":
                    r4 = i
                    break
            if r4 is not None:
                block = _read_block(sd_df, r4)
                for _, row in block.iterrows():
                    strike = _num(row.get("Strike Price"))
                    code = str(row.get("Series Code", "")).strip()
                    if strike is None or not code:
                        continue
                    conn.execute(text("""
                      INSERT INTO option_strike_detail
                        (contract_id, trade_date, series_code, strike, futures_ref, moneyness,
                         put_oi, call_oi, put_volume, call_volume, dominant_side, note_th)
                      VALUES (:c,:d,:code,:strike,:fref,:money,:put,:call,:pv,:cv,:dom,:note)
                      ON DUPLICATE KEY UPDATE
                        futures_ref=VALUES(futures_ref), moneyness=VALUES(moneyness),
                        put_oi=VALUES(put_oi), call_oi=VALUES(call_oi),
                        put_volume=VALUES(put_volume), call_volume=VALUES(call_volume),
                        dominant_side=VALUES(dominant_side), note_th=VALUES(note_th)
                    """), {
                        "c": contract_id, "d": d, "code": code, "strike": strike,
                        "fref": _num(row.get("Futures Ref")),
                        "money": row.get("Moneyness") if pd.notna(row.get("Moneyness")) else None,
                        "put": _i(_num(row.get("Put OI"))), "call": _i(_num(row.get("Call OI"))),
                        "pv": _i(_num(row.get("Put Volume"))), "cv": _i(_num(row.get("Call Volume"))),
                        "dom": row.get("Dominant Option Side") if pd.notna(row.get("Dominant Option Side")) else None,
                        "note": (str(row.get("Technical Function & Strategy Note"))[:500]
                                 if pd.notna(row.get("Technical Function & Strategy Note")) else None),
                    })
                    result["strikes"] += 1

    log.info("นำเข้า Gold Options Analysis สำเร็จ: %s", result)
    return result
