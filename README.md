# 冷链探头校准数据集（Cold-chain Probe Calibration Dataset）

面向冷链质控/追溯场景的小型服务：导入探头原始读数、维护校准证书（半开、互不重叠
的有效期与温度偏移），按 **探头 × UTC 日** 发布“校准后高于 8 ℃ 的连续分钟时段”，
并在数据或证书变更后维护发布版本的**过期 / 供数**状态。

技术栈：FastAPI + SQLite（WAL）+ 原生 HTML/JS 单页界面，Docker Compose 一键启动。

---

## 1. 启动

### Docker Compose（推荐交付方式）

```bash
docker compose up --build
# 打开 http://localhost:8000/        网页界面
# 接口文档（Swagger）  http://localhost:8000/docs
# 健康检查            http://localhost:8000/health
```

SQLite 数据库位于容器内 `/data/calibration.db`，通过命名卷 `calibration-data`
持久化；`docker compose down` 不删数据，`docker compose down -v` 才会清空。

### 本地直接运行（无 Docker 时）

```bash
pip install -r requirements.txt
CAL_DB_PATH=data/calibration.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 2. 数据模型与核心规则

### 2.1 读数 `readings`

- 主键 `(probe_id, minute_utc)`，`minute_utc` 为 **Unix epoch 分钟（整数，UTC）**。
- CSV 三列：`probe_id, minute_utc, temperature_c`。
  - `minute_utc` 支持 epoch 分钟整数，或 ISO-8601 UTC 时间
    （`2024-01-01T00:00Z` / `...+00:00`，必须落在整分钟，拒绝无时区时间）。
  - 同一文件内同探头同分钟的重复行：**后写覆盖先写**。
- **更正替换**：导入时同探头同分钟已存在读数，则用新温度覆盖原读数
  （`UPSERT`），响应分别统计 `inserted` / `replaced`。

### 2.2 校准证书 `calibrations`

- 每条证书含 `probe_id`、半开有效期 `[valid_from, valid_to)`（epoch 分钟）和
  `offset_c`（温度偏移，校准值 = 原始读数 + 偏移，偏移可为负）。
- 同一探头的有效期**互不重叠**；端点相接允许（`[a,b)` 与 `[b,c)` 不冲突）。
  重叠写入返回 `409`，事务回滚，不改动任何数据。
- 支持新增 / 修改（PATCH）/ 删除；`valid_from >= valid_to` 返回 `400`。

### 2.3 发布 `publications`

按探头 × UTC 日发布，发布内容（`payload`）包括：

- `segments`：校准后温度**严格大于 8.0 ℃** 的连续分钟时段，半开
  `[start_minute, end_minute)`，含起止 UTC 时间、持续分钟数、时段内峰值；
- 连续性在以下情况**截断**：
  1. 缺分钟（该分钟没有读数，分钟本身不存在）；
  2. 证书断档（该分钟有读数但没有任何证书覆盖）；
- `uncalibrated_minutes` / `uncalibrated_minutes_utc`：**有读数却未校准**的
  分钟全量列表（追溯方核查时必须看到这些断档点）。

### 2.4 版本与过期语义（关键）

- 全局单调递增的 `data_version`（`/health` 可查）；每次成功的导入或证书变更
  都会 +1，失败不递增。
- 更正读数或修改/新增/删除证书后，受影响 UTC 日（按分钟/有效期落点）当前
  “供数中（serving）”的发布**立即变为“已过期（stale）”**，并在响应中返回
  被标记的日期清单。
- **过期旧版仍可正常查询**，其 payload 原样保留；只有重算**成功**后，新版本
  才以 `serving` 替换供数，旧行转为 `superseded`（历史可查）。
- 重算在**单个数据库事务**内完成：重算失败则整体回滚，不产生半成品行，
  旧版（含 stale 旧版）继续可查。
- 一致性：所有写操作（导入、改证、发布）经全局写锁 + `BEGIN IMMEDIATE`
  串行化，每个发布版本只使用**同一已提交快照**的读数与证书，不会出现
  “新读数 + 旧证书”的混合结果。读接口走独立短连接，不阻塞写。
- 任何导入/改证失败均整笔回滚：读数、证书、版本号、过期标记都保持原状。
- **部署假设：单 uvicorn worker（单进程多线程）**。写锁是进程内锁；如需
  多进程/多副本横向扩展，应改用基于数据库的互斥（如 SQLite 写事务本身已
  可保证单库串行化，但进程内锁需替换）或将存储换成支持行锁的外部数据库。

> 过期标记采用**按 UTC 日的保守策略**：证书有效期与某日有任何交集，变更时
> 该日即标记过期（即使实际覆盖分钟未变）。这保证“后补证书改变历史判断”一定
> 可见；重算后内容未变也只会得到一份新的 serving 版本，不影响正确性。

---

## 3. HTTP 接口摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/readings/import` | multipart 字段 `file` 上传 CSV，或表单字段 `csv_text` |
| GET  | `/api/readings?probe_id=&day=` | 查询读数（day 可选，`YYYY-MM-DD`） |
| POST | `/api/certificates` | 新增证书（JSON） |
| GET  | `/api/certificates?probe_id=` | 列出证书 |
| PATCH | `/api/certificates/{id}` | 修改有效期/偏移（字段可选） |
| DELETE | `/api/certificates/{id}` | 删除证书 |
| POST | `/api/publications` | 发布/重算单日 `{probe_id, day}` |
| POST | `/api/publications/recompute-all` | 重算所有有读数的探头×日（逐日独立事务） |
| GET  | `/api/publications?probe_id=&day=&include_history=` | 查询当前版本（默认）或全部历史 |
| GET  | `/api/publications/{probe_id}/{day}` | 单组查询，无版本返回 404 |
| GET  | `/health` | 健康检查与当前 data_version |

