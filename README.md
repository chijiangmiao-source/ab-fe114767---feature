# 深空地面站 · 32 位帧计数滑动窗口接收器

在重启、乱序投递与 32 位计数器回绕之后，保证同一链路上的旧帧绝不会被再次接受。
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

## 主 / 副接收站与一次收敛

值班员可在主站**当前接收状态**上建立副接收站，用于短暂断联期间的分集接收：

- **同源快照**：副站只能从主站（不能从另一个副站）建立，建立瞬间复制主站的最高扩展序号与位图作为共同基准；副站从此以该基准校准 64 位扩展序号，快照之后主站再前进不影响基准。
- **断联分集**：断联期间两站各自按同一套窗口规则收帧、各自持久化各自的窗口与回执。
- **一次收敛**：恢复联通后执行一次收敛——把两站位图**投影到较高的最高扩展序号后取并集**，位 0（该最高序号）置位；偏移出 64 位窗口的位置直接掩码丢弃，**不会**被重新带回可接受范围。收敛在同一个 `BEGIN IMMEDIATE` 事务内读写两站窗口，并把副站快照标记为已收敛。
- **收敛后**：双方刷新得到完全相同的窗口；副站快照 episode 关闭，新的裁决一律拒绝（相同回执的只读回放除外），主站继续正常接收。同一快照只能收敛一次。
- **越权隔离**：不同来源的副站、把主站当副站、对已收敛快照再次收敛，都返回 `409` 且不改动任何窗口。

收敛响应返回合并后的窗口，以及**被副站补入、主站原先缺失**的扩展序号位置（`added`）。

## 稳定回执标识

- 相同 `receipt_id` + 相同 `counter` + 完全相同 `payload` 的重传：**返回首次裁决**（accepted/duplicate/expired/rejected 原样回放）；在同链路**另一站**重传同样回放（回执按逻辑链路归并，同时记录实际接收站）
- `receipt_id` 全局唯一；复用标识但改变**链路、计数或载荷** → HTTP `409` 拒绝，且不改动窗口
- 每次到达、每次建站与每次收敛都在**同一个 SQLite `BEGIN IMMEDIATE` 事务序列**（同一把写锁）内读取并写入窗口状态与回执记录，并发到达与收敛串行化，窗口与回执不可能出现不一致

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

`verify` 服务等待 `web` 健康后执行：构建检查（compileall）→ 全部代码测试（回绕/过期/重复/并列/回执/重启/并发/副站收敛）→ 对运行中的服务做 HTTP 冒烟，然后退出：

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
| `POST` | `/api/links` | `{"name":"..."}` 创建主链路 |
| `GET` | `/api/links` | 列出全部站（主/副）及窗口状态 |
| `GET` | `/api/links/{id}` | 当前最高扩展序号、位图、最近 64 个已接受位置；主站含 `stations` 列表，副站含 `snapshot` 基准 |
| `POST` | `/api/links/{id}/frames` | 在站 `{id}` 提交到达帧 |
| `POST` | `/api/links/{id}/stations` | `{"name":"..."}` 从主站 `{id}` 当前快照建立副站 |
| `POST` | `/api/links/{id}/stations/{sid}/converge` | 执行一次收敛，返回合并窗口与副站补入位置 |

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

收敛响应（两站窗口写为一致状态后返回）：

```json
{
  "highest": 4294967298,         // 合并后最高扩展序号
  "bitmap": 7,
  "recent": [4294967298, 4294967297, 4294967296],
  "added": [4294967296],         // 副站补入、主站原先缺失的位置（新→旧）
  "primary":   {"id": "...", "name": "...", "highest": 4294967297, "recent": [...]},
  "secondary": {"id": "...", "name": "...", "highest": 4294967298, "recent": [...]}
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
