#!/usr/bin/env sh
# Compose verify 服务入口：
#   1) 代码测试（求解器：唯一故障 / 多解裁决 / 不可行；接口冒烟）
#   2) 构建检查（全部源码字节码编译）
#   3) API 冒烟（对正在运行的 app 服务发起真实 HTTP 请求）
# 任一步失败即以非零退出码退出。
set -eu

echo "== [1/3] 单元与接口测试 =="
python tests/test_solver.py
python tests/test_api.py

echo "== [2/3] 构建检查（字节码编译）=="
python -m compileall -q app tests

echo "== [3/3] API 冒烟（目标: ${APP_URL:-http://app:8080}）"
python tests/smoke_api.py

echo "== verify 全部通过 =="
