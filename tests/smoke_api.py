"""Compose verify 使用的 API 冒烟脚本：对运行中的 app 服务发真实请求。

覆盖：健康检查、唯一故障定位、多解同重量裁决、不可行结论持久化、
非法输入（可定位拒绝）、复核取回、账本叶追加与证据封存
（创建 / 幂等 / 冲突 / 包含路径复算）。

封存检查对重启透明：首次运行以稳定标识创建封存；`docker compose
restart app` 后再次运行（重启验收）时，改为核对既有封存——对封存前
记录取得的包含路径必须仍能复算到相同根。任一步失败以退出码 1 结束。
"""

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("APP_URL", "http://app:8080").rstrip("/")

# 跨重启保持稳定的封存标识：首次运行创建，重启后运行据此核对原封存。
SEAL_ID = "compose-acceptance-seal"


def fold_path(leaf_hex, steps):
    """独立复算：叶摘要沿包含路径折叠（节点 = SHA-256(0x01||左||右)）。"""
    acc = bytes.fromhex(leaf_hex)
    for st in steps:
        sib = bytes.fromhex(st["hash"])
        if st["side"] == "left":
            acc = hashlib.sha256(b"\x01" + sib + acc).digest()
        else:
            acc = hashlib.sha256(b"\x01" + acc + sib).digest()
    return acc.hex()


def call(method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"冒烟失败: {name} {detail}")


