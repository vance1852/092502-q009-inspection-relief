import threading
import unittest
from datetime import datetime, timedelta, timezone

from relief_core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from relief_core.qualification_service import EXCEPTION_REASON_CODES, QualificationService
from relief_core.storage import Database

RULE = {
    "min_consecutive_qualified_days": 3,
    "qualified_ratio_days": 3,
    "required_qualified_ratio": 1.0,
    "window_duration_days": 5,
    "review_interval_days": 5,
    "hazard_lookback_days": 30,
    "inspection_lookback_days": 30,
    "serious_lookback_days": 30,
}


class MutableClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, days=0):
        self.value += timedelta(days=days)


class QualificationTest(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.database = Database()
        self.service = QualificationService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="监管局")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                    display_name="监管员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rv", actor_id="a1", new_actor_id="rv1",
                                    display_name="复核员", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="au", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="示范工厂",
                                   timezone_name="Asia/Shanghai")
        self.service.register_site(request_id="site2", actor_id="op1", site_id="s2",
                                   organization_id="o1", name="二号工厂",
                                   timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    # ------------------------------------------------------------ 辅助方法

    def publish_rule(self, request_id="rule", criteria=None, effective="2026-09-25",
                     series=None):
        draft = self.service.create_rule_draft(request_id=request_id + "-draft", actor_id="a1",
                                               criteria=criteria or RULE, series_id=series)
        rule_id = draft.resource_id
        self.service.approve_rule(request_id=request_id + "-approve", actor_id="rv1",
                                  rule_id=rule_id)
        self.service.publish_rule(request_id=request_id + "-publish", actor_id="a1",
                                  rule_id=rule_id, effective_date=effective)
        return rule_id

    def report(self, day, *, site="s1", quality="qualified", on_time=True, request_id=None,
               submitted_hour=9):
        hour = submitted_hour if on_time else 20
        return self.service.record_daily_report(
            request_id=request_id or f"rep-{site}-{day}", actor_id="op1", site_id=site,
            report_date=day, quality=quality,
            submitted_at=f"{day}T{hour:02d}:00:00Z", due_at=f"{day}T18:00:00Z")

    def qualify_three_days(self):
        for day in ("2026-09-23", "2026-09-24", "2026-09-25"):
            self.report(day)
        return self.service.settle_day(request_id="settle-0925", actor_id="op1", site_id="s1",
                                       settlement_date="2026-09-25")

    # ------------------------------------------------------------ 规则生命周期

    def test_rule_requires_approval_then_publish_with_effective_date(self):
        draft = self.service.create_rule_draft(request_id="r1", actor_id="a1", criteria=RULE)
        rule = self.service.get_rule(draft.resource_id)
        self.assertEqual("draft", rule.status)
        self.assertIsNone(rule.effective_date)
        with self.assertRaises(ConflictError):
            self.service.publish_rule(request_id="p1", actor_id="a1",
                                      rule_id=draft.resource_id, effective_date="2026-09-25")
        self.service.approve_rule(request_id="ap1", actor_id="rv1", rule_id=draft.resource_id)
        self.service.publish_rule(request_id="p2", actor_id="a1", rule_id=draft.resource_id,
                                  effective_date="2026-09-25")
        self.assertEqual("published", self.service.get_rule(draft.resource_id).status)

    def test_effective_date_cannot_be_in_the_past(self):
        rule_id = self.publish_rule(request_id="rule", effective="2026-09-25")
        self.assertEqual(rule_id, rule_id)
        with self.assertRaises(ValidationError):
            self.publish_rule(request_id="rule2", effective="2026-09-20")

    def test_reviewer_cannot_draft_and_operator_cannot_approve(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_rule_draft(request_id="x1", actor_id="rv1", criteria=RULE)
        draft = self.service.create_rule_draft(request_id="x2", actor_id="a1", criteria=RULE)
        with self.assertRaises(PermissionDenied):
            self.service.approve_rule(request_id="x3", actor_id="op1",
                                      rule_id=draft.resource_id)

    def test_settlement_requires_effective_rule(self):
        with self.assertRaises(ConflictError):
            self.service.settle_day(request_id="settle-x", actor_id="op1", site_id="s1",
                                    settlement_date="2026-09-25")

    def test_invalid_criteria_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create_rule_draft(
                request_id="bad1", actor_id="a1",
                criteria={**RULE, "required_qualified_ratio": 1.5})
        with self.assertRaises(ValidationError):
            self.service.create_rule_draft(
                request_id="bad2", actor_id="a1",
                criteria={**RULE, "window_duration_days": 0})
        with self.assertRaises(ValidationError):
            self.service.create_rule_draft(
                request_id="bad3", actor_id="a1", criteria={**RULE, "unknown_field": 1})

    # ------------------------------------------------------------ 资格口径

    def test_quality_more_than_checkin_streak_late_report_blocks(self):
        self.publish_rule()
        self.report("2026-09-23")
        self.report("2026-09-24", on_time=False)
        self.report("2026-09-25")
        self.service.settle_day(request_id="set", actor_id="op1", site_id="s1",
                                settlement_date="2026-09-25")
        snapshot = self.service.get_snapshot("s1", "2026-09-25")
        self.assertFalse(snapshot.eligible)
        self.assertIsNone(self.service.get_window("s1"))
        self.assertTrue(any("迟报" in reason for reason in snapshot.reasons))

    def test_deficient_quality_breaks_streak(self):
        self.publish_rule()
        self.report("2026-09-23")
        self.report("2026-09-24", quality="deficient")
        self.report("2026-09-25")
        self.service.settle_day(request_id="set", actor_id="op1", site_id="s1",
                                settlement_date="2026-09-25")
        snapshot = self.service.get_snapshot("s1", "2026-09-25")
        self.assertFalse(snapshot.eligible)
        self.assertEqual(1, snapshot.consecutive_qualified_days)

    def test_open_and_repeated_hazards_block_closed_hazard_passes(self):
        self.publish_rule(criteria={**RULE, "max_rectification_count": 1})
        # 已闭环且只整改一次：不阻断
        self.service.record_hazard(request_id="h1", actor_id="op1", site_id="s1",
                                   hazard_key="hz-1", title="除尘设施积尘",
                                   found_date="2026-09-23", severity="general",
                                   rectification_count=1, closed_date="2026-09-24")
        receipt = self.qualify_three_days()
        self.assertTrue(receipt.resource_id and not receipt.replayed)
        self.assertEqual("active", self.service.get_window("s1").status)
        # 另一处场所：未闭环隐患阻断
        self.service.record_hazard(request_id="h2", actor_id="op1", site_id="s2",
                                   hazard_key="hz-2", title="危废暂存不规范",
                                   found_date="2026-09-24", severity="major")
        for day in ("2026-09-23", "2026-09-24", "2026-09-25"):
            self.report(day, site="s2", request_id=f"rep-s2-{day}")
        self.service.settle_day(request_id="set-s2", actor_id="op1", site_id="s2",
                                settlement_date="2026-09-25")
        self.assertFalse(self.service.get_snapshot("s2", "2026-09-25").eligible)

    def test_repeated_rectification_blocks(self):
        self.publish_rule(criteria={**RULE, "max_rectification_count": 1})
        self.service.record_hazard(request_id="h1", actor_id="op1", site_id="s1",
                                   hazard_key="hz-1", title="同类问题反复",
                                   found_date="2026-09-23", severity="general",
                                   rectification_count=3, closed_date="2026-09-24")
        self.qualify_three_days()
        self.assertFalse(self.service.get_snapshot("s1", "2026-09-25").eligible)

    def test_failed_inspection_and_findings_block(self):
        self.publish_rule()
        self.service.record_inspection(request_id="i1", actor_id="op1", site_id="s1",
                                       inspection_key="insp-1", inspection_date="2026-09-22",
                                       result="fail", finding_count=2)
        self.qualify_three_days()
        self.assertFalse(self.service.get_snapshot("s1", "2026-09-25").eligible)

    def test_assistance_is_positive_and_never_blocks(self):
        self.publish_rule()
        self.service.record_assistance(request_id="as1", actor_id="op1", site_id="s1",
                                       assistance_key="help-1", request_date="2026-09-24",
                                       topic="咨询危废台账规范")
        self.qualify_three_days()
        snapshot = self.service.get_snapshot("s1", "2026-09-25")
        self.assertTrue(snapshot.eligible)
        self.assertTrue(any("主动求助" in reason for reason in snapshot.reasons))

    def test_serious_event_is_absolute_veto(self):
        self.publish_rule()
        self.service.record_serious_event(request_id="ev1", actor_id="op1", site_id="s1",
                                          event_key="se-1", event_date="2026-09-24",
                                          occurred_at="2026-09-24T03:00:00Z",
                                          title="超标排放处罚", severity="serious")
        self.qualify_three_days()
        self.assertFalse(self.service.get_snapshot("s1", "2026-09-25").eligible)
        self.assertIn("一票否决", "".join(self.service.get_snapshot("s1", "2026-09-25").reasons))

    # ------------------------------------------------------------ 窗口

    def test_window_opened_once_and_expires_after_duration(self):
        self.publish_rule()
        self.qualify_three_days()
        window = self.service.get_window("s1")
        self.assertEqual(("2026-09-25", "2026-09-30", "2026-09-30"),
                         (window.start_date, window.end_date, window.next_review_date))
        # 连续合格不重复开窗
        self.clock.advance(days=1)
        self.report("2026-09-26")
        self.service.settle_day(request_id="set-26", actor_id="op1", site_id="s1",
                                settlement_date="2026-09-26")
        self.assertEqual(window.window_id, self.service.get_window("s1").window_id)
        # 到期后下一次合格结算把旧窗口置为 expired，并开启新窗口
        self.clock.advance(days=5)
        for offset, day in enumerate(("2026-09-29", "2026-09-30", "2026-10-01")):
            self.report(day, request_id=f"rep-late-{offset}")
        self.service.settle_day(request_id="set-1001", actor_id="op1", site_id="s1",
                                settlement_date="2026-10-01")
        statuses = {window.start_date: window.status for window in self.service.list_windows("s1")}
        self.assertEqual("expired", statuses["2026-09-25"])
        self.assertEqual("active", statuses["2026-10-01"])
        self.assertEqual(2, len(statuses))

    def test_window_not_opened_twice_concurrently(self):
        self.publish_rule()
        self.qualify_three_days()
        # 多线程重复结算同一天：只有一份快照，窗口唯一索引兜底
        errors = []

        def settle(rid):
            try:
                self.service.settle_day(request_id=rid, actor_id="op1", site_id="s1",
                                        settlement_date="2026-09-25")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=settle, args=(f"cc-{i}",)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertEqual(1, len([s for s in self.service.list_snapshots("s1")
                                 if s.settlement_date == "2026-09-25"]))

    # ------------------------------------------------------------ 紧急突破

    def test_emergency_breaches_window_immediately_without_erasing_qualification(self):
        self.publish_rule()
        self.qualify_three_days()
        self.service.record_serious_event(request_id="emg", actor_id="op1", site_id="s1",
                                          event_key="emg-1", event_date="2026-09-25",
                                          occurred_at="2026-09-25T10:30:00Z",
                                          title="突发泄漏", severity="emergency")
        self.assertEqual("breached", self.service.get_window("s1").status)
        # 原资格快照不变
        self.assertTrue(self.service.get_snapshot("s1", "2026-09-25").eligible)
        # 重复事件不产生第二份突破决定
        self.service.record_serious_event(request_id="emg-replay", actor_id="op1", site_id="s1",
                                          event_key="emg-1", event_date="2026-09-25",
                                          occurred_at="2026-09-25T10:30:00Z",
                                          title="突发泄漏", severity="emergency")
        breaches = [e for e in self.service.list_window_exceptions("s1")
                    if e.kind == "emergency_breach"]
        self.assertEqual(1, len(breaches))
        self.assertEqual("emg-1", breaches[0].evidence["event_key"])
        # 未解除不能恢复
        self.service.request_review(request_id="review-emg", actor_id="op1", site_id="s1",
                                    reason="企业主张已处置")
        review = self.service.list_reviews("s1")[0]
        with self.assertRaises(ConflictError):
            self.service.decide_review(request_id="dec-x", actor_id="rv1",
                                       review_id=review.review_id, decision="resumed",
                                       decision_note="恢复")
        self.service.resolve_emergency(request_id="resolve", actor_id="op1", site_id="s1",
                                       event_key="emg-1")
        self.service.decide_review(request_id="dec-ok", actor_id="rv1",
                                   review_id=review.review_id, decision="resumed",
                                   decision_note="现场处置完毕，恢复免访窗口")
        self.assertEqual("active", self.service.get_window("s1").status)
        self.assertIsNotNone(self.service.list_window_exceptions("s1")[0].lifted_at)

    def test_emergency_without_active_window_is_recorded_without_decision(self):
        self.publish_rule()
        self.report("2026-09-23")
        self.service.record_serious_event(request_id="emg", actor_id="op1", site_id="s1",
                                          event_key="emg-9", event_date="2026-09-24",
                                          occurred_at="2026-09-24T10:30:00Z",
                                          title="无窗口时的紧急事件", severity="emergency")
        self.report("2026-09-24")
        self.report("2026-09-25")
        self.service.settle_day(request_id="set", actor_id="op1", site_id="s1",
                                settlement_date="2026-09-25")
        self.assertIsNone(self.service.get_window("s1"))
        self.assertEqual([], self.service.list_window_exceptions("s1"))

    # ------------------------------------------------------------ 暂停/终止

    def test_suspend_and_terminate_require_codes_and_evidence(self):
        self.publish_rule()
        self.qualify_three_days()
        with self.assertRaises(ValidationError):
            self.service.suspend_window(request_id="bad-code", actor_id="op1", site_id="s1",
                                        reason_code="NOT_A_CODE", reason_text="随便看看",
                                        evidence={"x": 1})
        with self.assertRaises(ValidationError):
            self.service.suspend_window(request_id="no-evidence", actor_id="op1", site_id="s1",
                                        reason_code="SPECIAL_CAMPAIGN", reason_text="专项行动",
                                        evidence={})
        with self.assertRaises(PermissionDenied):
            self.service.terminate_window(request_id="auditor", actor_id="au1", site_id="s1",
                                          reason_code="SPECIAL_CAMPAIGN", reason_text="x",
                                          evidence={"d": 1})
        self.service.suspend_window(
            request_id="sus", actor_id="op1", site_id="s1", reason_code="PUBLIC_TIP_VERIFIED",
            reason_text="群众举报夜间偷排，需现场核实",
            evidence={"tip_id": "tip-2026-09-1", "preliminary_verification": "影像吻合"})
        self.assertEqual("suspended", self.service.get_window("s1").status)
        with self.assertRaises(ConflictError):
            self.service.suspend_window(request_id="sus2", actor_id="op1", site_id="s1",
                                        reason_code="SPECIAL_CAMPAIGN", reason_text="再次暂停",
                                        evidence={"doc": "1"})
        self.service.terminate_window(
            request_id="ter", actor_id="op1", site_id="s1",
            reason_code="MAJOR_HAZARD_VERIFIED", reason_text="现场核实重大隐患",
            evidence={"hazard_key": "hz-x", "inspector": "op1"})
        self.assertEqual("terminated", self.service.get_window("s1").status)

    def test_exception_codes_are_documented(self):
        self.assertEqual(5, len(EXCEPTION_REASON_CODES))

    # ------------------------------------------------------------ 复核

    def test_review_upheld_and_adjusted(self):
        self.publish_rule()
        self.qualify_three_days()
        self.service.suspend_window(request_id="sus", actor_id="op1", site_id="s1",
                                    reason_code="SPECIAL_CAMPAIGN", reason_text="专项行动",
                                    evidence={"document": "通知"})
        self.service.request_review(request_id="rv1", actor_id="op1", site_id="s1",
                                    reason="企业说明不存在问题")
        review = self.service.list_reviews("s1")[0]
        self.service.decide_review(request_id="d1", actor_id="rv1", review_id=review.review_id,
                                   decision="adjusted", decision_note="调整复核日",
                                   new_review_date="2026-09-28")
        self.assertEqual("active", self.service.get_window("s1").status)
        self.assertEqual("2026-09-28", self.service.get_window("s1").next_review_date)
        with self.assertRaises(ConflictError):
            self.service.decide_review(request_id="d2", actor_id="rv1",
                                       review_id=review.review_id, decision="upheld",
                                       decision_note="重复决定")

    # ------------------------------------------------------------ 封账与更正

    def test_late_facts_follow_correction_flow_and_old_snapshot_kept(self):
        rule_v1 = self.publish_rule(request_id="r1")
        self.qualify_three_days()
        original = self.service.get_snapshot("s1", "2026-09-25")
        # 封账后补报历史隐患：进入更正表，旧快照不重算
        receipt = self.service.record_hazard(
            request_id="late", actor_id="op1", site_id="s1", hazard_key="hz-late",
            title="封账前发现但迟报的隐患", found_date="2026-09-22", severity="major")
        self.assertEqual("snapshot_correction", receipt.resource_type)
        self.assertFalse(receipt.replayed)
        corrections = self.service.list_corrections("s1", include_consumed=False)
        self.assertEqual(1, len(corrections))
        self.assertEqual(original.snapshot_id,
                         self.service.get_snapshot("s1", "2026-09-25").snapshot_id)
        self.assertTrue(self.service.get_snapshot("s1", "2026-09-25").eligible)
        # 发布新规则版本：旧快照仍绑定旧规则
        self.service.revise_rule(request_id="r2-draft", actor_id="a1",
                                 series_id=self.service.get_rule(rule_v1).series_id,
                                 criteria={**RULE, "min_consecutive_qualified_days": 5})
        rule_v2 = self.service.list_rules(self.service.get_rule(rule_v1).series_id)[-1].rule_id
        self.service.approve_rule(request_id="r2-appr", actor_id="rv1", rule_id=rule_v2)
        self.service.publish_rule(request_id="r2-pub", actor_id="a1", rule_id=rule_v2,
                                  effective_date="2026-09-26")
        # 下一结算日：迟到隐患计入，即使自查继续合格也不合格
        self.clock.advance(days=1)
        self.report("2026-09-26")
        self.service.settle_day(request_id="set-26", actor_id="op1", site_id="s1",
                                settlement_date="2026-09-26")
        new_snapshot = self.service.get_snapshot("s1", "2026-09-26")
        self.assertEqual(rule_v2, new_snapshot.rule_id)
        self.assertFalse(new_snapshot.eligible)
        self.assertEqual(rule_v1, self.service.get_snapshot("s1", "2026-09-25").rule_id)
        self.assertEqual([], self.service.list_corrections("s1", include_consumed=False))

    def test_correction_same_key_same_content_replays_different_conflicts(self):
        self.publish_rule()
        self.qualify_three_days()
        kwargs = dict(actor_id="op1", site_id="s1", hazard_key="hz-x",
                      title="迟报隐患", found_date="2026-09-21", severity="general")
        first = self.service.record_hazard(request_id="c1", **kwargs)
        second = self.service.record_hazard(request_id="c2", **kwargs)
        self.assertEqual(first.resource_id, second.resource_id)
        self.assertEqual(1, len(self.service.list_corrections("s1", include_consumed=False)))
        with self.assertRaises(ConflictError):
            self.service.record_hazard(request_id="c3", **{**kwargs, "severity": "major"})

    # ------------------------------------------------------------ 解释视图

    def test_qualification_view_explains_status_review_date_and_evidence(self):
        self.publish_rule()
        self.qualify_three_days()
        self.service.record_serious_event(request_id="emg", actor_id="op1", site_id="s1",
                                          event_key="emg-1", event_date="2026-09-25",
                                          occurred_at="2026-09-25T10:30:00Z",
                                          title="突发泄漏", severity="emergency")
        view = self.service.get_qualification("s1")
        text = "\n".join(view.explanation)
        self.assertIn("下一次复核日 2026-09-30", text)
        self.assertIn("突发泄漏", text)
        self.assertIn("emg-1", text)
        self.assertIn("原资格", text)
        with self.assertRaises(NotFoundError):
            self.service.get_qualification("s2")

    def test_settlement_must_follow_date_order(self):
        self.publish_rule()
        self.qualify_three_days()
        with self.assertRaises(ConflictError):
            self.service.settle_day(request_id="back", actor_id="op1", site_id="s1",
                                    settlement_date="2026-09-24")


if __name__ == "__main__":
    unittest.main()
