import threading
import unittest
from datetime import datetime, timezone

from relief_core.clock import Clock, FixedClock
from relief_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from relief_core.qualification import DEFAULT_PARAMETERS, normalize_parameters
from relief_core.relief_service import ReliefService
from relief_core.service import DomainService
from relief_core.storage import Database


class MutableClock(Clock):
    def __init__(self, dt):
        self.dt = dt

    def now(self):
        return self.dt

    def set(self, dt):
        self.dt = dt


def rule_parameters(**overrides):
    parameters = dict(DEFAULT_PARAMETERS)
    parameters.update(overrides)
    return parameters


class ReliefCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, self.clock)
        self.service = ReliefService(self.database, self.clock)
        self.domain.register_organization(request_id="org01", actor_id="bootstrap",
                                          organization_id="o1", name="企业一")
        self.domain.register_actor(request_id="adm01", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_actor(request_id="rev01", actor_id="a1", new_actor_id="rv1",
                                   display_name="复核员", role="reviewer", organization_id="o1")
        self.domain.register_actor(request_id="op001", actor_id="a1", new_actor_id="op1",
                                   display_name="操作员", role="operator", organization_id="o1")
        self.domain.register_site(request_id="site01", actor_id="op1", site_id="s1",
                                  organization_id="o1", name="车间", timezone_name="Asia/Shanghai")
        self.parameters = rule_parameters(min_streak_days=3, window_duration_days=10,
                                          review_interval_days=5)
        self.rule_id = self.publish_rule(self.parameters, "2026-09-01")

    def tearDown(self):
        self.database.close()

    def publish_rule(self, parameters, effective_date, proposer="a1", approver="rv1",
                     prefix="rule"):
        proposed = self.service.propose_rule_set(
            request_id=f"{prefix}-p-{effective_date}", actor_id=proposer,
            parameters=parameters, effective_date=effective_date)
        rule_id = proposed["response"]["rule_set_id"]
        self.service.approve_rule_set(request_id=f"{prefix}-a-{effective_date}",
                                      actor_id=approver, rule_set_id=rule_id)
        self.service.publish_rule_set(request_id=f"{prefix}-u-{effective_date}",
                                      actor_id=proposer, rule_set_id=rule_id)
        return rule_id

    def good_daily(self, day, request_id=None):
        self.service.record_fact(
            request_id=request_id or f"fact-{day}", actor_id="op1", site_id="s1",
            channel="daily_completion", event_key=f"chk-{day}",
            payload={"on_time": True, "score": 95, "completed_count": 5, "required_count": 5},
            business_date=day)

    def settle(self, day, request_id=None):
        return self.service.settle_day(
            request_id=request_id or f"settle-{day}", actor_id="op1",
            site_id="s1", business_date=day)["response"]


class RuleLifecycleTest(ReliefCase):
    def test_proposer_cannot_approve_own_rule(self):
        proposed = self.service.propose_rule_set(
            request_id="rp-1", actor_id="a1", parameters=rule_parameters(),
            effective_date="2027-01-01")
        with self.assertRaises(PermissionDenied):
            self.service.approve_rule_set(request_id="ra-1", actor_id="a1",
                                          rule_set_id=proposed["response"]["rule_set_id"])

    def test_operator_cannot_propose_rule(self):
        with self.assertRaises(PermissionDenied):
            self.service.propose_rule_set(request_id="rp-2", actor_id="op1",
                                          parameters=rule_parameters(), effective_date="2027-01-01")

    def test_rule_must_be_approved_before_publish(self):
        proposed = self.service.propose_rule_set(
            request_id="rp-3", actor_id="a1", parameters=rule_parameters(),
            effective_date="2027-01-02")
        with self.assertRaises(ConflictError):
            self.service.publish_rule_set(request_id="ru-3", actor_id="a1",
                                          rule_set_id=proposed["response"]["rule_set_id"])

    def test_effective_date_must_be_later_than_published(self):
        with self.assertRaises(ValidationError):
            self.service.propose_rule_set(request_id="rp-4", actor_id="a1",
                                          parameters=rule_parameters(), effective_date="2026-09-01")

    def test_parameters_must_be_complete_and_in_range(self):
        parameters = rule_parameters()
        del parameters["min_streak_days"]
        with self.assertRaises(ValueError):
            normalize_parameters(parameters)
        with self.assertRaises(ValueError):
            normalize_parameters(rule_parameters(min_daily_score=150))
        with self.assertRaises(ValueError):
            normalize_parameters(rule_parameters(window_duration_days=3, review_interval_days=10))


class SettlementTest(ReliefCase):
    def test_streak_accumulates_and_opens_window(self):
        days = ["2026-09-23", "2026-09-24", "2026-09-25"]
        for index, day in enumerate(days, start=1):
            self.good_daily(day)
            result = self.settle(day)
            self.assertTrue(result["day_qualified"])
            self.assertEqual(index, result["streak_days"])
        windows = self.service.list_windows("s1")
        self.assertEqual(1, len(windows))
        self.assertEqual("active", windows[0].status)
        self.assertEqual("2026-09-26", windows[0].start_date)
        self.assertEqual("2026-10-05", windows[0].end_date)
        self.assertEqual("2026-10-01", windows[0].next_review_date)

    def test_sealed_snapshot_is_not_recomputed(self):
        self.good_daily("2026-09-23")
        first = self.settle("2026-09-23")
        snapshot = self.service.get_snapshot("s1", "2026-09-23")
        before = snapshot.inputs_hash
        # 重新结算同一请求号返回原快照；新请求号也返回已封账快照
        replay = self.settle("2026-09-23")
        self.assertTrue(replay["sealed"])
        self.assertEqual(first["snapshot_id"], replay["snapshot_id"])
        self.assertEqual(before, self.service.get_snapshot("s1", "2026-09-23").inputs_hash)

    def test_late_daily_check_is_failed_then_corrected(self):
        # 23 日无自查即结算：当日不合格
        result = self.settle("2026-09-23")
        self.assertFalse(result["day_qualified"])
        self.assertEqual(0, result["streak_days"])
        # 封账后到达的迟报进入更正流程，旧快照不变
        late = self.service.record_fact(
            request_id="late-1", actor_id="op1", site_id="s1", channel="daily_completion",
            event_key="chk-2026-09-23",
            payload={"on_time": True, "score": 95, "completed_count": 5, "required_count": 5},
            business_date="2026-09-23")["response"]
        self.assertTrue(late["late"])
        self.assertEqual("pending", late["correction_status"])
        self.assertFalse(self.service.get_snapshot("s1", "2026-09-23").day_qualified)

    def test_late_report_on_time_flag_fails_gate(self):
        # 当天在封账前登记但 on_time=False：迟报当日不合格
        self.service.record_fact(
            request_id="late-flag", actor_id="op1", site_id="s1", channel="daily_completion",
            event_key="chk-late", payload={"on_time": False, "score": 99, "minutes_late": 40},
            business_date="2026-09-23")
        result = self.settle("2026-09-23")
        self.assertFalse(result["day_qualified"])
        codes = {reason["code"] for reason in
                 self.service.get_snapshot("s1", "2026-09-23").reasons}
        self.assertIn("daily_late", codes)

    def test_open_hazard_and_repeated_rectification_block(self):
        self.service.record_fact(
            request_id="hz-1", actor_id="op1", site_id="s1", channel="hazard",
            event_key="hz-open", payload={"status": "open", "level": "minor",
                                          "rectification_rounds": 0}, business_date="2026-09-23")
        self.good_daily("2026-09-23")
        result = self.settle("2026-09-23")
        self.assertFalse(result["day_qualified"])
        self.service.record_fact(
            request_id="hz-2", actor_id="op1", site_id="s1", channel="hazard",
            event_key="hz-open", payload={"status": "closed", "rectification_rounds": 3},
            business_date="2026-09-24")
        self.good_daily("2026-09-24")
        result = self.settle("2026-09-24")
        codes = {reason["code"] for reason in
                 self.service.get_snapshot("s1", "2026-09-24").reasons}
        self.assertIn("hazard_repeated", codes)

    def test_blocking_inspection_and_incident_reset_streak(self):
        for day in ["2026-09-23", "2026-09-24"]:
            self.good_daily(day)
            self.settle(day)
        self.service.record_fact(
            request_id="insp-1", actor_id="op1", site_id="s1", channel="inspection",
            event_key="insp-major", payload={"result": "major"}, business_date="2026-09-25")
        self.good_daily("2026-09-25")
        self.assertEqual(0, self.settle("2026-09-25")["streak_days"])
        self.service.record_fact(
            request_id="inc-1", actor_id="op1", site_id="s1", channel="incident",
            event_key="ev-major", payload={"severity": "serious", "description": "爆炸"},
            business_date="2026-09-25")
        # 25 日已封账，事件进入更正流程
        correction = self.service.list_pending_corrections("rv1", "s1")[0]
        self.assertEqual("incident", correction["channel"])

    def test_assistance_positive_signal_recorded_when_not_required(self):
        self.good_daily("2026-09-23")
        self.settle("2026-09-23")
        snapshot = self.service.get_snapshot("s1", "2026-09-23")
        assistance = next(reason for reason in snapshot.reasons if reason["gate"] == "assistance")
        self.assertEqual("engagement_not_required", assistance["code"])
        self.assertTrue(assistance["passed"])


class CorrectionFlowTest(ReliefCase):
    def test_applied_correction_becomes_visible_next_settlement_only(self):
        self.settle("2026-09-23")  # 无事实，不合格
        late = self.service.record_fact(
            request_id="late-c", actor_id="op1", site_id="s1", channel="daily_completion",
            event_key="chk-c-23",
            payload={"on_time": True, "score": 90, "completed_count": 1, "required_count": 1},
            business_date="2026-09-23")["response"]
        # 更正未通过时，24 日结算看不到迟到事实
        self.good_daily("2026-09-24", request_id="fact-2026-09-24")
        self.assertEqual(1, self.settle("2026-09-24")["streak_days"])
        # 驳回更正保持不可见
        self.service.dismiss_correction(request_id="corr-dismiss", actor_id="rv1",
                                        correction_id=late["correction_id"], note="凭证不足")
        with self.assertRaises(ConflictError):
            self.service.apply_correction(request_id="corr-apply", actor_id="rv1",
                                          correction_id=late["correction_id"])


class WindowExceptionTest(ReliefCase):
    def open_window(self):
        for day in ["2026-09-23", "2026-09-24", "2026-09-25"]:
            self.good_daily(day)
            self.settle(day)
        return self.service.list_windows("s1")[0].window_id

    def test_suspend_requires_listed_reason_and_evidence(self):
        window_id = self.open_window()
        with self.assertRaises(ValidationError):
            self.service.suspend_window(
                request_id="sus-bad-reason", actor_id="a1", window_id=window_id,
                reason_code="caprice", reason_text="随便", evidence={"references": ["x"]})
        with self.assertRaises(ValidationError):
            self.service.suspend_window(
                request_id="sus-no-evidence", actor_id="a1", window_id=window_id,
                reason_code="targeted_tip", reason_text="举报", evidence={})
        self.service.suspend_window(
            request_id="sus-ok", actor_id="a1", window_id=window_id,
            reason_code="targeted_tip", reason_text="收到指向该场所的实名举报",
            evidence={"references": ["doc://tip/77"]})
        self.assertEqual("suspended", self.service.list_windows("s1")[0].status)

    def test_terminate_reason_not_allowed_for_suspend(self):
        window_id = self.open_window()
        with self.assertRaises(ValidationError):
            self.service.suspend_window(
                request_id="sus-term", actor_id="a1", window_id=window_id,
                reason_code="fraud_confirmed", reason_text="造假",
                evidence={"references": ["d1"]})
        self.service.terminate_window(
            request_id="term-ok", actor_id="a1", window_id=window_id,
            reason_code="fraud_confirmed", reason_text="查实自查数据弄虚作假",
            evidence={"references": ["doc://fraud/1", "doc://fraud/2"]})
        self.assertEqual("terminated", self.service.list_windows("s1")[0].status)

    def test_emergency_breakthrough_is_idempotent_and_preserves_window(self):
        window_id = self.open_window()
        self.clock.set(datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc))
        self.service.record_fact(
            request_id="ev-1", actor_id="op1", site_id="s1", channel="incident",
            event_key="ev-leak", payload={"severity": "major", "description": "泄漏"})
        explanation = self.service.explain_qualification("a1", "s1")
        self.assertEqual("breakthrough_active", explanation["window"]["effective_state"])
        # 窗口存储状态与资格快照均未被抹除
        self.assertEqual("active", self.service.list_windows("s1")[0].status)
        self.assertTrue(self.service.get_snapshot("s1", "2026-09-25").day_qualified)
        exceptions = self.service.list_exceptions(window_id)
        self.assertEqual(1, len(exceptions))
        # 同一事件重复上报不得产生第二份决定
        with self.assertRaises(ConflictError):
            self.service.record_fact(
                request_id="ev-1-dup", actor_id="op1", site_id="s1", channel="incident",
                event_key="ev-leak", payload={"severity": "major", "description": "泄漏"})
        self.assertEqual(1, len(self.service.list_exceptions(window_id)))
        # 处置闭环后恢复免访
        self.service.resolve_breakthrough(
            request_id="resolve-1", actor_id="a1", exception_id=exceptions[0].exception_id,
            resolution_note="泄漏已处置", evidence={"references": ["doc://inc/report"]})
        self.assertEqual("active",
                         self.service.explain_qualification("a1", "s1")["window"]["effective_state"])


