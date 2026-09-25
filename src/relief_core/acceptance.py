"""运行基础档案与“无事不扰”资格服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from relief_core.clock import FixedClock
from relief_core.qualification_service import QualificationService
from relief_core.storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记与资格结算链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = QualificationService(
            database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范企业")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001",
                               new_actor_id="operator-001", display_name="环保负责人",
                               role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产场所",
                              timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001",
                                           site_id="site-001", category="compliance_fact",
                                           external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001",
                                            site_id="site-001", category="compliance_fact",
                                            external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # 规则起草 -> 审批 -> 带生效日期发布。
        criteria = {"min_consecutive_qualified_days": 2, "qualified_ratio_days": 2,
                    "required_qualified_ratio": 1.0, "window_duration_days": 30,
                    "review_interval_days": 30, "hazard_lookback_days": 60,
                    "inspection_lookback_days": 180, "serious_lookback_days": 365}
        draft = service.create_rule_draft(request_id="req-rule-draft", actor_id="admin-001",
                                          criteria=criteria)
        service.approve_rule(request_id="req-rule-approve", actor_id="admin-001",
                             rule_id=draft.resource_id)
        service.publish_rule(request_id="req-rule-publish", actor_id="admin-001",
                             rule_id=draft.resource_id, effective_date="2026-09-25")

        # 连续两天按时高质量自查，企业曾主动求助。
        for day in ("2026-09-24", "2026-09-25"):
            service.record_daily_report(request_id=f"req-report-{day}", actor_id="operator-001",
                                        site_id="site-001", report_date=day, quality="qualified",
                                        submitted_at=f"{day}T09:00:00Z",
                                        due_at=f"{day}T18:00:00Z")
        service.record_assistance(request_id="req-assist", actor_id="operator-001",
                                  site_id="site-001", assistance_key="help-001",
                                  request_date="2026-09-24", topic="咨询台账规范")
        settlement = service.settle_day(request_id="req-settle", actor_id="operator-001",
                                        site_id="site-001", settlement_date="2026-09-25")
        snapshot = service.get_snapshot("site-001", "2026-09-25")
        window = service.get_window("site-001")
        resettled = service.settle_day(request_id="req-settle-again", actor_id="operator-001",
                                       site_id="site-001", settlement_date="2026-09-25")

        # 紧急事件即时突破窗口，但不抹去原资格；解除并复核后恢复。
        service.record_serious_event(request_id="req-emergency", actor_id="operator-001",
                                     site_id="site-001", event_key="emergency-001",
                                     event_date="2026-09-25",
                                     occurred_at="2026-09-25T10:30:00Z",
                                     title="临时管线泄漏", severity="emergency")
        breached_window = service.get_window("site-001")
        service.resolve_emergency(request_id="req-emergency-resolve", actor_id="operator-001",
                                  site_id="site-001", event_key="emergency-001")
        service.request_review(request_id="req-review", actor_id="operator-001",
                               site_id="site-001", reason="企业申请复核：现场已处置")
        review = service.list_reviews("site-001")[0]
        service.decide_review(request_id="req-review-decide", actor_id="admin-001",
                              review_id=review.review_id, decision="resumed",
                              decision_note="应急处置完毕，恢复免访窗口")

        # 封账后迟到事实进入更正流程，旧快照不重算。
        correction = service.record_hazard(request_id="req-late-hazard", actor_id="operator-001",
                                           site_id="site-001", hazard_key="late-hazard-001",
                                           title="封账前发现的迟报隐患", found_date="2026-09-20",
                                           severity="general")

        view = service.get_qualification("site-001")
        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "records": len(service.list_domain_data("site-001")),
            "audit_events": event_count,
            "audit_valid": valid,
            "first_replayed": first.replayed,
            "second_replayed": replay.replayed,
            "eligible": snapshot.eligible,
            "window_opened": window is not None and window.status == "active",
            "resettle_same_snapshot": resettled.resource_id == settlement.resource_id,
            "breached_without_erasing": (
                breached_window.status == "breached"
                and service.get_snapshot("site-001", "2026-09-25").eligible),
            "window_restored": service.get_window("site-001").status == "active",
            "late_fact_corrected": correction.resource_type == "snapshot_correction",
            "explains_review_date": any("下一次复核日" in line for line in view.explanation),
            "explains_exception_evidence": any("emergency-001" in line for line in view.explanation),
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    checks = ("audit_valid", "eligible", "window_opened", "resettle_same_snapshot",
              "breached_without_erasing", "window_restored", "late_fact_corrected",
              "explains_review_date", "explains_exception_evidence")
    return 0 if result["status"] == "ok" and all(result[key] for key in checks) and \
        not result["first_replayed"] and result["second_replayed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
