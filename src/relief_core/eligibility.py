"""定义“无事不扰”资格规则的标准字段与按日评估逻辑。

规则只描述判断口径，不保存任何企业数据；评估函数是纯函数，便于在结算时
对历史日期使用历史规则重放（正式快照一旦写入不会被重算）。
"""

from __future__ import annotations

from datetime import date
from typing import Any


DEFAULT_CRITERIA: dict[str, Any] = {
    # 免访窗口时长与窗口内复核间隔。
    "window_duration_days": 30,
    "review_interval_days": 30,
    # 连续合格自查（按时且质量合格）的最低天数及窗口内按时合格率。
    "min_consecutive_qualified_days": 30,
    "qualified_ratio_days": 30,
    "required_qualified_ratio": 1.0,
    # 隐患闭环口径。
    "require_open_hazards_closed": True,
    "max_rectification_count": 1,
    "hazard_lookback_days": 60,
    # 历史检查口径。
    "no_fail_inspections": True,
    "max_inspection_findings": 0,
    "inspection_lookback_days": 180,
    # 企业主动求助只作正向参考，不设阻断门槛，仅限定回溯窗口。
    "assistance_lookback_days": 90,
    # 严重/紧急事件：回溯期内的严重事件一律阻断；未解除的紧急事件始终阻断。
    "serious_lookback_days": 365,
}

# 结算窗口至少回看多少天的每日自查，用于计算按时合格率。
MIN_DAILY_LOOKBACK_DAYS = 30

_INT_FIELDS = (
    "window_duration_days",
    "review_interval_days",
    "min_consecutive_qualified_days",
    "qualified_ratio_days",
    "max_rectification_count",
    "hazard_lookback_days",
    "max_inspection_findings",
    "inspection_lookback_days",
    "assistance_lookback_days",
    "serious_lookback_days",
)
_BOOL_FIELDS = ("require_open_hazards_closed", "no_fail_inspections")


