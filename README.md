# Vibration Spike Alert API

基于 **Python 3.12 + FastAPI + PostgreSQL** 的振动突增告警 HTTP 服务。

- `POST /readings` 按设备批量接收采样：采样 ID、UTC 整秒时间戳、修订号、有限非负幅值。
- 修订语义：高修订可修改时间或幅值；旧修订返回 `ignored`；同修订不同内容返回 `409`。
- 基线判定：每条读数取此前 **60 分钟**窗口（左端闭合、右端开放，同秒读数不计入）内有效幅值的
  **精确中位数**（偶数个取中间两者均值）；窗口内 **少于 20 条**则基线未知（不命中）；
  仅当幅值 **严格大于基线三倍**（`amplitude > 3 * baseline`）才命中。
- 告警合并：命中读数按事件时间排序，相邻命中（中间无未命中）且时间差 **≤ 300 秒**合并为一条告警；
  告警 ID 取该段第一个命中读数的采样 ID。
- 更正重算：修订一条读数后，受其旧/新位置影响的全部基线窗口自动重算，告警整体重合并。
- 变更流：`GET /changes` 用游标续读 `created` / `updated` / `revoked`；读数、告警、变更
  在同一事务提交；重试同一批（修订与内容不变）不会产生重复变更。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

- API 基址：<http://localhost:8000>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- PostgreSQL 监听宿主 `localhost:5432`（库/用户/密码均为 `vibration`）

表结构在 API 启动时自动创建（`app/schema.sql`），无需手工迁移。停止用 `docker compose down`
（加 `-v` 可同时清空数据卷）。

## 接口

### `POST /readings`

按设备批量提交读数（单批 1–1000 条）。响应逐条给出该条的处理状态、所用基线与判定结果。

请求字段：

| 字段 | 说明 |
| --- | --- |
| `device_id` | 设备标识（字符串） |
| `readings[].sample_id` | 采样 ID，设备内唯一 |
| `readings[].ts` | 事件时间，UTC Unix **整秒**，非负整数 |
| `readings[].rev` | 修订号，从 1 起单调递增，大者胜 |
| `readings[].amplitude` | 幅值，有限、非负（不接受 NaN / Infinity / 负数） |

响应中每条结果：

- `status`：`accepted`（写入或幂等重试）/ `ignored`（更新的修订已存在）/ 冲突时整批 `409`。
- `baseline`：窗口精确中位数；不足 20 条为 `null`。
- `window_count`：基线窗口内有效读数条数。
- `hit`：是否突增命中；基线未知时恒为 `false`。

### `GET /alerts`

查询当前生效的合并告警，可加 `device_id` 过滤、`limit`（默认 100，最大 1000）。

### `GET /changes?cursor=<seq>&limit=<n>`

续读变更。首次用 `cursor=0`（默认），之后用上一页的 `next_cursor`；取满一页才会返回
`next_cursor`，否则为 `null`（已读到最新）。`kind` 为：

- `created`：新告警出现，`alert` 为告警内容；
- `updated`：告警范围/峰值/成员变化（如更正拆分或扩展了命中段），`alert` 为新内容；
- `revoked`：告警消失（或合并段首条命中改变导致 ID 变化），`alert` 为 `null`。

## 请求与响应示例（与实现一致）

下面示例先为设备 `turbine-A` 写入 20 条每分钟一条、幅值恒为 1.0 的平稳读数，再写入两条
突增（4.5、6.0，相距 2 分钟）、一条平稳值、以及 5 分钟后的另一条突增。

```bash
# 1) 20 条平稳基线（幅值恒为 1.0，每分钟一条），一次批量提交
curl -s http://localhost:8000/readings -H 'content-type: application/json' -d '{
  "device_id": "turbine-A",
  "readings": [
    {"sample_id": "s00", "ts": 1700000000, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s01", "ts": 1700000060, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s02", "ts": 1700000120, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s03", "ts": 1700000180, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s04", "ts": 1700000240, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s05", "ts": 1700000300, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s06", "ts": 1700000360, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s07", "ts": 1700000420, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s08", "ts": 1700000480, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s09", "ts": 1700000540, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s10", "ts": 1700000600, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s11", "ts": 1700000660, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s12", "ts": 1700000720, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s13", "ts": 1700000780, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s14", "ts": 1700000840, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s15", "ts": 1700000900, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s16", "ts": 1700000960, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s17", "ts": 1700001020, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s18", "ts": 1700001080, "rev": 1, "amplitude": 1.0},
    {"sample_id": "s19", "ts": 1700001140, "rev": 1, "amplitude": 1.0}
  ]
}'
```

```bash
# 2) 突增批次
curl -s http://localhost:8000/readings -H 'content-type: application/json' -d '{
  "device_id": "turbine-A",
  "readings": [
    {"sample_id": "h1",   "ts": 1700001200, "rev": 1, "amplitude": 4.5},
    {"sample_id": "h2",   "ts": 1700001320, "rev": 1, "amplitude": 6.0},
    {"sample_id": "calm", "ts": 1700001380, "rev": 1, "amplitude": 1.1},
    {"sample_id": "h3",   "ts": 1700001680, "rev": 1, "amplitude": 5.0}
  ]
}'
```