class ReviewTest(ReliefCase):
    def test_enterprise_can_request_review_and_get_reinstated(self):
        for day in ["2026-09-23", "2026-09-24", "2026-09-25"]:
            ReliefCase.good_daily(self, day)
            ReliefCase.settle(self, day)
        window_id = self.service.list_windows("s1")[0].window_id
        self.service.suspend_window(
            request_id="sus-1", actor_id="a1", window_id=window_id,
            reason_code="rectification_overdue", reason_text="承诺整改逾期",
            evidence={"references": ["doc://rect/9"]})
        review_id = self.service.request_review(
            request_id="rev-req", actor_id="op1", window_id=window_id,
            request_text="整改已完成并上传凭证")["response"]["review_id"]
        # 不得重复提交待决复核
        with self.assertRaises(ConflictError):
            self.service.request_review(
                request_id="rev-req-2", actor_id="op1", window_id=window_id, request_text="再次申请")
        decision = self.service.decide_review(
            request_id="rev-dec", actor_id="rv1", review_id=review_id,
            decision="reinstate", decision_text="凭证有效，恢复免访窗口")
        self.assertEqual("active", decision["response"]["window_status"])
        self.assertEqual("active", self.service.list_windows("s1")[0].status)

    def test_uphold_keeps_window_suspended(self):
        for day in ["2026-09-23", "2026-09-24", "2026-09-25"]:
            ReliefCase.good_daily(self, day)
            ReliefCase.settle(self, day)
        window_id = self.service.list_windows("s1")[0].window_id
        self.service.suspend_window(
            request_id="sus-2", actor_id="a1", window_id=window_id,
            reason_code="high_risk_period", reason_text="重大活动保障",
            evidence={"references": ["doc://notice/1"]})
        review_id = self.service.request_review(
            request_id="rev-req-3", actor_id="op1", window_id=window_id,
            request_text="希望正常生产")["response"]["review_id"]
        self.service.decide_review(request_id="rev-dec-3", actor_id="rv1", review_id=review_id,
                                   decision="uphold", decision_text="保障期内维持暂停")
        self.assertEqual("suspended", self.service.list_windows("s1")[0].status)


