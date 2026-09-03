"""
พยากรณ์ราคาทองคำล่วงหน้ารายวันด้วย ARIMAX

ARIMAX = ARIMA + eXogenous variables — ใช้ statsmodels' SARIMAX (ไม่ตั้ง
seasonal_order จึงเทียบเท่า ARIMAX ธรรมดา ไม่มีองค์ประกอบ seasonal)
ตัวแปรภายนอก (exogenous) ที่ใช้คือสถานะสุทธิของ Managed Money และ Swap Dealer
จากข้อมูล COT — เป็นตัวเลขสถานะจริงจากตลาด ไม่ใช่ค่าตีความ

ข้อจำกัดสำคัญที่ต้องเปิดเผยตรงไปตรงมา
--------------------------------------
COT เป็นข้อมูลรายสัปดาห์ แต่ราคาทองเป็นรายวัน การนำมาใช้เป็น exogenous ร่วมกัน
ต้อง forward-fill ค่า COT ให้ครบทุกวัน (ใช้ค่าของสัปดาห์ล่าสุดซ้ำไปจนกว่าจะมี
รายงานใหม่) ซึ่งเป็นวิธีมาตรฐานสำหรับผสมข้อมูลต่างความถี่ แต่ที่สำคัญกว่านั้นคือ
**การพยากรณ์ไปข้างหน้าไม่มีทางรู้ค่า COT ในอนาคตจริง ๆ** เพราะเป็นข้อมูลที่ยังไม่
เกิดขึ้น โมเดลนี้จึงตรึงค่า exogenous ไว้เท่ากับค่าล่าสุดที่ทราบตลอดช่วงพยากรณ์
(naive persistence) ซึ่งเป็นสมมติฐานที่ทำให้ความแม่นยำลดลงเมื่อพยากรณ์ไกลขึ้น
ไม่ใช่การพยากรณ์ที่มองเห็นอนาคตของ COT จริง — เหมาะสำหรับดูแนวโน้มระยะสั้น
(ไม่กี่วันทำการ) มากกว่าการเทรดตามตัวเลขที่ได้ตรง ๆ
"""
from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import text

from .db import engine

log = logging.getLogger(__name__)

DEFAULT_ORDER = (1, 1, 1)      # (p, d, q) — ค่าเริ่มต้นที่เหมาะกับราคาสินทรัพย์ทั่วไป
MIN_OBSERVATIONS = 90          # ต้องมีข้อมูลอย่างน้อยกี่วันถึงจะ fit โมเดลได้อย่างมีความหมาย


class ForecastUnavailable(Exception):
    """ยกเว้นเมื่อข้อมูลไม่พอหรือโมเดล fit ไม่สำเร็จ พร้อมเหตุผลที่อ่านได้"""


@dataclass
class ForecastPoint:
    date: str
    predicted: float
    ci_lower: float
    ci_upper: float


def _load_daily_frame(contract_id: int, symbol: str) -> pd.DataFrame:
    """โหลดราคารายวัน + COT รายสัปดาห์ แล้ว forward-fill COT ให้ครบทุกวัน"""
    with engine.connect() as conn:
        px = pd.read_sql(text("""
            SELECT trade_date, close_px FROM price_daily
            WHERE symbol=:s ORDER BY trade_date
        """), conn, params={"s": symbol}, parse_dates=["trade_date"])

        cot = pd.read_sql(text("""
            SELECT report_date, mm_net, swap_net FROM cot_derived
            WHERE contract_id=:c ORDER BY report_date
        """), conn, params={"c": contract_id}, parse_dates=["report_date"])

    if px.empty:
        raise ForecastUnavailable("ยังไม่มีข้อมูลราคารายวันในระบบ — รัน run_ingest.py ก่อน")
    if cot.empty:
        raise ForecastUnavailable("ยังไม่มีข้อมูล COT ที่คำนวณแล้วในระบบ (cot_derived ว่าง)")

    px = px.set_index("trade_date").sort_index()
    cot = cot.set_index("report_date").sort_index()

    # forward-fill COT ลงบนปฏิทินรายวันของราคา — วันก่อนรายงาน COT ฉบับแรก
    # จะไม่มีค่า exogenous จึงต้องตัดทิ้ง (ไม่มีข้อมูลให้ forward-fill ย้อนหลัง)
    cot_daily = cot.reindex(px.index, method="ffill")
    frame = px.join(cot_daily).dropna()

    return frame


def _business_days_ahead(last_date: pd.Timestamp, n: int) -> list[pd.Timestamp]:
    """สร้างรายการวันทำการถัดไป n วัน ข้ามเสาร์-อาทิตย์"""
    return list(pd.bdate_range(start=last_date + pd.Timedelta(days=1), periods=n))


