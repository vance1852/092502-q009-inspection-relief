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
CREATE TABLE IF NOT EXISTS rule_sets (
    rule_set_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('draft', 'approved', 'published')),
    parameters_json TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    predecessor_id TEXT REFERENCES rule_sets(rule_set_id),
    proposed_by TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS qualification_facts (
    fact_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    business_date TEXT NOT NULL,
    channel TEXT NOT NULL CHECK(channel IN
        ('daily_completion', 'hazard', 'inspection', 'assistance', 'incident')),
    event_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    late_correction INTEGER NOT NULL DEFAULT 0 CHECK(late_correction IN (0, 1)),
    UNIQUE(site_id, business_date, channel, event_key)
);
CREATE INDEX IF NOT EXISTS idx_qualification_facts_date
    ON qualification_facts(site_id, business_date);
CREATE TABLE IF NOT EXISTS daily_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    business_date TEXT NOT NULL,
    rule_set_id TEXT NOT NULL REFERENCES rule_sets(rule_set_id),
    streak_days INTEGER NOT NULL CHECK(streak_days >= 0),
    day_qualified INTEGER NOT NULL CHECK(day_qualified IN (0, 1)),
    metrics_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    corrections_json TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    UNIQUE(site_id, business_date)
);
CREATE TABLE IF NOT EXISTS fact_corrections (
    correction_id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL UNIQUE REFERENCES qualification_facts(fact_id),
    site_id TEXT NOT NULL,
    business_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'applied', 'dismissed')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    applied_by TEXT,
    applied_at TEXT,
    consumed_snapshot_id TEXT REFERENCES daily_snapshots(snapshot_id)
);
CREATE TABLE IF NOT EXISTS relief_windows (
    window_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES daily_snapshots(snapshot_id),
    rule_set_id TEXT NOT NULL REFERENCES rule_sets(rule_set_id),
    status TEXT NOT NULL CHECK(status IN ('active', 'suspended', 'terminated', 'expired')),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    next_review_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active_key TEXT,
    UNIQUE(site_id, active_key)
);
CREATE TABLE IF NOT EXISTS window_exceptions (
    exception_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES relief_windows(window_id),
    site_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN
        ('regulator_suspend', 'regulator_terminate', 'emergency_breakthrough',
         'review_reinstate', 'review_uphold')),
    reason_code TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decision_key TEXT NOT NULL,
    UNIQUE(window_id, kind, decision_key)
);
CREATE TABLE IF NOT EXISTS review_requests (
    review_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES relief_windows(window_id),
    site_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('requested', 'decided')),
    request_text TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decision TEXT CHECK(decision IS NULL OR decision IN ('reinstate', 'uphold')),
    decision_text TEXT,
    decided_by TEXT,
    decided_at TEXT,
    next_review_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_requests_window ON review_requests(window_id);
CREATE TABLE IF NOT EXISTS qualification_resets (
    reset_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    reset_date TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    window_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qualification_resets_site
    ON qualification_resets(site_id, reset_date);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        # 进程内串行化所有写事务与读操作，保证并发结算/重复事件不会交错产生两份决定。
        self.lock = threading.RLock()

    @contextmanager
    def locked(self) -> Iterator[sqlite3.Connection]:
        """在持有进程锁时读取数据库。"""

        with self.lock:
            yield self.connection

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交；整个事务持有进程锁。"""

        with self.lock:
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
