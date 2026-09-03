-- =====================================================================
-- GoldCOT Signal — MySQL 8 schema
-- charset: utf8mb4 / engine: InnoDB
-- =====================================================================

CREATE DATABASE IF NOT EXISTS goldcot
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;
USE goldcot;

-- ---------------------------------------------------------------------
-- 1. ทะเบียนสัญญาที่ติดตาม
-- ---------------------------------------------------------------------
CREATE TABLE contract (
  id            SMALLINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  cftc_code     VARCHAR(12)  NOT NULL,          -- '088691' = GOLD (COMEX)
  symbol        VARCHAR(16)  NOT NULL,          -- 'XAUUSD'
  display_name  VARCHAR(64)  NOT NULL,
  exchange      VARCHAR(32)  NOT NULL DEFAULT 'COMEX',
  is_active     TINYINT(1)   NOT NULL DEFAULT 1,
  created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_contract_cftc (cftc_code)
) ENGINE=InnoDB;

INSERT INTO contract (cftc_code, symbol, display_name, exchange)
VALUES ('088691', 'XAUUSD', 'Gold — COMEX 100 oz', 'COMEX')
ON DUPLICATE KEY UPDATE display_name = VALUES(display_name);

-- ---------------------------------------------------------------------
-- 2. COT ดิบ (Disaggregated, Futures + Options Combined)
--    ห้ามแก้หลังบันทึก ยกเว้น CFTC revise ซึ่งจะ upsert ทับ
-- ---------------------------------------------------------------------
CREATE TABLE cot_raw (
  id                BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  contract_id       SMALLINT UNSIGNED NOT NULL,
  report_date       DATE NOT NULL,              -- วันอังคารที่เก็บสถานะ
  published_at      DATE NULL,                  -- วันศุกร์ที่ CFTC เผยแพร่
  open_interest     INT NOT NULL,

  prod_long         INT NOT NULL DEFAULT 0,
  prod_short        INT NOT NULL DEFAULT 0,

  swap_long         INT NOT NULL DEFAULT 0,
  swap_short        INT NOT NULL DEFAULT 0,
  swap_spread       INT NOT NULL DEFAULT 0,

  mm_long           INT NOT NULL DEFAULT 0,
  mm_short          INT NOT NULL DEFAULT 0,
  mm_spread         INT NOT NULL DEFAULT 0,

  other_long        INT NOT NULL DEFAULT 0,
  other_short       INT NOT NULL DEFAULT 0,
  other_spread      INT NOT NULL DEFAULT 0,

  nonrept_long      INT NOT NULL DEFAULT 0,
  nonrept_short     INT NOT NULL DEFAULT 0,

  source            VARCHAR(64)  NOT NULL DEFAULT 'cftc_socrata',
  ingested_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,

  UNIQUE KEY uq_cot_raw (contract_id, report_date),
  KEY ix_cot_raw_date (report_date),
  CONSTRAINT fk_cot_raw_contract FOREIGN KEY (contract_id)
    REFERENCES contract(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 3. ราคาทองรายวัน + สแนปช็อตรายสัปดาห์ที่ align กับ report_date
-- ---------------------------------------------------------------------
CREATE TABLE price_daily (
  id          BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  symbol      VARCHAR(16) NOT NULL,
  trade_date  DATE NOT NULL,
  open_px     DECIMAL(12,3) NULL,
  high_px     DECIMAL(12,3) NULL,
  low_px      DECIMAL(12,3) NULL,
  close_px    DECIMAL(12,3) NOT NULL,
  volume      BIGINT NULL,
  UNIQUE KEY uq_price_daily (symbol, trade_date),
  KEY ix_price_daily_date (trade_date)
) ENGINE=InnoDB;

CREATE TABLE price_weekly (
  id           BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  symbol       VARCHAR(16) NOT NULL,
  report_date  DATE NOT NULL,                   -- ตรงกับ cot_raw.report_date
  close_px     DECIMAL(12,3) NOT NULL,          -- ปิดวันอังคาร
  ret_1w       DECIMAL(10,6) NULL,
  ret_4w       DECIMAL(10,6) NULL,
  pct_52w      DECIMAL(6,2)  NULL,              -- 0–100 เปอร์เซ็นไทล์ในกรอบ 52w
  UNIQUE KEY uq_price_weekly (symbol, report_date)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 4. ชั้นคำนวณ — Signal Engine อ่านจากตารางนี้เท่านั้น
-- ---------------------------------------------------------------------
CREATE TABLE cot_derived (
  id             BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  contract_id    SMALLINT UNSIGNED NOT NULL,
  report_date    DATE NOT NULL,

  -- Net position (long − short) เก็บเป็น net long เสมอ กันสับสนเรื่องเครื่องหมาย
  mm_net         INT NOT NULL,
  swap_net       INT NOT NULL,
  prod_net       INT NOT NULL,
  other_net      INT NOT NULL,

  -- Δ สัปดาห์ต่อสัปดาห์
  d_mm_net       INT NULL,
  d_mm_long      INT NULL,
  d_mm_short     INT NULL,
  d_swap_net     INT NULL,
  d_swap_long    INT NULL,
  d_swap_short   INT NULL,
  d_prod_short   INT NULL,
  d_oi           INT NULL,

  -- z-score ของ Δ เทียบ stdev 52 สัปดาห์
  z_mm_net       DECIMAL(8,4) NULL,
  z_mm_long      DECIMAL(8,4) NULL,
  z_swap_net     DECIMAL(8,4) NULL,
  z_swap_short   DECIMAL(8,4) NULL,
  z_prod_short   DECIMAL(8,4) NULL,

  -- บริบทระดับสะสม ใช้ประกอบ ไม่ใช้ตัดสิน
  mm_net_pct_3y  DECIMAL(6,2) NULL,
  mm_net_over_oi DECIMAL(8,5) NULL,

  computed_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,

  UNIQUE KEY uq_cot_derived (contract_id, report_date),
  CONSTRAINT fk_derived_contract FOREIGN KEY (contract_id)
    REFERENCES contract(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 5. Option Open Interest รายราคาใช้สิทธิ
-- ---------------------------------------------------------------------
CREATE TABLE option_oi (
  id            BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  contract_id   SMALLINT UNSIGNED NOT NULL,
  trade_date    DATE NOT NULL,
  expiry_date   DATE NOT NULL,
  strike        DECIMAL(12,2) NOT NULL,
  option_type   ENUM('C','P') NOT NULL,
  open_interest INT NOT NULL DEFAULT 0,
  d_oi          INT NULL,
  volume        INT NULL,
  UNIQUE KEY uq_option_oi (contract_id, trade_date, expiry_date, strike, option_type),
  KEY ix_option_oi_scan (contract_id, trade_date, expiry_date),
  CONSTRAINT fk_option_contract FOREIGN KEY (contract_id)
    REFERENCES contract(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 5b. Gold Options Analysis — นำเข้าจาก "Gold Options Analysis Template"
--     (Barchart ไม่มี API ฟรี จึงนำเข้าด้วยมือจากไฟล์ xlsx ที่วิเคราะห์ไว้แล้ว)
-- ---------------------------------------------------------------------
CREATE TABLE option_market_overview (
  id             BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  contract_id    SMALLINT UNSIGNED NOT NULL,
  trade_date     DATE NOT NULL,
  call_oi_total  INT NOT NULL,
  put_oi_total   INT NOT NULL,
  oi_pc_ratio    DECIMAL(6,3) NULL,
  macro_sentiment VARCHAR(64) NULL,          -- เช่น 'EXTREME BULLISH'
  UNIQUE KEY uq_option_overview (contract_id, trade_date),
  CONSTRAINT fk_opt_overview_contract FOREIGN KEY (contract_id)
    REFERENCES contract(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE option_series_summary (
  id                BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  contract_id       SMALLINT UNSIGNED NOT NULL,
  trade_date        DATE NOT NULL,
  series_code       VARCHAR(24) NOT NULL,     -- เช่น 'OGZ6', 'G5MQ6'
  futures_ref       DECIMAL(12,2) NULL,
  put_oi            INT NULL,
  call_oi           INT NULL,
  total_oi          INT NULL,
  oi_pc_ratio       DECIMAL(6,3) NULL,
  put_volume        INT NULL,
  call_volume       INT NULL,
  total_volume      INT NULL,
  vol_pc_ratio      DECIMAL(6,3) NULL,
  market_sentiment  VARCHAR(64) NULL,         -- เช่น 'Extreme Bullish'
  interpretation_th TEXT NULL,                -- บทวิเคราะห์ภาษาไทย (ถ้ามี)
  UNIQUE KEY uq_option_series (contract_id, trade_date, series_code),
  KEY ix_option_series_scan (contract_id, trade_date),
  CONSTRAINT fk_opt_series_contract FOREIGN KEY (contract_id)
    REFERENCES contract(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE option_strike_detail (
  id             BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  contract_id    SMALLINT UNSIGNED NOT NULL,
  trade_date     DATE NOT NULL,
  series_code    VARCHAR(24) NOT NULL,
  strike         DECIMAL(12,2) NOT NULL,
  futures_ref    DECIMAL(12,2) NULL,
  moneyness      VARCHAR(16) NULL,            -- 'ATM' | 'OTM Call' | 'OTM Put'
  put_oi         INT NULL,
  call_oi        INT NULL,
  put_volume     INT NULL,
  call_volume    INT NULL,
  dominant_side  VARCHAR(24) NULL,            -- 'Call Dominated' | 'Put Dominated'
  note_th        VARCHAR(500) NULL,           -- หมายเหตุกลยุทธ์/แนวรับแนวต้าน
  UNIQUE KEY uq_option_strike (contract_id, trade_date, series_code, strike),
  KEY ix_option_strike_scan (contract_id, trade_date),
  CONSTRAINT fk_opt_strike_contract FOREIGN KEY (contract_id)
    REFERENCES contract(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 6. กฎที่ปรับได้จากหน้าแอดมิน
-- ---------------------------------------------------------------------
CREATE TABLE signal_rule (
  id           INT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  signal_code  VARCHAR(8)  NOT NULL,            -- 'S1'..'S4', 'GLOBAL'
  version      INT UNSIGNED NOT NULL DEFAULT 1,
  is_active    TINYINT(1)  NOT NULL DEFAULT 1,
  params       JSON        NOT NULL,
  note         VARCHAR(255) NULL,
  updated_at   TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
                           ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_rule (signal_code, version)
) ENGINE=InnoDB;

INSERT INTO signal_rule (signal_code, params, note) VALUES
 ('GLOBAL', JSON_OBJECT(
    'z_threshold', 0.75,
    'min_contracts', 2000,
    'lookback_weeks', 52,
    'price_up_pct', 0.015,
    'price_high_pct', 80,
    'price_low_pct', 20),
  'ค่าเริ่มต้น — ควรจูนจากผล backtest'),
 ('S3', JSON_OBJECT('unwind_penalty', 1.20),
  'น้ำหนักถ่วงลบเมื่อคลายสถานะสองฝั่งพร้อมกัน')
ON DUPLICATE KEY UPDATE params = VALUES(params);

-- ---------------------------------------------------------------------
-- 7. ผลลัพธ์สัญญาณ
-- ---------------------------------------------------------------------
CREATE TABLE `signal` (
  id            BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  contract_id   SMALLINT UNSIGNED NOT NULL,
  report_date   DATE NOT NULL,
  signal_code   VARCHAR(8) NOT NULL,            -- S1 | S2 | S3 | S4 | NONE
  is_primary    TINYINT(1) NOT NULL DEFAULT 1,  -- S4 บันทึกเป็น 0 (ชั้นบริบท)
  direction     ENUM('BULLISH','BEARISH','WARNING','NEUTRAL') NOT NULL,
  bias_score    SMALLINT NOT NULL,              -- −100 .. +100
  confidence    TINYINT UNSIGNED NOT NULL,      -- 0 .. 100
  price_ref     DECIMAL(12,3) NULL,
  price_zone    ENUM('LOW','MID','HIGH') NULL,
  price_state   ENUM('UP','FLAT','DOWN') NULL,
  rationale     JSON NOT NULL,                  -- เหตุผลรายข้อ ใช้แสดงบนการ์ด
  rule_version  INT UNSIGNED NOT NULL DEFAULT 1,
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_signal (contract_id, report_date, signal_code),
  KEY ix_signal_recent (contract_id, report_date DESC),
  CONSTRAINT fk_signal_contract FOREIGN KEY (contract_id)
    REFERENCES contract(id) ON DELETE CASCADE
) ENGINE=InnoDB;


-- ---------------------------------------------------------------------
-- 8. บันทึกการทำงานของ ETL
-- ---------------------------------------------------------------------
CREATE TABLE job_run (
  id           BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  job_name     VARCHAR(48) NOT NULL,
  status       ENUM('RUNNING','SUCCESS','FAILED') NOT NULL DEFAULT 'RUNNING',
  rows_written INT NULL,
  message      TEXT NULL,
  started_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at  TIMESTAMP NULL,
  KEY ix_job_run (job_name, started_at DESC)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- 9. วิวสำหรับ endpoint /timeline — รวม 5 มิติบนเส้นเวลาเดียว
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_timeline AS
SELECT
  d.contract_id,
  d.report_date,
  p.close_px,
  p.ret_4w,
  p.pct_52w,
  d.mm_net,  d.swap_net,  d.prod_net,
  d.d_mm_net, d.d_mm_long, d.d_swap_net, d.d_swap_short, d.d_prod_short,
  d.z_mm_net, d.z_swap_net,
  d.mm_net_pct_3y,
  s.signal_code,
  s.direction,
  s.bias_score,
  s.confidence
FROM cot_derived d
JOIN contract c        ON c.id = d.contract_id
LEFT JOIN price_weekly p ON p.symbol = c.symbol AND p.report_date = d.report_date
LEFT JOIN `signal` s     ON s.contract_id = d.contract_id
                        AND s.report_date = d.report_date
                        AND s.is_primary  = 1;
