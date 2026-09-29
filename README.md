# 深空地面站 · 32 位帧计数滑动窗口接收器

在重启、乱序投递与 32 位计数器回绕之后，保证同一链路上的旧帧绝不会被再次接受。
值班员可在已有链路上建立**副接收站**：两站在短暂断联期间分别接收帧，恢复联通后一次性收敛窗口。
提供浏览器页面、HTTP 接口、健康检查，以及以退出码报告结果的 Compose `verify` 验收服务。

## 核心算法

线路上的计数是 32 位无符号整数（`0 .. 2³²−1`），到达时无法直接知道它经历了第几次回绕（纪元 / epoch）。
内部以 **64 位扩展序号** 维护状态，收到帧时在当前最高序号所在纪元的相邻三个候选中选择**距当前最高值最近**者：

```
candidate(k) = counter + k·2³²,   k ∈ (epoch−1, epoch, epoch+1)
```

- **距离并列**：两个候选恰好相距 `2³¹`，无法定位纪元 → `rejected`（拒绝，状态不变）
- **首次帧**：直接以 counter 初始化窗口（epoch 0），位图位置 0
- **更新的序号**：位图按差值左移，位置 0 置位（大跨度跳跃会使旧记录自然滑出）
- **窗口内更旧的序号**：仅置位，最高值不变
- **位图已置位** → `duplicate`（重复）
- **偏移 ≥ 64**（落在 `highest−63 .. highest` 窗口之外）→ `expired`（过期）

位图与最高扩展序号持久化在 SQLite 中；重启后从同一行状态继续推导，回绕前后的乱序帧每帧只接受一次。

## 稳定回执标识

- 相同 `receipt_id` + 相同 `counter` + 完全相同 `payload` 的重传：**返回首次裁决**（accepted/duplicate/expired/rejected 原样回放）
- `receipt_id` 全局唯一；复用标识但改变**链路、计数或载荷** → HTTP `409` 拒绝，且不改动窗口
- 同一回执在**另一站**（主站 ↔ 副站）重传仍回放首次裁决；首次到达的站点不参与冲突判定
- 每次到达都在**同一个 SQLite `BEGIN IMMEDIATE` 事务**内读取并写入窗口状态与回执记录，配合实例锁使并发到达与收敛串行化，窗口与回执不可能出现不一致

## 副接收站与收敛

断联期间由两站分别接收帧，恢复联通后合并窗口，使旧帧不会因任一站落后而重新放行。

- **同源快照**：副站只能从本链路当前窗口冻结出的明确快照建立（`POST /api/links/{id}/snapshots`）。快照不可变，副站逐字继承快照的 64 位最高序号与位图，这是两站共同的校准基准。
- **异源拒绝**：用其它链路的快照建立副站，或把别链路的副站用于本链路收帧/收敛 → HTTP `409 foreign origin`，不改动任何窗口。
- **各自收帧**：断联期间主站提交到 `/api/links/{id}/frames`，副站提交到 `/api/links/{id}/stations/{sid}/frames`，各自独立滑动窗口；页面展示主、副站各自的最高扩展序号与最近位置。
- **一次收敛**：`POST /api/links/{id}/stations/{sid}/converge` 把两站位图以共同快照为基准**投影到较高最高序号后取并集**；投影后落在 `highest−63 .. highest` 窗口之外的位置一律**丢弃**，不会被重新带回可接受范围。收敛后两站窗口完全相同，重复收敛是幂等的。
- **同事务序列**：建快照、建副站、两站收帧与收敛都与稳定回执记录处于同一 SQLite 事务序列（同一实例锁 + `BEGIN IMMEDIATE`），收敛不可能与任一站正在提交的帧交错。
- 收敛响应返回合并后的窗口（`highest` / `bitmap` / `recent`）以及被另一站补入的位置：`added_primary`（副站补入主站）、`added_secondary`（主站补入副站）。

## 运行（Docker Compose）

```bash
# 默认宿主端口 8080
docker compose up --build

# 可配置宿主端口
HOST_PORT=9090 docker compose up --build
```

- 页面：http://localhost:8080/
- 健康检查：http://localhost:8080/healthz
- 数据持久化在命名卷 `receiver-data`（容器内 `/data/receiver.db`）

## 验收（verify 服务）

`verify` 服务等待 `web` 健康后执行：构建检查（compileall）→ 全部代码测试（回绕/过期/重复/并列/回执/重启/并发/**副站分叉/跨站回执/收敛**）→ 对运行中的服务做 HTTP 冒烟，然后退出：

```bash
docker compose up --build --abort-on-container-exit verify
# 或
docker compose run --rm verify
echo $?   # 0 = 验收通过，非 0 = 失败
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | `200 {"status":"ok"}` |
| `GET` | `/` | 单页操作界面 |
| `POST` | `/api/links` | `{"name":"..."}` 创建链路 |
| `GET` | `/api/links` | 列出链路及窗口状态 |
| `GET` | `/api/links/{id}` | 主站最高扩展序号、位图、最近位置及副站列表 |
| `POST` | `/api/links/{id}/frames` | 在主站提交到达帧 |
| `POST` | `/api/links/{id}/snapshots` | 冻结当前窗口为同源快照 |
| `GET` | `/api/snapshots/{id}` | 查看快照 |
| `POST` | `/api/links/{id}/stations` | `{"snapshot_id":"...","name":"..."}` 从同源快照建立副站 |
| `GET` | `/api/links/{id}/stations` | 列出本链路副站 |
| `GET` | `/api/stations/{sid}` | 副站最高扩展序号、位图、最近位置 |
| `POST` | `/api/links/{id}/stations/{sid}/frames` | 在副站提交到达帧 |
| `POST` | `/api/links/{id}/stations/{sid}/converge` | 主/副站一次性收敛 |

收敛响应：

```json
{
  "highest": 4294967299,
  "bitmap": 63,
  "recent": [4294967299, 4294967298, 4294967297, 4294967296, 4294967295, 4294967294],
  "added_primary":   [4294967299, 4294967297],
  "added_secondary": [4294967298, 4294967296]
}
```

提交帧请求体：

```json
{ "counter": 4294967295, "receipt_id": "tx-0001", "payload": {"sensor": "temp"} }
```

裁决响应：

```json
{
  "status": "accepted",          // accepted | duplicate | expired | rejected
  "retransmit": false,           // true = 命中回执记录、回放首次裁决
  "counter": 4294967295,
  "extended": 4294967295,        // 解析出的 64 位扩展序号
  "highest": 4294967295,         // 当前最高扩展序号
  "recent": [4294967295]         // 最近 64 个已接受位置（新→旧）
}
```

## 本地开发（无需 Docker，纯标准库）

```bash
python3 -m unittest discover -s tests -v          # 单元 + 持久化 + HTTP 测试
DB_PATH=/tmp/rx.db PORT=8080 python3 -m app.server
python3 scripts/smoke.py http://127.0.0.1:8080    # 端到端冒烟
```

## 目录结构

```
app/window.py        # 纯函数：纪元候选、距离并列、滑动窗口位图裁决
app/db.py            # SQLite：窗口状态 + 回执记录，单事务一致推导
app/server.py        # 标准库 HTTP 服务与 JSON API
app/static/index.html# 单页界面
tests/               # 算法 / 持久化 / 回执 / 重启 / 并发 / HTTP 测试
scripts/smoke.py     # 针对运行服务的端到端冒烟
scripts/verify.sh    # Compose verify 入口（退出码即验收结论）
```
