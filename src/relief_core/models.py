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
class RuleSet:
    """一套经审批发布、带生效日期的“无事不扰”资格规则。"""

    rule_set_id: str
    status: str
    parameters: dict[str, Any]
    effective_date: str
    predecessor_id: str | None
    proposed_by: str
    proposed_at: str
    approved_by: str | None
    approved_at: str | None
    published_at: str | None


@dataclass(frozen=True)
class QualificationFact:
    """汇入资格结算的一条业务事实（五个通道之一）。"""

    fact_id: str
    site_id: str
    business_date: str
    channel: str
    event_key: str
    payload: dict[str, Any]
    occurred_at: str
    recorded_at: str
    late_correction: bool


@dataclass(frozen=True)
class DailySnapshot:
    """按场所、按业务日封账的资格快照，不因新规则或迟到数据重算。"""

    snapshot_id: str
    site_id: str
    business_date: str
    rule_set_id: str
    streak_days: int
    day_qualified: bool
    metrics: dict[str, Any]
    reasons: list[dict[str, Any]]
    corrections: list[dict[str, Any]]
    inputs_hash: str
    sealed_at: str


@dataclass(frozen=True)
class ReliefWindow:
    """一张有期限的免访窗口及其当前状态。"""

    window_id: str
    site_id: str
    snapshot_id: str
    rule_set_id: str
    status: str
    start_date: str
    end_date: str
    next_review_date: str
    created_at: str


@dataclass(frozen=True)
class WindowException:
    """针对窗口的一次带证据、可幂等重放的例外处置。"""

    exception_id: str
    window_id: str
    site_id: str
    kind: str
    reason_code: str
    reason_text: str
    evidence: dict[str, Any]
    status: str
    actor_id: str
    created_at: str
    decision_key: str


@dataclass(frozen=True)
class ReviewRequest:
    """企业对窗口暂停/终止提出的复核申请及其结论。"""

    review_id: str
    window_id: str
    site_id: str
    status: str
    request_text: str
    requested_by: str
    requested_at: str
    decision: str | None
    decision_text: str | None
    decided_by: str | None
    decided_at: str | None
    next_review_date: str | None
