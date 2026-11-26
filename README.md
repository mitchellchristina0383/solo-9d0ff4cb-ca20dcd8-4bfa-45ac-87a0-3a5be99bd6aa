# 四交换机环网拔插 / 广播倒换演练

一个无外部依赖数据库的 Flask 演练页：四台交换机组成环网，可把任意一条链路指定为备用链路，并分别配置每条链路的**开通延时**和**阻塞延时**。页面可从任一台交换机发起广播探测，并按探测时刻真正处于 `forwarding` 的链路计算可达交换机和转发路径。

## 快速开始

### Docker Compose

```bash
docker compose up --build -d
```

打开：

- <http://127.0.0.1:5000>
- 或 <http://127.0.0.1:8000>（Compose 同时映射了 5000 和 8000 到容器 5000）

停止：

```bash
docker compose down
```

容器使用 `waitress-serve` 单进程、多线程运行，内存中保存演练状态和命令队列。

### 本地直接运行

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python wsgi.py
```

打开 <http://127.0.0.1:5000>。

开发自测依赖：

```bash
pip install -r requirements-dev.txt
pytest -q
python scripts/smoke_live.py  # 需先启动服务
```

## 拓扑与默认状态

```text
        L12
 交换机1 ─────────────── 交换机2
   │                       │
L41│                       │L23
   │                       │
 交换机4 ─────────────── 交换机3
        L34
```

四条链路为：

| 链路 | 连接 | 默认角色/状态 |
|---|---|---|
| `L12` | 交换机1 ↔ 交换机2 | 常用，转发 |
| `L23` | 交换机2 ↔ 交换机3 | 常用，转发 |
| `L34` | 交换机3 ↔ 交换机4 | 常用，转发 |
| `L41` | 交换机4 ↔ 交换机1 | 默认备用，阻塞 |

三条常用链路形成无环路径，备用链路阻塞。可以在页面右上角“指定备用链路”下拉框，或在任意链路卡片中选择“设为备用”。

## 实际实现的状态与倒换规则

每条链路有三个实际状态：

- `down`：物理拔出。拔线事件发生的同一请求内立即停止转发；
- `blocked`：已插回或作为备用，物理连接正常但不转发；
- `forwarding`：当前实际转发广播。

每条链路保存两个延时（页面单位为秒，API 也接受毫秒字段）：

- `open_delay_ms`：新下发的“开通”命令从下发到原定生效时刻的延时；
- `block_delay_ms`：新下发的“阻塞”命令从下发到原定生效时刻的延时。

核心规则：

1. **拔线立即生效**：链路马上变 `down`，并从实际转发图删除。
2. **插回先阻塞**：插回只把物理状态恢复，实际状态置为 `blocked`，不会立刻转发。
3. **常用链路断开**：若指定备用链路可用，控制器下发备用链路的开通命令；命令在 `开通延时` 后的原定时刻执行。
4. **修复后切回原线路**：原链路插回后仍阻塞。控制器遵循“先阻塞旧/备用路径，再开通原线路”的顺序：
   - 先按备用链路的 `阻塞延时` 下发并等待阻塞；
   - 备用真正阻塞后，再按原链路的 `开通延时` 下发开通；
   - 最终恢复为常用链路转发、指定备用阻塞。
5. **倒换中可继续拔插**：所有命令都有固定的 `issued_at_clock_ms` 和 `execute_at_clock_ms`。后续拔插、修改延时或再次倒换都不会取消、重排或重算已下发命令。命令到期仍会尝试执行；若链路仍拔出则保持 Down，若开通它会形成环，则被环路保护拒绝并保持阻塞，随后控制器继续收敛。
6. **无环保证**：引擎用并查集检查当前 `forwarding` 图；任何开通命令只有在加入该边不会连接两个已经连通的节点时才会变成 `forwarding`。目标态也遵循“四环链路全部在位时只阻塞指定备用边；至少一条环边缺失时，环已断开，其余在位链路均可转发”。
7. **健康链路不做额外切换**：协调器只处理与目标态不一致且需要倒换的边，不会阻塞本来就在正常转发、且不是新备用角色的健康链路。
8. **广播探测**：从所选交换机开始，只沿当时 `forwarding` 链路做 BFS，返回抵达顺序、未抵达交换机、逐交换机路径和经过链路。BFS 的 visited 集合也是额外防死循环保护。

页面每 750 ms 拉取一次状态，每 250 ms 本地刷新倒计时；后台调度线程每 50 ms 处理一次到期命令。

## HTTP API 摘要

| 方法与路径 | 说明 |
|---|---|
| `GET /api/state` | 完整快照：交换机、链路、命令、事件、最近探测、拓扑无环检查 |
| `POST /api/reset` | 重置为默认无环状态 |
| `POST /api/links/<id>/unplug` | 立即拔线并停止转发 |
| `POST /api/links/<id>/plug` | 插回，先进入 `blocked` |
| `POST /api/links/<id>/toggle` | 切换插拔，也可传 `{"plugged": true/false}` |
| `PATCH /api/links/<id>/delays` | 设置延时 |
| `PATCH /api/links/<id>` | 同上 |
| `POST /api/backup` | body：`{"link_id":"l23"}` |
| `POST /api/links/<id>/backup` | 把该链路设为备用 |
| `POST/GET /api/broadcast` | 从 `source` / `switch_id` 指定的交换机探测 |
| `POST/GET /api/probe` | `/api/broadcast` 的别名 |
| `GET /healthz` | 容器健康检查 |

延时字段示例：

```bash
curl -X PATCH http://127.0.0.1:5000/api/links/l41/delays \
  -H 'Content-Type: application/json' \
  -d '{"open_delay_seconds":1.2,"block_delay_seconds":0.8}'
