"""运行基础服务与“无事不扰”资格服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .qualification import DEFAULT_PARAMETERS
from .relief_service import ReliefService
from .service import DomainService
from .storage import Database


def _seed(service: DomainService) -> None:
    service.register_organization(request_id="req-org", actor_id="bootstrap",
                                  organization_id="org-001", name="示范企业")
    service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                           display_name="系统管理员", role="admin", organization_id="org-001")
    service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                           display_name="复核员", role="reviewer", organization_id="org-001")
    service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                           display_name="环保负责人", role="operator", organization_id="org-001")
    service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                          organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")


def _relief_scenario(domain: DomainService, relief: ReliefService) -> dict[str, object]:
    parameters = dict(DEFAULT_PARAMETERS)
    parameters.update(min_streak_days=3, window_duration_days=10, review_interval_days=5)
    proposed = relief.propose_rule_set(request_id="req-rule-propose", actor_id="admin-001",
                                       parameters=parameters, effective_date="2026-09-01")
    rule_id = proposed["response"]["rule_set_id"]
    relief.approve_rule_set(request_id="req-rule-approve", actor_id="reviewer-001", rule_set_id=rule_id)
    relief.publish_rule_set(request_id="req-rule-publish", actor_id="admin-001", rule_set_id=rule_id)

    days = ["2026-09-23", "2026-09-24", "2026-09-25"]
    window_opened = False
    for day in days:
        relief.record_fact(request_id=f"req-daily-{day}", actor_id="operator-001", site_id="site-001",
                           channel="daily_completion", event_key=f"check-{day}",
                           payload={"on_time": True, "score": 95,
                                    "completed_count": 5, "required_count": 5}, business_date=day)
        settled = relief.settle_day(request_id=f"req-settle-{day}", actor_id="operator-001",
                                    site_id="site-001", business_date=day)["response"]
        window_opened = window_opened or bool(settled.get("window"))

    window_id = relief.list_windows("site-001")[0].window_id

    # 严重事件在窗口生效期间即时到达：突破窗口但保留原资格
    later = ReliefService(relief.database,
                          FixedClock(datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc)))
    later.record_fact(request_id="req-incident", actor_id="operator-001", site_id="site-001",
                      channel="incident", event_key="incident-001",
                      payload={"severity": "major", "description": "管线泄漏"})
    breakthrough = later.explain_qualification("admin-001", "site-001")
    exception_id = breakthrough["exceptions"][0]["exception_id"]
    later.resolve_breakthrough(request_id="req-incident-resolve", actor_id="admin-001",
                               exception_id=exception_id, resolution_note="泄漏处置完毕",
                               evidence={"references": ["doc://incident-001/report"]})

    explanation = later.explain_qualification("admin-001", "site-001")
    valid, event_count = domain.verify_audit()
    return {
        "rule_set_id": rule_id,
        "window_opened": window_opened,
        "window_id": window_id,
        "window_state_after_resolve": explanation["window"]["effective_state"],
        "next_review_date": explanation["next_review_date"],
        "qualification_qualified": explanation["qualification"]["day_qualified"],
        "audit_valid": valid,
        "audit_events": event_count,
    }


def run() -> dict[str, object]:
    """执行登记、结算、窗口、紧急突破到闭环的完整链路并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        domain = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        relief = ReliefService(database, domain.clock)
        _seed(domain)
        first = domain.record_domain_data(request_id="req-data", actor_id="operator-001",
                                          site_id="site-001", category="compliance_fact",
                                          external_key="record-001", data={"name": "基础资料"})
        replay = domain.record_domain_data(request_id="req-data", actor_id="operator-001",
                                           site_id="site-001", category="compliance_fact",
                                           external_key="record-001", data={"name": "基础资料"})
        relief_result = _relief_scenario(domain, relief)
        result = {"status": "ok", "records": len(domain.list_domain_data("site-001")),
                  "first_replayed": first.replayed, "second_replayed": replay.replayed,
                  **relief_result}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"] and result["window_opened"]
          and result["window_state_after_resolve"] == "active"
          and result["qualification_qualified"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
