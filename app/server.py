"""噪声故障定位 Web 接口（仅依赖 Python 标准库）。

路由
----
GET  /                     录入页面
GET  /healthz              健康检查
POST /api/submit           提交通道与校验，求解并保存结论，返回复核编号
GET  /api/review/<id>      按复核编号取回提交内容、结论与封存信息
POST /api/seal             以稳定封存标识锚定连续账本前缀，返回根摘要与包含路径
GET  /api/ledger           账本前沿状态（大小、当前根、是否停摆、最早损坏序号）
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from solver import ValidationError, recompute, solve
from storage import (
    LedgerHaltedError,
    RootMismatchError,
    SealConflictError,
    UnknownReviewError,
    create_seal,
    halted_info,
    init_db,
    ledger_status,
    load_submission,
    save_submission,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 1 << 20  # 1 MiB
MAX_SEAL_ID = 128


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

    def _send_halted(self, corrupt_seq: int, reason: str) -> None:
        self._send_json(503, {
            "error": "证据账本已损坏，停止接受新复核",
            "field": "ledger",
            "corrupt_seq": corrupt_seq,
            "reason": reason,
        })

    def _read_json_body(self) -> dict | None:
        """读取并解析 JSON 请求体；失败时已发送 400，返回 None。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
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
            self._send_json(200, {"status": "ok"})
        elif route == "/api/ledger":
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
        halt = halted_info()
        if halt is not None:
            self._send_halted(halt["corrupt_seq"], halt["reason"])
            return

        data = self._read_json_body()
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
        try:
            # 复核记录与规范字节摘要叶在同一 SQLite 事务中落库，
            # 之后才能返回复核编号。
            review_id, seq, leaf_digest = save_submission(payload, conclusion)
        except LedgerHaltedError as exc:
            self._send_halted(exc.corrupt_seq, exc.reason)
            return
        self._send_json(200, {
            "review_id": review_id,
            "input": payload,
            "conclusion": conclusion,
            "ledger": {"seq": seq, "leaf_digest": leaf_digest},
        })

    def _handle_seal(self) -> None:
        halt = halted_info()
        if halt is not None:
            self._send_halted(halt["corrupt_seq"], halt["reason"])
            return

        data = self._read_json_body()
        if data is None:
            return

        errors = []
        seal_id = data.get("seal_id")
        review_id = data.get("review_id")
        root = data.get("root")
        if (
            not isinstance(seal_id, str)
            or not seal_id
            or len(seal_id) > MAX_SEAL_ID
            or seal_id != seal_id.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in seal_id)
        ):
            errors.append({
                "field": "seal_id",
                "message": f"封存标识须为 1–{MAX_SEAL_ID} 字符、无首尾空白的可见字符串",
            })
        if not isinstance(review_id, str) or not review_id:
            errors.append({"field": "review_id", "message": "必须提供目标复核编号"})
        if root is not None:
            if (
                not isinstance(root, str)
                or len(root) != 64
                or not all(c in "0123456789abcdefABCDEF" for c in root)
            ):
                errors.append({"field": "root", "message": "摘要须为 64 位十六进制字符串"})
            else:
                root = root.lower()
        if errors:
            self._send_json(400, {"error": "封存请求非法", "errors": errors})
            return

        try:
            seal = create_seal(seal_id, review_id, root)
        except UnknownReviewError:
            self._send_json(404, {
                "error": "复核编号不存在",
                "field": "review_id",
                "review_id": review_id,
            })
        except SealConflictError as exc:
            self._send_json(409, {
                "error": "封存标识已被不同载荷使用，已拒绝且未改变原封存",
                "field": "seal_id",
                "existing": exc.existing,
            })
        except RootMismatchError as exc:
            self._send_json(400, {
                "error": "请求摘要与账本计算根不一致，未创建封存",
                "field": "root",
                "computed_root": exc.computed,
            })
        except LedgerHaltedError as exc:
            self._send_halted(exc.corrupt_seq, exc.reason)
        else:
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
