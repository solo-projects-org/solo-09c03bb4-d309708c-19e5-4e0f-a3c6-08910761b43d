"""SQLite 连接与建表。数据库路径用环境变量 APP_DB 覆盖，默认 data/app.db。

连接统一用 isolation_level=None（自动提交）：多语句操作必须用 tx() 显式
界定事务，避免每条 SELECT 各自自动提交、同一操作内读到不同已提交状态。
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

_DEFAULT_DB = str(Path(__file__).resolve().parent.parent / "data" / "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
    province   TEXT NOT NULL,
    month      TEXT NOT NULL,              -- YYYY-MM
    meter_no   TEXT NOT NULL,
    kwh        REAL NOT NULL CHECK (kwh >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (province, month, meter_no)
);

CREATE TABLE IF NOT EXISTS emission_factors (
    scope      TEXT NOT NULL CHECK (scope IN ('province', 'national')),
    province   TEXT NOT NULL DEFAULT '',   -- 全国因子为空串
    year       INTEGER NOT NULL,
    factor     REAL NOT NULL CHECK (factor > 0),  -- kg CO2 / kWh
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, province, year)
);

CREATE TABLE IF NOT EXISTS previews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    province    TEXT NOT NULL,
    month       TEXT NOT NULL,
    payload     TEXT NOT NULL,             -- 规范 JSON：当期用电明细 + 所用因子来源/数值
    publishable INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    province        TEXT NOT NULL,
    month           TEXT NOT NULL,
    preview_id      INTEGER NOT NULL REFERENCES previews(id),
    total_kwh       REAL NOT NULL,
    total_emissions REAL NOT NULL,         -- kg CO2
    published_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_details (
    publication_id INTEGER NOT NULL REFERENCES publications(id),
    meter_no       TEXT NOT NULL,
    kwh            REAL NOT NULL,
    factor_source  TEXT NOT NULL,          -- 'province' | 'national'
    factor_value   REAL NOT NULL,
    factor_year    INTEGER NOT NULL,
    emissions      REAL NOT NULL,          -- kg CO2
    PRIMARY KEY (publication_id, meter_no)
);

CREATE INDEX IF NOT EXISTS idx_publications_period ON publications(province, month, id);
"""


def db_path() -> str:
    return os.environ.get("APP_DB", _DEFAULT_DB)


def get_conn() -> sqlite3.Connection:
    # timeout=30：遇到写锁时最多等待 30 秒（WAL 下读不阻塞写）。
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection, mode: str = "IMMEDIATE"):
    """显式事务：事务内所有读看到同一个已提交快照。

    IMMEDIATE（默认）在 BEGIN 时即取得写锁，事务提交前其他连接的写只能等待，
    用于「读后写」的校验-入库序列（预览、确认发布及一切多语句写）。
    DEFERRED 用于纯读操作的一致性快照（已发布查询）。
    """
    conn.execute(f"BEGIN {mode}")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def init_db() -> None:
    Path(db_path()).parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
