"""SQLAlchemy engine — อ่านค่าเชื่อมต่อจาก environment"""
import os
from sqlalchemy import create_engine

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://goldcot:changeme@localhost:3306/goldcot?charset=utf8mb4",
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,   # กัน connection ค้างหลัง MySQL ตัดการเชื่อมต่อ
    pool_recycle=3600,
    future=True,
)
