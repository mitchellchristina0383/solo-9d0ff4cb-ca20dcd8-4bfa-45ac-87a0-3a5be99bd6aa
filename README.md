# 四交换机环网拔插 / 广播倒换 / 业务带宽预留演练

一个无外部依赖数据库的 Flask 演练页：四台交换机组成环网，可把任意一条链路指定为备用链路，并分别配置每条链路的**开通延时**和**阻塞延时**。页面可从任一台交换机发起广播探测，并按探测时刻真正处于 `forwarding` 的链路计算可达交换机和转发路径。还可以登记整数 Mbps 的业务带宽需求，系统只沿**当前实际转发路径**做整路径、双向共用 100 Mbps/链路的额度预留，并在倒换、撤销后按登记顺序自动补位。

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

## 业务带宽预留规则（割接前确认）

页面顶部“业务带宽预留 · 割接确认”卡片可选择**起点 / 终点交换机**并填写**整数 Mbps** 登记业务。核心规则（引擎实现在 `app/network.py` 的 `register_service` / `revoke_service` / `_reconcile_services_locked`）：

1. **额度模型**：每条链路容量固定 `100 Mbps`（`LINK_CAPACITY_MBPS`），正反方向共用同一个额度桶；快照中每条链路带 `capacity_mbps`、`reserved_mbps`、`available_mbps`。
2. **只按实际转发路径**：用当前所有 `forwarding` 链路做 BFS，取唯一路径（转发图始终无环，路径唯一），与广播探测看到的拓扑一致；阻塞 / Down 链路不会被预留。
3. **整路径够才占用**：按登记顺序逐条核查路径上每一条链路的剩余额度，全部满足才一次性把需求加到这些链路上；任一段不足则该业务保持 `waiting`，**不产生任何部分占用**，等待原因会指出最紧的瓶颈链路、已预留 / 剩余额度。
4. **倒换时的保留与释放**：每次有命令到期（链路开始 / 停止 `forwarding`）或拔线立即停转后，都会执行同一套核对：
   - 在用业务的实际路径链路序列与已预留路径完全一致 → **保留**，额度不动；
   - 路径消失（两端不再连通）或发生变化 → **释放**该路径全部额度，业务回到等待队列并记录释放说明与事件；
   - 随后严格按**登记顺序**尝试所有等待业务。
5. **队首不阻塞后续**：某个业务无路 / 带宽不足时只记录原因并继续处理后面的登记，后续小额业务仍可立即接通。
6. **撤销补位**：撤销任意业务（`DELETE /api/services/<id>` 或行内“撤销”按钮）后释放其额度，并用同一核对过程按登记顺序为等待业务补位；撤销等待中的业务不影响在用预留。
7. **重置清空**：`POST /api/reset` 重建拓扑时一并清空全部业务、预留额度和登记序号。
8. **页面可见信息**：业务表列出登记顺序、起止点、需求、`已预留 / 等待中` 状态、已预留路径（交换机链 + 链路标签 + 每段占用 Mbps）或未占用的候选路径、当前等待原因、最近一次倒换释放说明；链路卡片和 SVG 拓扑徽标实时显示 `已预留 x/100 Mbps` 用量条，额度 ≥80% 变琥珀、占满变红；事件日志记录登记、预留成功、倒换释放、撤销补位。

典型争用 / 恢复演练（已自动化，见 `scripts/smoke_services_live.py` 与 `tests/test_services.py`）：

```text
初始树 L12,L23,L34：
  svc1 S2→S3 60M  预留 L23
  svc2 S1→S3 50M  等待（L23 仅剩 40M）
  svc3 S1→S2 40M  预留 L12（队首 svc2 不阻塞它）
拔掉 L23、零延时启用 L41 后，新树 L12,L41,L34：
  svc3 路径仍是 L12           → 保留 40M
  svc1 改走 L12,L41,L34 60M   → 顺序优先，L12 恰好用满到 100，预留成功
  svc2 走 L41,L34 需 50M      → L41 已被 svc1 占用 60M、仅剩 40M，继续等待
撤销 svc1：
  L41/L34 各释放 60M          → svc2 立即补位，预留 L41,L34 50M
```

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
| `POST/GET /api/services` | 登记 / 列表业务；body：`{"source":"s1","target":"s3","demand_mbps":50}`，成功返回 200 + 完整快照（另附 `service` 与该业务字段） |
| `DELETE /api/services/<id>` | 撤销业务并按登记顺序补位 |
| `POST /api/services/<id>/revoke` | 同上（HTML / 表单友好别名） |
| `POST /api/services/revoke` | body：`{"service_id":"svc1"}` 的撤销别名 |
| `GET /api/service`、`/api/reservations`、`/api/business(es)` | `/api/services` 的路径别名；登记字段也接受 `from/to`、`mbps`、`bandwidth_mbps/bandwidth` |
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

已在交付前执行（沙箱 Python 3.11，依赖装在独立目录后以 `PYTHONPATH` 引入）：