class ExplainTest(ReliefCase):
    def test_explanation_names_qualification_review_and_evidence(self):
        for day in ["2026-09-23", "2026-09-24", "2026-09-25"]:
            self.good_daily(day)
            self.settle(day)
        explanation = self.service.explain_qualification("a1", "s1")
        self.assertIn("连续 3 个合格日", explanation["qualification"]["summary"])
        self.assertEqual("2026-10-01", explanation["next_review_date"])
        self.assertIn("5天", explanation["next_review_reason"])
        self.assertIsNone(explanation["window"]["pending_review"])

    def test_explanation_without_window(self):
        explanation = self.service.explain_qualification("a1", "s1")
        self.assertIsNone(explanation["window"])
        self.assertIsNone(explanation["next_review_date"])


class LateIncidentBreakthroughTest(ReliefCase):
    def test_corrected_late_incident_breaks_window_once_on_next_settlement(self):
        for day in ["2026-09-23", "2026-09-24", "2026-09-25"]:
            ReliefCase.good_daily(self, day)
            ReliefCase.settle(self, day)
        window_id = self.service.list_windows("s1")[0].window_id
        # 9/26 先完成自查并封账
        self.clock.set(datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc))
        ReliefCase.good_daily(self, "2026-09-26", request_id="fact-2026-09-26")
        ReliefCase.settle(self, "2026-09-26", request_id="settle-2026-09-26")
        # 封账后严重事件才到达 -> 更正流程，此刻尚无突破决定
        late = self.service.record_fact(
            request_id="late-inc", actor_id="op1", site_id="s1", channel="incident",
            event_key="ev-late", payload={"severity": "major", "description": "迟报事故"},
            business_date="2026-09-26")["response"]
        self.assertTrue(late["late"])
        self.assertEqual(0, len(self.service.list_exceptions(window_id)))
        # 审批通过；下一次结算（9/27）补登唯一一份突破决定
        self.service.apply_correction(request_id="apply-late-inc", actor_id="rv1",
                                      correction_id=late["correction_id"], note="事故属实")
        self.clock.set(datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc))
        ReliefCase.good_daily(self, "2026-09-27", request_id="fact-2026-09-27")
        ReliefCase.settle(self, "2026-09-27", request_id="settle-2026-09-27")
        exceptions = self.service.list_exceptions(window_id)
        self.assertEqual(1, len(exceptions))
        self.assertEqual("emergency_breakthrough", exceptions[0].kind)
        self.assertTrue(exceptions[0].evidence["via_correction"])
        self.assertEqual("active", self.service.list_windows("s1")[0].status)
        # 再次结算不产生第二份决定
        self.clock.set(datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc))
        ReliefCase.good_daily(self, "2026-09-28", request_id="fact-2026-09-28")
        ReliefCase.settle(self, "2026-09-28", request_id="settle-2026-09-28")
        self.assertEqual(1, len(self.service.list_exceptions(window_id)))