```

也支持 `open_delay_ms`、`block_delay_ms`，以及 `open_delay` / `block_delay`（按秒处理）。

广播示例：

```bash
curl -X POST http://127.0.0.1:5000/api/broadcast \
  -H 'Content-Type: application/json' \
  -d '{"source":"s1"}'
```

## 自测结果

已在交付前执行：

- `python3 -m pytest -q /workspace/tests`：**12 passed**。
  - 初始态备用阻塞、广播可达全部；
  - 拔线立即分区、备用按原定时限开通；
  - 修复后先阻塞备用、再开通原线并切回；
  - 在首次开通延时内插回，已下发备用开通命令仍按原时刻生效，随后切回；
  - 在切回阻塞延时内再次拔线/插回，原阻塞命令时刻不变并按时执行，之后重新收敛；
  - 修改延时不重排旧命令；
  - 500 步随机拔插、改延时、改备用和探测的性质测试，始终满足转发图无环且广播 BFS 可达集合等于实际连通分量；
  - Flask test client 页面、API、零延时同步倒换和参数校验。
- `python scripts/smoke_live.py`：对真实运行的 HTTP 服务通过，覆盖“倒换等待中插回 → 备用按时开通 → 切回阻塞等待中再次拔插 → 原阻塞命令按时执行 → 修复后切回 L23、L41 阻塞、广播达四台”。
- 用 `waitress` 模块在临时端口启动 WSGI 入口并再次运行同一 smoke 脚本：**LIVE SMOKE PASS**。
- 用 jsdom 加载真实页面并模拟点击：四张链路卡、四个 SVG 交换机、拔线后立即 Down、备用待生效提示、选择 S2 探测并显示 S3/S4 不可达、无环徽标均通过。
- `node --check app/static/app.js` 和 Python 编译检查通过。
- `docker-compose.yml` 已用 YAML 解析检查。

当前沙箱没有安装 Docker CLI/daemon，也没有 podman/buildah，因此未能在这里实际执行 `docker compose up --build`；Dockerfile 按标准 `python:3.12-slim + pip install requirements + waitress wsgi:app` 编写，WSGI 启动路径已用 waitress 等价验证。

## 余项 / 可继续增强

- 状态仅保存在单进程内存中，容器重建会恢复初始演练；未接数据库，这对单次演练足够。
- 没有模拟真实数据中心的 MAC 学习、逐包泛洪或 STP 协议报文；这里做的是满足演练语义的确定性三层可达/BFS 模型和环保护状态机。
- 只有一条指定备用链路，符合本次需求；多次断链时广播只到达当前实际连通段，不保证所有交换机可达。
- 若一条过期 `open` 命令在到期时已不再安全，会按时处理但被防环检查压制，并在命令结果和事件日志中标记 `safety_blocked`，随后继续收敛；这是“绝不成环”优先于陈旧命令预期效果的显式策略。
