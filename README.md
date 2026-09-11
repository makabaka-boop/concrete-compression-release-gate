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

请求体（JSON，所有数值必须大于零）：

| 字段 | 类型 | 单位 | 说明 |
| --- | --- | --- | --- |
| `design_strength_mpa` | number | MPa | 设计强度 |
| `specimens` | array（恰好 3 项） | — | 试件列表 |
| `specimens[].area_mm2` | number | mm² | 受压面积 |
| `specimens[].load_kn` | number | kN | 破坏载荷 |

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

### `GET /health`

返回 `{"status": "ok"}`，用于容器健康检查。

## 计算规则

1. 单块强度 = `load_kn × 1000 ÷ area_mm2`（kN 换算为 N 后除以 mm² 得 MPa），按 **ROUND_HALF_UP** 保留 **0.1 MPa**。
2. 平均强度 = 三个**舍入后**单块强度的算术平均值，再按 ROUND_HALF_UP 保留 0.1 MPa。
3. 放行需同时满足（等于阈值计入通过）：
   - 平均强度 ≥ 设计强度；
   - 最低单块强度 ≥ 设计强度的 **85.0%**。
4. 未通过时 `reasons` 包含全部未满足条件，固定顺序：
   - `MEAN_BELOW_DESIGN` — 平均强度低于设计强度；
   - `MIN_BELOW_85_PERCENT` — 最低单值低于设计强度的 85.0%。
   两者同时不满足时返回两项，通过时为空数组。

## 错误处理

字段缺失、试件数量不为 3、数值非正数或无法解析为数值时，统一返回 `422 Unprocessable Entity`，响应中不包含任何部分强度结果。

## 项目结构

```
app/
  main.py      # FastAPI 入口，POST /evaluate 与 GET /health
  schemas.py   # Pydantic 请求/响应契约（正数校验、恰好三个试件）
  service.py   # Decimal 强度计算与批次放行裁决
tests/
  test_api.py  # 通过真实 HTTP 验证链路的 pytest 用例
Dockerfile     # python:3.12-slim 镜像
compose.yaml   # api 服务（API_PORT 可覆盖）+ verify 一次性服务
```
