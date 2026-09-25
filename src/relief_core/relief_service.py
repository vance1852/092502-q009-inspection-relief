"""“无事不扰”资格服务：规则版本、按日结算、免访窗口、例外与复核。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import qualification as q
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import (DailySnapshot, ReliefWindow, ReviewRequest,
                     RuleSet, WindowException)
from .storage import Database

ACTIVE_KEY = "active"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")


class ReliefService:
    """协调资格规则、封账快照、免访窗口与例外复核的全部业务规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _today(self, timezone_name: str) -> date:
        return self.clock.now().astimezone(ZoneInfo(timezone_name)).date()

    def _date(self, value: str | None, field: str = "business_date") -> date:
        if value is None:
            raise ValidationError(f"{field} 不能为空")
        try:
            return q.parse_date(value)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def _text(self, value: Any, field: str, limit: int = 500) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = str(request_id or "").strip()
        if not IDENTIFIER.fullmatch(request_id):
            raise ValidationError("request_id 格式无效")
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != digest(payload):
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    "response": json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, digest(payload), resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, "response": response}

    # ------------------------------------------------------------- 规则生命周期

    def propose_rule_set(self, *, request_id: str, actor_id: str,
                         parameters: dict[str, Any], effective_date: str) -> dict[str, Any]:
        """起草一版资格规则，参数必须完整，生效日期不得早于在途发布版本。"""

        payload = {"actor_id": actor_id, "parameters": parameters, "effective_date": effective_date}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            try:
                normalized = q.normalize_parameters(parameters)
                effective = q.parse_date(effective_date)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            latest = connection.execute(
                "SELECT rule_set_id, effective_date FROM rule_sets WHERE status='published' "
                "ORDER BY effective_date DESC, rule_set_id DESC LIMIT 1"
            ).fetchone()
            if latest and effective <= q.parse_date(latest["effective_date"]):
                raise ValidationError("新版本生效日期必须晚于当前已发布版本")

            def create() -> tuple[str, str, dict[str, Any]]:
                rule_set_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO rule_sets(rule_set_id,status,parameters_json,effective_date,"
                    "predecessor_id,proposed_by,proposed_at) VALUES(?,?,?,?,?,?,?)",
                    (rule_set_id, "draft", canonical_json(normalized), effective.isoformat(),
                     latest["rule_set_id"] if latest else None, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="rule_set.proposed",
                             resource_type="rule_set", resource_id=rule_set_id,
                             detail={"effective_date": effective.isoformat(),
                                     "fingerprint": q.parameters_fingerprint(normalized)},
                             occurred_at=self._now())
                response = {"rule_set_id": rule_set_id, "status": "draft",
                            "effective_date": effective.isoformat()}
                return "rule_set", rule_set_id, response

            return self._idempotent(connection, request_id=request_id, action="propose_rule_set",
                                    payload=payload, create=create)

    def _load_rule_set(self, connection, rule_set_id: str):
        row = connection.execute("SELECT * FROM rule_sets WHERE rule_set_id=?", (rule_set_id,)).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        return row

    def approve_rule_set(self, *, request_id: str, actor_id: str, rule_set_id: str) -> dict[str, Any]:
        """审批通过草案；审批人不能是起草人本人。"""

        payload = {"actor_id": actor_id, "rule_set_id": rule_set_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")

            def create() -> tuple[str, str, dict[str, Any]]:
                rule = self._load_rule_set(connection, rule_set_id)
                if rule["status"] != "draft":
                    raise ConflictError("只有草案规则可以审批")
                if rule["proposed_by"] == actor_id:
                    raise PermissionDenied("规则起草人不能审批自己起草的版本")
                connection.execute(
                    "UPDATE rule_sets SET status='approved', approved_by=?, approved_at=? WHERE rule_set_id=?",
                    (actor_id, self._now(), rule_set_id),
                )
                append_event(connection, actor_id=actor_id, action="rule_set.approved",
                             resource_type="rule_set", resource_id=rule_set_id,
                             detail={"effective_date": rule["effective_date"]}, occurred_at=self._now())
                response = {"rule_set_id": rule_set_id, "status": "approved"}
                return "rule_set", rule_set_id, response

            return self._idempotent(connection, request_id=request_id, action="approve_rule_set",
                                    payload=payload, create=create)

    def publish_rule_set(self, *, request_id: str, actor_id: str, rule_set_id: str) -> dict[str, Any]:
        """发布已审批规则；自生效日期起的新快照才使用它，旧快照不重算。"""

        payload = {"actor_id": actor_id, "rule_set_id": rule_set_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")

            def create() -> tuple[str, str, dict[str, Any]]:
                rule = self._load_rule_set(connection, rule_set_id)
                if rule["status"] != "approved":
                    raise ConflictError("只有审批通过的规则可以发布")
                connection.execute(
                    "UPDATE rule_sets SET status='published', published_at=? WHERE rule_set_id=?",
                    (self._now(), rule_set_id),
                )
                append_event(connection, actor_id=actor_id, action="rule_set.published",
                             resource_type="rule_set", resource_id=rule_set_id,
                             detail={"effective_date": rule["effective_date"]}, occurred_at=self._now())
                response = {"rule_set_id": rule_set_id, "status": "published",
                            "effective_date": rule["effective_date"]}
                return "rule_set", rule_set_id, response

            return self._idempotent(connection, request_id=request_id, action="publish_rule_set",
                                    payload=payload, create=create)

    def list_rule_sets(self) -> list[RuleSet]:
        with self.database.locked() as connection:
            rows = connection.execute(
                "SELECT * FROM rule_sets ORDER BY effective_date, rule_set_id"
            ).fetchall()
        return [self._rule_set_model(row) for row in rows]

    def _rule_set_model(self, row) -> RuleSet:
        return RuleSet(row["rule_set_id"], row["status"], json.loads(row["parameters_json"]),
                       row["effective_date"], row["predecessor_id"], row["proposed_by"],
                       row["proposed_at"], row["approved_by"], row["approved_at"], row["published_at"])

    # --------------------------------------------------------------- 事实与更正

    def record_fact(self, *, request_id: str, actor_id: str, site_id: str, channel: str,
                    event_key: str, payload: dict[str, Any],
                    business_date: str | None = None) -> dict[str, Any]:
        """登记一条资格事实。

        业务日已封账（该日快照已结算）后到达的数据按封账规则进入更正流程：
        事实以迟到形式留痕，须经审批通过后才进入此后日期的结算，绝不改写旧快照。
        严重事件在同一事务内即时突破当前免访窗口，但不改动窗口与既有资格。
        """

        if channel not in q.QUALIFICATION_CHANNELS:
            raise ValidationError("channel 不在允许范围内")
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("payload 必须是非空对象")
        event_key = str(event_key or "").strip()
        if not event_key:
            raise ValidationError("event_key 不能为空")
        payload = dict(payload)
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            site = self._site(connection, site_id)
            today = self._today(site["timezone_name"])
            if business_date is None:
                business_date = today.isoformat()
            fact_date = self._date(business_date)
            if fact_date > today:
                raise ValidationError("不能登记未来业务日的事实")
            payload_out = {"actor_id": actor_id, "site_id": site_id, "channel": channel,
                           "event_key": event_key, "payload": payload,
                           "business_date": fact_date.isoformat()}
            sealed = connection.execute(
                "SELECT 1 FROM daily_snapshots WHERE site_id=? AND business_date=?",
                (site_id, fact_date.isoformat()),
            ).fetchone()
            late = sealed is not None
            self._validate_payload(channel, payload)

            def create() -> tuple[str, str, dict[str, Any]]:
                fact_id = uuid.uuid4().hex
                recorded_at = self._now()
                try:
                    connection.execute(
                        "INSERT INTO qualification_facts(fact_id,site_id,business_date,channel,event_key,"
                        "payload_json,payload_hash,occurred_at,recorded_at,late_correction) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (fact_id, site_id, fact_date.isoformat(), channel, event_key,
                         canonical_json(payload), digest(payload),
                         str(payload.get("occurred_at") or recorded_at), recorded_at, int(late)),
                    )
                except Exception as exc:
                    raise ConflictError("同一场所、业务日、通道和事件键已经登记") from exc
                response: dict[str, Any] = {"fact_id": fact_id, "late": late}
                if late:
                    correction_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO fact_corrections(correction_id,fact_id,site_id,business_date,"
                        "status,note,created_at) VALUES(?,?,?,?,?,?,?)",
                        (correction_id, fact_id, site_id, fact_date.isoformat(),
                         "pending", "封账后到达，按更正流程处理", recorded_at),
                    )
                    response["correction_id"] = correction_id
                    response["correction_status"] = "pending"
                append_event(connection, actor_id=actor_id,
                             action="fact.late_recorded" if late else "fact.recorded",
                             resource_type="qualification_fact", resource_id=fact_id,
                             detail={"site_id": site_id, "business_date": fact_date.isoformat(),
                                     "channel": channel, "event_key": event_key, "late": late},
                             occurred_at=recorded_at)
                if channel == q.CHANNEL_INCIDENT and not late:
                    self._emergency_breakthrough(connection, actor_id=actor_id, site=site,
                                                 fact_date=fact_date, event_key=event_key,
                                                 payload=payload, occurred_at=recorded_at)
                return "qualification_fact", fact_id, response

            return self._idempotent(connection, request_id=request_id, action="record_fact",
                                    payload=payload_out, create=create)

    def _validate_payload(self, channel: str, payload: dict[str, Any]) -> None:
        if channel == q.CHANNEL_DAILY:
            if "score" in payload and not isinstance(payload["score"], (int, float)):
                raise ValidationError("daily_completion.score 必须是数值")
            if not isinstance(payload.get("on_time"), bool):
                raise ValidationError("daily_completion.on_time 必须是布尔值")
        if channel == q.CHANNEL_HAZARD and payload.get("status", "open") not in ("open", "closed"):
            raise ValidationError("hazard.status 只能是 open 或 closed")
        if channel == q.CHANNEL_INCIDENT and not str(payload.get("severity", "")).strip():
            raise ValidationError("incident.severity 不能为空")

    def _current_rule(self, connection, business_date: date):
        rows = connection.execute("SELECT * FROM rule_sets WHERE status='published'").fetchall()
        rule = q.latest_effective([dict(row) for row in rows], business_date)
        return rule

    def _emergency_breakthrough(self, connection, *, actor_id: str, site, fact_date: date,
                                event_key: str, payload: dict[str, Any], occurred_at: str) -> None:
        """严重事件即时突破当前窗口；同一事件键只产生一份突破决定。"""

        rule = self._current_rule(connection, fact_date)
        if rule is None:
            return
        parameters = json.loads(rule["parameters_json"])
        if payload.get("severity") not in set(parameters["incident_block_severities"]):
            return
        window = connection.execute(
            "SELECT * FROM relief_windows WHERE site_id=? AND active_key=? AND status='active'",
            (site["site_id"], ACTIVE_KEY),
        ).fetchone()
        if window is None:
            return
        exception_id = uuid.uuid4().hex
        evidence = {
            "incident_event_key": event_key,
            "incident_date": fact_date.isoformat(),
            "severity": payload.get("severity"),
            "description": payload.get("description", ""),
            "fact_payload_hash": digest(payload),
        }
        try:
            connection.execute(
                "INSERT INTO window_exceptions(exception_id,window_id,site_id,kind,reason_code,"
                "reason_text,evidence_json,status,actor_id,created_at,decision_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (exception_id, window["window_id"], site["site_id"], "emergency_breakthrough",
                 q.EMERGENCY_REASON, "发生严重事件，即时突破免访窗口以安排紧急检查；原资格保留",
                 canonical_json(evidence), "open", actor_id, occurred_at, event_key),
            )
        except Exception as exc:
            raise ConflictError("该严重事件已生成突破决定，不得重复产生") from exc
        append_event(connection, actor_id=actor_id, action="window.emergency_breakthrough",
                     resource_type="relief_window", resource_id=window["window_id"],
                     detail={"window_id": window["window_id"], "decision_key": event_key,
                             "evidence": evidence}, occurred_at=occurred_at)

    def list_pending_corrections(self, actor_id: str, site_id: str | None = None) -> list[dict[str, Any]]:
        query = ("SELECT c.*, f.channel, f.event_key FROM fact_corrections c "
                 "JOIN qualification_facts f ON f.fact_id=c.fact_id WHERE c.status='pending'")
        parameters: list[Any] = []
        if site_id:
            query += " AND c.site_id=?"
            parameters.append(site_id)
        query += " ORDER BY c.created_at"
        with self.database.locked() as connection:
            self._actor(connection, actor_id)
            rows = connection.execute(query, parameters).fetchall()
            return [dict(row) for row in rows]

    def apply_correction(self, *, request_id: str, actor_id: str, correction_id: str,
                         note: str = "") -> dict[str, Any]:
        """通过迟到数据的更正：自此后的结算可见，旧快照不重算。"""

        payload = {"actor_id": actor_id, "correction_id": correction_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM fact_corrections WHERE correction_id=?", (correction_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("更正申请不存在")
                if row["status"] != "pending":
                    raise ConflictError("只有待审更正可以通过")
                connection.execute(
                    "UPDATE fact_corrections SET status='applied', note=?, applied_by=?, applied_at=? "
                    "WHERE correction_id=?",
                    (note or row["note"], actor_id, self._now(), correction_id),
                )
                append_event(connection, actor_id=actor_id, action="correction.applied",
                             resource_type="fact_correction", resource_id=correction_id,
                             detail={"site_id": row["site_id"], "business_date": row["business_date"]},
                             occurred_at=self._now())
                response = {"correction_id": correction_id, "status": "applied"}
                return "fact_correction", correction_id, response

            return self._idempotent(connection, request_id=request_id, action="apply_correction",
                                    payload=payload, create=create)

    def dismiss_correction(self, *, request_id: str, actor_id: str, correction_id: str,
                           note: str) -> dict[str, Any]:
        """驳回更正：该迟到事实保持对结算不可见。"""

        payload = {"actor_id": actor_id, "correction_id": correction_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            note = self._text(note, "note")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM fact_corrections WHERE correction_id=?", (correction_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("更正申请不存在")
                if row["status"] != "pending":
                    raise ConflictError("只有待审更正可以驳回")
                connection.execute(
                    "UPDATE fact_corrections SET status='dismissed', note=? WHERE correction_id=?",
                    (note, correction_id),
                )
                append_event(connection, actor_id=actor_id, action="correction.dismissed",
                             resource_type="fact_correction", resource_id=correction_id,
                             detail={"note": note}, occurred_at=self._now())
                response = {"correction_id": correction_id, "status": "dismissed"}
                return "fact_correction", correction_id, response

            return self._idempotent(connection, request_id=request_id, action="dismiss_correction",
                                    payload=payload, create=create)

    # ----------------------------------------------------------------- 按日结算

    def settle_day(self, *, request_id: str, actor_id: str, site_id: str,
                   business_date: str) -> dict[str, Any]:
        """结算并封账一个业务日。幂等：已封账日期直接返回原快照，绝不重算。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "business_date": business_date}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            site = self._site(connection, site_id)
            day = self._date(business_date)
            if day > self._today(site["timezone_name"]):
                raise ValidationError("不能结算未来业务日")
            existing = connection.execute(
                "SELECT * FROM daily_snapshots WHERE site_id=? AND business_date=?",
                (site_id, day.isoformat()),
            ).fetchone()
            if existing is not None:
                return {"request_id": request_id, "resource_type": "daily_snapshot",
                        "resource_id": existing["snapshot_id"], "replayed": True,
                        "response": {"snapshot_id": existing["snapshot_id"], "sealed": True,
                                     "business_date": day.isoformat()}}

            def create() -> tuple[str, str, dict[str, Any]]:
                rule = self._current_rule(connection, day)
                if rule is None:
                    raise ValidationError("该业务日没有已生效的规则版本，无法结算")
                parameters = json.loads(rule["parameters_json"])
                previous = connection.execute(
                    "SELECT * FROM daily_snapshots WHERE site_id=? AND business_date=?",
                    (site_id, (day - timedelta(days=1)).isoformat()),
                ).fetchone()
                facts = self._visible_facts(connection, site_id, day, parameters)
                result = q.evaluate(parameters, day, facts)
                previous_streak = previous["streak_days"] if previous else 0
                previous_date = q.parse_date(previous["business_date"]) if previous else None
                # 终止窗口会写入资格重置标记：它不改写旧快照，但自重置日起连续天数重新起算。
                lower_bound = previous_date.isoformat() if previous_date else "0000-01-01"
                reset_boundary = connection.execute(
                    "SELECT 1 FROM qualification_resets WHERE site_id=? "
                    "AND reset_date >= ? AND reset_date <= ? LIMIT 1",
                    (site_id, lower_bound, day.isoformat()),
                ).fetchone()
                if reset_boundary is not None:
                    previous_date = None
                    previous_streak = 0
                streak = q.next_streak(result["gates_passed"], previous_date, previous_streak, day)
                correction_items = self._newly_applied_corrections(connection, facts)
                breakthrough_keys = {
                    item["event_key"] for item in correction_items
                    if item["channel"] == q.CHANNEL_INCIDENT
                    and str(item.get("severity")) in set(parameters["incident_block_severities"])
                }
                inputs_hash = q.settlement_inputs_hash(
                    parameters, day.isoformat(),
                    [{"fact_id": f["fact_id"], "payload_hash": f["payload_hash"],
                      "late_correction": bool(f["late_correction"])} for f in facts],
                    previous["snapshot_id"] if previous else None)
                snapshot_id = uuid.uuid4().hex
                sealed_at = self._now()
                connection.execute(
                    "INSERT INTO daily_snapshots(snapshot_id,site_id,business_date,rule_set_id,"
                    "streak_days,day_qualified,metrics_json,reasons_json,corrections_json,"
                    "inputs_hash,sealed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (snapshot_id, site_id, day.isoformat(), rule["rule_set_id"], streak,
                     int(result["gates_passed"]), canonical_json(result["metrics"]),
                     canonical_json(result["reasons"]), canonical_json(correction_items),
                     inputs_hash, sealed_at),
                )
                for item in correction_items:
                    connection.execute(
                        "UPDATE fact_corrections SET consumed_snapshot_id=? WHERE correction_id=?",
                        (snapshot_id, item["correction_id"]),
                    )
                append_event(connection, actor_id=actor_id, action="snapshot.sealed",
                             resource_type="daily_snapshot", resource_id=snapshot_id,
                             detail={"site_id": site_id, "business_date": day.isoformat(),
                                     "rule_set_id": rule["rule_set_id"], "streak_days": streak,
                                     "day_qualified": result["gates_passed"],
                                     "inputs_hash": inputs_hash}, occurred_at=sealed_at)
                window_response = self._maintain_window(
                    connection, actor_id=actor_id, site=site, day=day,
                    rule_set_id=rule["rule_set_id"], parameters=parameters,
                    snapshot_id=snapshot_id, qualified=result["gates_passed"], streak=streak,
                    sealed_at=sealed_at, breakthrough_keys=breakthrough_keys)
                response = {"snapshot_id": snapshot_id, "sealed": False,
                            "business_date": day.isoformat(),
                            "day_qualified": result["gates_passed"], "streak_days": streak,
                            "window": window_response}
                return "daily_snapshot", snapshot_id, response

            return self._idempotent(connection, request_id=request_id, action="settle_day",
                                    payload=payload, create=create)

    def _visible_facts(self, connection, site_id: str, day: date,
                       parameters: dict[str, Any]) -> list[dict[str, Any]]:
        lookback = max(parameters["hazard_lookback_days"], parameters["inspection_lookback_days"],
                       parameters["engagement_lookback_days"], parameters["incident_block_days"])
        start = day - timedelta(days=max(lookback - 1, 0))
        rows = connection.execute(
            "SELECT f.* FROM qualification_facts f LEFT JOIN fact_corrections c ON c.fact_id=f.fact_id "
            "WHERE f.site_id=? AND f.business_date BETWEEN ? AND ? "
            "AND (c.correction_id IS NULL OR c.status='applied') "
            "ORDER BY f.business_date, f.recorded_at, f.fact_id",
            (site_id, start.isoformat(), day.isoformat()),
        ).fetchall()
        return [{"fact_id": row["fact_id"], "site_id": row["site_id"],
                 "business_date": row["business_date"], "channel": row["channel"],
                 "event_key": row["event_key"], "payload": json.loads(row["payload_json"]),
                 "payload_hash": row["payload_hash"], "recorded_at": row["recorded_at"],
                 "late_correction": bool(row["late_correction"])} for row in rows]

    def _newly_applied_corrections(self, connection, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not facts:
            return []
        fact_ids = [fact["fact_id"] for fact in facts if fact["late_correction"]]
        if not fact_ids:
            return []
        placeholders = ",".join("?" for _ in fact_ids)
        rows = connection.execute(
            f"SELECT c.correction_id,c.fact_id,c.business_date,c.applied_at,f.channel,f.event_key,"
            f"f.payload_json FROM fact_corrections c JOIN qualification_facts f ON f.fact_id=c.fact_id "
            f"WHERE c.status='applied' AND c.consumed_snapshot_id IS NULL AND c.fact_id IN ({placeholders})",
            fact_ids,
        ).fetchall()
        items = []
        for row in rows:
            item = {"correction_id": row["correction_id"], "fact_id": row["fact_id"],
                    "business_date": row["business_date"], "applied_at": row["applied_at"],
                    "channel": row["channel"], "event_key": row["event_key"]}
            payload = json.loads(row["payload_json"])
            item["severity"] = payload.get("severity")
            items.append(item)
        return items

    def _maintain_window(self, connection, *, actor_id: str, site, day: date,
                         rule_set_id: str, parameters: dict[str, Any], snapshot_id: str,
                         qualified: bool, streak: int, sealed_at: str,
                         breakthrough_keys: set[str] | None = None) -> dict[str, Any] | None:
        """到期窗口失效；连续合格达标且无在途窗口时开出有期限免访窗口。

        封账后到达、经更正通过的严重事件在此刻对窗口产生突破（决定仍按事件键去重）。
        """

        connection.execute(
            "UPDATE relief_windows SET status='expired', active_key=NULL "
            "WHERE site_id=? AND active_key=? AND end_date < ?",
            (site["site_id"], ACTIVE_KEY, day.isoformat()),
        )
        for event_key in sorted(breakthrough_keys or ()):
            self._late_incident_breakthrough(connection, actor_id=actor_id, site=site,
                                             day=day, event_key=event_key, occurred_at=sealed_at)
        if not qualified or streak < parameters["min_streak_days"]:
            return None
        current = connection.execute(
            "SELECT * FROM relief_windows WHERE site_id=? AND active_key=?",
            (site["site_id"], ACTIVE_KEY),
        ).fetchone()
        if current is not None:
            return {"window_id": current["window_id"], "action": "unchanged",
                    "status": current["status"]}
        window_id = uuid.uuid4().hex
        start_date = day + timedelta(days=1)
        end_date = start_date + timedelta(days=parameters["window_duration_days"] - 1)
        next_review = start_date + timedelta(days=parameters["review_interval_days"])
        connection.execute(
            "INSERT INTO relief_windows(window_id,site_id,snapshot_id,rule_set_id,status,"
            "start_date,end_date,next_review_date,created_at,active_key) "
            "VALUES(?,?,?,?, 'active', ?,?,?,?,?)",
            (window_id, site["site_id"], snapshot_id, rule_set_id,
             start_date.isoformat(), end_date.isoformat(), next_review.isoformat(),
             sealed_at, ACTIVE_KEY),
        )
        append_event(connection, actor_id=actor_id, action="window.opened",
                     resource_type="relief_window", resource_id=window_id,
                     detail={"site_id": site["site_id"], "snapshot_id": snapshot_id,
                             "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                             "next_review_date": next_review.isoformat()}, occurred_at=sealed_at)
        return {"window_id": window_id, "action": "opened", "status": "active",
                "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                "next_review_date": next_review.isoformat()}

    def _late_incident_breakthrough(self, connection, *, actor_id: str, site, day: date,
                                    event_key: str, occurred_at: str) -> None:
        """更正通过后首次结算时，为严重事件补登唯一一份突破决定。"""

        window = connection.execute(
            "SELECT * FROM relief_windows WHERE site_id=? AND active_key=? AND status='active'",
            (site["site_id"], ACTIVE_KEY),
        ).fetchone()
        if window is None:
            return
        fact = connection.execute(
            "SELECT * FROM qualification_facts WHERE site_id=? AND channel=? AND event_key=?",
            (site["site_id"], q.CHANNEL_INCIDENT, event_key),
        ).fetchone()
        if fact is None:
            return
        payload = json.loads(fact["payload_json"])
        evidence = {
            "incident_event_key": event_key, "incident_date": fact["business_date"],
            "severity": payload.get("severity"), "description": payload.get("description", ""),
            "fact_payload_hash": fact["payload_hash"], "via_correction": True,
        }
        try:
            connection.execute(
                "INSERT INTO window_exceptions(exception_id,window_id,site_id,kind,reason_code,"
                "reason_text,evidence_json,status,actor_id,created_at,decision_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, window["window_id"], site["site_id"], "emergency_breakthrough",
                 q.EMERGENCY_REASON, "封账后到达的严重事件经更正通过，即时突破窗口；原资格保留",
                 canonical_json(evidence), "open", actor_id, occurred_at, event_key),
            )
        except Exception:
            return
        append_event(connection, actor_id=actor_id, action="window.emergency_breakthrough",
                     resource_type="relief_window", resource_id=window["window_id"],
                     detail={"window_id": window["window_id"], "decision_key": event_key,
                             "via_correction": True, "evidence": evidence}, occurred_at=occurred_at)

    def settle_range(self, *, actor_id: str, site_id: str, start_date: str,
                     end_date: str, request_id_prefix: str = "settle") -> list[dict[str, Any]]:
        """连续结算一个闭区间，已封账日期跳过（仍不重算）。"""

        start = self._date(start_date, "start_date")
        end = self._date(end_date, "end_date")
        if end < start:
            raise ValidationError("end_date 不能早于 start_date")
        results = []
        day = start
        while day <= end:
            results.append(self.settle_day(
                request_id=f"{request_id_prefix}-{site_id}-{day.isoformat()}",
                actor_id=actor_id, site_id=site_id, business_date=day.isoformat()))
            day += timedelta(days=1)
        return results

    def get_snapshot(self, site_id: str, business_date: str) -> DailySnapshot:
        with self.database.locked() as connection:
            row = connection.execute(
                "SELECT * FROM daily_snapshots WHERE site_id=? AND business_date=?",
                (site_id, business_date),
            ).fetchone()
            if row is None:
                raise NotFoundError("该业务日尚未结算")
            return self._snapshot_model(row)

    def latest_snapshot(self, site_id: str) -> DailySnapshot | None:
        with self.database.locked() as connection:
            row = connection.execute(
                "SELECT * FROM daily_snapshots WHERE site_id=? ORDER BY business_date DESC LIMIT 1",
                (site_id,),
            ).fetchone()
            return self._snapshot_model(row) if row else None

    def _snapshot_model(self, row) -> DailySnapshot:
        return DailySnapshot(row["snapshot_id"], row["site_id"], row["business_date"],
                             row["rule_set_id"], row["streak_days"], bool(row["day_qualified"]),
                             json.loads(row["metrics_json"]), json.loads(row["reasons_json"]),
                             json.loads(row["corrections_json"]), row["inputs_hash"], row["sealed_at"])

    # ------------------------------------------------------------- 窗口例外处置

    def _load_window(self, connection, window_id: str):
        row = connection.execute("SELECT * FROM relief_windows WHERE window_id=?", (window_id,)).fetchone()
        if row is None:
            raise NotFoundError("免访窗口不存在")
        return row

    def _validate_evidence(self, evidence: Any) -> dict[str, Any]:
        if not isinstance(evidence, dict) or not evidence:
            raise ValidationError("必须提供明确证据，证据必须是非空对象")
        refs = evidence.get("references")
        if not isinstance(refs, list) or not refs or not all(isinstance(item, str) and item for item in refs):
            raise ValidationError("evidence.references 必须是非空字符串列表（证据出处）")
        return evidence

    def _add_exception(self, connection, *, actor_id: str, window, kind: str, reason_code: str,
                       reason_text: str, evidence: dict[str, Any], status: str,
                       decision_key: str, occurred_at: str):
        exception_id = uuid.uuid4().hex
        try:
            connection.execute(
                "INSERT INTO window_exceptions(exception_id,window_id,site_id,kind,reason_code,"
                "reason_text,evidence_json,status,actor_id,created_at,decision_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (exception_id, window["window_id"], window["site_id"], kind, reason_code, reason_text,
                 canonical_json(evidence), status, actor_id, occurred_at, decision_key),
            )
        except Exception as exc:
            raise ConflictError("同一依据已经产生过例外决定，不得重复决定") from exc
        append_event(connection, actor_id=actor_id, action=f"window.{kind}",
                     resource_type="relief_window", resource_id=window["window_id"],
                     detail={"window_id": window["window_id"], "reason_code": reason_code,
                             "decision_key": decision_key, "evidence": evidence, "status": status},
                     occurred_at=occurred_at)
        return exception_id

    def suspend_window(self, *, request_id: str, actor_id: str, window_id: str,
                       reason_code: str, reason_text: str, evidence: dict[str, Any]) -> dict[str, Any]:
        """监管人员依据明确例外暂停窗口；原因码与证据缺一不可。"""

        payload = {"actor_id": actor_id, "window_id": window_id, "reason_code": reason_code,
                   "reason_text": reason_text, "evidence": evidence}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            reason_code = self._text(reason_code, "reason_code", 80)
            if reason_code not in q.ALLOWED_SUSPEND_REASONS:
                raise ValidationError("暂停原因不在明确允许的例外清单内")
            reason_text = self._text(reason_text, "reason_text")
            evidence = self._validate_evidence(evidence)

            def create() -> tuple[str, str, dict[str, Any]]:
                window = self._load_window(connection, window_id)
                if window["status"] != "active":
                    raise ConflictError("只有生效中的窗口可以暂停")
                self._add_exception(connection, actor_id=actor_id, window=window,
                                    kind="regulator_suspend", reason_code=reason_code,
                                    reason_text=reason_text, evidence=evidence,
                                    status="active", decision_key=request_id,
                                    occurred_at=self._now())
                connection.execute(
                    "UPDATE relief_windows SET status='suspended' WHERE window_id=?", (window_id,)
                )
                response = {"window_id": window_id, "status": "suspended", "reason_code": reason_code}
                return "relief_window", window_id, response

            return self._idempotent(connection, request_id=request_id, action="suspend_window",
                                    payload=payload, create=create)

    def terminate_window(self, *, request_id: str, actor_id: str, window_id: str,
                         reason_code: str, reason_text: str, evidence: dict[str, Any]) -> dict[str, Any]:
        """监管人员依据明确例外终止窗口。"""

        payload = {"actor_id": actor_id, "window_id": window_id, "reason_code": reason_code,
                   "reason_text": reason_text, "evidence": evidence}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            reason_code = self._text(reason_code, "reason_code", 80)
            if reason_code not in q.ALLOWED_TERMINATE_REASONS:
                raise ValidationError("终止原因不在明确允许的例外清单内")
            reason_text = self._text(reason_text, "reason_text")
            evidence = self._validate_evidence(evidence)

            def create() -> tuple[str, str, dict[str, Any]]:
                window = self._load_window(connection, window_id)
                if window["status"] not in ("active", "suspended"):
                    raise ConflictError("只有生效中或已暂停的窗口可以终止")
                self._add_exception(connection, actor_id=actor_id, window=window,
                                    kind="regulator_terminate", reason_code=reason_code,
                                    reason_text=reason_text, evidence=evidence,
                                    status="active", decision_key=request_id,
                                    occurred_at=self._now())
                connection.execute(
                    "UPDATE relief_windows SET status='terminated', active_key=NULL WHERE window_id=?",
                    (window_id,),
                )
                # 资格重置标记：不改动任何已封账快照，但此后结算的连续合格天数重新起算。
                site = self._site(connection, window["site_id"])
                reset_date = self._today(site["timezone_name"]).isoformat()
                connection.execute(
                    "INSERT INTO qualification_resets(reset_id,site_id,reset_date,reason_code,"
                    "window_id,created_at) VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, window["site_id"], reset_date, reason_code, window_id,
                     self._now()),
                )
                append_event(connection, actor_id=actor_id, action="qualification.reset",
                             resource_type="site", resource_id=window["site_id"],
                             detail={"window_id": window_id, "reset_date": reset_date,
                                     "reason_code": reason_code}, occurred_at=self._now())
                response = {"window_id": window_id, "status": "terminated",
                            "reason_code": reason_code, "eligibility_reset_date": reset_date}
                return "relief_window", window_id, response

            return self._idempotent(connection, request_id=request_id, action="terminate_window",
                                    payload=payload, create=create)

    def resolve_breakthrough(self, *, request_id: str, actor_id: str, exception_id: str,
                             resolution_note: str, evidence: dict[str, Any]) -> dict[str, Any]:
        """严重事件处置完毕后关闭突破状态；窗口与原资格自始保留。"""

        payload = {"actor_id": actor_id, "exception_id": exception_id,
                   "resolution_note": resolution_note, "evidence": evidence}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            resolution_note = self._text(resolution_note, "resolution_note")
            evidence = self._validate_evidence(evidence)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM window_exceptions WHERE exception_id=?", (exception_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("例外记录不存在")
                if row["kind"] != "emergency_breakthrough":
                    raise ValidationError("只有紧急突破可以闭环")
                if row["status"] != "open":
                    raise ConflictError("该突破已闭环")
                merged = {**json.loads(row["evidence_json"]), "resolution_note": resolution_note,
                          "resolution_references": evidence.get("references")}
                connection.execute(
                    "UPDATE window_exceptions SET status='resolved', evidence_json=? WHERE exception_id=?",
                    (canonical_json(merged), exception_id),
                )
                append_event(connection, actor_id=actor_id, action="window.breakthrough_resolved",
                             resource_type="window_exception", resource_id=exception_id,
                             detail={"window_id": row["window_id"], "resolution_note": resolution_note},
                             occurred_at=self._now())
                response = {"exception_id": exception_id, "status": "resolved"}
                return "window_exception", exception_id, response

            return self._idempotent(connection, request_id=request_id, action="resolve_breakthrough",
                                    payload=payload, create=create)

    def list_exceptions(self, window_id: str) -> list[WindowException]:
        with self.database.locked() as connection:
            rows = connection.execute(
                "SELECT * FROM window_exceptions WHERE window_id=? ORDER BY created_at", (window_id,)
            ).fetchall()
            return [self._exception_model(row) for row in rows]

    def _exception_model(self, row) -> WindowException:
        return WindowException(row["exception_id"], row["window_id"], row["site_id"], row["kind"],
                               row["reason_code"], row["reason_text"], json.loads(row["evidence_json"]),
                               row["status"], row["actor_id"], row["created_at"], row["decision_key"])

    # ------------------------------------------------------------------- 复核

    def request_review(self, *, request_id: str, actor_id: str, window_id: str,
                       request_text: str) -> dict[str, Any]:
        """企业对暂停/终止的窗口申请复核；同一窗口同时只允许一条待决复核。"""

        payload = {"actor_id": actor_id, "window_id": window_id, "request_text": request_text}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            request_text = self._text(request_text, "request_text")

            def create() -> tuple[str, str, dict[str, Any]]:
                window = self._load_window(connection, window_id)
                if window["status"] not in ("suspended", "terminated"):
                    raise ConflictError("只有暂停或终止的窗口可以申请复核")
                pending = connection.execute(
                    "SELECT 1 FROM review_requests WHERE window_id=? AND status='requested'",
                    (window_id,),
                ).fetchone()
                if pending:
                    raise ConflictError("该窗口已有待决复核申请")
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_requests(review_id,window_id,site_id,status,request_text,"
                    "requested_by,requested_at) VALUES(?,?,?, 'requested', ?,?,?)",
                    (review_id, window_id, window["site_id"], request_text, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="review.requested",
                             resource_type="review_request", resource_id=review_id,
                             detail={"window_id": window_id}, occurred_at=self._now())
                response = {"review_id": review_id, "status": "requested"}
                return "review_request", review_id, response

            return self._idempotent(connection, request_id=request_id, action="request_review",
                                    payload=payload, create=create)

    def decide_review(self, *, request_id: str, actor_id: str, review_id: str,
                      decision: str, decision_text: str) -> dict[str, Any]:
        """复核决定：恢复窗口（并安排下次复核日）或维持原处置。"""

        payload = {"actor_id": actor_id, "review_id": review_id, "decision": decision,
                   "decision_text": decision_text}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            decision_text = self._text(decision_text, "decision_text")
            if decision not in ("reinstate", "uphold"):
                raise ValidationError("decision 只能是 reinstate 或 uphold")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute("SELECT * FROM review_requests WHERE review_id=?",
                                         (review_id,)).fetchone()
                if row is None:
                    raise NotFoundError("复核申请不存在")
                if row["status"] != "requested":
                    raise ConflictError("该复核已作出决定")
                window = self._load_window(connection, row["window_id"])
                decided_at = self._now()
                decided_date = self.clock.now().astimezone(
                    ZoneInfo(self._site(connection, window["site_id"])["timezone_name"])).date()
                next_review_date = None
                if decision == "reinstate":
                    if window["status"] != "suspended":
                        raise ConflictError("终止的窗口不能通过复核恢复，需重新满足连续合格条件")
                    rule = self._load_rule_set(connection, window["rule_set_id"])
                    parameters = json.loads(rule["parameters_json"])
                    next_review = decided_date + timedelta(days=parameters["review_interval_days"])
                    end_date = q.parse_date(window["end_date"])
                    if next_review > end_date:
                        next_review = end_date
                    next_review_date = next_review.isoformat()
                    connection.execute(
                        "UPDATE relief_windows SET status='active', next_review_date=? WHERE window_id=?",
                        (next_review_date, window["window_id"]),
                    )
                    self._add_exception(connection, actor_id=actor_id, window=window,
                                        kind="review_reinstate", reason_code="review_reinstate",
                                        reason_text=decision_text,
                                        evidence={"review_id": review_id,
                                                  "request_text": row["request_text"]},
                                        status="active", decision_key=review_id,
                                        occurred_at=decided_at)
                else:
                    self._add_exception(connection, actor_id=actor_id, window=window,
                                        kind="review_uphold", reason_code="review_uphold",
                                        reason_text=decision_text,
                                        evidence={"review_id": review_id,
                                                  "request_text": row["request_text"]},
                                        status="active", decision_key=review_id,
                                        occurred_at=decided_at)
                connection.execute(
                    "UPDATE review_requests SET status='decided', decision=?, decision_text=?,"
                    "decided_by=?, decided_at=?, next_review_date=? WHERE review_id=?",
                    (decision, decision_text, actor_id, decided_at, next_review_date, review_id),
                )
                append_event(connection, actor_id=actor_id, action=f"review.{decision}",
                             resource_type="review_request", resource_id=review_id,
                             detail={"window_id": window["window_id"], "decision": decision},
                             occurred_at=decided_at)
                response = {"review_id": review_id, "decision": decision,
                            "window_status": "active" if decision == "reinstate" else window["status"],
                            "next_review_date": next_review_date}
                return "review_request", review_id, response

            return self._idempotent(connection, request_id=request_id, action="decide_review",
                                    payload=payload, create=create)

    def get_review(self, review_id: str) -> ReviewRequest:
        with self.database.locked() as connection:
            row = connection.execute("SELECT * FROM review_requests WHERE review_id=?",
                                     (review_id,)).fetchone()
            if row is None:
                raise NotFoundError("复核申请不存在")
            return ReviewRequest(row["review_id"], row["window_id"], row["site_id"], row["status"],
                                 row["request_text"], row["requested_by"], row["requested_at"],
                                 row["decision"], row["decision_text"], row["decided_by"],
                                 row["decided_at"], row["next_review_date"])

    def list_windows(self, site_id: str) -> list[ReliefWindow]:
        with self.database.locked() as connection:
            rows = connection.execute(
                "SELECT * FROM relief_windows WHERE site_id=? ORDER BY start_date DESC", (site_id,)
            ).fetchall()
            return [self._window_model(row) for row in rows]

    def _window_model(self, row) -> ReliefWindow:
        return ReliefWindow(row["window_id"], row["site_id"], row["snapshot_id"], row["rule_set_id"],
                            row["status"], row["start_date"], row["end_date"], row["next_review_date"],
                            row["created_at"])

    # ----------------------------------------------------------- 业务原因解释查询

    def explain_qualification(self, actor_id: str, site_id: str) -> dict[str, Any]:
        """用自然业务原因解释当前资格、免访状态、下次复核日及每次例外的证据。"""

        with self.database.locked() as connection:
            return self._explain_qualification_locked(connection, actor_id, site_id)

    def _explain_qualification_locked(self, connection, actor_id: str, site_id: str) -> dict[str, Any]:
        actor = self._actor(connection, actor_id)
        site = self._site(connection, site_id)
        today = self.clock.now().astimezone(ZoneInfo(site["timezone_name"])).date()

        snapshot_row = connection.execute(
            "SELECT * FROM daily_snapshots WHERE site_id=? ORDER BY business_date DESC LIMIT 1",
            (site_id,),
        ).fetchone()
        qualification: dict[str, Any] | None = None
        required_streak = None
        rule_set_id = None
        reset_row = connection.execute(
            "SELECT * FROM qualification_resets WHERE site_id=? ORDER BY reset_date DESC, created_at DESC LIMIT 1",
            (site_id,),
        ).fetchone()
        last_reset = ({"reset_date": reset_row["reset_date"], "reason_code": reset_row["reason_code"]}
                      if reset_row else None)
        if snapshot_row is not None:
            parameters = json.loads(self._load_rule_set(connection, snapshot_row["rule_set_id"])["parameters_json"])
            required_streak = parameters["min_streak_days"]
            rule_set_id = snapshot_row["rule_set_id"]
            reasons = json.loads(snapshot_row["reasons_json"])
            qualification = {
                "business_date": snapshot_row["business_date"],
                "day_qualified": bool(snapshot_row["day_qualified"]),
                "streak_days": snapshot_row["streak_days"],
                "required_streak_days": required_streak,
                "summary": self._qualification_summary(snapshot_row, required_streak),
                "reasons": [{"gate": item["gate"], "code": item["code"], "passed": item["passed"],
                             "message": item["message"], "detail": item["detail"]} for item in reasons],
                "corrections": json.loads(snapshot_row["corrections_json"]),
                "last_eligibility_reset": last_reset,
                "snapshot_id": snapshot_row["snapshot_id"],
            }

        window_row = connection.execute(
            "SELECT * FROM relief_windows WHERE site_id=? ORDER BY created_at DESC LIMIT 1", (site_id,)
        ).fetchone()
        window_view: dict[str, Any] | None = None
        exceptions_view: list[dict[str, Any]] = []
        pending_review = None
        if window_row is not None:
            exception_rows = connection.execute(
                "SELECT * FROM window_exceptions WHERE window_id=? ORDER BY created_at",
                (window_row["window_id"],),
            ).fetchall()
            exceptions_view = [self._explain_exception(row) for row in exception_rows]
            review_row = connection.execute(
                "SELECT * FROM review_requests WHERE window_id=? AND status='requested' LIMIT 1",
                (window_row["window_id"],),
            ).fetchone()
            if review_row:
                pending_review = {"review_id": review_row["review_id"],
                                  "requested_at": review_row["requested_at"],
                                  "request_text": review_row["request_text"]}
            window_view = self._explain_window(window_row, exceptions_view, today, pending_review)

        pending_corrections = connection.execute(
            "SELECT COUNT(*) AS count FROM fact_corrections WHERE site_id=? AND status='pending'",
            (site_id,),
        ).fetchone()["count"]
        return {
            "site_id": site_id,
            "as_of_date": today.isoformat(),
            "rule_set_id": rule_set_id,
            "qualification": qualification,
            "window": window_view,
            "next_review_date": window_view["next_review_date"] if window_view else None,
            "next_review_reason": window_view["next_review_reason"] if window_view else "当前没有免访窗口，暂无需安排的复核日",
            "exceptions": exceptions_view,
            "pending_corrections": pending_corrections,
        }

    def _qualification_summary(self, snapshot_row, required_streak: int) -> str:
        qualified = bool(snapshot_row["day_qualified"])
        streak = snapshot_row["streak_days"]
        day = snapshot_row["business_date"]
        if qualified and streak >= required_streak:
            return (f"截至 {day} 已连续 {streak} 个合格日，满足连续 {required_streak} 日的免访资格门槛")
        if qualified:
            return (f"截至 {day} 当日各项条件合格，已连续 {streak} 个合格日，"
                    f"尚需连续合格满 {required_streak} 日才可获得免访窗口")
        failed = [item for item in json.loads(snapshot_row["reasons_json"]) if not item["passed"]]
        if failed:
            return f"截至 {day} 当日不合格：{ '；'.join(item['message'] for item in failed) }，连续合格天数重新起算"
        return f"截至 {day} 当日不合格，连续合格天数清零"

    def _explain_window(self, window_row, exceptions_view: list[dict[str, Any]],
                        today: date, pending_review: dict[str, Any] | None) -> dict[str, Any]:
        stored_status = window_row["status"]
        end_date = q.parse_date(window_row["end_date"])
        open_breakthrough = next((item for item in exceptions_view
                                  if item["kind"] == "emergency_breakthrough" and item["status"] == "open"), None)
        if stored_status == "terminated":
            effective = "terminated"
            summary = "免访窗口已被监管人员依据明确例外终止，企业需重新满足连续合格条件"
        elif today > end_date or stored_status == "expired":
            effective = "expired"
            summary = f"免访窗口已于 {window_row['end_date']} 到期结束"
        elif open_breakthrough:
            effective = "breakthrough_active"
            summary = ("发生严重事件，免访窗口被即时突破以安排紧急检查；窗口与原资格保留，"
                       "事件处置闭环后恢复免访")
        elif stored_status == "suspended":
            effective = "suspended"
            summary = "免访窗口因明确例外暂停，暂停期间监管可上门，企业可申请复核"
        else:
            effective = "active"
            summary = (f"免访窗口生效中（{window_row['start_date']} 至 {window_row['end_date']}），"
                       "期内无特殊情况不安排上门检查")
        if pending_review:
            next_review_reason = "企业复核申请审理中，复核结论将确定下次复核日"
            next_review_date = window_row["next_review_date"]
        elif effective == "active":
            next_review_date = window_row["next_review_date"]
            next_review_reason = f"窗口有效期内每{self._interval_text(window_row)}安排一次例行复核"
        elif effective == "breakthrough_active":
            next_review_date = window_row["next_review_date"]
            next_review_reason = "严重事件处置优先，处置闭环后回到例行复核节奏"
        else:
            next_review_date = None
            next_review_reason = "窗口不在生效状态，没有需要执行的复核日"
        return {
            "window_id": window_row["window_id"], "stored_status": stored_status,
            "effective_state": effective, "summary": summary,
            "start_date": window_row["start_date"], "end_date": window_row["end_date"],
            "next_review_date": next_review_date, "next_review_reason": next_review_reason,
            "pending_review": pending_review,
        }

    def _interval_text(self, window_row) -> str:
        with self.database.locked() as connection:
            rule = connection.execute(
                "SELECT parameters_json FROM rule_sets WHERE rule_set_id=?",
                (window_row["rule_set_id"],),
            ).fetchone()
        interval = json.loads(rule["parameters_json"])["review_interval_days"]
        return f"{interval}天"

    def _explain_exception(self, row) -> dict[str, Any]:
        kind_messages = {
            "regulator_suspend": "监管暂停",
            "regulator_terminate": "监管终止",
            "emergency_breakthrough": "严重事件紧急突破",
            "review_reinstate": "复核恢复窗口",
            "review_uphold": "复核维持原处置",
        }
        kind = row["kind"]
        status_text = {"open": "生效中", "resolved": "已闭环", "active": "有效"}.get(row["status"], row["status"])
        if kind == "emergency_breakthrough":
            evidence = json.loads(row["evidence_json"])
            if row["status"] == "open":
                explanation = (f"因 {evidence.get('incident_date')} 发生{evidence.get('severity')}级"
                               f"严重事件（事件键 {evidence.get('incident_event_key')}），"
                               "即时突破免访窗口安排紧急检查；原资格保留，未抹除任何快照")
            else:
                explanation = f"严重事件（{evidence.get('incident_event_key')}）已处置闭环，免访恢复"
        else:
            explanation = f"{kind_messages.get(kind, kind)}：依据【{row['reason_code']}】{row['reason_text']}（{status_text}）"
        return {
            "exception_id": row["exception_id"], "window_id": row["window_id"],
            "kind": kind, "kind_text": kind_messages.get(kind, kind),
            "reason_code": row["reason_code"], "reason_text": row["reason_text"],
            "evidence": json.loads(row["evidence_json"]), "status": row["status"],
            "status_text": status_text, "actor_id": row["actor_id"],
            "created_at": row["created_at"], "decision_key": row["decision_key"],
            "business_explanation": explanation,
        }