时间字段在请求中可传 epoch 分钟整数或 ISO UTC 字符串；响应同时给出两种形式。

### 快速示例

```bash
# 1) 导入读数
curl -F "file=@readings.csv" http://localhost:8000/api/readings/import

# 2) 新增证书（2024-01-01 全天，偏移 +0.2℃）
curl -X POST http://localhost:8000/api/certificates -H 'Content-Type: application/json' \
  -d '{"probe_id":"P-01","valid_from":"2024-01-01T00:00Z",
       "valid_to":"2024-01-02T00:00Z","offset_c":0.2}'

# 3) 发布某日
curl -X POST http://localhost:8000/api/publications -H 'Content-Type: application/json' \
  -d '{"probe_id":"P-01","day":"2024-01-01"}'

# 4) 查询（过期旧版同样返回，status=stale）
curl "http://localhost:8000/api/publications?probe_id=P-01&day=2024-01-01"
```

CSV 示例：

```csv
probe_id,minute_utc,temperature_c
P-01,2024-01-01T00:00Z,7.4
P-01,2024-01-01T00:01Z,8.3
P-01,2024-01-01T00:02Z,9.1
```

---

## 4. 网页界面

访问根路径 `/`（自动跳转到 `/static/index.html`），四个操作区：

1. **读数导入**：选择 CSV 文件或直接粘贴文本；展示插入/替换数与被置过期的日期；
2. **校准证书**：表单新增，列表内可改（有效期/偏移）、可删，重叠时提示且不落库；
3. **发布**：按探头 + UTC 日发布/重算，或一键全部重算；
4. **版本查询**：表格展示每个发布版本的状态徽章（供数中/已过期/被替代）、
   超温时段明细和“有读数却未校准”的分钟清单，可勾选查看全部历史版本。

页头实时显示当前 `data_version`。

---

## 5. 测试与自测

```bash
pip install -r requirements-dev.txt   # 含 pytest/httpx
python -m pytest tests/ -q
```

测试覆盖（共 30 个用例）：

- 纯计算引擎：缺分钟/断档截断、半开证书边界、严格 >8 ℃、峰值统计、负偏移；
- 导入：插入→更正替换、文件内重复折叠、ISO/整数时间、坏文件整笔回滚；
- 证书：重叠 409、端点相接、非法半开区间、修改/删除/404；
- 发布生命周期：serving →（更正/补证）→ stale → 重算 serving、历史 superseded、
  无读数 404、注入重算失败后 stale 旧版仍在；
- 交错一致性：失败导入/失败改证不改状态、镜像状态逐版比对发布内容、
  4 线程并发（导入×15、改证×10、发布×12、坏导入×10）后所有不变量成立；
- 边界：UTC 日界切分、多探头独立过期、按日过滤、ISO 渲染。

另对运行中的 uvicorn 实例做过 19 项 HTTP 端到端冒烟检查（multipart 上传、
409/400/404、过期与重算、UI 与 docs 可达性），全部通过。

## 6. 目录结构

```
app/
  main.py        FastAPI 路由与异常映射
  services.py    导入/证书/发布事务（写锁 + BEGIN IMMEDIATE）
  engine.py      纯函数：单日超温时段与未校准分钟计算
  db.py          SQLite 连接、WAL、schema、版本与过期标记
  timeutil.py    UTC 分钟/日转换
  static/index.html  单页网页
tests/           pytest 套件
Dockerfile, docker-compose.yml, requirements.txt
```
