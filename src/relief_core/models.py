"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示企业或监管组织下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class RuleVersion:
    """描述资格规则的一个已发布或在审版本。"""

    rule_id: str
    series_id: str
    version: int
    status: str
    criteria: dict[str, Any]
    effective_date: str | None
    created_by: str
    approved_by: str | None
    published_by: str | None
    created_at: str
    approved_at: str | None
    published_at: str | None


@dataclass(frozen=True)
class QualificationSnapshot:
    """描述一个场所一个自然日结算后的资格结果，永不重算。"""

    snapshot_id: str
    site_id: str
    settlement_date: str
    rule_id: str
    eligible: bool
    consecutive_qualified_days: int
    factors: dict[str, Any]
    reasons: list[str]
    settled_by: str
    settled_at: str


@dataclass(frozen=True)
class WindowException:
    """描述针对免访窗口的一次受控例外（紧急突破/暂停/终止）。"""

    exception_id: str
    window_id: str
    site_id: str
    kind: str
    reason_code: str
    reason_text: str
    evidence: dict[str, Any]
    source_type: str
    source_ref: str
    decided_by: str
    decided_at: str
    lifted_at: str | None
    lift_note: str | None


@dataclass(frozen=True)
class ExemptionWindow:
    """描述有期限的免访窗口及其当前状态。"""

    window_id: str
    site_id: str
    start_date: str
    end_date: str
    next_review_date: str
    basis_snapshot_id: str
    status: str
    created_at: str
    exceptions: list[WindowException]


@dataclass(frozen=True)
class ReviewRequest:
    """描述企业针对窗口例外或资格结论发起的复核。"""

    review_id: str
    site_id: str
    window_id: str | None
    reason: str
    status: str
    created_by: str
    decided_by: str | None
    created_at: str
    decided_at: str | None
    decision_note: str | None


@dataclass(frozen=True)
class QualificationView:
    """面向查询的当前资格视图，附自然语言业务解释。"""

    site_id: str
    as_of_date: str
    eligible: bool
    window_status: str | None
    window_id: str | None
    window_end_date: str | None
    next_review_date: str | None
    basis_snapshot_id: str
    basis_rule_id: str
    basis_date: str
    explanation: list[str]
    exceptions: list[WindowException]
