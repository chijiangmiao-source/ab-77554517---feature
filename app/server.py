"""噪声故障定位 Web 接口（仅依赖 Python 标准库）。

路由
----
GET  /                     录入页面
GET  /healthz              健康检查（含账本完整性状态）
POST /api/submit           提交通道与校验，求解并保存结论，返回复核编号
GET  /api/review/<id>      按复核编号取回提交内容、结论与账本/封存信息
POST /api/seal             以稳定封存标识锚定目标复核的账本前缀（幂等）
GET  /api/seal/<id>        取回封存根摘要与目标复核的包含路径
GET  /api/ledger/status    账本完整性状态（最早损坏序号等）
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from solver import ValidationError, recompute, solve
from storage import (
    ROOT_RE,
    SEAL_ID_RE,
    SealConflict,
    create_seal,
    init_db,
    ledger_status,
    load_seal,
    load_submission,
    save_submission,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 1 << 20  # 1 MiB


class Handler(BaseHTTPRequestHandler):
    server_version = "PixelLocator/1.0"

    # ---- 工具 ----
    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str, content_type: str) -> None:
        path = STATIC_DIR / name
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        """读取并解析 JSON 对象请求体；失败时已发送 400，返回 None。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {
                "error": "请求体缺失或过大",
                "field": "body",
                "errors": [{"field": "body", "message": "需要 JSON 请求体且不超过 1MiB"}],
            })
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(400, {
                "error": "JSON 解析失败",
                "field": "body",
                "errors": [{"field": "body", "message": f"JSON 解析失败: {exc}"}],
            })
            return None
        if not isinstance(data, dict):
            self._send_json(400, {
                "error": "请求体必须是对象",
                "field": "body",
                "errors": [{"field": "body", "message": "请求体必须是 JSON 对象"}],
            })
            return None
        return data

    def _ledger_unavailable(self) -> bool:
        """账本损坏时停止接受新复核/封存（读取不受影响）。"""
        status = ledger_status()
        if status["healthy"]:
            return False
        self._send_json(503, {
            "error": "账本完整性校验失败，已停止接受新复核",
            "field": "ledger",
            "corrupt_seq": status["corrupt_seq"],
            "detail": status["detail"],
        })
        return True

    def log_message(self, fmt, *args):  # 静默默认访问日志
        return

    # ---- GET ----
    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/" or route == "/index.html":
            self._send_static("index.html", "text/html; charset=utf-8")
        elif route == "/static/app.js":
            self._send_static("app.js", "application/javascript; charset=utf-8")
        elif route == "/healthz":
            ledger = ledger_status()
            self._send_json(200, {
                "status": "ok" if ledger["healthy"] else "degraded",
                "ledger": ledger,
            })
        elif route == "/api/ledger/status":
            self._send_json(200, ledger_status())
        elif route.startswith("/api/review/"):
            review_id = route.rsplit("/", 1)[-1]
            record = load_submission(review_id)
            if record is None:
                self._send_json(404, {
                    "error": "复核编号不存在",
                    "field": "review_id",
                    "review_id": review_id,
                })
            else:
                self._send_json(200, record)
        elif route.startswith("/api/seal/"):
            seal_id = route.rsplit("/", 1)[-1]
            seal = load_seal(seal_id)
            if seal is None:
                self._send_json(404, {
                    "error": "封存标识不存在",
                    "field": "seal_id",
                    "seal_id": seal_id,
                })
            else:
                self._send_json(200, seal)
        else:
            self._send_json(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route == "/api/submit":
            self._handle_submit()
        elif route == "/api/seal":
            self._handle_seal()
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_submit(self) -> None:
        # 账本损坏时停止接受新复核；拒绝不产生任何记录，不占序号。
        if self._ledger_unavailable():
            return
        data = self._read_json()
        if data is None:
            return

        channels = data.get("channels")
        checks = data.get("checks")
        try:
            result = solve(channels, checks)
        except ValidationError as exc:
            self._send_json(400, {
                "error": "输入校验未通过，未生成复核记录",
                "errors": [{"field": f, "message": msg} for f, msg in exc.errors],
            })
            return

        # 用规范化（排序后）的通道与原始提交的校验做逐校验复算。
        # solve 内部对通道排序、对校验去重；为保持复算条目与用户
        # 提交顺序一致，这里重新规范化一次校验集合。
        ordered = list(result.channels)
        norm_checks = _normalize_checks_for_recompute(ordered, checks)
        recomputed = recompute(ordered, norm_checks, list(result.vector)) if result.feasible else []

        conclusion = {
            "feasible": result.feasible,
            "weight": result.weight,
            "faulty": list(result.faulty),
            "vector": (
                {ch: bit for ch, bit in zip(result.channels, result.vector)}
                if result.feasible else {}
            ),
            "left_size": result.left_size,
            "left_index_size": result.left_index_size,
            "recompute": recomputed,
            "message": (
                f"最小故障通道 {result.weight} 个：{', '.join(result.faulty)}"
                if result.feasible else "不存在能同时满足全部异或约束的故障向量（不可行）"
            ),
        }
        payload = {"channels": ordered, "checks": [
            {"channels": list(members), "parity": parity}
            for members, parity in norm_checks
        ]}
        # 复核记录与“排序后输入、观测、结论”的规范字节摘要在同一
        # SQLite 事务中落库，之后才能返回复核编号。
        review_id, seq, leaf = save_submission(payload, conclusion)
        self._send_json(200, {
            "review_id": review_id,
            "input": payload,
            "conclusion": conclusion,
            "ledger": {"seq": seq, "leaf": leaf},
            "seals": [],
        })

    def _handle_seal(self) -> None:
        # 账本损坏时同样停止发起新封存；既有封存仍可读取。
        if self._ledger_unavailable():
            return
        data = self._read_json()
        if data is None:
            return

        errors = []
        seal_id = data.get("seal_id")
        if not isinstance(seal_id, str) or not SEAL_ID_RE.match(seal_id):
            errors.append((
                "seal_id",
                "封存标识须为字母或数字开头的 1–128 位标识（可含 . _ : -）",
            ))
        review_id = data.get("review_id")
        if (
            not isinstance(review_id, str)
            or not review_id
            or not all(c in "0123456789abcdef" for c in review_id)
        ):
            errors.append(("review_id", "复核编号格式非法"))
        expected_root = data.get("expected_root")
        if expected_root is not None and (
            not isinstance(expected_root, str) or not ROOT_RE.match(expected_root)
        ):
            errors.append(("expected_root", "期望根摘要须为 64 位小写十六进制"))
        if errors:
            self._send_json(400, {
                "error": "封存请求非法",
                "errors": [{"field": f, "message": m} for f, m in errors],
            })
            return

        try:
            seal = create_seal(seal_id, review_id, expected_root)
        except SealConflict as exc:
            body = {"error": str(exc), "field": "seal_id", "seal_id": seal_id}
            if exc.existing is not None:
                body["existing"] = exc.existing
            if exc.computed is not None:
                body["computed_root"] = exc.computed
            self._send_json(409, body)
            return
        if seal is None:
            self._send_json(404, {
                "error": "复核编号不存在",
                "field": "review_id",
                "review_id": review_id,
            })
            return
        self._send_json(200, seal)


def _normalize_checks_for_recompute(ordered_channels, raw_checks):
    """把提交的校验按已排序通道名规范化（去本条重复、去整组重复）。"""
    seen_sets: set[frozenset[str]] = set()
    out = []
    for ck in raw_checks:
        members = []
        local: set[str] = set()
        for name in ck.get("channels", []):
            name = name.strip()
            if name in ordered_channels and name not in local:
                local.add(name)
                members.append(name)
        members.sort()
        key = frozenset(members)
        if members and key not in seen_sets:
            seen_sets.add(key)
            out.append((tuple(members), int(ck["parity"])))
    return out


def main() -> None:
    init_db()
    host = os.environ.get("APP_HOST", "0.0.0.0")
    port = int(os.environ.get("APP_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"pixel locator listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
