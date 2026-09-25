"""实现“无事不扰”资格服务：规则审批、按日结算、免访窗口与复核。

关键不变量：
- 资格快照按 (场所, 自然日) 唯一且不可变，新规则不重算旧快照；
- 封账线之后到达的历史事实只进入更正表，在下一次结算时计入；
- 紧急事件即时把窗口置为 breached，但不改动原资格快照；
- 暂停/终止只能引用受控例外码并携带证据；
- 所有写操作幂等，全部在 IMMEDIATE 事务内完成并写入审计链。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from .audit import append_event, canonical_json
from .clock import SystemClock
from .eligibility import MIN_DAILY_LOOKBACK_DAYS, evaluate, normalize_criteria
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import (
    ExemptionWindow,
    QualificationSnapshot,
    QualificationView,
    ReviewRequest,
    RuleVersion,
    WindowException,
)
from .service import DomainService
from .storage import Database

# 监管人员暂停/终止窗口时允许引用的明确例外码。
EXCEPTION_REASON_CODES: dict[str, str] = {
    "EMERGENCY_DISPOSAL": "突发事件应急处置需要现场核查",
    "MAJOR_HAZARD_VERIFIED": "核实存在重大隐患，需要现场督促整改",
    "PUBLIC_TIP_VERIFIED": "群众举报经初步核实需要现场检查",
    "SPECIAL_CAMPAIGN": "上级部署的专项执法行动",
    "REPEATED_RECTIFICATION_VERIFIED": "隐患反复整改属实，需要现场复核",
}

QUALITIES = frozenset({"qualified", "deficient"})
HAZARD_SEVERITIES = frozenset({"general", "major"})
EVENT_SEVERITIES = frozenset({"serious", "emergency"})
INSPECTION_RESULTS = frozenset({"pass", "fail"})
_FACT_TYPES = frozenset({"daily", "hazard", "hazard_close", "inspection",
                         "assistance", "serious", "serious_resolve"})


class QualificationService(DomainService):
    """在基础档案服务之上提供资格结算与免访窗口管理。"""

    def __init__(self, database: Database, clock=None) -> None:
        super().__init__(database, clock or SystemClock())

    # ------------------------------------------------------------------ 工具

    def _today(self) -> date:
        return self.clock.now().date()

    def _date(self, value: str, field: str) -> date:
        try:
            parsed = date.fromisoformat(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期") from exc
        return parsed

    def _timestamp(self, value: str | None, field: str, default_day: date | None = None) -> str:
        if value is None:
            if default_day is None:
                raise ValidationError(f"{field} 不能为空")
            return f"{default_day.isoformat()}T18:00:00Z"
        text = str(value).strip()
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        return text

    def _site_row(self, connection, site_id: str, actor):
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")
        return site

    def _closed_line(self, connection, site_id: str) -> date | None:
        """封账线：该场所最近一次已结算日期。"""

        row = connection.execute(
            "SELECT MAX(settlement_date) AS d FROM qualification_snapshots WHERE site_id=?",
            (site_id,),
        ).fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    def _correction(self, connection, site_id: str, closed_line: date, fact_type: str,
                    fact_key: str):
        return connection.execute(
            "SELECT * FROM snapshot_corrections WHERE site_id=? AND settlement_date=? "
            "AND fact_type=? AND fact_key=?",
            (site_id, closed_line.isoformat(), fact_type, fact_key)).fetchone()

    def _route_fact(self, connection, *, site_id: str, fact_type: str, fact_key: str,
                    fact_date: date, payload: dict[str, Any],
                    insert_sql: tuple[str, tuple], resource_type: str,
                    resource_id: str) -> tuple[str, str, dict[str, Any]]:
        """按封账线写入正式事实表或更正表。

        同一业务键携带相同内容重放为已存在结果；内容不同则冲突，更正不可改写。
        """

        if fact_type not in _FACT_TYPES:
            raise ValidationError("未知事实类型")
        closed_line = self._closed_line(connection, site_id)
        if closed_line is not None and fact_date <= closed_line:
            stored = self._correction(connection, site_id, closed_line, fact_type, fact_key)
            if stored is not None:
                if stored["payload_json"] != canonical_json(payload):
                    raise ConflictError("同一业务键已经登记不同内容（更正流程中不可改写）")
                return "snapshot_correction", stored["correction_id"], {
                    "correction_id": stored["correction_id"], "replayed": True}
            correction_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO snapshot_corrections(correction_id,site_id,settlement_date,fact_type,"
                "fact_key,fact_ref_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (correction_id, site_id, closed_line.isoformat(), fact_type, fact_key, resource_id,
                 canonical_json(payload), self._now()))
            return "snapshot_correction", correction_id, {
                "correction_id": correction_id, "settlement_date": closed_line.isoformat(),
                "replayed": False}
        sql, params = insert_sql
        connection.execute(sql, params)
        return resource_type, resource_id, {resource_type + "_id": resource_id, "replayed": False}

    def _ensure_absent(self, connection, table: str, site_id: str, key_column: str,
                       key_value: str) -> None:
        row = connection.execute(
            f"SELECT 1 FROM {table} WHERE site_id=? AND {key_column}=?", (site_id, key_value)
        ).fetchone()
        if row is not None:
            raise ConflictError("同一业务键已经登记，不同内容请走更正流程")

    def _criteria(self, raw) -> dict[str, Any]:
        try:
            return normalize_criteria(raw)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    # ------------------------------------------------------------ 规则生命周期

    def create_rule_draft(self, *, request_id: str, actor_id: str,
                          criteria: dict[str, Any] | None = None,
                          series_id: str | None = None) -> object:
        criteria = self._criteria(criteria)
        series_id = self._identifier(series_id or ("rule-" + uuid.uuid4().hex[:12]), "series_id")
        payload = {"actor_id": actor_id, "series_id": series_id, "criteria": criteria}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                rule_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO qualification_rules(rule_id,series_id,version,status,criteria_json,"
                    "effective_date,created_by,created_at) VALUES(?,?,1,'draft',?,?,?,?)",
                    (rule_id, series_id, canonical_json(criteria), None, actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="rule.drafted",
                             resource_type="qualification_rule", resource_id=rule_id,
                             detail={"series_id": series_id, "version": 1, "criteria": criteria},
                             occurred_at=self._now())
                return "qualification_rule", rule_id, {"rule_id": rule_id, "series_id": series_id,
                                                       "version": 1, "status": "draft"}

            return self._idempotent(connection, request_id=request_id, action="create_rule_draft",
                                    payload=payload, create=create)

    def revise_rule(self, *, request_id: str, actor_id: str, series_id: str,
                    criteria: dict[str, Any]) -> object:
        criteria = self._criteria(criteria)
        payload = {"actor_id": actor_id, "series_id": series_id, "criteria": criteria}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            series_id = self._identifier(series_id, "series_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                rows = connection.execute(
                    "SELECT * FROM qualification_rules WHERE series_id=? ORDER BY version",
                    (series_id,)).fetchall()
                if not rows:
                    raise NotFoundError("规则系列不存在")
                if any(row["status"] == "draft" for row in rows):
                    raise ConflictError("该系列已有待审批草稿")
                version = rows[-1]["version"] + 1
                rule_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO qualification_rules(rule_id,series_id,version,status,"
                    "criteria_json,effective_date,created_by,created_at) "
                    "VALUES(?,?,?,'draft',?,?,?,?)",
                    (rule_id, series_id, version, canonical_json(criteria), None,
                     actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="rule.drafted",
                             resource_type="qualification_rule", resource_id=rule_id,
                             detail={"series_id": series_id, "version": version,
                                     "criteria": criteria}, occurred_at=self._now())
                return "qualification_rule", rule_id, {"rule_id": rule_id, "series_id": series_id,
                                                       "version": version, "status": "draft"}

            return self._idempotent(connection, request_id=request_id, action="revise_rule",
                                    payload=payload, create=create)

    def approve_rule(self, *, request_id: str, actor_id: str, rule_id: str) -> object:
        payload = {"actor_id": actor_id, "rule_id": rule_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            rule_id = self._identifier(rule_id, "rule_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM qualification_rules WHERE rule_id=?", (rule_id,)).fetchone()
                if row is None:
                    raise NotFoundError("规则版本不存在")
                if row["status"] != "draft":
                    raise ConflictError("只有草稿规则可以提交审批通过")
                connection.execute(
                    "UPDATE qualification_rules SET status='approved',approved_by=?,approved_at=? "
                    "WHERE rule_id=?", (actor_id, self._now(), rule_id))
                append_event(connection, actor_id=actor_id, action="rule.approved",
                             resource_type="qualification_rule", resource_id=rule_id,
                             detail={"series_id": row["series_id"], "version": row["version"]},
                             occurred_at=self._now())
                return "qualification_rule", rule_id, {"rule_id": rule_id, "status": "approved"}

            return self._idempotent(connection, request_id=request_id, action="approve_rule",
                                    payload=payload, create=create)

    def publish_rule(self, *, request_id: str, actor_id: str, rule_id: str,
                     effective_date: str) -> object:
        effective = self._date(effective_date, "effective_date")
        payload = {"actor_id": actor_id, "rule_id": rule_id,
                   "effective_date": effective.isoformat()}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            rule_id = self._identifier(rule_id, "rule_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM qualification_rules WHERE rule_id=?", (rule_id,)).fetchone()
                if row is None:
                    raise NotFoundError("规则版本不存在")
                if row["status"] != "approved":
                    raise ConflictError("只有审批通过的规则可以发布")
                if effective < self._today():
                    raise ValidationError("生效日期不能早于发布日，规则不追溯既往")
                connection.execute(
                    "UPDATE qualification_rules SET status='published',effective_date=?,"
                    "published_by=?,published_at=? WHERE rule_id=?",
                    (effective.isoformat(), actor_id, self._now(), rule_id))
                append_event(connection, actor_id=actor_id, action="rule.published",
                             resource_type="qualification_rule", resource_id=rule_id,
                             detail={"series_id": row["series_id"], "version": row["version"],
                                     "effective_date": effective.isoformat()},
                             occurred_at=self._now())
                return "qualification_rule", rule_id, {"rule_id": rule_id, "status": "published",
                                                       "effective_date": effective.isoformat()}

            return self._idempotent(connection, request_id=request_id, action="publish_rule",
                                    payload=payload, create=create)

    def get_rule(self, rule_id: str) -> RuleVersion:
        row = self.database.connection.execute(
            "SELECT * FROM qualification_rules WHERE rule_id=?", (rule_id,)).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        return self._rule_from_row(row)

    def list_rules(self, series_id: str | None = None) -> list[RuleVersion]:
        if series_id:
            rows = self.database.connection.execute(
                "SELECT * FROM qualification_rules WHERE series_id=? ORDER BY version",
                (series_id,)).fetchall()
        else:
            rows = self.database.connection.execute(
                "SELECT * FROM qualification_rules ORDER BY series_id, version").fetchall()
        return [self._rule_from_row(row) for row in rows]

    def _rule_from_row(self, row) -> RuleVersion:
        return RuleVersion(row["rule_id"], row["series_id"], row["version"], row["status"],
                           json.loads(row["criteria_json"]), row["effective_date"],
                           row["created_by"], row["approved_by"], row["published_by"],
                           row["created_at"], row["approved_at"], row["published_at"])

    def _effective_rule(self, connection, day: date):
        row = connection.execute(
            "SELECT * FROM qualification_rules WHERE status='published' AND effective_date<=? "
            "ORDER BY effective_date DESC, version DESC LIMIT 1", (day.isoformat(),)).fetchone()
        if row is None:
            raise ConflictError(f"{day.isoformat()} 没有已生效的资格规则，无法结算")
        return row

    # ------------------------------------------------------------- 事实登记

    def record_daily_report(self, *, request_id: str, actor_id: str, site_id: str,
                            report_date: str, quality: str,
                            due_at: str | None = None,
                            submitted_at: str | None = None) -> object:
        day = self._date(report_date, "report_date")
        if quality not in QUALITIES:
            raise ValidationError("quality 必须是 qualified 或 deficient")
        if day > self._today():
            raise ValidationError("不能预登记未来日期的自查")
        submitted_value = self._timestamp(submitted_at, "submitted_at", None) if submitted_at else self._now()
        due_value = self._timestamp(due_at, "due_at", day)
        on_time = submitted_value <= due_value
        fact_payload = {"report_date": day.isoformat(), "submitted_at": submitted_value,
                        "due_at": due_value, "on_time": on_time, "quality": quality}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._site_row(connection, site_id, actor)
            report_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                self._ensure_absent(connection, "daily_reports", site_id, "report_date",
                                    day.isoformat())
                result = self._route_fact(
                    connection, site_id=site_id, fact_type="daily", fact_key=day.isoformat(),
                    fact_date=day, payload=fact_payload,
                    insert_sql=(
                        "INSERT INTO daily_reports(report_id,site_id,report_date,submitted_at,"
                        "due_at,on_time,quality,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (report_id, site_id, day.isoformat(), submitted_value, due_value,
                         int(on_time), quality, actor_id, self._now())),
                    resource_type="daily_report", resource_id=report_id)
                append_event(connection, actor_id=actor_id,
                             action="daily_report.corrected"
                             if result[0] == "snapshot_correction" else "daily_report.recorded",
                             resource_type=result[0], resource_id=result[1],
                             detail={"site_id": site_id, "report_date": day.isoformat(),
                                     "on_time": on_time, "quality": quality},
                             occurred_at=self._now())
                return result

            return self._idempotent(connection, request_id=request_id,
                                    action="record_daily_report",
                                    payload={"actor_id": actor_id, "site_id": site_id,
                                             **fact_payload}, create=create)

    def record_hazard(self, *, request_id: str, actor_id: str, site_id: str, hazard_key: str,
                      title: str, found_date: str, severity: str, rectification_count: int = 0,
                      closed_date: str | None = None) -> object:
        found = self._date(found_date, "found_date")
        if found > self._today():
            raise ValidationError("不能登记未来发现日期的隐患")
        if severity not in HAZARD_SEVERITIES:
            raise ValidationError("severity 必须是 general 或 major")
        if not isinstance(rectification_count, int) or rectification_count < 0:
            raise ValidationError("rectification_count 必须是非负整数")
        closed_value = None
        if closed_date is not None:
            closed = self._date(closed_date, "closed_date")
            if closed < found:
                raise ValidationError("闭环日期不能早于发现日期")
            closed_value = closed.isoformat()
        title = self._text(title, "title")
        hazard_key = self._identifier(hazard_key, "hazard_key")
        fact_payload = {"hazard_key": hazard_key, "title": title,
                        "found_date": found.isoformat(), "severity": severity,
                        "rectification_count": rectification_count, "closed_date": closed_value}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._site_row(connection, site_id, actor)
            hazard_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                self._ensure_absent(connection, "hazards", site_id, "hazard_key", hazard_key)
                result = self._route_fact(
                    connection, site_id=site_id, fact_type="hazard", fact_key=hazard_key,
                    fact_date=found, payload=fact_payload,
                    insert_sql=(
                        "INSERT INTO hazards(hazard_id,site_id,hazard_key,title,found_date,"
                        "severity,closed_date,rectification_count,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (hazard_id, site_id, hazard_key, title, found.isoformat(), severity,
                         closed_value, rectification_count, actor_id, self._now())),
                    resource_type="hazard", resource_id=hazard_id)
                append_event(connection, actor_id=actor_id,
                             action="hazard.corrected"
                             if result[0] == "snapshot_correction" else "hazard.recorded",
                             resource_type=result[0], resource_id=result[1],
                             detail={"site_id": site_id, "hazard_key": hazard_key,
                                     "severity": severity, "closed_date": closed_value},
                             occurred_at=self._now())
                return result

            return self._idempotent(connection, request_id=request_id, action="record_hazard",
                                    payload={"actor_id": actor_id, "site_id": site_id,
                                             **fact_payload}, create=create)

    def close_hazard(self, *, request_id: str, actor_id: str, site_id: str, hazard_key: str,
                     closed_date: str, rectification_count: int | None = None) -> object:
        closed = self._date(closed_date, "closed_date")
        if closed > self._today():
            raise ValidationError("闭环日期不能是未来日期")
        hazard_key = self._identifier(hazard_key, "hazard_key")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._site_row(connection, site_id, actor)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM hazards WHERE site_id=? AND hazard_key=?",
                    (site_id, hazard_key)).fetchone()
                correction_source = None
                if row is None:
                    closed_line = self._closed_line(connection, site_id)
                    if closed_line is None:
                        raise NotFoundError("隐患不存在（未登记的隐患请先登记）")
                    correction_source = self._correction(connection, site_id, closed_line,
                                                         "hazard", hazard_key)
                    if correction_source is None:
                        raise NotFoundError("隐患不存在（未登记的隐患请先登记）")
                    base = json.loads(correction_source["payload_json"])
                    found_value = base["found_date"]
                else:
                    found_value = row["found_date"]
                    base = {"title": row["title"], "severity": row["severity"]}
                if closed < date.fromisoformat(found_value):
                    raise ValidationError("闭环日期不能早于发现日期")
                count = (row["rectification_count"] if row is not None and rectification_count is None
                         else rectification_count if rectification_count is not None
                         else json.loads(correction_source["payload_json"])["rectification_count"])
                if not isinstance(count, int) or count < 0:
                    raise ValidationError("rectification_count 必须是非负整数")
                if row is not None and count < row["rectification_count"]:
                    raise ValidationError("整改次数不能小于已记录次数")
                payload = {"hazard_key": hazard_key, "title": base["title"],
                           "severity": base["severity"], "found_date": found_value,
                           "closed_date": closed.isoformat(), "rectification_count": count}
                closed_line = self._closed_line(connection, site_id)
                if closed_line is not None and date.fromisoformat(found_value) <= closed_line:
                    stored = self._correction(connection, site_id, closed_line,
                                              "hazard_close", hazard_key)
                    if stored is not None:
                        if stored["payload_json"] != canonical_json(payload):
                            raise ConflictError("该隐患闭环更正已存在且内容不同")
                        result = ("snapshot_correction", stored["correction_id"],
                                  {"correction_id": stored["correction_id"], "replayed": True})
                    else:
                        correction_id = uuid.uuid4().hex
                        connection.execute(
                            "INSERT INTO snapshot_corrections(correction_id,site_id,"
                            "settlement_date,fact_type,fact_key,fact_ref_id,payload_json,"
                            "created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (correction_id, site_id, closed_line.isoformat(), "hazard_close",
                             hazard_key, row["hazard_id"] if row else correction_source["fact_ref_id"],
                             canonical_json(payload), self._now()))
                        result = ("snapshot_correction", correction_id,
                                  {"correction_id": correction_id, "replayed": False})
                else:
                    connection.execute(
                        "UPDATE hazards SET closed_date=?, rectification_count=? WHERE hazard_id=?",
                        (closed.isoformat(), count, row["hazard_id"]))
                    result = ("hazard", row["hazard_id"], {"hazard_id": row["hazard_id"]})
                append_event(connection, actor_id=actor_id,
                             action="hazard.closed" if result[0] == "hazard"
                             else "hazard_close.corrected",
                             resource_type=result[0], resource_id=result[1],
                             detail={"site_id": site_id, "hazard_key": hazard_key,
                                     "closed_date": closed.isoformat()}, occurred_at=self._now())
                return result

            return self._idempotent(connection, request_id=request_id, action="close_hazard",
                                    payload={"actor_id": actor_id, "site_id": site_id,
                                             "hazard_key": hazard_key,
                                             "closed_date": closed.isoformat(),
                                             "rectification_count": rectification_count},
                                    create=create)

    def record_inspection(self, *, request_id: str, actor_id: str, site_id: str,
                          inspection_key: str, inspection_date: str, result: str,
                          finding_count: int) -> object:
        day = self._date(inspection_date, "inspection_date")
        if day > self._today():
            raise ValidationError("不能登记未来日期的检查")
        if result not in INSPECTION_RESULTS:
            raise ValidationError("result 必须是 pass 或 fail")
        if not isinstance(finding_count, int) or finding_count < 0:
            raise ValidationError("finding_count 必须是非负整数")
        inspection_key = self._identifier(inspection_key, "inspection_key")
        fact_payload = {"inspection_key": inspection_key, "inspection_date": day.isoformat(),
                        "result": result, "finding_count": finding_count}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._site_row(connection, site_id, actor)
            inspection_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                self._ensure_absent(connection, "inspections", site_id, "inspection_key",
                                    inspection_key)
                result_row = self._route_fact(
                    connection, site_id=site_id, fact_type="inspection", fact_key=inspection_key,
                    fact_date=day, payload=fact_payload,
                    insert_sql=(
                        "INSERT INTO inspections(inspection_id,site_id,inspection_key,"
                        "inspection_date,result,finding_count,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (inspection_id, site_id, inspection_key, day.isoformat(), result,
                         finding_count, actor_id, self._now())),
                    resource_type="inspection", resource_id=inspection_id)
                append_event(connection, actor_id=actor_id,
                             action="inspection.corrected"
                             if result_row[0] == "snapshot_correction"
                             else "inspection.recorded",
                             resource_type=result_row[0], resource_id=result_row[1],
                             detail={"site_id": site_id, "inspection_date": day.isoformat(),
                                     "result": result}, occurred_at=self._now())
                return result_row

            return self._idempotent(connection, request_id=request_id,
                                    action="record_inspection",
                                    payload={"actor_id": actor_id, "site_id": site_id,
                                             **fact_payload}, create=create)

    def record_assistance(self, *, request_id: str, actor_id: str, site_id: str,
                          assistance_key: str, request_date: str, topic: str) -> object:
        day = self._date(request_date, "request_date")
        if day > self._today():
            raise ValidationError("不能登记未来日期的求助")
        topic = self._text(topic, "topic")
        assistance_key = self._identifier(assistance_key, "assistance_key")
        fact_payload = {"assistance_key": assistance_key, "request_date": day.isoformat(),
                        "topic": topic}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._site_row(connection, site_id, actor)
            assistance_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                self._ensure_absent(connection, "assistance_requests", site_id,
                                    "assistance_key", assistance_key)
                result_row = self._route_fact(
                    connection, site_id=site_id, fact_type="assistance", fact_key=assistance_key,
                    fact_date=day, payload=fact_payload,
                    insert_sql=(
                        "INSERT INTO assistance_requests(assistance_id,site_id,assistance_key,"
                        "request_date,topic,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (assistance_id, site_id, assistance_key, day.isoformat(), topic,
                         actor_id, self._now())),
                    resource_type="assistance_request", resource_id=assistance_id)
                append_event(connection, actor_id=actor_id,
                             action="assistance.corrected"
                             if result_row[0] == "snapshot_correction"
                             else "assistance.recorded",
                             resource_type=result_row[0], resource_id=result_row[1],
                             detail={"site_id": site_id, "request_date": day.isoformat()},
                             occurred_at=self._now())
                return result_row

            return self._idempotent(connection, request_id=request_id,
                                    action="record_assistance",
                                    payload={"actor_id": actor_id, "site_id": site_id,
                                             **fact_payload}, create=create)

    def record_serious_event(self, *, request_id: str, actor_id: str, site_id: str,
                             event_key: str, event_date: str, occurred_at: str, title: str,
                             severity: str) -> object:
        day = self._date(event_date, "event_date")
        if day > self._today():
            raise ValidationError("不能登记未来日期的事件")
        if severity not in EVENT_SEVERITIES:
            raise ValidationError("severity 必须是 serious 或 emergency")
        occurred = self._timestamp(occurred_at, "occurred_at")
        title = self._text(title, "title")
        event_key = self._identifier(event_key, "event_key")
        fact_payload = {"event_key": event_key, "event_date": day.isoformat(),
                        "occurred_at": occurred, "title": title, "severity": severity,
                        "resolved": False}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._site_row(connection, site_id, actor)
            event_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                self._ensure_absent(connection, "serious_events", site_id, "event_key", event_key)
                result_row = self._route_fact(
                    connection, site_id=site_id, fact_type="serious", fact_key=event_key,
                    fact_date=day, payload=fact_payload,
                    insert_sql=(
                        "INSERT INTO serious_events(event_id,site_id,event_key,event_date,"
                        "occurred_at,title,severity,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (event_id, site_id, event_key, day.isoformat(), occurred, title, severity,
                         actor_id, self._now())),
                    resource_type="serious_event", resource_id=event_id)
                append_event(connection, actor_id=actor_id,
                             action="serious_event.corrected"
                             if result_row[0] == "snapshot_correction"
                             else "serious_event.recorded",
                             resource_type=result_row[0], resource_id=result_row[1],
                             detail={"site_id": site_id, "event_date": day.isoformat(),
                                     "severity": severity}, occurred_at=self._now())
                # 紧急事件即时突破在途窗口；无论事件是当期登记还是封账后补报，
                # 突破决定只以事件业务键去重，且不改写任何历史资格快照。
                if severity == "emergency":
                    self._apply_emergency_breach(connection, actor_id=actor_id, site_id=site_id,
                                                 source_ref=event_key, title=title,
                                                 occurred_at=occurred)
                return result_row

            return self._idempotent(connection, request_id=request_id,
                                    action="record_serious_event",
                                    payload={"actor_id": actor_id, "site_id": site_id,
                                             **fact_payload}, create=create)

    def resolve_emergency(self, *, request_id: str, actor_id: str, site_id: str,
                          event_key: str) -> object:
        event_key = self._identifier(event_key, "event_key")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._site_row(connection, site_id, actor)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM serious_events WHERE site_id=? AND event_key=?",
                    (site_id, event_key)).fetchone()
                if row is not None:
                    if row["severity"] != "emergency":
                        raise ValidationError("只有紧急事件需要解除")
                    if row["resolved_at"]:
                        return "serious_event", row["event_id"], {"event_id": row["event_id"],
                                                                  "replayed": True}
                    connection.execute("UPDATE serious_events SET resolved_at=? WHERE event_id=?",
                                       (self._now(), row["event_id"]))
                    ref_id = row["event_id"]
                else:
                    # 事件本身封账后补报、只存在于更正表时，解除也走更正流程。
                    closed_line = self._closed_line(connection, site_id)
                    if closed_line is None:
                        raise NotFoundError("严重事件不存在")
                    stored = self._correction(connection, site_id, closed_line, "serious",
                                              event_key)
                    if stored is None:
                        raise NotFoundError("严重事件不存在")
                    item = json.loads(stored["payload_json"])
                    if item.get("severity") != "emergency":
                        raise ValidationError("只有紧急事件需要解除")
                    payload = {**item, "resolved": True}
                    resolve_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO snapshot_corrections(correction_id,site_id,settlement_date,"
                        "fact_type,fact_key,fact_ref_id,payload_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (resolve_id, site_id, closed_line.isoformat(), "serious_resolve",
                         event_key, stored["fact_ref_id"], canonical_json(payload), self._now()))
                    ref_id = resolve_id
                append_event(connection, actor_id=actor_id, action="emergency.resolved",
                             resource_type="serious_event", resource_id=ref_id,
                             detail={"site_id": site_id, "event_key": event_key},
                             occurred_at=self._now())
                return "serious_event", ref_id, {"event_id": ref_id}

            return self._idempotent(connection, request_id=request_id, action="resolve_emergency",
                                    payload={"actor_id": actor_id, "site_id": site_id,
                                             "event_key": event_key}, create=create)

    def _apply_emergency_breach(self, connection, *, actor_id: str, site_id: str,
                                source_ref: str, title: str, occurred_at: str) -> str | None:
        window = connection.execute(
            "SELECT * FROM exemption_windows WHERE site_id=? AND status='active'",
            (site_id,)).fetchone()
        if window is None:
            return None
        duplicate = connection.execute(
            "SELECT 1 FROM window_exceptions WHERE site_id=? AND source_type='serious_event' "
            "AND source_ref=?", (site_id, source_ref)).fetchone()
        if duplicate:  # 重复事件不得产生第二份突破决定
            return None
        exception_id = uuid.uuid4().hex
        evidence = {"event_key": source_ref, "title": title, "occurred_at": occurred_at}
        connection.execute(
            "INSERT INTO window_exceptions(exception_id,window_id,site_id,kind,reason_code,"
            "reason_text,evidence_json,source_type,source_ref,decided_by,decided_at) "
            "VALUES(?,?,?, 'emergency_breach','EMERGENCY_DISPOSAL', ?,?, 'serious_event', ?,?,?)",
            (exception_id, window["window_id"], site_id,
             EXCEPTION_REASON_CODES["EMERGENCY_DISPOSAL"], canonical_json(evidence),
             source_ref, actor_id, self._now()))
        connection.execute("UPDATE exemption_windows SET status='breached' WHERE window_id=?",
                           (window["window_id"],))
        append_event(connection, actor_id=actor_id, action="window.breached",
                     resource_type="exemption_window", resource_id=window["window_id"],
                     detail={"site_id": site_id, "exception_id": exception_id,
                             "source_ref": source_ref}, occurred_at=self._now())
        return exception_id

    # -------------------------------------------------------------- 按日结算

    def settle_day(self, *, request_id: str, actor_id: str, site_id: str,
                   settlement_date: str | None = None) -> object:
        day = self._date(settlement_date, "settlement_date") if settlement_date else self._today()
        if day > self._today():
            raise ValidationError("不能结算未来日期")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_row(connection, site_id, actor)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM qualification_snapshots WHERE site_id=? AND settlement_date=?",
                    (site_id, day.isoformat())).fetchone()
                if existing:
                    # 同一日重复结算只做只读重放，绝不使用新规则重算。
                    return "qualification_snapshot", existing["snapshot_id"], {
                        "snapshot_id": existing["snapshot_id"], "settlement_replayed": True,
                        "eligible": bool(existing["eligible"])}
                previous = connection.execute(
                    "SELECT settlement_date FROM qualification_snapshots WHERE site_id=? "
                    "ORDER BY settlement_date DESC LIMIT 1", (site_id,)).fetchone()
                if previous and date.fromisoformat(previous["settlement_date"]) >= day:
                    raise ConflictError("不能早于最近一次结算日补算，请按日期顺序结算")
                rule = self._effective_rule(connection, day)
                criteria = json.loads(rule["criteria_json"])
                facts, streak = self._gather_facts(connection, site_id, day, criteria)
                eligible, reasons, metrics = evaluate(
                    criteria, settlement_date=day, consecutive_qualified_days=streak, facts=facts)
                metrics["rule_id"] = rule["rule_id"]
                metrics["rule_version"] = rule["version"]
                snapshot_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO qualification_snapshots(snapshot_id,site_id,settlement_date,"
                    "rule_id,eligible,consecutive_qualified_days,factors_json,reasons_json,"
                    "settled_by,settled_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (snapshot_id, site_id, day.isoformat(), rule["rule_id"], int(eligible), streak,
                     canonical_json(metrics), canonical_json(reasons), actor_id, self._now()))
                connection.execute(
                    "UPDATE snapshot_corrections SET consumed_by_snapshot_id=? "
                    "WHERE site_id=? AND consumed_by_snapshot_id IS NULL AND settlement_date<=?",
                    (snapshot_id, site_id, day.isoformat()))
                append_event(connection, actor_id=actor_id, action="qualification.settled",
                             resource_type="qualification_snapshot", resource_id=snapshot_id,
                             detail={"site_id": site_id, "settlement_date": day.isoformat(),
                                     "rule_id": rule["rule_id"], "eligible": eligible,
                                     "metrics": metrics}, occurred_at=self._now())
                window_id = self._maintain_windows(connection, actor_id=actor_id, site_id=site_id,
                                                   day=day, criteria=criteria, eligible=eligible,
                                                   snapshot_id=snapshot_id)
                return "qualification_snapshot", snapshot_id, {
                    "snapshot_id": snapshot_id, "eligible": eligible,
                    "window_id": window_id, "replayed": False}

            return self._idempotent(connection, request_id=request_id, action="settle_day",
                                    payload={"actor_id": actor_id, "site_id": site_id,
                                             "settlement_date": day.isoformat()}, create=create)

    def _gather_facts(self, connection, site_id: str, day: date, criteria) -> tuple[dict, int]:
        window_days = max(MIN_DAILY_LOOKBACK_DAYS, criteria["min_consecutive_qualified_days"],
                          criteria["qualified_ratio_days"])
        daily_start = day - timedelta(days=window_days - 1)
        daily: dict[str, dict[str, Any]] = {}
        for offset in range(window_days):
            d = (daily_start + timedelta(days=offset)).isoformat()
            daily[d] = {"date": d, "on_time": False, "quality": "missing"}
        for row in connection.execute(
                "SELECT * FROM daily_reports WHERE site_id=? AND report_date BETWEEN ? AND ?",
                (site_id, daily_start.isoformat(), day.isoformat())):
            daily[row["report_date"]] = {"date": row["report_date"],
                                         "on_time": bool(row["on_time"]), "quality": row["quality"]}
        hazards: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
                "SELECT * FROM hazards WHERE site_id=? AND found_date<=?",
                (site_id, day.isoformat())):
            hazards[row["hazard_key"]] = {"hazard_key": row["hazard_key"],
                                          "found_date": row["found_date"],
                                          "closed_date": row["closed_date"],
                                          "severity": row["severity"],
                                          "rectification_count": row["rectification_count"]}
        inspections = [{"inspection_key": row["inspection_key"],
                        "inspection_date": row["inspection_date"], "result": row["result"],
                        "finding_count": row["finding_count"]}
                       for row in connection.execute(
                           "SELECT * FROM inspections WHERE site_id=? AND inspection_date<=?",
                           (site_id, day.isoformat()))]
        assistances = [{"assistance_key": row["assistance_key"],
                        "request_date": row["request_date"], "topic": row["topic"]}
                       for row in connection.execute(
                           "SELECT * FROM assistance_requests WHERE site_id=? AND request_date<=?",
                           (site_id, day.isoformat()))]
        serious: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
                "SELECT * FROM serious_events WHERE site_id=? AND event_date<=?",
                (site_id, day.isoformat())):
            serious[row["event_key"]] = {"event_key": row["event_key"],
                                         "event_date": row["event_date"],
                                         "severity": row["severity"],
                                         "resolved": bool(row["resolved_at"])}
        # 封账后的迟到事实只存在于更正表；consumed 标记仅用于对账，事实始终参与评估。
        for row in connection.execute(
                "SELECT * FROM snapshot_corrections WHERE site_id=? AND settlement_date<=?",
                (site_id, day.isoformat())):
            item = json.loads(row["payload_json"])
            kind, key = row["fact_type"], row["fact_key"]
            if kind == "daily" and daily_start.isoformat() <= key <= day.isoformat():
                daily[key] = {"date": key, "on_time": bool(item["on_time"]),
                              "quality": item["quality"]}
            elif kind == "hazard":
                hazards.setdefault(key, {"hazard_key": key, "found_date": item["found_date"],
                                         "closed_date": item.get("closed_date"),
                                         "severity": item["severity"],
                                         "rectification_count": item["rectification_count"]})
            elif kind == "hazard_close":
                hazard = hazards.setdefault(key, {"hazard_key": key,
                                                  "found_date": item.get("found_date",
                                                                         item["closed_date"]),
                                                  "closed_date": None,
                                                  "severity": item.get("severity", "general"),
                                                  "rectification_count": 0})
                hazard["closed_date"] = item["closed_date"]
                hazard["rectification_count"] = item["rectification_count"]
            elif kind == "inspection":
                inspections.append({"inspection_key": key,
                                    "inspection_date": item["inspection_date"],
                                    "result": item["result"],
                                    "finding_count": item["finding_count"]})
            elif kind == "assistance":
                assistances.append({"assistance_key": key, "request_date": item["request_date"],
                                    "topic": item["topic"]})
            elif kind == "serious":
                serious.setdefault(key, {"event_key": key, "event_date": item["event_date"],
                                         "severity": item["severity"],
                                         "resolved": bool(item.get("resolved", False))})
            elif kind == "serious_resolve":
                serious.setdefault(key, {"event_key": key,
                                         "event_date": item.get("event_date"),
                                         "severity": "emergency", "resolved": True})
                serious[key]["resolved"] = True
        # 连续合格天数：从结算日向前逐日行走，缺失/迟报/质量不合格即中断。
        streak = 0
        cursor = day
        while True:
            entry = daily.get(cursor.isoformat())
            if entry and entry["on_time"] and entry["quality"] == "qualified":
                streak += 1
                cursor -= timedelta(days=1)
                continue
            break
        return ({"daily": list(daily.values()), "hazards": list(hazards.values()),
                 "inspections": inspections, "assistances": assistances,
                 "serious_events": list(serious.values())}, streak)

    def _maintain_windows(self, connection, *, actor_id, site_id, day, criteria, eligible,
                          snapshot_id) -> str | None:
        expired = connection.execute(
            "SELECT * FROM exemption_windows WHERE site_id=? AND status IN ('active','suspended',"
            "'breached') AND end_date<?", (site_id, day.isoformat())).fetchall()
        for window in expired:
            connection.execute("UPDATE exemption_windows SET status='expired' WHERE window_id=?",
                               (window["window_id"],))
            append_event(connection, actor_id=actor_id, action="window.expired",
                         resource_type="exemption_window", resource_id=window["window_id"],
                         detail={"site_id": site_id, "end_date": window["end_date"]},
                         occurred_at=self._now())
        if not eligible:
            # 每日结算不自动终止窗口：只有紧急突破与监管人员凭明确例外的决定能改变窗口。
            return None
        open_window = connection.execute(
            "SELECT * FROM exemption_windows WHERE site_id=? AND status IN ('active','suspended',"
            "'breached')", (site_id,)).fetchone()
        if open_window is not None:
            return None
        end = day + timedelta(days=criteria["window_duration_days"])
        review = day + timedelta(days=criteria["review_interval_days"])
        if review > end:
            review = end
        window_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO exemption_windows(window_id,site_id,start_date,end_date,"
            "next_review_date,basis_snapshot_id,status,created_at) "
            "VALUES(?,?,?,?,?,?,'active',?)",
            (window_id, site_id, day.isoformat(), end.isoformat(), review.isoformat(),
             snapshot_id, self._now()))
        append_event(connection, actor_id=actor_id, action="window.opened",
                     resource_type="exemption_window", resource_id=window_id,
                     detail={"site_id": site_id, "start_date": day.isoformat(),
                             "end_date": end.isoformat(),
                             "next_review_date": review.isoformat(),
                             "basis_snapshot_id": snapshot_id}, occurred_at=self._now())
        return window_id

    # ----------------------------------------------------------- 暂停/终止例外

    def _record_window_exception(self, *, action_name: str, request_id: str, actor_id: str,
                                 site_id: str, kind: str, allowed_statuses: tuple[str, ...],
                                 reason_code: str, reason_text: str,
                                 evidence: dict[str, Any]) -> object:
        if reason_code not in EXCEPTION_REASON_CODES:
            raise ValidationError("例外原因码不在允许范围内")
        reason_text = self._text(reason_text, "reason_text", 500)
        if not isinstance(evidence, dict) or not evidence:
            raise ValidationError("例外必须携带非空证据对象")
        payload = {"actor_id": actor_id, "site_id": site_id, "kind": kind,
                   "reason_code": reason_code, "reason_text": reason_text, "evidence": evidence}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_row(connection, site_id, actor)

            def create() -> tuple[str, str, dict[str, Any]]:
                placeholders = ",".join("?" * len(allowed_statuses))
                window = connection.execute(
                    f"SELECT * FROM exemption_windows WHERE site_id=? AND status IN ({placeholders})",
                    (site_id, *allowed_statuses)).fetchone()
                if window is None:
                    raise ConflictError("当前没有可施加该例外的在途免访窗口")
                exception_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO window_exceptions(exception_id,window_id,site_id,kind,"
                    "reason_code,reason_text,evidence_json,source_type,source_ref,decided_by,"
                    "decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (exception_id, window["window_id"], site_id, kind, reason_code, reason_text,
                     canonical_json(evidence), "manual_decision", exception_id,
                     actor_id, self._now()))
                new_status = "suspended" if kind == "suspend" else "terminated"
                connection.execute("UPDATE exemption_windows SET status=? WHERE window_id=?",
                                   (new_status, window["window_id"]))
                append_event(connection, actor_id=actor_id, action=action_name,
                             resource_type="exemption_window", resource_id=window["window_id"],
                             detail={"site_id": site_id, "exception_id": exception_id,
                                     "reason_code": reason_code, "evidence": evidence},
                             occurred_at=self._now())
                return "window_exception", exception_id, {"exception_id": exception_id,
                                                          "window_id": window["window_id"],
                                                          "status": new_status}

            return self._idempotent(connection, request_id=request_id, action=action_name,
                                    payload=payload, create=create)

    def suspend_window(self, *, request_id: str, actor_id: str, site_id: str, reason_code: str,
                       reason_text: str, evidence: dict[str, Any]) -> object:
        return self._record_window_exception(
            action_name="window.suspended", request_id=request_id, actor_id=actor_id,
            site_id=site_id, kind="suspend", allowed_statuses=("active",),
            reason_code=reason_code, reason_text=reason_text, evidence=evidence)

    def terminate_window(self, *, request_id: str, actor_id: str, site_id: str, reason_code: str,
                         reason_text: str, evidence: dict[str, Any]) -> object:
        return self._record_window_exception(
            action_name="window.terminated", request_id=request_id, actor_id=actor_id,
            site_id=site_id, kind="terminate",
            allowed_statuses=("active", "suspended", "breached"),
            reason_code=reason_code, reason_text=reason_text, evidence=evidence)

    # ------------------------------------------------------------------ 复核

    def request_review(self, *, request_id: str, actor_id: str, site_id: str,
                       reason: str) -> object:
        reason = self._text(reason, "reason", 500)
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._site_row(connection, site_id, actor)

            def create() -> tuple[str, str, dict[str, Any]]:
                window = connection.execute(
                    "SELECT * FROM exemption_windows WHERE site_id=? ORDER BY created_at DESC "
                    "LIMIT 1", (site_id,)).fetchone()
                if window is None:
                    raise ConflictError("该场所尚无免访窗口，暂无对象可复核")
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_requests(review_id,site_id,window_id,reason,status,"
                    "created_by,created_at) VALUES(?,?,?,?,'pending',?,?)",
                    (review_id, site_id, window["window_id"], reason, actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="review.requested",
                             resource_type="review_request", resource_id=review_id,
                             detail={"site_id": site_id, "window_id": window["window_id"]},
                             occurred_at=self._now())
                return "review_request", review_id, {"review_id": review_id,
                                                     "window_id": window["window_id"]}

            return self._idempotent(connection, request_id=request_id, action="request_review",
                                    payload={"actor_id": actor_id, "site_id": site_id,
                                             "reason": reason}, create=create)

    def decide_review(self, *, request_id: str, actor_id: str, review_id: str, decision: str,
                      decision_note: str, new_end_date: str | None = None,
                      new_review_date: str | None = None) -> object:
        if decision not in ("upheld", "resumed", "adjusted"):
            raise ValidationError("decision 必须是 upheld、resumed 或 adjusted")
        decision_note = self._text(decision_note, "decision_note", 500)
        new_end = self._date(new_end_date, "new_end_date") if new_end_date else None
        new_review = self._date(new_review_date, "new_review_date") if new_review_date else None
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            review_id = self._identifier(review_id, "review_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                review = connection.execute(
                    "SELECT * FROM review_requests WHERE review_id=?", (review_id,)).fetchone()
                if review is None:
                    raise NotFoundError("复核申请不存在")
                if review["status"] != "pending":
                    raise ConflictError("该复核已经作出决定")
                window = connection.execute(
                    "SELECT * FROM exemption_windows WHERE window_id=?",
                    (review["window_id"],)).fetchone()
                if decision in ("resumed", "adjusted") and window["status"] not in (
                        "active", "suspended", "breached"):
                    raise ConflictError("窗口已终止或到期，不能恢复或调整")
                if decision == "resumed":
                    if window["status"] not in ("suspended", "breached"):
                        raise ConflictError("只有暂停或突破状态的窗口可以恢复")
                    if window["status"] == "breached":
                        unresolved = connection.execute(
                            "SELECT 1 FROM serious_events WHERE site_id=? AND severity='emergency' "
                            "AND resolved_at IS NULL LIMIT 1", (window["site_id"],)).fetchone()
                        unreserved = connection.execute(
                            "SELECT 1 FROM snapshot_corrections WHERE site_id=? AND fact_type="
                            "'serious' AND json_extract(payload_json,'$.severity')='emergency' "
                            "AND fact_key NOT IN (SELECT fact_key FROM snapshot_corrections "
                            "WHERE site_id=? AND fact_type='serious_resolve') LIMIT 1",
                            (window["site_id"], window["site_id"])).fetchone()
                        if unresolved or unreserved:
                            raise ConflictError("紧急事件尚未解除，不能恢复窗口")
                    connection.execute(
                        "UPDATE window_exceptions SET lifted_at=?, lift_note=? "
                        "WHERE window_id=? AND lifted_at IS NULL AND kind IN "
                        "('emergency_breach','suspend')",
                        (self._now(), decision_note, window["window_id"]))
                    connection.execute(
                        "UPDATE exemption_windows SET status='active' WHERE window_id=?",
                        (window["window_id"],))
                elif decision == "adjusted":
                    if new_end is None and new_review is None:
                        raise ValidationError("adjusted 决定必须给出新的窗口到期日或复核日")
                    if new_end is not None:
                        if new_end <= date.fromisoformat(window["start_date"]):
                            raise ValidationError("新到期日必须晚于窗口开始日")
                        connection.execute(
                            "UPDATE exemption_windows SET end_date=? WHERE window_id=?",
                            (new_end.isoformat(), window["window_id"]))
                    if new_review is not None:
                        connection.execute(
                            "UPDATE exemption_windows SET next_review_date=? WHERE window_id=?",
                            (new_review.isoformat(), window["window_id"]))
                    if window["status"] in ("suspended", "breached"):
                        connection.execute(
                            "UPDATE window_exceptions SET lifted_at=?, lift_note=? "
                            "WHERE window_id=? AND lifted_at IS NULL",
                            (self._now(), decision_note, window["window_id"]))
                        connection.execute(
                            "UPDATE exemption_windows SET status='active' WHERE window_id=?",
                            (window["window_id"],))
                connection.execute(
                    "UPDATE review_requests SET status=?,decided_by=?,decided_at=?,"
                    "decision_note=? WHERE review_id=?",
                    (decision, actor_id, self._now(), decision_note, review_id))
                append_event(connection, actor_id=actor_id, action="review.decided",
                             resource_type="review_request", resource_id=review_id,
                             detail={"site_id": review["site_id"], "decision": decision,
                                     "window_id": review["window_id"]}, occurred_at=self._now())
                return "review_request", review_id, {"review_id": review_id, "decision": decision}

            return self._idempotent(connection, request_id=request_id, action="decide_review",
                                    payload={"actor_id": actor_id, "review_id": review_id,
                                             "decision": decision,
                                             "decision_note": decision_note,
                                             "new_end_date": new_end_date,
                                             "new_review_date": new_review_date}, create=create)

    # ------------------------------------------------------------------ 查询

    def get_snapshot(self, site_id: str, settlement_date: str) -> QualificationSnapshot:
        row = self.database.connection.execute(
            "SELECT * FROM qualification_snapshots WHERE site_id=? AND settlement_date=?",
            (site_id, settlement_date)).fetchone()
        if row is None:
            raise NotFoundError("该日期没有资格快照")
        return self._snapshot_from_row(row)

    def list_snapshots(self, site_id: str) -> list[QualificationSnapshot]:
        rows = self.database.connection.execute(
            "SELECT * FROM qualification_snapshots WHERE site_id=? ORDER BY settlement_date",
            (site_id,)).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def _snapshot_from_row(self, row) -> QualificationSnapshot:
        return QualificationSnapshot(
            row["snapshot_id"], row["site_id"], row["settlement_date"], row["rule_id"],
            bool(row["eligible"]), row["consecutive_qualified_days"],
            json.loads(row["factors_json"]), json.loads(row["reasons_json"]),
            row["settled_by"], row["settled_at"])

    def list_corrections(self, site_id: str, include_consumed: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM snapshot_corrections WHERE site_id=?"
        if not include_consumed:
            sql += " AND consumed_by_snapshot_id IS NULL"
        rows = self.database.connection.execute(sql + " ORDER BY settlement_date, created_at",
                                                (site_id,)).fetchall()
        return [{"correction_id": row["correction_id"], "settlement_date": row["settlement_date"],
                 "fact_type": row["fact_type"], "fact_key": row["fact_key"],
                 "fact_ref_id": row["fact_ref_id"],
                 "payload": json.loads(row["payload_json"]),
                 "consumed_by_snapshot_id": row["consumed_by_snapshot_id"]} for row in rows]

    def list_window_exceptions(self, site_id: str) -> list[WindowException]:
        rows = self.database.connection.execute(
            "SELECT e.* FROM window_exceptions e JOIN exemption_windows w "
            "ON e.window_id=w.window_id WHERE w.site_id=? ORDER BY e.decided_at",
            (site_id,)).fetchall()
        return [self._exception_from_row(row) for row in rows]

    def _exception_from_row(self, row) -> WindowException:
        return WindowException(
            row["exception_id"], row["window_id"], row["site_id"], row["kind"],
            row["reason_code"], row["reason_text"], json.loads(row["evidence_json"]),
            row["source_type"], row["source_ref"], row["decided_by"], row["decided_at"],
            row["lifted_at"], row["lift_note"])

    def get_window(self, site_id: str) -> ExemptionWindow | None:
        row = self.database.connection.execute(
            "SELECT * FROM exemption_windows WHERE site_id=? ORDER BY created_at DESC LIMIT 1",
            (site_id,)).fetchone()
        if row is None:
            return None
        return self._window_from_row(row)

    def list_windows(self, site_id: str) -> list[ExemptionWindow]:
        rows = self.database.connection.execute(
            "SELECT * FROM exemption_windows WHERE site_id=? ORDER BY created_at",
            (site_id,)).fetchall()
        return [self._window_from_row(row) for row in rows]

    def _window_from_row(self, row) -> ExemptionWindow:
        return ExemptionWindow(row["window_id"], row["site_id"], row["start_date"],
                               row["end_date"], row["next_review_date"], row["basis_snapshot_id"],
                               row["status"], row["created_at"],
                               self._exceptions_for(row["window_id"]))

    def _exceptions_for(self, window_id: str) -> list[WindowException]:
        rows = self.database.connection.execute(
            "SELECT * FROM window_exceptions WHERE window_id=? ORDER BY decided_at",
            (window_id,)).fetchall()
        return [self._exception_from_row(row) for row in rows]

    def list_reviews(self, site_id: str) -> list[ReviewRequest]:
        rows = self.database.connection.execute(
            "SELECT * FROM review_requests WHERE site_id=? ORDER BY created_at",
            (site_id,)).fetchall()
        return [ReviewRequest(row["review_id"], row["site_id"], row["window_id"], row["reason"],
                              row["status"], row["created_by"], row["decided_by"],
                              row["created_at"], row["decided_at"], row["decision_note"])
                for row in rows]

    def get_qualification(self, site_id: str) -> QualificationView:
        """返回当前资格视图，并用自然业务语言解释资格、复核日与每次例外证据。"""

        snapshot_row = self.database.connection.execute(
            "SELECT * FROM qualification_snapshots WHERE site_id=? ORDER BY settlement_date DESC "
            "LIMIT 1", (site_id,)).fetchone()
        if snapshot_row is None:
            raise NotFoundError("该场所尚未进行过资格结算")
        window_row = self.database.connection.execute(
            "SELECT * FROM exemption_windows WHERE site_id=? ORDER BY created_at DESC LIMIT 1",
            (site_id,)).fetchone()
        return self._build_view(site_id, snapshot_row, window_row, self._today())

    def _build_view(self, site_id: str, snapshot_row, window_row, as_of: date) -> QualificationView:
        reasons = json.loads(snapshot_row["reasons_json"])
        explanation: list[str] = [
            f"截至 {snapshot_row['settlement_date']} 的每日结算：该场所"
            f"{'符合' if snapshot_row['eligible'] else '不符合'}“无事不扰”资格"
            f"（依据规则版本 {snapshot_row['rule_id']}；历史快照已封账，不随新规则重算）。"]
        explanation.extend("· " + reason for reason in reasons)
        window_status = window_id = window_end = next_review = None
        exceptions: list[WindowException] = []
        if window_row is not None:
            window_status = window_row["status"]
            window_id = window_row["window_id"]
            window_end = window_row["end_date"]
            next_review = window_row["next_review_date"]
            exceptions = self._exceptions_for(window_row["window_id"])
            status_text = {"active": "生效中，原则上免予上门检查",
                           "suspended": "已暂停，暂停期间可依法上门",
                           "breached": "因紧急事件被即时突破，原资格结论仍保留，处置后可申请复核恢复",
                           "terminated": "已由监管人员凭明确例外终止",
                           "expired": "已到期，需重新按日结算取得资格"}[window_status]
            explanation.append(
                f"免访窗口 {window_id}：{status_text}；有效期 {window_row['start_date']} 至 "
                f"{window_row['end_date']}；下一次复核日 {window_row['next_review_date']}。")
            for exc in exceptions:
                state = "已解除" if exc.lifted_at else "当前有效"
                explanation.append(
                    f"  - 例外（{exc.kind}/{exc.reason_code}，{state}）：{exc.reason_text}；"
                    f"证据 {canonical_json(exc.evidence)}；来源 {exc.source_type}:"
                    f"{exc.source_ref}；由 {exc.decided_by} 于 {exc.decided_at} 决定"
                    + (f"；解除说明：{exc.lift_note}" if exc.lift_note else ""))
        else:
            explanation.append("当前没有免访窗口；待每日结算合格后自动生成有期限窗口。")
        if window_row is not None and not snapshot_row["eligible"] and window_status in (
                "active", "suspended", "breached"):
            explanation.append(
                "注意：最近一次结算已不满足资格，窗口只能由监管人员依据明确例外暂停或终止，"
                "企业也可就结论申请复核。")
        pending = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM snapshot_corrections WHERE site_id=? "
            "AND consumed_by_snapshot_id IS NULL", (site_id,)).fetchone()["c"]
        if pending:
            explanation.append(
                f"另有 {pending} 条封账后迟到事实进入更正流程，将在下一次按日结算时计入。")
        return QualificationView(site_id, as_of.isoformat(), bool(snapshot_row["eligible"]),
                                 window_status, window_id, window_end, next_review,
                                 snapshot_row["snapshot_id"], snapshot_row["rule_id"],
                                 snapshot_row["settlement_date"], explanation, exceptions)
