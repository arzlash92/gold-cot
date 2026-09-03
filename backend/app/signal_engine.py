"""
Signal engine — แปลงเงื่อนไข S1–S4 จากเอกสารต้นฉบับให้เป็นกฎเชิงตัวเลข

หลักการที่ยึดตลอดไฟล์นี้:
  1. ทุก position เก็บเป็น "net long" เสมอ  (net = long - short)
     ดังนั้น "Swap Dealer เพิ่ม net short" == d_swap_net < 0
  2. การเปลี่ยนแปลงต้องผ่านทั้งเกณฑ์ z-score และเกณฑ์จำนวนสัญญาขั้นต่ำ
     จึงจะนับว่า "มีนัยสำคัญ" — กันสัญญาณรบกวนรายสัปดาห์
  3. ลำดับความสำคัญเมื่อกฎชนกัน: S3 > S1 > S2 ส่วน S4 เป็นชั้นบริบท

โมดูลนี้ไม่แตะฐานข้อมูลเลย รับ dataclass เข้า คืน dataclass ออก
ทำให้เขียน unit test ได้โดยไม่ต้องมี MySQL
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Literal, Optional

Direction = Literal["BULLISH", "BEARISH", "WARNING", "NEUTRAL"]
PriceState = Literal["UP", "FLAT", "DOWN"]
PriceZone = Literal["LOW", "MID", "HIGH"]


# ---------------------------------------------------------------------
# พารามิเตอร์ — โหลดจากตาราง signal_rule ได้ ไม่ต้อง deploy ใหม่
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class RuleParams:
    z_threshold: float = 0.75        # |z| ขั้นต่ำที่ถือว่าเป็นการเคลื่อนไหวจริง
    min_contracts: int = 2_000       # กันกรณีตลาดนิ่งจน stdev เล็กผิดปกติ
    price_up_pct: float = 0.015      # ret_4w เกินเท่านี้ = ราคากำลังขึ้น
    price_high_pct: float = 80.0     # เปอร์เซ็นไทล์ 52w ที่ถือว่า "ราคาสูง"
    price_low_pct: float = 20.0
    unwind_penalty: float = 1.20     # น้ำหนักถ่วงลบเมื่อ S3 ติด

    w_mm_net: float = 0.50           # Managed Money = ผู้กำหนดทิศทางราคา
    w_swap_net: float = 0.25         # Swap Dealer = ฝ่ายรับความเสี่ยง ยืนยันโครงสร้าง
    w_prod_short: float = 0.10       # Producer = บริบทฝั่ง physical
    w_price_mom: float = 0.15
    score_scale: float = 35.0        # แปลง raw score เป็นช่วง -100..+100


# ---------------------------------------------------------------------
# ข้อมูลเข้าของหนึ่งสัปดาห์
# ---------------------------------------------------------------------
@dataclass
class WeekInput:
    report_date: str

    close_px: float
    ret_4w: float                    # เช่น 0.032 = +3.2%
    pct_52w: float                   # 0–100

    # Δ สัปดาห์ต่อสัปดาห์ (หน่วย: สัญญา)
    d_mm_net: int
    d_mm_long: int
    d_swap_net: int
    d_swap_short: int
    d_prod_short: int

    # z-score ของ Δ เทียบ stdev 52 สัปดาห์
    z_mm_net: float
    z_mm_long: float
    z_swap_net: float
    z_swap_short: float
    z_prod_short: float

    # ระดับสะสม ใช้ประกอบคำอธิบาย ไม่ใช้ตัดสิน
    mm_net: int = 0
    swap_net: int = 0
    prod_net: int = 0

    prev_direction: Optional[Direction] = None   # ใช้คิด streak
    streak_weeks: int = 0


@dataclass
class SignalResult:
    report_date: str
    signal_code: str                 # S1 | S2 | S3 | NONE
    direction: Direction
    bias_score: int                  # -100 .. +100
    confidence: int                  # 0 .. 100
    price_zone: PriceZone
    price_state: PriceState
    price_ref: float
    overlay: Optional[str] = None    # S4 ถ้ามี
    rationale: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------
# ตัวช่วย
# ---------------------------------------------------------------------
def _significant(delta: int, z: float, p: RuleParams) -> bool:
    """ผ่านทั้งสองด่านถึงจะนับว่าเป็นการเคลื่อนไหวจริง"""
    if delta is None or z is None:
        return False
    return abs(z) >= p.z_threshold and abs(delta) >= p.min_contracts


def _classify_price(w: WeekInput, p: RuleParams) -> tuple[PriceState, PriceZone]:
    if w.ret_4w > p.price_up_pct:
        state: PriceState = "UP"
    elif w.ret_4w < -p.price_up_pct:
        state = "DOWN"
    else:
        state = "FLAT"

    if w.pct_52w >= p.price_high_pct:
        zone: PriceZone = "HIGH"
    elif w.pct_52w <= p.price_low_pct:
        zone = "LOW"
    else:
        zone = "MID"
    return state, zone


def _fmt(n: int) -> str:
    """18400 -> '18.4k' อ่านบนการ์ดสัญญาณง่ายกว่าเลขเต็ม"""
    sign = "+" if n > 0 else ""
    if abs(n) >= 1000:
        return f"{sign}{n / 1000:.1f}k"
    return f"{sign}{n}"


# ---------------------------------------------------------------------
# กฎแต่ละข้อ — คืน (ติดหรือไม่, เหตุผลรายข้อ)
# ---------------------------------------------------------------------
def _check_s1(w, p, state, zone) -> tuple[bool, list[dict]]:
    """สัญญาณ 1: ราคาขึ้น + MM เพิ่ม net long + Swap เพิ่ม net short"""
    c1 = state == "UP"
    c2 = _significant(w.d_mm_net, w.z_mm_net, p) and w.d_mm_net > 0
    c3 = _significant(w.d_swap_net, w.z_swap_net, p) and w.d_swap_net < 0
    reasons = [
        {"ok": c1, "text": f"ราคา 4 สัปดาห์ {w.ret_4w:+.1%} — {'อยู่ในช่วงปรับขึ้น' if c1 else 'ยังไม่เข้าเกณฑ์ขาขึ้น'}"},
        {"ok": c2, "text": f"Managed Money net long {_fmt(w.d_mm_net)} สัญญา (z={w.z_mm_net:+.2f})"},
        {"ok": c3, "text": f"Swap Dealer net short {_fmt(-w.d_swap_net)} สัญญา (z={w.z_swap_net:+.2f})"},
    ]
    return (c1 and c2 and c3), reasons


def _check_s2(w, p, state, zone) -> tuple[bool, list[dict]]:
    """สัญญาณ 2: ราคายังสูงหรือทรงตัว แต่ MM เริ่มลด net long"""
    c1 = zone in ("HIGH", "MID") and state in ("UP", "FLAT")
    c2 = _significant(w.d_mm_net, w.z_mm_net, p) and w.d_mm_net < 0
    reasons = [
        {"ok": c1, "text": f"ราคายืนเปอร์เซ็นไทล์ {w.pct_52w:.0f} ของ 52 สัปดาห์ และยังไม่ปรับลง"},
        {"ok": c2, "text": f"Managed Money ลด net long {_fmt(w.d_mm_net)} สัญญา — แรงขับเคลื่อนหลักเริ่มถอย"},
    ]
    return (c1 and c2), reasons


def _check_s3(w, p, state, zone) -> tuple[bool, list[dict]]:
    """สัญญาณ 3: ราคายังสูง แต่ทั้ง MM และ Swap ปิดสถานะพร้อมกัน — Unwind"""
    c1 = zone == "HIGH"
    c2 = _significant(w.d_mm_long, w.z_mm_long, p) and w.d_mm_long < 0
    c3 = _significant(w.d_swap_short, w.z_swap_short, p) and w.d_swap_short < 0
    reasons = [
        {"ok": c1,
         "text": (f"ราคายังยืนระดับสูง — เปอร์เซ็นไทล์ {w.pct_52w:.0f} ของ 52 สัปดาห์"
                  if c1 else f"ราคาอยู่เปอร์เซ็นไทล์ {w.pct_52w:.0f} ยังไม่เข้าเขตราคาสูง")},
        {"ok": c2,
         "text": (f"Managed Money ปิด Long {_fmt(w.d_mm_long)} สัญญา"
                  if c2 else f"Managed Money ยังไม่ปิด Long อย่างมีนัยสำคัญ ({_fmt(w.d_mm_long)})")},
        {"ok": c3,
         "text": (f"Swap Dealer ปิด Short {_fmt(w.d_swap_short)} สัญญา"
                  if c3 else f"Swap Dealer ยังไม่ปิด Short อย่างมีนัยสำคัญ ({_fmt(w.d_swap_short)})")},
    ]
    return (c1 and c2 and c3), reasons


def _check_s4(w, p, state) -> Optional[dict]:
    """สัญญาณ 4: พฤติกรรม hedge ฝั่ง physical — เป็นชั้นบริบท ไม่ใช่สัญญาณเดี่ยว"""
    if not _significant(w.d_prod_short, w.z_prod_short, p):
        return None

    if w.d_prod_short > 0:
        text = (f"Producer เพิ่ม Short {_fmt(w.d_prod_short)} สัญญา — "
                + ("ทยอยล็อกกำไรฝั่ง physical มีแรงขายรออยู่เหนือตลาด"
                   if state == "UP" else "เพิ่มการป้องกันความเสี่ยงราคา"))
        tilt = -1
    else:
        text = (f"Producer ลด Short {_fmt(w.d_prod_short)} สัญญา — "
                + ("ถอน hedge ในช่วงราคาลง อาจมองว่าราคาต่ำพอจะหยุดล็อกแล้ว"
                   if state == "DOWN" else "คลายการป้องกันความเสี่ยง"))
        tilt = +1
    return {"text": text, "tilt": tilt}


# ---------------------------------------------------------------------
# คะแนนทิศทางและความเชื่อมั่น
# ---------------------------------------------------------------------
def _bias_score(w: WeekInput, p: RuleParams, s3_fired: bool) -> int:
    z_price = max(-3.0, min(3.0, w.ret_4w / 0.02))    # +2% ต่อ 4 สัปดาห์ ≈ 1.0

    raw = (p.w_mm_net * w.z_mm_net
           + p.w_swap_net * (-w.z_swap_net)
           + p.w_prod_short * (-w.z_prod_short)
           + p.w_price_mom * z_price)

    if s3_fired:
        # การคลายสถานะสองฝั่งพร้อมกันต้องกดคะแนนเป็นลบ แม้ z ดิบจะดูเป็นบวก
        raw -= p.unwind_penalty * (abs(w.z_mm_long) + abs(w.z_swap_short)) / 2

    return int(max(-100, min(100, round(raw * p.score_scale))))


def _confidence(w: WeekInput, p: RuleParams, reasons: list[dict]) -> int:
    passed = sum(1 for r in reasons if r.get("ok"))
    total = max(1, len(reasons))

    part_rules = 40 * passed / total

    zs = [abs(z) for z in (w.z_mm_net, w.z_swap_net, w.z_mm_long) if z is not None]
    mean_z = sum(zs) / len(zs) if zs else 0.0
    part_mag = 30 * min(1.0, mean_z / 2.0)

    part_streak = 30 * min(1.0, w.streak_weeks / 3.0)

    return int(round(min(100, part_rules + part_mag + part_streak)))


# ---------------------------------------------------------------------
# จุดเข้าใช้งานหลัก
# ---------------------------------------------------------------------
def evaluate(w: WeekInput, params: RuleParams | None = None) -> SignalResult:
    p = params or RuleParams()
    state, zone = _classify_price(w, p)

    s3_ok, s3_reasons = _check_s3(w, p, state, zone)
    s1_ok, s1_reasons = _check_s1(w, p, state, zone)
    s2_ok, s2_reasons = _check_s2(w, p, state, zone)

    # ลำดับความสำคัญ: S3 ชนะทุกกฎ เพราะเป็นโครงสร้างที่ปิดสถานะทั้งสองฝั่ง
    if s3_ok:
        code, direction, reasons = "S3", "BEARISH", s3_reasons
    elif s1_ok:
        code, direction, reasons = "S1", "BULLISH", s1_reasons
    elif s2_ok:
        code, direction, reasons = "S2", "WARNING", s2_reasons
    else:
        code, direction = "NONE", "NEUTRAL"
        # เมื่อไม่มีกฎใดติด ยังต้องบอกผู้ใช้ว่าขาดเงื่อนไขข้อไหน
        reasons = [r for r in (s3_reasons + s1_reasons) if not r["ok"]][:3]

    result = SignalResult(
        report_date=w.report_date,
        signal_code=code,
        direction=direction,
        bias_score=_bias_score(w, p, s3_ok),
        confidence=_confidence(w, p, reasons),
        price_zone=zone,
        price_state=state,
        price_ref=w.close_px,
        rationale=reasons,
    )

    s4 = _check_s4(w, p, state)
    if s4:
        result.overlay = s4["text"]
        result.rationale.append({"ok": True, "text": "🔵 " + s4["text"]})
        result.bias_score = int(max(-100, min(100, result.bias_score + 4 * s4["tilt"])))

    return result


def evaluate_series(weeks: list[WeekInput],
                    params: RuleParams | None = None) -> list[SignalResult]:
    """ประเมินทั้งอนุกรม พร้อมนับ streak ต่อเนื่องให้อัตโนมัติ"""
    out: list[SignalResult] = []
    prev_dir: Optional[Direction] = None
    streak = 0

    for w in weeks:
        w.prev_direction = prev_dir
        w.streak_weeks = streak
        r = evaluate(w, params)
        out.append(r)

        streak = streak + 1 if r.direction == prev_dir and r.direction != "NEUTRAL" else 1
        prev_dir = r.direction

    return out


# ---------------------------------------------------------------------
# ตรวจสอบเร็ว ๆ ด้วยสัปดาห์สังเคราะห์
# ---------------------------------------------------------------------
if __name__ == "__main__":
    cases = {
        "S1 ควรติด": WeekInput(
            report_date="2026-05-12", close_px=2410, ret_4w=0.045, pct_52w=88,
            d_mm_net=21000, d_mm_long=19000, d_swap_net=-17000, d_swap_short=16000,
            d_prod_short=5200, z_mm_net=1.9, z_mm_long=1.8, z_swap_net=-1.6,
            z_swap_short=1.5, z_prod_short=0.9, mm_net=182000),
        "S3 ควรติดและกดคะแนนเป็นลบ": WeekInput(
            report_date="2026-08-25", close_px=2588, ret_4w=0.004, pct_52w=91,
            d_mm_net=-18400, d_mm_long=-18400, d_swap_net=12100, d_swap_short=-12100,
            d_prod_short=-3100, z_mm_net=-1.7, z_mm_long=-1.7, z_swap_net=1.4,
            z_swap_short=-1.4, z_prod_short=-0.8, mm_net=141000),
        "ตลาดนิ่ง ไม่ควรมีสัญญาณ": WeekInput(
            report_date="2026-03-03", close_px=2190, ret_4w=0.003, pct_52w=52,
            d_mm_net=800, d_mm_long=600, d_swap_net=-400, d_swap_short=300,
            d_prod_short=200, z_mm_net=0.2, z_mm_long=0.15, z_swap_net=-0.1,
            z_swap_short=0.1, z_prod_short=0.05),
    }
    for name, w in cases.items():
        r = evaluate(w)
        print(f"\n=== {name} ===")
        print(f"{r.signal_code} · {r.direction} · bias {r.bias_score:+d} · conf {r.confidence}")
        for x in r.rationale:
            print(f"   {'✓' if x['ok'] else '·'} {x['text']}")
