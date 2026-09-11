# 混凝土试件批次强度裁决 API

纯后端接口：接收一组（恰好三个）混凝土试件的试验数据，计算单块强度与平均强度，并立即给出批次放行的唯一结论。所有计算使用 `Decimal`，避免浮点误差。

## 快速开始

```bash
# 构建并启动 API（默认宿主端口 8000）
docker compose up --build -d api

# 宿主端口可用 API_PORT 覆盖
API_PORT=9000 docker compose up -d api

# 一次性验证服务：等待 API 健康后，通过真实 HTTP 运行 pytest
docker compose up --build --exit-code-from verify --abort-on-container-exit verify
```

本地开发（Python 3.12）：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
API_BASE_URL=http://localhost:8000 pytest tests/ -v
```

## 接口

### `POST /evaluate`

请求体（JSON，`design_strength_mpa` 与试件数值必须大于零）：

| 字段 | 类型 | 单位 | 说明 |
| --- | --- | --- | --- |
| `design_strength_mpa` | number | MPa | 设计强度 |
| `specimens` | array（恰好 3 项） | — | 试件列表 |
| `specimens[].area_mm2` | number | mm² | 受压面积 |
| `specimens[].load_kn` | number | kN | 破坏载荷 |
| `calibration_factor` | number，可选 | — | 压力机校准载荷修正系数，范围 0.9500–1.0500（含边界）；省略时按 1 处理，显式 `null` 视为非法 |
| `evaluation_id` | string，可选 | — | 幂等标识（非空、最长 128 字符）；携带时启用重试去重，详见「幂等与台账」 |

示例：

```bash
curl -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
        "design_strength_mpa": 30.0,
        "specimens": [
          {"area_mm2": 22500, "load_kn": 800},
          {"area_mm2": 22500, "load_kn": 800},
          {"area_mm2": 22500, "load_kn": 500}
        ]
      }'
```

响应 `200 OK`（平均值达标，但最低单值低于设计强度的 85.0%，批次不放行）：

```json
{
  "strengths_mpa": [35.6, 35.6, 22.2],
  "mean_strength_mpa": 31.1,
  "passed": false,
  "reasons": ["MIN_BELOW_85_PERCENT"]
}
```

响应字段：

| 字段 | 说明 |
| --- | --- |
| `strengths_mpa` | 三个试件的单块强度（MPa，0.1 精度），顺序与请求一致 |
| `mean_strength_mpa` | 三项强度的算术平均值（MPa，0.1 精度） |
| `passed` | 批次是否放行 |
| `reasons` | 未通过原因；通过时为空数组 |
| `applied_calibration_factor` | 实际应用的校准系数；**仅当请求显式携带 `calibration_factor` 时返回**，省略时响应保持原有四个字段 |
| `replayed` | 是否回放了已保存的首次结果；**仅当请求携带 `evaluation_id` 时返回**，首次提交为 `false`，相同请求重试为 `true` |

携带校准系数的示例（临界批次按校准后载荷裁决）：

```bash
curl -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
        "design_strength_mpa": 30.0,
        "calibration_factor": 1.01,
        "specimens": [
          {"area_mm2": 22500, "load_kn": 660},
          {"area_mm2": 22500, "load_kn": 670},
          {"area_mm2": 22500, "load_kn": 680}
        ]
      }'
```

响应 `200 OK`（未校准时平均强度 29.8 MPa 低于设计值，校准后转为放行）：

```json
{
  "strengths_mpa": [29.6, 30.1, 30.5],
  "mean_strength_mpa": 30.1,
  "passed": true,
  "reasons": [],
  "applied_calibration_factor": 1.01
}
```

## 幂等与台账

实验室网络重试可能让同一组试件被重复裁决。请求携带 `evaluation_id` 时，接口按以下规则去重并留痕：

1. **请求指纹**：以规范化后的设计强度、三块试件（面积、载荷，含顺序）与校准系数生成指纹。数值按 `Decimal` 值规范化——`30.0`、`30.00`、`"3E+1"` 等值不同形，视为同一请求；但显式 `calibration_factor: 1.0` 与省略系数不同（前者响应多 `applied_calibration_factor` 字段）。
2. **首次提交**：调用原有 Decimal 裁决服务计算，将 `evaluation_id`、指纹与完整结果写入本地 SQLite 台账，返回结果并带 `replayed: false`。
3. **相同重试**：`evaluation_id` 与业务输入均与首次一致时，不重新计算，直接返回首次保存的结果并带 `replayed: true`。
4. **标识冲突**：`evaluation_id` 已对应不同业务输入时返回 `409 Conflict`，错误体指出 `evaluation_id` 冲突，**已保存的首次结果不被覆盖**。
5. **非法请求**：试件或校准系数非法时仍返回 `422`，且在写入台账之前——不会留下占位记录，同一标识修正后可正常首次提交。
6. **未携带标识**：行为与扩展前完全一致——即时计算、不写台账、响应不含 `replayed` 字段。

携带标识的示例：

```bash
curl -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
        "evaluation_id": "batch-2026-09-11-017",
        "design_strength_mpa": 30.0,
        "specimens": [
          {"area_mm2": 22500, "load_kn": 700},
          {"area_mm2": 22500, "load_kn": 720},
          {"area_mm2": 22500, "load_kn": 710}
        ]
      }'
