
-- เพิ่มตาราง Gold Options Analysis 3 ตัว โดยไม่กระทบข้อมูลเดิมที่มีอยู่แล้ว
--
-- ไฟล์นี้จำเป็นเฉพาะกับฐานข้อมูลที่สร้างไว้ "ก่อน" ตารางเหล่านี้ถูกเพิ่มเข้า
-- schema.sql เท่านั้น — ถ้า clone โปรเจกต์นี้มาใหม่แล้วรัน db/schema.sql ตามปกติ
-- จะได้ตารางทั้ง 3 ตัวนี้ครบอยู่แล้ว "ไม่ต้อง" รันไฟล์นี้ซ้ำอีก
-- (ใส่ IF NOT EXISTS ไว้แล้ว รันซ้ำโดยไม่ได้ตั้งใจก็ไม่พังอะไร)
USE goldcot;

-- ---------------------------------------------------------------------
-- 5b. Gold Options Analysis — นำเข้าจาก "Gold Options Analysis Template"
--     (Barchart ไม่มี API ฟรี จึงนำเข้าด้วยมือจากไฟล์ xlsx ที่วิเคราะห์ไว้แล้ว)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS option_market_overview (
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

CREATE TABLE IF NOT EXISTS option_series_summary (
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

CREATE TABLE IF NOT EXISTS option_strike_detail (
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

