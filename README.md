# 硅像素读出板 · 奇偶校验噪声故障定位

录入 **2–36 个唯一通道**与至多 **28 条奇偶校验**（每条引用非空、互不
重复的通道集合及观测奇偶值），系统求出使全部 XOR 约束同时成立的
**最小汉明重量故障向量**；重量相同时按通道标识升序形成的选择向量做
标准字典序裁决。提交后得到复核编号，刷新页面可凭编号取回故障通道、
选择向量与逐校验复算结果。无可行解释时保存**不可行结论**，不返回
任何近似集合。

## 算法（折半综合征索引 + 两侧候选精确合并）

通道按标识排序后折半（左 `l = n//2`，右 `n-l`，n≤36 时每半至多
2¹⁸ 个部分向量）：

1. 枚举左半全部部分向量，以综合征（引用该半区故障通道的校验奇偶
   组合，紧凑为一个 int）为键建立索引，每键保留重量最小、同重量
   选择向量字典序最小的候选；
2. 右半按重量层枚举，精确查找所需左半综合征 `sL = target XOR sR`，
   合并两侧候选并按 `(总重量, 完整选择向量字典序)` 裁决；
3. 右半重量层严格超过已知最优重量后剪枝。

算法**不枚举完整 2ⁿ 故障向量、不使用随机搜索、不以高斯消元的任意解
替代最优解**；两侧合并是精确的，故结论即为全局最优。

## 运行

```bash
# 启动页面与接口（默认宿主机端口 8080，可用 APP_PORT 更改）
APP_PORT=9090 docker compose up -d app
# 打开 http://localhost:9090/
```

健康检查：`GET /healthz`（容器内含 HEALTHCHECK，Compose 也据此
门控 verify 服务）。

## 复核

- `POST /api/submit`：`{"channels": [...], "checks": [{"channels": [...], "parity": 0|1}]}`
- `GET /api/review/<复核编号>`：取回提交内容、结论与逐校验复算。
- 非法输入返回 `400`，`errors[].field` 为可定位字段（如
  `channels[2]`、`checks[0].channels`、`checks[0].parity`），不保存
  记录；页面保留编辑内容并清除旧证据。

复核记录持久化在命名卷 `locator-data`（容器内 `/data/locator.db`）。

## verify 服务

```bash
docker compose up --build verify
```

`verify` 服务对唯一故障、多解裁决、不可行用例运行代码测试
（`tests/test_solver.py`）与接口测试（`tests/test_api.py`），执行
字节码构建检查（`compileall`），并对运行中的 `app` 服务做 API 冒烟
（`tests/smoke_api.py`：健康检查、提交、裁决、不可行持久化、可定位
拒绝、刷新取回）。全部通过后退出并返回 `0`；任一步失败返回非零码。

本地不使用 Docker 时也可直接运行（仅需 Python 3.11 标准库）：

```bash
python tests/test_solver.py
python tests/test_api.py
APP_DB=/tmp/l.db python app/server.py
APP_URL=http://127.0.0.1:8080 python tests/smoke_api.py
```
