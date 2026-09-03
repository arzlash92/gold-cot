"""
Backtest harness — วัดว่ากฎ S1–S3 มีอำนาจทำนายจริงหรือไม่ และ threshold ชุดไหนดีที่สุด

วิธีวัด: หลังสัญญาณติดในสัปดาห์ t วัดผลตอบแทนราคาที่ t+1, t+4, t+8 สัปดาห์
แล้วเทียบกับ "ค่าเฉลี่ยของทุกสัปดาห์" (baseline) ส่วนต่างคือ edge
ถ้า edge ไม่ต่างจากศูนย์อย่างมีนัยสำคัญ แปลว่ากฎนั้นยังไม่มีค่าใช้งาน

การใช้งาน
---------
    # ข้อมูลจริง — CSV ต้องมีคอลัมน์:
    # report_date, close_px, mm_long, mm_short, swap_long, swap_short,
    # prod_short, open_interest
    python backtest.py --csv data/gold_cot_weekly.csv

    # โหมดตรวจสอบกลไก (ข้อมูลสังเคราะห์ ไม่ใช้ตัดสินใจจริง)
    python backtest.py --synthetic

ข้อควรระวังเรื่อง look-ahead: ทุกฟีเจอร์ (z-score, percentile, ret_4w)
คำนวณจากหน้าต่างที่ปิดที่สัปดาห์ปัจจุบันเท่านั้น ไม่มีข้อมูลอนาคตรั่วเข้ามา
ส่วนผลตอบแทนที่ใช้วัดผลเป็นของอนาคตล้วน จึงแยกขาดจากฟีเจอร์
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.signal_engine import RuleParams, WeekInput, evaluate_series  # noqa: E402

LOOKBACK = 52
HORIZONS = (1, 4, 8)


# ---------------------------------------------------------------------
# 1. ข้อมูล
# ---------------------------------------------------------------------
REQUIRED = ["report_date", "close_px", "mm_long", "mm_short",
            "swap_long", "swap_short", "prod_short", "open_interest"]


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise SystemExit(f"CSV ขาดคอลัมน์: {', '.join(missing)}")
    df["report_date"] = pd.to_datetime(df["report_date"])
    return df.sort_values("report_date").reset_index(drop=True)


def synth(n: int = 520, seed: int = 20260827) -> pd.DataFrame:
    """
    ข้อมูลสังเคราะห์สำหรับทดสอบว่าโค้ดทำงานถูก ไม่ใช่สำหรับหาค่า threshold
    สร้างโดยให้ position ตามหลังราคา (lag) จึงมีความสัมพันธ์อ่อน ๆ ฝังอยู่
    ผลลัพธ์ที่ได้จึงบอกได้แค่ว่า harness วัดค่าได้ ไม่ได้บอกว่าตลาดจริงเป็นแบบนี้
    """
    rng = np.random.default_rng(seed)
    px = 1500 + np.cumsum(rng.normal(3.2, 26, n))
    mom = pd.Series(px).pct_change(4).fillna(0).to_numpy()

    mm_long = 110_000 + np.cumsum(rng.normal(0, 5_500, n) + np.roll(mom, 1) * 260_000)
    mm_short = 55_000 + np.cumsum(rng.normal(0, 3_200, n) - np.roll(mom, 1) * 90_000)
    swap_short = 130_000 + np.cumsum(rng.normal(0, 4_800, n) + np.roll(mom, 1) * 190_000)
    swap_long = 25_000 + np.cumsum(rng.normal(0, 2_100, n))
    prod_short = 160_000 + np.cumsum(rng.normal(0, 4_000, n) + np.roll(mom, 2) * 120_000)

    dates = pd.date_range("2016-09-06", periods=n, freq="7D")
    return pd.DataFrame({
        "report_date": dates, "close_px": px.round(2),
        "mm_long": mm_long.clip(1000).round(), "mm_short": mm_short.clip(1000).round(),
        "swap_long": swap_long.clip(1000).round(), "swap_short": swap_short.clip(1000).round(),
        "prod_short": prod_short.clip(1000).round(),
        "open_interest": (mm_long + swap_short + prod_short).round(),
    })


# ---------------------------------------------------------------------
# 2. ฟีเจอร์ — ต้องตรงกับ etl.rebuild_derived()
# ---------------------------------------------------------------------
def derive(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["mm_net"] = d.mm_long - d.mm_short
    d["swap_net"] = d.swap_long - d.swap_short

    for col in ["mm_net", "mm_long", "swap_net", "swap_short", "prod_short"]:
        d["d_" + col] = d[col].diff()
        sd = d["d_" + col].rolling(LOOKBACK, min_periods=12).std()
        d["z_" + col] = (d["d_" + col] / sd).replace([np.inf, -np.inf], np.nan)

    d["ret_4w"] = d.close_px.pct_change(4)
    d["pct_52w"] = d.close_px.rolling(LOOKBACK, min_periods=12).apply(
        lambda x: (x < x.iloc[-1]).mean() * 100, raw=False)

    for h in HORIZONS:
        d[f"fwd_{h}w"] = d.close_px.shift(-h) / d.close_px - 1

    return d.dropna(subset=["z_mm_net", "pct_52w", "ret_4w"]).reset_index(drop=True)


# ---------------------------------------------------------------------
# 3. รันกฎ
# ---------------------------------------------------------------------
def run_rules(d: pd.DataFrame, p: RuleParams) -> pd.Series:
    weeks = [
        WeekInput(
            report_date=str(r.report_date.date()), close_px=float(r.close_px),
            ret_4w=float(r.ret_4w), pct_52w=float(r.pct_52w),
            d_mm_net=int(r.d_mm_net), d_mm_long=int(r.d_mm_long),
            d_swap_net=int(r.d_swap_net), d_swap_short=int(r.d_swap_short),
            d_prod_short=int(r.d_prod_short),
            z_mm_net=float(r.z_mm_net), z_mm_long=float(r.z_mm_long),
            z_swap_net=float(r.z_swap_net), z_swap_short=float(r.z_swap_short),
            z_prod_short=float(r.z_prod_short),
        )
        for r in d.itertuples()
    ]
    return pd.Series([s.signal_code for s in evaluate_series(weeks, p)], index=d.index)


# ---------------------------------------------------------------------
# 4. วัดผล
# ---------------------------------------------------------------------
# ทิศทางที่กฎแต่ละข้อ "ควร" ทำนาย — ใช้แปลง edge เป็นคะแนนถูกทาง
EXPECTED = {"S1": +1, "S2": -1, "S3": -1}


def score(d: pd.DataFrame, sig: pd.Series) -> pd.DataFrame:
    rows = []
    for code, want in EXPECTED.items():
        mask = sig == code
        n = int(mask.sum())
        for h in HORIZONS:
            fwd = d[f"fwd_{h}w"]
            sub = fwd[mask].dropna()
            base = fwd.dropna()
            if len(sub) < 3:
                rows.append({"signal": code, "h": h, "n": n, "edge": np.nan,
                             "t": np.nan, "hit": np.nan})
                continue
            edge = sub.mean() - base.mean()
            t = edge / (sub.std(ddof=1) / np.sqrt(len(sub))) if sub.std(ddof=1) else np.nan
            hit = (np.sign(sub) == want).mean()
            rows.append({"signal": code, "h": h, "n": n,
                         "edge": edge, "t": t, "hit": hit,
                         "edge_signed": edge * want})
    return pd.DataFrame(rows)


def summarize(res: pd.DataFrame) -> float:
    """คะแนนรวมหนึ่งตัวสำหรับจัดอันดับ: edge ถูกทางที่ 4 สัปดาห์ ถ่วงด้วยจำนวนสัญญาณ"""
    r4 = res[(res.h == 4) & res.edge.notna()]
    if r4.empty or (r4.n < 6).all():
        return -np.inf
    w = r4.n.clip(upper=40)
    return float((r4.edge_signed * w).sum() / w.sum() * 100)


# ---------------------------------------------------------------------
# 5. Grid search
# ---------------------------------------------------------------------
GRID = {
    "z_threshold":   [0.50, 0.75, 1.00, 1.25, 1.50],
    "min_contracts": [0, 1_000, 2_000, 4_000, 8_000],
    "price_high_pct": [70.0, 80.0, 90.0],
}


def grid_search(d: pd.DataFrame) -> pd.DataFrame:
    out = []
    keys = list(GRID)
    for combo in itertools.product(*(GRID[k] for k in keys)):
        p = RuleParams(**dict(zip(keys, combo)))
        sig = run_rules(d, p)
        res = score(d, sig)
        counts = sig.value_counts()
        out.append({
            **dict(zip(keys, combo)),
            "n_S1": int(counts.get("S1", 0)),
            "n_S2": int(counts.get("S2", 0)),
            "n_S3": int(counts.get("S3", 0)),
            "coverage": float((sig != "NONE").mean()),
            "score": summarize(res),
        })
    return pd.DataFrame(out).sort_values("score", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--out", default="backtest_results.csv")
    a = ap.parse_args()

    if a.csv:
        raw, label = load_csv(a.csv), Path(a.csv).name
    elif a.synthetic:
        raw, label = synth(), "ข้อมูลสังเคราะห์ (ตรวจกลไกเท่านั้น)"
    else:
        raise SystemExit("ระบุ --csv หรือ --synthetic")

    d = derive(raw)
    print(f"ชุดข้อมูล: {label}")
    print(f"ช่วงเวลา : {d.report_date.min().date()} ถึง {d.report_date.max().date()} "
          f"({len(d)} สัปดาห์)\n")

    print("── ค่าเริ่มต้นปัจจุบัน (z≥0.75, ≥2,000 สัญญา, เขตราคาสูง ≥80) ──")
    base_p = RuleParams()
    base_sig = run_rules(d, base_p)
    base_res = score(d, base_sig)
    print(base_sig.value_counts().to_string(), "\n")
    print(base_res.to_string(index=False,
          formatters={"edge": "{:+.2%}".format, "edge_signed": "{:+.2%}".format,
                      "t": "{:+.2f}".format, "hit": "{:.0%}".format}), "\n")

    for h in HORIZONS:
        print(f"baseline ผลตอบแทนเฉลี่ยทุกสัปดาห์ที่ +{h}w: "
              f"{d[f'fwd_{h}w'].mean():+.2%}")

    print("\n── Grid search (75 ชุดพารามิเตอร์) ──")
    g = grid_search(d)
    g.to_csv(a.out, index=False)
    print(g.head(10).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print(f"\nบันทึกผลทั้งหมดไว้ที่ {a.out}")


if __name__ == "__main__":
    main()