实际响应（节选）：

```json
{
  "device_id": "turbine-A",
  "results": [
    {"sample_id": "h1", "rev": 1, "status": "accepted", "ts": 1700001200,
     "amplitude": 4.5, "baseline": 1.0, "window_count": 20, "hit": true},
    {"sample_id": "h2", "rev": 1, "status": "accepted", "ts": 1700001320,
     "amplitude": 6.0, "baseline": 1.0, "window_count": 21, "hit": true},
    {"sample_id": "calm", "rev": 1, "status": "accepted", "ts": 1700001380,
     "amplitude": 1.1, "baseline": 1.0, "window_count": 22, "hit": false},
    {"sample_id": "h3", "rev": 1, "status": "accepted", "ts": 1700001680,
     "amplitude": 5.0, "baseline": 1.0, "window_count": 23, "hit": true}
  ]
}
```

说明：`h1`/`h2` 严格超过 3×1.0 而命中，且相距 120 秒合并；`calm` 未命中，打断合并段；
`h3` 与 `h2` 相隔 360 秒（>300），独立成告警。

### 查看当前告警

```bash
curl -s 'http://localhost:8000/alerts?device_id=turbine-A'
```

```json
{
  "alerts": [
    {"alert_id": "h1", "device_id": "turbine-A",
     "start_ts": 1700001200, "end_ts": 1700001320,
     "peak_ts": 1700001320, "peak_amplitude": 6.0,
     "hit_count": 2, "sample_ids": ["h1", "h2"]},
    {"alert_id": "h3", "device_id": "turbine-A",
     "start_ts": 1700001680, "end_ts": 1700001680,
     "peak_ts": 1700001680, "peak_amplitude": 5.0,
     "hit_count": 1, "sample_ids": ["h3"]}
  ]
}
```

### 续读变更

```bash
curl -s 'http://localhost:8000/changes?cursor=0'
# 响应含 changes[] 与 next_cursor；下一页用 next_cursor 作为 cursor 续读。
```

每条变更形如（`changed_at` 为 ISO-8601 UTC 时间）：

```json
{
  "seq": 2,
  "device_id": "turbine-A",
  "alert_id": "h1",
  "kind": "created",
  "alert": {"alert_id": "h1", "device_id": "turbine-A",
            "start_ts": 1700001200, "end_ts": 1700001320,
            "peak_ts": 1700001320, "peak_amplitude": 6.0,
            "hit_count": 2, "sample_ids": ["h1", "h2"]},
  "changed_at": "2026-10-05T23:08:25.856479+00:00"
}
```

### 更正（高修订）触发重算

把 `h2` 用更高修订号改成平稳值 1.2：

```bash
curl -s http://localhost:8000/readings -H 'content-type: application/json' -d '{
  "device_id": "turbine-A",
  "readings": [
    {"sample_id": "h2", "ts": 1700001320, "rev": 2, "amplitude": 1.2}
  ]
}'
```

```json
{
  "device_id": "turbine-A",
  "results": [
    {"sample_id": "h2", "rev": 2, "status": "accepted", "ts": 1700001320,
     "amplitude": 1.2, "baseline": 1.0, "window_count": 21, "hit": false}
  ]
}
```

此后 `h1` 告警收缩为单条命中；`/changes` 会出现一条 `{"kind":"updated","alert_id":"h1"}`，
其 `alert.sample_ids` 变为 `["h1"]`。用旧修订（`rev=1`）再发只会得到
`"status":"ignored"`；同修订改内容则整批返回 `409 revision_conflict`，请求全部回滚，
`/alerts` 与 `/changes` 均不发生任何变化。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://USER@localhost:5432/vibration   # 任意可访问的 PG 14+
uvicorn app.main:app --reload
```

运行测试（需要能创建数据库的账号，测试库名为 `vibration_test`）：

```bash
DATABASE_URL=postgresql://postgres@localhost:5432/vibration_test pytest -q
```

性能自检（先灌入 10 万条历史，再连续提交 100 条纯追加批次）：

```bash
python -m scripts.bench 50
```

## 性能

在单设备 10 万条/天的历史规模下，基线重算采用“单次索引范围扫描 + 滑动窗口精确中位数 +
批量回写”，告警按 `(device_id, ts)` 索引顺序单遍重合并。实测 100 条纯追加批次
（经 uvicorn HTTP）：**p50 ≈ 145 ms，p95 ≈ 160 ms**，满足 p95 ≤ 1 s。

## 数据模型

见 `app/schema.sql`：

- `readings`：`(device_id, sample_id)` 主键，保存最新修订的时间/幅值及基线判定；
  `(device_id, ts)` 索引支撑窗口扫描。
- `alerts`：当前生效的合并告警，主键 `(device_id, alert_id)`。
- `alert_changes`：BIGINT IDENTITY 顺序号的事务性 outbox，与读数、告警同事务写入。
