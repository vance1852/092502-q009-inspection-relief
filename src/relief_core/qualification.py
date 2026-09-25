"""“无事不扰”资格规则与按日结算的纯函数引擎。

本模块不接触数据库与时钟：输入是规则参数、业务日、当日可见的事实集合以及
前一日快照，输出是确定性的当日指标、业务原因、连续合格天数与输入指纹。
旧快照是否重算、迟到事实是否可见，由服务层在调用前决定，引擎只负责结算。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from .audit import canonical_json, digest

# 五个汇入通道
CHANNEL_DAILY = "daily_completion"
CHANNEL_HAZARD = "hazard"
CHANNEL_INSPECTION = "inspection"
CHANNEL_ASSISTANCE = "assistance"
CHANNEL_INCIDENT = "incident"

QUALIFICATION_CHANNELS = frozenset([
    CHANNEL_DAILY, CHANNEL_HAZARD, CHANNEL_INSPECTION,
    CHANNEL_ASSISTANCE, CHANNEL_INCIDENT,
])

# 第一版规则的默认参数；新规则版本必须显式给出全部键，避免隐式漂移。
DEFAULT_PARAMETERS: dict[str, Any] = {
    "rule_version": 1,
    # 连续多少个合格日方具备免访资格
    "min_streak_days": 30,
    # 每日完成质量：最低分值，迟报或漏报当日即不合格
    "min_daily_score": 80,
    # 隐患闭环：回看天数、允许的未闭环隐患数、允许的整改轮次上限
    "hazard_lookback_days": 30,
    "max_open_hazards": 0,
    "max_rectification_rounds": 1,
    # 历史检查：回看天数内出现这些结论即不合格
    "inspection_lookback_days": 365,
    "blocking_inspection_results": ["major", "serious"],
    # 企业主动求助（正向信号）；默认不强制，开启后需在回看期内有求助记录
    "require_engagement": False,
    "engagement_lookback_days": 90,
    "min_assistance_signals": 1,
    # 严重事件：回看天数内出现这些等级即硬性阻断并清零连续天数
    "incident_block_days": 365,
    "incident_block_severities": ["major", "serious"],
    # 免访窗口期限与复核节奏
    "window_duration_days": 90,
    "review_interval_days": 30,
}

_PARAMETER_TYPES: dict[str, type | tuple[type, ...]] = {
    "rule_version": int,
    "min_streak_days": int,
    "min_daily_score": (int, float),
    "hazard_lookback_days": int,
    "max_open_hazards": int,
    "max_rectification_rounds": int,
    "inspection_lookback_days": int,
    "blocking_inspection_results": list,
    "require_engagement": bool,
    "engagement_lookback_days": int,
    "min_assistance_signals": int,
    "incident_block_days": int,
    "incident_block_severities": list,
    "window_duration_days": int,
    "review_interval_days": int,
}

# 监管人员可据以暂停窗口的明确例外
ALLOWED_SUSPEND_REASONS = frozenset([
    "targeted_tip",            # 指向明确的线索举报
    "periodic_review_finding", # 复核中发现的具体问题
    "high_risk_period",        # 重大活动/高风险时段保障
    "rectification_overdue",   # 承诺整改逾期未闭环
])

# 监管人员可据以终止窗口的明确例外
ALLOWED_TERMINATE_REASONS = frozenset([
    "major_violation_confirmed", # 查实重大违法
    "fraud_confirmed",           # 自查数据弄虚作假
    "qualification_revoked",     # 资格被依法撤销
])

EMERGENCY_REASON = "serious_incident_auto_breakthrough"


def parse_date(value: str) -> date:
    """解析 YYYY-MM-DD 业务日。"""

    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"业务日必须是 YYYY-MM-DD：{value!r}") from exc


def normalize_parameters(raw: dict[str, Any] | None) -> dict[str, Any]:
    """校验并补全一份规则参数，返回按键排序的稳定副本。"""

    if raw is None:
        parameters = dict(DEFAULT_PARAMETERS)
    else:
        if not isinstance(raw, dict):
            raise ValueError("规则参数必须是对象")
        missing = set(DEFAULT_PARAMETERS) - set(raw)
        unknown = set(raw) - set(DEFAULT_PARAMETERS)
        if missing:
            raise ValueError(f"规则参数缺少：{sorted(missing)}")
        if unknown:
            raise ValueError(f"规则参数不支持：{sorted(unknown)}")
        parameters = dict(raw)

    for key, expected_type in _PARAMETER_TYPES.items():
        if not isinstance(parameters[key], expected_type) or isinstance(parameters[key], bool) and expected_type is not bool:
            raise ValueError(f"规则参数 {key} 类型无效")
    bounds_int = [
        "min_streak_days", "hazard_lookback_days", "max_open_hazards",
        "max_rectification_rounds", "inspection_lookback_days",
        "engagement_lookback_days", "min_assistance_signals",
        "incident_block_days", "window_duration_days", "review_interval_days",
    ]
    for key in bounds_int:
        if parameters[key] < 0:
            raise ValueError(f"规则参数 {key} 不能为负")
    if parameters["window_duration_days"] < parameters["review_interval_days"]:
        raise ValueError("窗口期限不能短于复核间隔")
    for key in ("blocking_inspection_results", "incident_block_severities"):
        if not parameters[key] or not all(isinstance(v, str) and v for v in parameters[key]):
            raise ValueError(f"规则参数 {key} 必须是非空字符串列表")
    if not 0 <= parameters["min_daily_score"] <= 100:
        raise ValueError("min_daily_score 必须在 0 到 100 之间")
    # 列表内容规范化，保证指纹稳定
    parameters["blocking_inspection_results"] = sorted(parameters["blocking_inspection_results"])
    parameters["incident_block_severities"] = sorted(parameters["incident_block_severities"])
    return {key: parameters[key] for key in sorted(parameters)}


def latest_effective(rule_sets: Iterable[dict[str, Any]], business_date: date) -> dict[str, Any] | None:
    """在已发布规则中选出 business_date 当天生效的最新版本。"""

    candidates = [
        rule for rule in rule_sets
        if rule["status"] == "published" and parse_date(rule["effective_date"]) <= business_date
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda rule: (parse_date(rule["effective_date"]), rule["rule_set_id"]))


def _gate(gate: str, code: str, message: str, passed: bool, **detail: Any) -> dict[str, Any]:
    return {"gate": gate, "code": code, "passed": passed, "message": message, "detail": detail}


def _reason_message(code: str) -> str:
    return {
        "daily_ok": "当日按时完成自查且质量达标",
        "daily_missing": "当日未提交每日自查",
        "daily_late": "当日自查迟报，完成时间不计入连续合格",
        "daily_score": "当日自查得分低于规则要求",
        "daily_incomplete": "当日规定自查项未全部完成",
        "hazard_ok": "回看期内隐患均已闭环且无反复整改",
        "hazard_open_major": "回看期内存在未闭环的重大隐患",
        "hazard_open": "回看期内存在超过规则允许数量的未闭环隐患",
        "hazard_repeated": "回看期内存在反复整改（超过允许轮次）的隐患",
        "inspection_ok": "回看期内历史检查无阻断性结论",
        "inspection_blocking": "回看期内历史检查存在阻断性结论",
        "engagement_ok": "企业在回看期内有主动求助记录",
        "engagement_not_required": "本版规则未把主动求助设为硬性条件，仅作正向记录",
        "engagement_insufficient": "回看期内缺少企业主动求助记录",
        "incident_ok": "阻断期内无严重事件",
        "incident_blocking": "阻断期内发生严重事件，当日不合格并重新累计连续天数",
    }.get(code, code)


def evaluate(parameters: dict[str, Any], business_date: date,
             facts: list[dict[str, Any]]) -> dict[str, Any]:
    """结算单个业务日。facts 为当日“可见”的全部事实（含回看窗口内）。

    每个 fact 至少含 channel、event_key、business_date(字符串)、payload。
    返回 metrics、reasons、gates_passed。
    """

    reasons: list[dict[str, Any]] = []

    def in_window(lookback: int) -> list[dict[str, Any]]:
        start = business_date - _days(lookback - 1)
        return [
            fact for fact in facts
            if start <= parse_date(fact["business_date"]) <= business_date
        ]

    # ---- 每日完成质量 ----
    daily_facts = [f for f in facts
                   if f["channel"] == CHANNEL_DAILY and f["business_date"] == business_date.isoformat()]
    daily_metric: dict[str, Any] = {"submitted": False, "on_time": False, "score": None,
                                    "completion_ratio": None}
    if not daily_facts:
        reasons.append(_gate("daily_completion", "daily_missing", _reason_message("daily_missing"), False))
    else:
        payload = daily_facts[0]["payload"]
        completed = int(payload.get("completed_count", 0))
        required = int(payload.get("required_count", completed or 0))
        ratio = (completed / required) if required else 0.0
        on_time = bool(payload.get("on_time", False))
        score = payload.get("score")
        daily_metric.update({"submitted": True, "on_time": on_time, "score": score,
                             "completion_ratio": round(ratio, 4)})
        if not on_time:
            reasons.append(_gate("daily_completion", "daily_late", _reason_message("daily_late"), False,
                                 minutes_late=payload.get("minutes_late")))
        elif score is None or score < parameters["min_daily_score"]:
            reasons.append(_gate("daily_completion", "daily_score", _reason_message("daily_score"), False,
                                 score=score, required=parameters["min_daily_score"]))
        elif required == 0 or completed < required:
            reasons.append(_gate("daily_completion", "daily_incomplete",
                                 _reason_message("daily_incomplete"), False,
                                 completed=completed, required=required))
        else:
            reasons.append(_gate("daily_completion", "daily_ok", _reason_message("daily_ok"), True))

    # ---- 隐患闭环：同一隐患取窗口内最新状态 ----
    hazards: dict[str, dict[str, Any]] = {}
    for fact in sorted(in_window(parameters["hazard_lookback_days"]),
                       key=lambda f: (f["business_date"], f.get("recorded_at", ""))):
        if fact["channel"] == CHANNEL_HAZARD:
            hazards[fact["event_key"]] = fact
    open_hazards = [f for f in hazards.values() if f["payload"].get("status", "open") != "closed"]
    open_major = [f for f in open_hazards if f["payload"].get("level") in ("major", "serious")]
    repeated = [f for f in hazards.values()
                if int(f["payload"].get("rectification_rounds", 0)) > parameters["max_rectification_rounds"]]
    hazard_metric = {"tracked": len(hazards), "open": len(open_hazards),
                     "open_major": len(open_major), "repeated_rectification": len(repeated)}
    if open_major:
        reasons.append(_gate("hazard", "hazard_open_major", _reason_message("hazard_open_major"), False,
                             hazard_keys=sorted(f["event_key"] for f in open_major)))
    elif len(open_hazards) > parameters["max_open_hazards"]:
        reasons.append(_gate("hazard", "hazard_open", _reason_message("hazard_open"), False,
                             open=len(open_hazards), allowed=parameters["max_open_hazards"]))
    elif repeated:
        reasons.append(_gate("hazard", "hazard_repeated", _reason_message("hazard_repeated"), False,
                             hazard_keys=sorted(f["event_key"] for f in repeated)))
    else:
        reasons.append(_gate("hazard", "hazard_ok", _reason_message("hazard_ok"), True))

    # ---- 历史检查 ----
    inspections = in_window(parameters["inspection_lookback_days"])
    inspections = [f for f in inspections if f["channel"] == CHANNEL_INSPECTION]
    blocking_results = set(parameters["blocking_inspection_results"])
    blocking = [f for f in inspections if f["payload"].get("result") in blocking_results]
    inspection_metric = {"recent": len(inspections), "blocking": len(blocking),
                         "results": sorted({str(f["payload"].get("result")) for f in inspections})}
    if blocking:
        reasons.append(_gate("inspection", "inspection_blocking",
                             _reason_message("inspection_blocking"), False,
                             event_keys=sorted(f["event_key"] for f in blocking)))
    else:
        reasons.append(_gate("inspection", "inspection_ok", _reason_message("inspection_ok"), True))

    # ---- 企业主动求助（正向信号） ----
    assistance = [f for f in in_window(parameters["engagement_lookback_days"])
                  if f["channel"] == CHANNEL_ASSISTANCE]
    resolved = [f for f in assistance if f["payload"].get("status") == "resolved"]
    assistance_metric = {"signals": len(assistance), "resolved": len(resolved)}
    if parameters["require_engagement"]:
        if len(assistance) < parameters["min_assistance_signals"]:
            reasons.append(_gate("assistance", "engagement_insufficient",
                                 _reason_message("engagement_insufficient"), False,
                                 signals=len(assistance),
                                 required=parameters["min_assistance_signals"]))
        else:
            reasons.append(_gate("assistance", "engagement_ok", _reason_message("engagement_ok"), True))
    else:
        reasons.append(_gate("assistance", "engagement_not_required",
                             _reason_message("engagement_not_required"), True,
                             signals=len(assistance)))

    # ---- 严重事件（硬性阻断） ----
    incidents = [f for f in in_window(parameters["incident_block_days"])
                 if f["channel"] == CHANNEL_INCIDENT]
    block_severities = set(parameters["incident_block_severities"])
    blocking_incidents = [f for f in incidents if f["payload"].get("severity") in block_severities]
    incident_metric = {"recent": len(incidents), "blocking": len(blocking_incidents),
                       "latest_severity": max(
                           (str(f["payload"].get("severity")) for f in incidents), default=None)}
    if blocking_incidents:
        reasons.append(_gate("incident", "incident_blocking",
                             _reason_message("incident_blocking"), False,
                             event_keys=sorted(f["event_key"] for f in blocking_incidents)))
    else:
        reasons.append(_gate("incident", "incident_ok", _reason_message("incident_ok"), True))

    metrics = {
        "daily_completion": daily_metric,
        "hazard": hazard_metric,
        "inspection": inspection_metric,
        "assistance": assistance_metric,
        "incident": incident_metric,
    }
    gates_passed = all(reason["passed"] for reason in reasons)
    return {"metrics": metrics, "reasons": reasons, "gates_passed": gates_passed}


def _days(value: int):
    from datetime import timedelta

    return timedelta(days=value)


def next_streak(gates_passed: bool, previous_date: date | None,
                previous_streak: int, business_date: date) -> int:
    """只有业务日严格衔接上一份快照时才延续连续合格天数，否则从当日重新起算。"""

    if not gates_passed:
        return 0
    if previous_date is not None and previous_date == business_date - _days(1):
        return previous_streak + 1
    return 1


def settlement_inputs_hash(parameters: dict[str, Any], business_date: str,
                           visible_facts: list[dict[str, Any]],
                           previous_snapshot_id: str | None) -> str:
    """对结算所依据的规则版本、业务日和可见事实集合取指纹。"""

    material = {
        "parameters": parameters,
        "business_date": business_date,
        "previous_snapshot_id": previous_snapshot_id,
        "facts": sorted(
            ({"fact_id": f.get("fact_id"), "payload_hash": f.get("payload_hash"),
              "late_correction": bool(f.get("late_correction"))} for f in visible_facts),
            key=lambda item: item["fact_id"] or "",
        ),
    }
    return digest(material)


def parameters_fingerprint(parameters: dict[str, Any]) -> str:
    """规则参数内容指纹，便于审计比对不同版本。"""

    return digest(canonical_json(parameters))