def fit_and_forecast(contract_id: int = 1, symbol: str = "XAUUSD",
                      horizon_days: int = 10,
                      order: tuple[int, int, int] = DEFAULT_ORDER) -> dict:
    """
    Fit ARIMAX (SARIMAX + exogenous) แล้วพยากรณ์ราคาล่วงหน้ารายวัน

    คำนวณสดทุกครั้งที่เรียก — ไม่ persist ผลลัพธ์ เพราะการ fit บนข้อมูลราคา
    รายวันหลักพันแถวใช้เวลาไม่กี่วินาที ไม่จำเป็นต้อง cache
    """
    frame = _load_daily_frame(contract_id, symbol)
    if len(frame) < MIN_OBSERVATIONS:
        raise ForecastUnavailable(
            f"มีข้อมูลที่ใช้ fit ได้เพียง {len(frame)} วัน (ต้องการอย่างน้อย "
            f"{MIN_OBSERVATIONS} วัน) — รอข้อมูล COT/ราคาสะสมเพิ่ม หรือรัน "
            "run_ingest.py ให้ครบก่อน"
        )

    endog = frame["close_px"].to_numpy(dtype=float)
    exog = frame[["mm_net", "swap_net"]].to_numpy(dtype=float)
    # ปรับสเกล exogenous ให้ใกล้เคียงราคา ป้องกันปัญหาตัวเลขต่างขนาดกันมาก
    # ทำให้ optimizer ของ SARIMAX ลู่เข้าได้ยากขึ้น (เช่น mm_net หลักหมื่นแสน
    # เทียบกับราคาทองหลักพัน) — หารด้วยส่วนเบี่ยงเบนมาตรฐานของแต่ละคอลัมน์
    exog_std = exog.std(axis=0)
    exog_std[exog_std == 0] = 1.0
    exog_scaled = exog / exog_std

    from statsmodels.tsa.statespace.sarimax import SARIMAX

    used_exog = True
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            model = SARIMAX(endog, exog=exog_scaled, order=order,
                             enforce_stationarity=False, enforce_invertibility=False)
            fitted = model.fit(disp=False)
        except Exception as e:                          # noqa: BLE001
            # ถ้า fit พร้อม exogenous ไม่ลู่เข้า (พบได้เมื่อข้อมูลสั้นหรือ
            # exogenous คงที่เป็นช่วงยาว) ให้ถอยไปใช้ ARIMA ธรรมดาแทน
            # และบอกผู้ใช้ตรง ๆ ว่าเกิด fallback ไม่ใช่แกล้งทำเป็นไม่มีปัญหา
            log.warning("ARIMAX fit ไม่สำเร็จ (%s) — ถอยไปใช้ ARIMA ไม่มี exogenous", e)
            used_exog = False
            model = SARIMAX(endog, order=order,
                             enforce_stationarity=False, enforce_invertibility=False)
            fitted = model.fit(disp=False)

    future_dates = _business_days_ahead(frame.index[-1], horizon_days)

    if used_exog:
        # ไม่รู้ค่า COT ในอนาคตจริง — ตรึงไว้เท่าค่าล่าสุดที่ทราบตลอดช่วงพยากรณ์
        # (naive persistence) เปิดเผยข้อจำกัดนี้ชัดเจนในผลลัพธ์ที่ส่งกลับ
        last_exog_scaled = exog_scaled[-1:].repeat(horizon_days, axis=0)
        fc = fitted.get_forecast(steps=horizon_days, exog=last_exog_scaled)
    else:
        fc = fitted.get_forecast(steps=horizon_days)

    mean = fc.predicted_mean
    ci = fc.conf_int(alpha=0.20)  # ช่วงความเชื่อมั่น 80%

    points = [
        ForecastPoint(
            date=str(d.date()),
            predicted=round(float(mean[i]), 2),
            ci_lower=round(float(ci[i, 0]), 2),
            ci_upper=round(float(ci[i, 1]), 2),
        )
        for i, d in enumerate(future_dates)
    ]

    return {
        "has_data": True,
        "symbol": symbol,
        "model": f"{'ARIMAX' if used_exog else 'ARIMA (fallback ไม่มี exogenous)'} "
                 f"order={order}",
        "exog_used": ["mm_net", "swap_net"] if used_exog else [],
        "last_actual_date": str(frame.index[-1].date()),
        "last_actual_price": round(float(endog[-1]), 2),
        "n_observations": len(frame),
        "horizon_days": horizon_days,
        "forecast": [p.__dict__ for p in points],
        "caveat": (
            "ตัวแปร COT ในช่วงพยากรณ์ถูกตรึงไว้เท่าค่าล่าสุดที่ทราบจริง "
            "(สมมติว่าไม่เปลี่ยนแปลง) เพราะไม่มีทางทราบค่า COT ในอนาคต "
            "ความแม่นยำจึงลดลงเมื่อพยากรณ์ไกลขึ้น ไม่ใช่คำแนะนำการลงทุน"
        ),
    }