```

首次响应 `200 OK`：`{"strengths_mpa":[31.1,32.0,31.6],"mean_strength_mpa":31.6,"passed":true,"reasons":[],"replayed":false}`；相同请求重试时同一结果带 `"replayed":true`。

### `GET /health`

返回 `{"status": "ok"}`，用于容器健康检查。

## 持久化与测试隔离

台账为本地 SQLite 数据库，路径由环境变量 **`EVALUATION_DB_PATH`** 决定，默认 **`./data/evaluations.db`**（相对应用工作目录；compose 中固定为 `/srv/app/data/evaluations.db` 并挂载命名卷 `ledger`，记录跨容器重建保留）。运行中还会出现同目录的 `*.db-wal` / `*.db-shm` 伴生文件（WAL 模式），属正常现象。删除数据库文件即清空全部台账记录。

测试隔离方式：`tests/` 下所有幂等用例均为每个请求生成 uuid 随机 `evaluation_id`，不与台账中既有记录互相影响，可对着同一数据库反复运行；如需彻底隔离的台账，启动服务时将 `EVALUATION_DB_PATH` 指向临时文件即可，例如：

```bash
EVALUATION_DB_PATH=/tmp/eval-test.db uvicorn app.main:app --port 8000
API_BASE_URL=http://localhost:8000 pytest tests/ -v
```

## 计算规则

1. 若请求显式携带 `calibration_factor`，先将每块试件的 `load_kn` 以 `Decimal` 乘以该系数得到校准载荷；校准载荷**不做任何中间舍入**。省略系数时按 1 处理，计算结果与未扩展前的契约完全一致。
2. 单块强度 = `load_kn × 1000 ÷ area_mm2`（kN 换算为 N 后除以 mm² 得 MPa；携带系数时使用校准后的载荷），按 **ROUND_HALF_UP** 保留 **0.1 MPa**。
3. 平均强度 = 三个**舍入后**单块强度的算术平均值，再按 ROUND_HALF_UP 保留 0.1 MPa。
4. 放行需同时满足（等于阈值计入通过）：
   - 平均强度 ≥ 设计强度；
   - 最低单块强度 ≥ 设计强度的 **85.0%**。
5. 未通过时 `reasons` 包含全部未满足条件，固定顺序：
   - `MEAN_BELOW_DESIGN` — 平均强度低于设计强度；
   - `MIN_BELOW_85_PERCENT` — 最低单值低于设计强度的 85.0%。
   两者同时不满足时返回两项，通过时为空数组。

## 错误处理

字段缺失、试件数量不为 3、数值非正数或无法解析为数值时，统一返回 `422 Unprocessable Entity`，响应中不包含任何部分强度结果。`calibration_factor` 越出 0.9500–1.0500、非数值或显式 `null` 时同样返回 422，错误位置（`detail[].loc`）指向 `calibration_factor`。`evaluation_id` 为空字符串、纯空白或非字符串时同样返回 422。

携带的 `evaluation_id` 已对应不同业务输入时返回 `409 Conflict`，错误体 `detail` 说明标识冲突并回显该 `evaluation_id`；已保存的首次结果不被覆盖，使用原业务输入重试仍可取回首次结果。

## 项目结构

```
app/
  main.py      # FastAPI 入口，POST /evaluate 与 GET /health
  schemas.py   # Pydantic 请求/响应契约（正数校验、恰好三个试件、可选校准系数、可选幂等标识）
  service.py   # Decimal 强度计算与批次放行裁决
  store.py     # SQLite 幂等台账：请求指纹、记录查询与写入
tests/
  test_api.py          # 通过真实 HTTP 验证计算与契约的 pytest 用例
  test_idempotency.py  # 通过真实 HTTP 验证幂等回放、冲突与隔离的 pytest 用例
Dockerfile     # python:3.12-slim 镜像
compose.yaml   # api 服务（API_PORT 可覆盖，ledger 卷保存台账）+ verify 一次性服务
```
