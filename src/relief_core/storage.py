"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qualification_rules (
    rule_id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft', 'approved', 'published')),
    criteria_json TEXT NOT NULL,
    effective_date TEXT,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    published_by TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    published_at TEXT,
    UNIQUE(series_id, version)
);
CREATE TABLE IF NOT EXISTS daily_reports (
    report_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    report_date TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    on_time INTEGER NOT NULL CHECK(on_time IN (0, 1)),
    quality TEXT NOT NULL CHECK(quality IN ('qualified', 'deficient')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, report_date)
);
CREATE TABLE IF NOT EXISTS hazards (
    hazard_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    hazard_key TEXT NOT NULL,
    title TEXT NOT NULL,
    found_date TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('general', 'major')),
    closed_date TEXT,
    rectification_count INTEGER NOT NULL CHECK(rectification_count >= 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, hazard_key)
);
CREATE TABLE IF NOT EXISTS inspections (
    inspection_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    inspection_key TEXT NOT NULL,
    inspection_date TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('pass', 'fail')),
    finding_count INTEGER NOT NULL CHECK(finding_count >= 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, inspection_key)
);
CREATE TABLE IF NOT EXISTS assistance_requests (
    assistance_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    assistance_key TEXT NOT NULL,
    request_date TEXT NOT NULL,
    topic TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, assistance_key)
);
CREATE TABLE IF NOT EXISTS serious_events (
    event_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    event_key TEXT NOT NULL,
    event_date TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('serious', 'emergency')),
    resolved_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, event_key)
);
CREATE TABLE IF NOT EXISTS qualification_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    settlement_date TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
    consecutive_qualified_days INTEGER NOT NULL CHECK(consecutive_qualified_days >= 0),
    factors_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    settled_by TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    UNIQUE(site_id, settlement_date)
);
CREATE TABLE IF NOT EXISTS snapshot_corrections (
    correction_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    settlement_date TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    fact_ref_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    consumed_by_snapshot_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, settlement_date, fact_type, fact_key)
);
CREATE TABLE IF NOT EXISTS exemption_windows (
    window_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    next_review_date TEXT NOT NULL,
    basis_snapshot_id TEXT NOT NULL REFERENCES qualification_snapshots(snapshot_id),
    status TEXT NOT NULL CHECK(status IN ('active', 'suspended', 'breached', 'terminated', 'expired')),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_single_open_window
    ON exemption_windows(site_id) WHERE status IN ('active', 'suspended', 'breached');
CREATE TABLE IF NOT EXISTS window_exceptions (
    exception_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES exemption_windows(window_id),
    site_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('emergency_breach', 'suspend', 'terminate')),
    reason_code TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    lifted_at TEXT,
    lift_note TEXT,
    UNIQUE(site_id, source_type, source_ref)
);
CREATE TABLE IF NOT EXISTS review_requests (
    review_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    window_id TEXT REFERENCES exemption_windows(window_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'upheld', 'resumed', 'adjusted')),
    created_by TEXT NOT NULL,
    decided_by TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decision_note TEXT
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._write_lock = threading.RLock()
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。

        单连接被多线程共享，进程内用锁串行化写事务；跨进程并发则由 SQLite
        的 BEGIN IMMEDIATE 写锁和各业务唯一约束共同保证不产生重复决定。
        """

        with self._write_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