def normalize_criteria(raw: dict[str, Any] | None) -> dict[str, Any]:
    """补齐默认值并校验规则口径，返回规范化后的字典。"""

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("规则口径必须是对象")
    criteria = dict(DEFAULT_CRITERIA)
    for key, value in raw.items():
        if key not in DEFAULT_CRITERIA:
            raise ValueError(f"未知规则字段: {key}")
        criteria[key] = value
    for field in _INT_FIELDS:
        value = criteria[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{field} 必须是非负整数")
    for field in _BOOL_FIELDS:
        if not isinstance(criteria[field], bool):
            raise ValueError(f"{field} 必须是布尔值")
    if criteria["window_duration_days"] < 1:
        raise ValueError("window_duration_days 至少为 1")
    if criteria["review_interval_days"] < 1:
        raise ValueError("review_interval_days 至少为 1")
    if criteria["min_consecutive_qualified_days"] < 1:
        raise ValueError("min_consecutive_qualified_days 至少为 1")
    if criteria["qualified_ratio_days"] < 1:
        raise ValueError("qualified_ratio_days 至少为 1")
    ratio = criteria["required_qualified_ratio"]
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0 < ratio <= 1:
        raise ValueError("required_qualified_ratio 必须在 (0, 1] 区间")
    criteria["required_qualified_ratio"] = float(ratio)
    return criteria


def _within(fact_date: str, settlement: date, lookback: int) -> bool:
    delta = settlement - date.fromisoformat(fact_date)
    return 0 <= delta.days <= lookback


def evaluate(criteria: dict[str, Any], *, settlement_date: date,
             consecutive_qualified_days: int, facts: dict[str, list[dict[str, Any]]]
             ) -> tuple[bool, list[str], dict[str, Any]]:
    """对单个场所单个结算日做纯函数评估。

    返回“是否合格、面向业务的原因列表、量化指标”。原因同时包含达标项与
    阻断项，供资格解释直接使用。
    """

    reasons: list[str] = []
    blockers: list[str] = []
    metrics: dict[str, Any] = {}

    # 合格率只看规则指定的最近窗口；连续天数单独按全量日序列计算。
    ratio_days = criteria["qualified_ratio_days"]
    daily = facts.get("daily", [])[-ratio_days:]
    total = len(daily)
    qualified = sum(1 for item in daily if item["on_time"] and item["quality"] == "qualified")
    on_time = sum(1 for item in daily if item["on_time"])
    deficient = total - qualified
    ratio = (qualified / total) if total else 0.0
    metrics.update({"daily_reports": total, "qualified_reports": qualified,
                    "on_time_reports": on_time, "deficient_or_late_reports": deficient,
                    "qualified_ratio": round(ratio, 4),
                    "consecutive_qualified_days": consecutive_qualified_days})
    if consecutive_qualified_days >= criteria["min_consecutive_qualified_days"]:
        reasons.append(f"连续 {consecutive_qualified_days} 天按时高质量自查，"
                       f"达到 {criteria['min_consecutive_qualified_days']} 天门槛")
    else:
        blockers.append(f"连续按时高质量自查仅 {consecutive_qualified_days} 天，"
                        f"未达 {criteria['min_consecutive_qualified_days']} 天门槛")
    if ratio >= criteria["required_qualified_ratio"]:
        reasons.append("结算窗口内每日自查全部按时且质量合格")
    else:
        blockers.append(f"近 {total} 天有 {deficient} 天迟报或质量不合格，"
                        f"合格率 {ratio:.0%} 低于要求的 {criteria['required_qualified_ratio']:.0%}")

    hazards = [h for h in facts.get("hazards", [])
               if _within(h["found_date"], settlement_date, criteria["hazard_lookback_days"])]
    open_hazards = [h for h in hazards if not h["closed_date"]]
    major_open = [h for h in open_hazards if h["severity"] == "major"]
    repeated = [h for h in hazards if h["rectification_count"] > criteria["max_rectification_count"]]
    metrics.update({"hazards_in_lookback": len(hazards), "open_hazards": len(open_hazards),
                    "major_open_hazards": len(major_open), "repeated_rectification_hazards": len(repeated)})
    if hazards and not open_hazards:
        reasons.append(f"近 {criteria['hazard_lookback_days']} 天发现的 {len(hazards)} 项隐患均已闭环")
    if criteria["require_open_hazards_closed"] and open_hazards:
        blockers.append(f"仍有 {len(open_hazards)} 项隐患未闭环"
                        + (f"，其中重大隐患 {len(major_open)} 项" if major_open else ""))
    if repeated:
        blockers.append(f"{len(repeated)} 项隐患反复整改超过 {criteria['max_rectification_count']} 次")

    inspections = [i for i in facts.get("inspections", [])
                   if _within(i["inspection_date"], settlement_date, criteria["inspection_lookback_days"])]
    failed = [i for i in inspections if i["result"] == "fail"]
    max_findings = max((i["finding_count"] for i in inspections), default=0)
    metrics.update({"inspections_in_lookback": len(inspections), "failed_inspections": len(failed),
                    "max_inspection_findings": max_findings})
    if inspections and not failed and max_findings <= criteria["max_inspection_findings"]:
        reasons.append(f"近 {criteria['inspection_lookback_days']} 天 {len(inspections)} 次历史检查均通过且无超标问题项")
    if criteria["no_fail_inspections"] and failed:
        blockers.append(f"近 {criteria['inspection_lookback_days']} 天存在 {len(failed)} 次检查不合格记录")
    if max_findings > criteria["max_inspection_findings"]:
        blockers.append(f"历史检查单次发现问题 {max_findings} 项，超过允许的 {criteria['max_inspection_findings']} 项")

    assists = [a for a in facts.get("assistances", [])
               if _within(a["request_date"], settlement_date, criteria["assistance_lookback_days"])]
    metrics["assistances_in_lookback"] = len(assists)
    if assists:
        reasons.append(f"近 {criteria['assistance_lookback_days']} 天主动求助 {len(assists)} 次，"
                       "合规配合意愿良好（求助本身不扣分）")

    serious = [e for e in facts.get("serious_events", [])
               if _within(e["event_date"], settlement_date, criteria["serious_lookback_days"])]
    unresolved_emergencies = [e for e in serious if e["severity"] == "emergency" and not e["resolved"]]
    metrics.update({"serious_events_in_lookback": len(serious),
                    "unresolved_emergencies": len(unresolved_emergencies)})
    if not serious:
        reasons.append(f"近 {criteria['serious_lookback_days']} 天无严重或紧急事件")
    if serious:
        blockers.append(f"近 {criteria['serious_lookback_days']} 天发生 {len(serious)} 起严重/紧急事件，"
                        "一票否决")
    if unresolved_emergencies:
        blockers.append(f"尚有 {len(unresolved_emergencies)} 起紧急事件未解除")

    eligible = not blockers
    return eligible, (reasons if eligible else reasons + blockers), metrics
