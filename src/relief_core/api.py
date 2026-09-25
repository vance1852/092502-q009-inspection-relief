"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .relief_service import ReliefService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


# “无事不扰”资格服务的写动作与领域方法的映射（请求体整体透传）。
RELIEF_POST_ROUTES = {
    "/rule-sets/propose": ("propose_rule_set", 201),
    "/rule-sets/approve": ("approve_rule_set", 200),
    "/rule-sets/publish": ("publish_rule_set", 200),
    "/facts": ("record_fact", 201),
    "/corrections/apply": ("apply_correction", 200),
    "/corrections/dismiss": ("dismiss_correction", 200),
    "/snapshots/settle": ("settle_day", 201),
    "/windows/suspend": ("suspend_window", 200),
    "/windows/terminate": ("terminate_window", 200),
    "/exceptions/resolve": ("resolve_breakthrough", 200),
    "/reviews/request": ("request_review", 201),
    "/reviews/decide": ("decide_review", 200),
}


def relief_route(service: ReliefService, method: str, path: str, body: dict[str, Any] | None,
                 headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把“无事不扰”资格相关请求分派到 ReliefService。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")

    def arg(name: str, default: str | None = None) -> str:
        value = query.get(name, [default])[0]
        if value is None or value == "":
            raise ValidationError(f"{name} 不能为空")
        return value

    try:
        if method == "POST" and parsed.path in RELIEF_POST_ROUTES:
            method_name, created_status = RELIEF_POST_ROUTES[parsed.path]
            result = getattr(service, method_name)(actor_id=actor_id, **body)
            status = 200 if result.get("replayed") else created_status
            return status, result
        if method == "GET" and parsed.path == "/rule-sets":
            return 200, {"items": [asdict(item) for item in service.list_rule_sets()]}
        if method == "GET" and parsed.path == "/corrections":
            site_id = query.get("site_id", [None])[0]
            return 200, {"items": service.list_pending_corrections(actor_id, site_id)}
        if method == "GET" and parsed.path == "/snapshots":
            snapshot = service.get_snapshot(arg("site_id"), arg("business_date"))
            return 200, asdict(snapshot)
        if method == "GET" and parsed.path == "/snapshots/latest":
            snapshot = service.latest_snapshot(arg("site_id"))
            return 200, asdict(snapshot) if snapshot else {"item": None}
        if method == "GET" and parsed.path == "/windows":
            return 200, {"items": [asdict(item) for item in service.list_windows(arg("site_id"))]}
        if method == "GET" and parsed.path == "/exceptions":
            return 200, {"items": [asdict(item) for item in service.list_exceptions(arg("window_id"))]}
        if method == "GET" and parsed.path == "/reviews":
            return 200, asdict(service.get_review(arg("review_id")))
        if method == "GET" and parsed.path == "/qualification/explain":
            return 200, service.explain_qualification(actor_id, arg("site_id"))
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    relief_service: ReliefService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        headers = {"X-Actor-Id": self.headers.get("X-Actor-Id", "")}
        path = self.path
        if path.startswith(("/rule-sets", "/facts", "/corrections", "/snapshots",
                            "/windows", "/exceptions", "/reviews", "/qualification")):
            status, payload = relief_route(self.relief_service, self.command, path, body, headers)
        else:
            status, payload = route(self.service, self.command, path, body, headers)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动环保业务基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    Handler.relief_service = ReliefService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