def main():
    print(f"API 冒烟目标: {BASE}")

    # 0. 健康检查
    status, body = call("GET", "/healthz")
    check("健康检查 200", status == 200 and body.get("status") == "ok")

    # 1. 唯一故障：仅 CH3
    status, data = call("POST", "/api/submit", {
        "channels": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"],
        "checks": [
            {"channels": ["CH0", "CH1", "CH3"], "parity": 1},
            {"channels": ["CH2", "CH3", "CH4"], "parity": 1},
            {"channels": ["CH3", "CH5"], "parity": 1},
            {"channels": ["CH0", "CH2", "CH4"], "parity": 0},
        ],
    })
    check("唯一故障提交 200", status == 200, str(data))
    check("唯一故障 = CH3 且重量 1",
          data["conclusion"]["faulty"] == ["CH3"]
          and data["conclusion"]["weight"] == 1,
          str(data["conclusion"].get("faulty")))
    check("逐校验复算全部一致",
          all(r["pass"] for r in data["conclusion"]["recompute"]))
    rid_unique = data["review_id"]

    # 2. 多解裁决：{a,b,c} 奇偶 1 有三个重量 1 解，
    #    选择向量标准字典序裁决给 c（(0,0,1) 最小）。
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "c"],
        "checks": [{"channels": ["a", "b", "c"], "parity": 1}],
    })
    check("多解提交 200", status == 200, str(data))
    check("多解同重量裁决给 c", data["conclusion"]["faulty"] == ["c"],
          str(data["conclusion"].get("faulty")))

    # 3. 不可行：结论必须持久化而非返回近似集合
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "c"],
        "checks": [
            {"channels": ["a", "b"], "parity": 0},
            {"channels": ["a", "b", "c"], "parity": 0},
            {"channels": ["c"], "parity": 1},
        ],
    })
    check("不可行提交 200（保存不可行结论）", status == 200, str(data))
    check("结论为不可行且无故障集合",
          data["conclusion"]["feasible"] is False
          and data["conclusion"]["faulty"] == [],
          str(data.get("conclusion")))
    rid_infeasible = data["review_id"]

    # 4. 刷新后取回两条记录
    status, got = call("GET", f"/api/review/{rid_unique}")
    check("唯一故障记录可取回", status == 200
          and got["conclusion"]["faulty"] == ["CH3"])
    status, got = call("GET", f"/api/review/{rid_infeasible}")
    check("不可行记录可取回且仍不可行",
          status == 200 and got["conclusion"]["feasible"] is False)

    # 5. 非法输入：可定位拒绝（重复通道 / 空集合 / 非法奇偶）
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "a"],
        "checks": [{"channels": [], "parity": 9}],
    })
    check("非法输入返回 400", status == 400, str(status))
    fields = {e["field"] for e in data.get("errors", [])}
    check("错误信息可定位（channels[2]）", "channels[2]" in fields, str(fields))
    check("错误信息可定位（空集合）",
          any(f.endswith(".channels") for f in fields), str(fields))
    check("错误信息可定位（parity）",
          any(f.endswith(".parity") for f in fields), str(fields))

    # 6. 重复校验集合被拒绝
    status, data = call("POST", "/api/submit", {
        "channels": ["a", "b", "c"],
        "checks": [
            {"channels": ["a", "b"], "parity": 0},
            {"channels": ["b", "a"], "parity": 1},
        ],
    })
    check("重复校验集合返回 400", status == 400)
    check("重复原因可读", any("重复" in e["message"] for e in data.get("errors", [])))

    # 7. 账本：合法提交携带账本叶；非法提交夹在中间也不占序号
    status, d_a = call("POST", "/api/submit", {
        "channels": ["p", "q"],
        "checks": [{"channels": ["p", "q"], "parity": 1}],
    })
    check("账本提交 A 200", status == 200, str(d_a))
    check("账本叶为 64 位摘要", len(d_a["ledger"]["leaf"]) == 64)
    status, _ = call("POST", "/api/submit", {
        "channels": ["p", "p"],
        "checks": [{"channels": ["p"], "parity": 2}],
    })
    check("夹在中间的非法提交返回 400", status == 400, str(status))
    status, d_b = call("POST", "/api/submit", {
        "channels": ["p", "q"],
        "checks": [{"channels": ["p", "q"], "parity": 1}],
    })
    check("账本序号连续（非法提交未占号）",
          d_b["ledger"]["seq"] == d_a["ledger"]["seq"] + 1,
          f'{d_a["ledger"]} -> {d_b["ledger"]}')

    # 8. 证据封存：稳定标识幂等锚定；重启后路径仍复算到相同根
    status, existing = call("GET", f"/api/seal/{SEAL_ID}")
    if status == 404:
        # 首次运行：对本运行的唯一故障复核发起封存，并验证幂等重传。
        status, seal = call("POST", "/api/seal",
                            {"seal_id": SEAL_ID, "review_id": rid_unique})
        check("封存创建 200", status == 200, str(seal))
        check("封存锚定提交瞬间前缀",
              seal["created"] is True
              and seal["size"] == seal["seq"]
              and seal["review_id"] == rid_unique,
              str(seal))
        status, again = call("POST", "/api/seal",
                             {"seal_id": SEAL_ID, "review_id": rid_unique})
        check("同标识同载荷重传返回原封存",
              status == 200 and again["created"] is False
              and again["root"] == seal["root"],
              str(again))
        conflict_target = rid_infeasible
    else:
        # 重启后运行：原封存必须仍在，目标为重启前保存的复核。
        check("既有封存可取回", status == 200, str(existing))
        seal = existing
        conflict_target = rid_unique  # 本轮新复核 ≠ 原封存目标
    # 两种情形下：对封存前记录取得的包含路径都必须复算到相同根。
    check("包含路径复算到相同根",
          fold_path(seal["leaf"], seal["path"]["steps"]) == seal["root"],
          seal["root"])
    # 被封存的复核（重启前保存）仍返回既有最小故障结论
    status, rec = call("GET", f"/api/review/{seal['review_id']}")
    check("封存目标复核仍可取回既有结论",
          status == 200 and rec["conclusion"]["faulty"] == ["CH3"]
          and any(s["seal_id"] == SEAL_ID for s in rec["seals"]),
          str(rec.get("conclusion", {})))
    # 复用标识却改变目标：必须拒绝且不得改变已封存根
    status, conflict = call("POST", "/api/seal",
                            {"seal_id": SEAL_ID, "review_id": conflict_target})
    check("复用标识改目标返回 409", status == 409, str(status))
    status, after = call("GET", f"/api/seal/{SEAL_ID}")
    check("冲突后已封存根不变",
          status == 200 and after["root"] == seal["root"], str(after))
    check("冲突后路径仍复算到相同根",
          fold_path(after["leaf"], after["path"]["steps"]) == seal["root"])

    # 9. 账本完整性状态健康
    status, lst = call("GET", "/api/ledger/status")
    check("账本状态健康", status == 200 and lst["healthy"] is True, str(lst))

    print("API 冒烟全部通过")


if __name__ == "__main__":
    main()