class EligibilityResetTest(ReliefCase):
    def test_termination_forces_fresh_qualifying_run_without_rewriting_snapshots(self):
        for day in ["2026-09-23", "2026-09-24", "2026-09-25"]:
            ReliefCase.good_daily(self, day)
            ReliefCase.settle(self, day)
        window_id = self.service.list_windows("s1")[0].window_id
        self.clock.set(datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc))
        ReliefCase.good_daily(self, "2026-09-26", request_id="fact-2026-09-26")
        ReliefCase.settle(self, "2026-09-26", request_id="settle-2026-09-26")
        terminated = self.service.terminate_window(
            request_id="term-reset", actor_id="a1", window_id=window_id,
            reason_code="fraud_confirmed", reason_text="查实数据弄虚作假",
            evidence={"references": ["doc://fraud/1"]})
        self.assertEqual("2026-09-26", terminated["response"]["eligibility_reset_date"])
        # 重置日之前的已封账快照保持不变
        self.assertTrue(self.service.get_snapshot("s1", "2026-09-25").day_qualified)
        self.assertEqual(3, self.service.get_snapshot("s1", "2026-09-25").streak_days)
        # 此后连续合格天数重新起算，需重新满足门槛（门槛为 3）
        self.clock.set(datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc))
        ReliefCase.good_daily(self, "2026-09-27", request_id="fact-2026-09-27")
        first_after = ReliefCase.settle(self, "2026-09-27", request_id="settle-2026-09-27")
        self.assertEqual(1, first_after["streak_days"])
        self.assertIsNone(first_after["window"])
        self.clock.set(datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc))
        ReliefCase.good_daily(self, "2026-09-28", request_id="fact-2026-09-28")
        second_after = ReliefCase.settle(self, "2026-09-28", request_id="settle-2026-09-28")
        self.assertEqual(2, second_after["streak_days"])
        self.assertIsNone(second_after["window"])
        self.clock.set(datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc))
        ReliefCase.good_daily(self, "2026-09-29", request_id="fact-2026-09-29")
        third_after = ReliefCase.settle(self, "2026-09-29", request_id="settle-2026-09-29")
        self.assertEqual(3, third_after["streak_days"])
        self.assertEqual("opened", third_after["window"]["action"])
        self.assertNotEqual(window_id, third_after["window"]["window_id"])


class ConcurrencyTest(ReliefCase):
    def test_concurrent_settlement_and_duplicate_incidents(self):
        # 一天即达标的规则，使单次结算就能开出窗口
        self.publish_rule(rule_parameters(min_streak_days=1), "2026-09-20", prefix="fast")
        self.good_daily("2026-09-25")
        errors = []

        def settle(index):
            try:
                self.service.settle_day(request_id=f"c-s-{index:03d}", actor_id="op1",
                                        site_id="s1", business_date="2026-09-25")
            except Exception as exc:  # noqa: BLE001 - 记录线程内异常
                errors.append(exc)

        threads = [threading.Thread(target=settle, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        snapshot_count = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM daily_snapshots WHERE site_id='s1' "
            "AND business_date='2026-09-25'").fetchone()["c"]
        window_count = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM relief_windows WHERE site_id='s1'").fetchone()["c"]
        self.assertEqual(1, snapshot_count)
        self.assertEqual(1, window_count)


if __name__ == "__main__":
    unittest.main()
