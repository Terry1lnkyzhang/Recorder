from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.ai.shared_service import (
    SharedServiceError,
    build_health_payload,
    handle_method_suggestions,
    handle_parameter_recommendation,
    handle_session_analysis,
)


class SharedAIServiceHandler(BaseHTTPRequestHandler):
    server_version = "RecorderSharedAIService/1.0"

    def do_GET(self) -> None:
        if self.path != "/health":
            self._write_json(404, {"error": "Not Found"})
            return
        self._write_json(200, build_health_payload())

    def do_POST(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            content_length = 0
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            self._write_json(400, {"error": "Invalid JSON body"})
            return

        authorization = self.headers.get("Authorization")
        try:
            if self.path == "/api/session-analysis":
                result = handle_session_analysis(payload, authorization)
            elif self.path == "/api/method-suggestions":
                result = handle_method_suggestions(payload, authorization)
            elif self.path == "/api/parameter-recommendation":
                result = handle_parameter_recommendation(payload, authorization)
            else:
                self._write_json(404, {"error": "Not Found"})
                return
        except SharedServiceError as exc:
            self._write_json(exc.status_code, {"error": exc.message})
            return

        self._write_json(200, result)

    def log_message(self, format: str, *args) -> None:
        return

    def _write_json(self, status_code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8010), SharedAIServiceHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