- `python3 -m pytest -q tests`：**51 passed**（原 12 个环网用例全部保持通过，新增 39 个）。
  - 新增 `tests/test_services.py`（25 个）：路径预留与用量、正反向共用同一额度桶、整路径原子性（失败不留部分占用）、队首等待不阻塞后续、停转立即释放、L41 开始转发后按顺序自动接通、**倒换后争用 + 撤销后恢复**完整场景、撤销补位顺序、无路可走与恢复、重置清空、整数参数校验、未知交换机 / 同端点 404/400，以及 300 步随机登记 / 撤销 / 拔插下“每链路 ≤100 Mbps、在用路径始终等于实际 BFS 路径”的不变量测试。
  - 新增 `tests/test_service_api.py`（14 个）：页面含登记表单与 testid、登记 / 别名字段 / 200 快照结构、参数校验、404、DELETE 与 POST 撤销及 body 撤销、API 级倒换争用与撤销补位、重置清空。
- `python scripts/smoke_services_live.py`：对真实 waitress HTTP 服务通过，输出 **SERVICE SMOKE PASS**，逐项断言“登记即预留 / 瓶颈等待 / 后续业务仍接通 / 双向共用 / 参数 400 / 拔 L23 后 L41 同步启用的争用格局 / 撤销 svc1 后 svc2 补位 L41+L34 / 重置清空”。
- `python scripts/smoke_live.py`：原环网 live smoke 仍为 **LIVE SMOKE PASS**（倒换等待中插回 → 备用按时开通 → 切回阻塞等待中再次拔插 → 原阻塞命令按时执行 → 修复后切回）。
- 另用真实延时（L41 开通 1.0 s、阻塞 0.6 s、L23 开通 0.6 s）验证后台调度线程：拔线后业务先“无路可走”，L41 开始转发后**无需任何额外请求**自动改走 `L12,L41` 预留；插回 L23 后 L41 停止转发时业务被释放（带释放说明），L23 重新转发后自动按 `L23,L34` 重新预留。
- 用 jsdom 加载真实页面并模拟操作，**JSDOM UI CHECK PASS**：空态、登记三笔业务、`已预留/等待中` 徽标、路径与 L23 原因、链路用量 `60 / 100 · 剩余 40`、SVG `预留 60/100 Mbps`、点击链路卡拔线后 L41 争用结果与 L41 原因、L12 `100/100 · 剩余 0`、事件日志中的 `service-released`、点击“撤销”后 svc2 变已预留、点击页头“重置演练”后表格清空，且无 window/console 报错。
- `node --check app/static/app.js` 与 Python 编译检查通过。
- `docker-compose.yml` 已用 PyYAML 解析核对（端口 5000/8000→5000、healthcheck、build 上下文正确），`Dockerfile` 内容核对无误；并用干净目标目录单独 `pip install -r requirements.txt` 后，以与镜像 `CMD` 完全相同的 `waitress-serve --host=0.0.0.0 --port=5000 --threads=8 wsgi:app` 启动，容器 healthcheck 所用的 `urllib.request.urlopen(.../healthz...)` 单行命令实测成功，两个 live smoke 对该实例通过。

### 关于本沙箱中的 Docker

当前沙箱没有安装 Docker CLI / daemon，也没有 podman/nerdctl，`/var/run/docker.sock` 不存在，且非 root、无 sudo、用户命名空间被禁（`unshare -Urn` Operation not permitted），因此**未能在这里实际执行 `docker compose up --build`**。这不是 compose 文件问题：`docker-compose.yml` / `Dockerfile` 本次均未改动，运行配方已用上述等价方式实测。在装有 Docker 的交付环境中按下面命令即可启动演练页：

```bash
docker compose up --build -d
# 打开 http://127.0.0.1:5000 或 http://127.0.0.1:8000
```

## 余项 / 可继续增强

- 状态仅保存在单进程内存中，容器重建会恢复初始演练（业务也会清空）；未接数据库，这对单次演练足够。
- 带宽模型是每条链路一个双向共用的 `100 Mbps` 整数额度桶，只做控制面的预留 / 核算，没有模拟真实队列、限速、突发或逐包调度；需求也只接受整数 Mbps。
- 路径选择使用当前无环转发图上的唯一 BFS 路径（与广播探测一致），没有引入等价多路径或带宽感知选路；额度不足时不会自动改走另一条路（环网稳态下另一条路通常被 STP/环保护阻塞）。
- 倒换期间“先阻塞旧路径、再开通新路径”的空窗会使相关业务被释放并等待，新路径转发后按登记顺序自动补接；这是需求规则的直接体现，不做跨空窗的额度锁定。
- 没有模拟真实数据中心的 MAC 学习、逐包泛洪或 STP 协议报文；这里做的是满足演练语义的确定性三层可达/BFS 模型和环保护状态机。
- 只有一条指定备用链路，符合本次需求；多次断链时广播只到达当前实际连通段，不保证所有交换机可达。
- 若一条过期 `open` 命令在到期时已不再安全，会按时处理但被防环检查压制，并在命令结果和事件日志中标记 `safety_blocked`，随后继续收敛；这是“绝不成环”优先于陈旧命令预期效果的显式策略。
- 本沙箱无法运行 Docker daemon，故未实际执行 `docker compose up --build`；已用相同镜像运行配方（干净依赖目录 + waitress 命令 + healthcheck 命令）等价验证，详见上文“自测结果 / 关于本沙箱中的 Docker”。
