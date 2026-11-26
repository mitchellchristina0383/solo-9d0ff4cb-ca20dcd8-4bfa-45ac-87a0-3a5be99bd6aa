"""Loop-safe state machine for the four-switch ring drill.

The module is intentionally independent of Flask so the timing and failover
logic can be tested with a fake clock.
"""

from __future__ import annotations

import itertools
import re
import threading
import time
from collections import defaultdict, deque
from typing import Any, Callable, Optional


def wall_clock_ms() -> int:
    """Monotonic clock used by the running web application."""

    return int(time.monotonic() * 1000)


#: Reservable bandwidth on every ring link, in Mbps.  The capacity is shared by
#: traffic in both directions (one bucket per link, not one per direction).
LINK_CAPACITY_MBPS = 100

#: Upper bound for a single registration, only to reject fat-fingered input.
MAX_DEMAND_MBPS = 1_000_000


class NetworkEngine:
    """In-memory simulation engine.

    Link state is one of:

    * ``forwarding`` - physically inserted and currently forwarding broadcasts;
    * ``blocked``    - inserted, but held discarding to prevent a ring loop;
    * ``down``       - unplugged, so forwarding stops immediately.

    Scheduled commands are never rewritten or cancelled when the operator makes
    another plug/unplug action.  They receive an execution attempt at their
    original deadline.  Before an ``open`` command is allowed to make a link
    forward, the engine checks that adding it cannot create a cycle.  This is
    the final loop-prevention guard for stale commands.
    """

    SWITCHES: list[dict[str, str]] = [
        {"id": "s1", "name": "交换机 1", "short": "SW1"},
        {"id": "s2", "name": "交换机 2", "short": "SW2"},
        {"id": "s3", "name": "交换机 3", "short": "SW3"},
        {"id": "s4", "name": "交换机 4", "short": "SW4"},
    ]

    DEFAULT_LINKS: list[tuple[str, str, str]] = [
        ("l12", "s1", "s2"),
        ("l23", "s2", "s3"),
        ("l34", "s3", "s4"),
        ("l41", "s4", "s1"),
    ]

    def __init__(self, now_func: Callable[[], int] = wall_clock_ms) -> None:
        self._lock = threading.RLock()
        self._now_func = now_func
        self._start_ms = now_func()
        self._id_counter = itertools.count(1)
        self._service_seq = itertools.count(1)
        self.services = []
        self._scheduler_stop = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None
        self.reset()

    # ------------------------------------------------------------------ setup

    def reset(self) -> dict[str, Any]:
        """Restore the healthy ring with L41 as the blocked backup link."""

        with self._lock:
            self._start_ms = self._now_func()
            self._id_counter = itertools.count(1)
            self._service_seq = itertools.count(1)
            # Registered services in registration order (deletions remove rows,
            # status changes never reorder them).
            self.services: list[dict[str, Any]] = []
            self.links: list[dict[str, Any]] = []
            for index, (link_id, source, target) in enumerate(self.DEFAULT_LINKS):
                self.links.append(
                    {
                        "id": link_id,
                        "name": link_id.upper(),
                        "source": source,
                        "target": target,
                        "designated_backup": link_id == "l41",
                        "plugged": True,
                        "state": "blocked" if link_id == "l41" else "forwarding",
                        "open_delay_ms": 1500,
                        "block_delay_ms": 1000,
                        "last_changed_clock_ms": 0,
                    }
                )
            self.commands: list[dict[str, Any]] = []
            self.events: list[dict[str, Any]] = []
            self.last_probe: Optional[dict[str, Any]] = None
            self._log_locked(
                "reset",
                "演练已重置：三条常用链路转发，L41 作为备用链路保持阻塞；全部业务登记与带宽预留已清空。",
            )
            return self.snapshot_locked()

    def start_scheduler(self) -> None:
        """Start the daemon thread which executes due commands in real time."""

        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._scheduler_stop = threading.Event()

        def run() -> None:
            while not self._scheduler_stop.wait(0.05):
                try:
                    self.process_due_commands()
                except Exception as exc:  # pragma: no cover - defensive runtime log
                    # The scheduler must never die because of one bad command.
                    with self._lock:
                        self._log_locked("scheduler-error", f"调度器处理异常：{exc!r}")

        thread = threading.Thread(target=run, name="ring-engine-scheduler", daemon=True)
        self._scheduler_thread = thread
        thread.start()

    def stop_scheduler(self) -> None:
        self._scheduler_stop.set()
        thread = self._scheduler_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._scheduler_thread = None

    # ------------------------------------------------------------- utilities

    def _clock_ms_locked(self) -> int:
        return max(0, int(self._now_func()) - int(self._start_ms))

    def _new_id_locked(self, prefix: str) -> str:
        return f"{prefix}{next(self._id_counter)}"

    def _link_locked(self, link_id: str) -> dict[str, Any]:
        for link in self.links:
            if link["id"] == link_id:
                return link
        raise KeyError(f"未知链路：{link_id}")

    def _switch_name_locked(self, switch_id: str) -> str:
        for switch in self.SWITCHES:
            if switch["id"] == switch_id:
                return switch["name"]
        return switch_id

    def _link_name_locked(self, link_id: str) -> str:
        try:
            return self._link_locked(link_id)["name"]
        except KeyError:
            return link_id.upper()

    def _log_locked(self, kind: str, message: str) -> None:
        event = {
            "id": self._new_id_locked("e"),
            "at_clock_ms": self._clock_ms_locked(),
            "kind": kind,
            "message": message,
        }
        self.events.append(event)
        # Keep the in-memory page responsive for a long drill.
        if len(self.events) > 300:
            self.events = self.events[-300:]

    @staticmethod
    def _union_find(nodes: list[str]) -> tuple[dict[str, str], Callable[[str], str], Callable[[str, str], bool]]:
        parent = {node: node for node in nodes}

        def find(node: str) -> str:
            root = node
            while parent[root] != root:
                root = parent[root]
            while parent[node] != node:
                nxt = parent[node]
                parent[node] = root
                node = nxt
            return root

        def union(a: str, b: str) -> bool:
            """Union; return False when a and b were already connected."""

            ra, rb = find(a), find(b)
            if ra == rb:
                return False
            parent[rb] = ra
            return True

        return parent, find, union

    def _cycle_checker_locked(self) -> tuple[Callable[[str], str], Callable[[str, str], bool]]:
        _, find, union = self._union_find([s["id"] for s in self.SWITCHES])
        for link in self.links:
            if link["state"] == "forwarding":
                union(link["source"], link["target"])
        return find, union

    def creates_cycle(self, candidate_link_id: str) -> bool:
        """Public loop check used by tests and API diagnostics."""

        with self._lock:
            link = self._link_locked(candidate_link_id)
            find, _ = self._cycle_checker_locked()
            return find(link["source"]) == find(link["target"])

    def topology_locked(self) -> dict[str, Any]:
        find, _ = self._cycle_checker_locked()
        groups: dict[str, list[str]] = defaultdict(list)
        for switch in self.SWITCHES:
            groups[find(switch["id"])].append(switch["id"])
        forwarding = [link["id"] for link in self.links if link["state"] == "forwarding"]
        return {
            "forwarding_link_ids": forwarding,
            "loop_free": not self._current_forwarding_graph_has_cycle_locked(),
            "components": list(groups.values()),
        }

    def _current_forwarding_graph_has_cycle_locked(self) -> bool:
        _, _, union = self._union_find([s["id"] for s in self.SWITCHES])
        for link in self.links:
            if link["state"] != "forwarding":
                continue
            if not union(link["source"], link["target"]):
                return True
        return False

    def _pending_command_for_link_locked(self, link_id: str) -> Optional[dict[str, Any]]:
        for command in self.commands:
            if command["status"] == "pending" and command["link_id"] == link_id:
                return command
        return None

    def _open_conflicts_with_pending_commands_locked(self, candidate_id: str) -> bool:
        """Check whether candidate plus pending opens would close a ring.

        This conservative planner avoids issuing a second open that is expected
        to lose the race with an already-scheduled failover command.  The
        execution-time cycle check remains authoritative.
        """

        _, _, union = self._union_find([s["id"] for s in self.SWITCHES])

        for link in self.links:
            if link["state"] == "forwarding":
                union(link["source"], link["target"])

        for command in self.commands:
            if command["status"] != "pending" or command["action"] != "open":
                continue
            link = self._link_locked(command["link_id"])
            if not link["plugged"]:
                continue
            if not union(link["source"], link["target"]):
                return True

        candidate = self._link_locked(candidate_id)
        return not union(candidate["source"], candidate["target"])

    # ------------------------------------------------------------- commands

    def _issue_command_locked(
        self,
        link_id: str,
        action: str,
        delay_ms: int,
        reason: str = "",
    ) -> dict[str, Any]:
        now_clock = self._clock_ms_locked()
        command = {
            "id": self._new_id_locked("c"),
            "link_id": link_id,
            "action": action,
            "status": "pending",
            "issued_at_clock_ms": now_clock,
            "execute_at_clock_ms": now_clock + max(0, int(delay_ms)),
            "delay_ms": max(0, int(delay_ms)),
            "result": None,
            "reason": reason,
            "executed_at_clock_ms": None,
        }
        self.commands.append(command)
        if len(self.commands) > 500:
            self.commands = self.commands[-500:]
        action_text = "开通转发" if action == "open" else "阻塞"
        execute_at = command["execute_at_clock_ms"]
        self._log_locked(
            "command-issued",
            f"{self._link_name_locked(link_id)} 已下发{action_text}命令，"
            f"将在演练时刻 {execute_at} ms 按原定时限生效。{reason}",
        )
        return command

    def process_due_commands(self) -> list[dict[str, Any]]:
        """Execute all commands whose original deadline has arrived."""

        executed: list[dict[str, Any]] = []
        with self._lock:
            while True:
                now_clock = self._clock_ms_locked()
                due = [
                    command
                    for command in self.commands
                    if command["status"] == "pending"
                    and command["execute_at_clock_ms"] <= now_clock
                ]
                if not due:
                    break
                due.sort(key=lambda item: (item["execute_at_clock_ms"], item["id"]))
                for command in due:
                    if command["status"] != "pending":
                        continue
                    self._execute_command_locked(command)
                    executed.append(command)
                self._reconcile_locked()
            # Any command deadline may have started/stopped a link forwarding;
            # revalidate bandwidth reservations against the final graph.
            self._reconcile_services_locked()
        return executed

    def _execute_command_locked(self, command: dict[str, Any]) -> None:
        now_clock = self._clock_ms_locked()
        command["status"] = "executed"
        command["executed_at_clock_ms"] = now_clock
        link = self._link_locked(command["link_id"])
        link_name = link["name"]

        if command["action"] == "block":
            if not link["plugged"]:
                link["state"] = "down"
                command["result"] = "down"
                message = f"{link_name} 的原定阻塞命令已到期；链路已拔出，实际保持 Down。"
            elif link["state"] == "forwarding":
                link["state"] = "blocked"
                link["last_changed_clock_ms"] = now_clock
                command["result"] = "blocked"
                message = f"{link_name} 的阻塞命令按原定时刻生效，现已阻塞，不再转发广播。"
            else:
                command["result"] = "already_blocked"
                message = f"{link_name} 的原定阻塞命令已到期；链路此前已处于阻塞状态。"
            self._log_locked("command-executed", message)
            return

        # action == open
        if not link["plugged"]:
            link["state"] = "down"
            command["result"] = "down"
            message = f"{link_name} 的原定开通命令已到期；但链路仍拔出，实际不能转发。"
        elif self.creates_cycle(link["id"]):
            # Highest-priority invariant: a stale command must never create a
            # Layer-2 forwarding loop.
            link["state"] = "blocked"
            link["last_changed_clock_ms"] = now_clock
            command["result"] = "safety_blocked"
            message = (
                f"{link_name} 的原定开通命令已按时到期，但开通会形成转发环路；"
                "环路保护立即将其保持在阻塞状态，并等待安全倒换。"
            )
        else:
            link["state"] = "forwarding"
            link["last_changed_clock_ms"] = now_clock
            command["result"] = "forwarding"
            message = f"{link_name} 的开通命令按原定时刻生效，现已实际转发。"
        self._log_locked("command-executed", message)

    # -------------------------------------------------------- reconciliation

    def _target_states_locked(self) -> dict[str, str]:
        """Return final desired states for the current physical topology.

        A four-link ring has one cycle.  When every cable is present, the
        designated backup must be the blocked edge.  If at least one cable is
        absent, the cycle is already broken, so every present link may forward.
        """

        all_plugged = all(link["plugged"] for link in self.links)
        target: dict[str, str] = {}
        for link in self.links:
            if not link["plugged"]:
                target[link["id"]] = "down"
            elif all_plugged:
                target[link["id"]] = "blocked" if link["designated_backup"] else "forwarding"
            else:
                target[link["id"]] = "forwarding"
        return target

    def _reconcile_locked(self) -> None:
        target = self._target_states_locked()

        # Block first.  This is the break-before-make part of switchback and
        # prevents a temporary ring loop.
        for link in self.links:
            if not link["plugged"]:
                continue
            if target[link["id"]] != "blocked" or link["state"] != "forwarding":
                continue
            if self._pending_command_for_link_locked(link["id"]) is not None:
                continue
            self._issue_command_locked(
                link["id"],
                "block",
                link["block_delay_ms"],
                "先阻塞备用/旧路径，再安全开通原线路。",
            )

        # Open only when the edge is safe in the current forwarding graph and
        # is not expected to conflict with an already-issued open command.
        for link in self.links:
            if not link["plugged"]:
                continue
            if target[link["id"]] != "forwarding" or link["state"] != "blocked":
                continue
            if self._pending_command_for_link_locked(link["id"]) is not None:
                continue
            if self.creates_cycle(link["id"]):
                continue
            if self._open_conflicts_with_pending_commands_locked(link["id"]):
                continue
            reason = "常用链路失联，启用可用备用链路。" if link["designated_backup"] else "安全条件已满足，恢复常用链路转发。"
            self._issue_command_locked(link["id"], "open", link["open_delay_ms"], reason)

    # -------------------------------------------------------------- actions

    def set_plugged(self, link_id: str, plugged: bool) -> dict[str, Any]:
        with self._lock:
            self.process_due_commands()
            link = self._link_locked(link_id)
            now_clock = self._clock_ms_locked()

            if plugged and link["plugged"]:
                self._log_locked("noop", f"{link['name']} 本来就已插好。")
            elif not plugged and not link["plugged"]:
                self._log_locked("noop", f"{link['name']} 本来就已拔出。")
            elif not plugged:
                link["plugged"] = False
                link["state"] = "down"
                link["last_changed_clock_ms"] = now_clock
                pending = [
                    c for c in self.commands if c["status"] == "pending" and c["link_id"] == link_id
                ]
                note = f" 该链路上 {len(pending)} 条已下发命令不会取消，仍按原定时限尝试。" if pending else ""
                self._log_locked("unplug", f"{link['name']} 拔线：立即停止转发，状态变为 Down。{note}")
            else:
                link["plugged"] = True
                link["state"] = "blocked"
                link["last_changed_clock_ms"] = now_clock
                pending = [
                    c for c in self.commands if c["status"] == "pending" and c["link_id"] == link_id
                ]
                note = f" 已有 {len(pending)} 条命令保持原定时限，不重新计时。" if pending else ""
                self._log_locked("plug", f"{link['name']} 插回：物理已连接，但先保持阻塞。{note}")

            self._reconcile_locked()
            self._process_due_without_lock_needed()
            return self.snapshot_locked()

    def _process_due_without_lock_needed(self) -> None:
        # The public method acquires RLock, which the current thread may re-enter.
        self.process_due_commands()

    def unplug(self, link_id: str) -> dict[str, Any]:
        return self.set_plugged(link_id, False)

    def plug(self, link_id: str) -> dict[str, Any]:
        return self.set_plugged(link_id, True)

    def set_delays(
        self,
        link_id: str,
        open_delay_ms: Optional[int | float] = None,
        block_delay_ms: Optional[int | float] = None,
    ) -> dict[str, Any]:
        with self._lock:
            self.process_due_commands()
            link = self._link_locked(link_id)

            def validate(value: int | float, field: str) -> int:
                try:
                    number = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{field} 必须是数字") from exc
                if number < 0 or number > 600000:
                    raise ValueError(f"{field} 必须在 0 到 600000 毫秒之间")
                return int(round(number))

            changes: list[str] = []
            if open_delay_ms is not None:
                value = validate(open_delay_ms, "开通延时")
                link["open_delay_ms"] = value
                changes.append(f"开通延时={value}ms")
            if block_delay_ms is not None:
                value = validate(block_delay_ms, "阻塞延时")
                link["block_delay_ms"] = value
                changes.append(f"阻塞延时={value}ms")
            if changes:
                self._log_locked(
                    "delay-config",
                    f"{link['name']} 已设置 {('，'.join(changes))}；已下发命令的原定时刻不变。",
                )
            # Configuration can reveal missing work, but does not rewrite jobs.
            self._reconcile_locked()
            self.process_due_commands()
            return self.snapshot_locked()

    def set_backup(self, link_id: str) -> dict[str, Any]:
        with self._lock:
            self.process_due_commands()
            new_backup = self._link_locked(link_id)
            old = next((link for link in self.links if link["designated_backup"]), None)
            if old and old["id"] == link_id:
                self._log_locked("backup-config", f"{new_backup['name']} 已经是指定备用链路。")
                return self.snapshot_locked()

            for link in self.links:
                link["designated_backup"] = link["id"] == link_id
            old_name = old["name"] if old else "无"
            self._log_locked(
                "backup-config",
                f"备用链路已由 {old_name} 改为 {new_backup['name']}；为避免环路，将先阻塞新备用，再开通旧备用。",
            )
            self._reconcile_locked()
            self.process_due_commands()
            return self.snapshot_locked()

    def _forwarding_adjacency_locked(self) -> dict[str, list[tuple[str, str]]]:
        """Adjacency of the graph made only of currently forwarding links."""

        adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for link in self.links:
            if link["state"] == "forwarding":
                adjacency[link["source"]].append((link["target"], link["id"]))
                adjacency[link["target"]].append((link["source"], link["id"]))
        return adjacency

    def _forwarding_path_locked(
        self, source_id: str, target_id: str
    ) -> Optional[tuple[list[str], list[str]]]:
        """Unique forwarding path as ``(switch_ids, link_ids)`` or ``None``.

        The forwarding graph is kept acyclic (a forest), so BFS discovers the
        single path whenever the two switches are in the same component.
        """

        if source_id == target_id:
            return [source_id], []

        adjacency = self._forwarding_adjacency_locked()
        parent: dict[str, tuple[str, str]] = {source_id: ("", "")}
        queue = deque([source_id])

        while queue:
            current = queue.popleft()
            if current == target_id:
                break
            for neighbor, link_id in adjacency[current]:
                if neighbor in parent:
                    continue
                parent[neighbor] = (current, link_id)
                queue.append(neighbor)

        if target_id not in parent:
            return None

        path_nodes: list[str] = []
        path_links: list[str] = []
        cursor = target_id
        while cursor != source_id:
            path_nodes.append(cursor)
            previous, link_id = parent[cursor]
            path_links.append(link_id)
            cursor = previous
        path_nodes.append(source_id)
        path_nodes.reverse()
        path_links.reverse()
        return path_nodes, path_links

    def send_broadcast(self, source_id: str) -> dict[str, Any]:
        with self._lock:
            self.process_due_commands()
            if not any(switch["id"] == source_id for switch in self.SWITCHES):
                raise KeyError(f"未知交换机：{source_id}")

            adjacency = self._forwarding_adjacency_locked()

            visited = {source_id}
            queue = deque([source_id])
            parent: dict[str, tuple[Optional[str], Optional[str]]] = {source_id: (None, None)}
            order = [source_id]

            while queue:
                current = queue.popleft()
                for neighbor, link_id in adjacency[current]:
                    # Visited set is also a defensive guard if a cycle were
                    # ever introduced by a future code change.
                    if neighbor in visited:
                        continue
                    visited.add(neighbor)
                    parent[neighbor] = (current, link_id)
                    order.append(neighbor)
                    queue.append(neighbor)

            deliveries: list[dict[str, Any]] = []
            for switch_id in order:
                path_nodes = [switch_id]
                path_links: list[str] = []
                cursor = switch_id
                while parent[cursor][0] is not None:
                    previous, link_id = parent[cursor]
                    assert previous is not None and link_id is not None
                    path_links.append(link_id)
                    path_nodes.append(previous)
                    cursor = previous
                path_nodes.reverse()
                path_links.reverse()
                deliveries.append(
                    {
                        "switch_id": switch_id,
                        "switch_name": self._switch_name_locked(switch_id),
                        "hop": len(path_links),
                        "path_switch_ids": path_nodes,
                        "path_link_ids": path_links,
                    }
                )

            reached = order
            unreached = [switch["id"] for switch in self.SWITCHES if switch["id"] not in visited]
            probe = {
                "id": self._new_id_locked("p"),
                "at_clock_ms": self._clock_ms_locked(),
                "source": source_id,
                "source_name": self._switch_name_locked(source_id),
                "reached": reached,
                "reached_names": [self._switch_name_locked(sid) for sid in reached],
                "unreached": unreached,
                "unreached_names": [self._switch_name_locked(sid) for sid in unreached],
                "deliveries": deliveries,
                "forwarding_link_ids": self.topology_locked()["forwarding_link_ids"],
                "loop_free": not self._current_forwarding_graph_has_cycle_locked(),
            }
            self.last_probe = probe
            if unreached:
                message = (
                    f"从{probe['source_name']}发送广播：抵达 {len(reached)} 台，"
                    f"未抵达 {('、'.join(probe['unreached_names']))}。"
                )
            else:
                message = f"从{probe['source_name']}发送广播：经当前实际转发链路抵达全部 4 台交换机。"
            probe["message"] = message
            self._log_locked("broadcast", message)
            return probe

    # ------------------------------------------------- bandwidth services

    @staticmethod
    def parse_demand_mbps(value: Any) -> int:
        """Validate the operator-provided demand as a positive integer Mbps."""

        if isinstance(value, bool):
            raise ValueError("带宽必须是正整数 Mbps")
        if isinstance(value, int):
            number = value
        elif isinstance(value, float):
            if not value.is_integer():
                raise ValueError("带宽必须是整数 Mbps，不能填写小数")
            number = int(value)
        elif isinstance(value, str):
            text = value.strip()
            if not re.fullmatch(r"[0-9]+", text):
                raise ValueError("带宽必须填写正整数 Mbps")
            number = int(text)
        else:
            raise ValueError("带宽必须是正整数 Mbps")

        if number < 1:
            raise ValueError("带宽必须是不小于 1 Mbps 的正整数")
        if number > MAX_DEMAND_MBPS:
            raise ValueError(f"单笔带宽不能超过 {MAX_DEMAND_MBPS} Mbps")
        return number

    def register_service(self, source_id: Any, target_id: Any, demand_mbps: Any) -> dict[str, Any]:
        """Register a business service and immediately try to reserve its path."""

        demand = self.parse_demand_mbps(demand_mbps)
        source_id = str(source_id)
        target_id = str(target_id)

        with self._lock:
            # Let any due failover command (and its reservation reconciliation)
            # settle before registering against the actual forwarding graph.
            self.process_due_commands()

            known = {switch["id"] for switch in self.SWITCHES}
            if source_id not in known:
                raise KeyError(f"未知起点交换机：{source_id}")
            if target_id not in known:
                raise KeyError(f"未知终点交换机：{target_id}")
            if source_id == target_id:
                raise ValueError("起点交换机和终点交换机不能相同")

            now_clock = self._clock_ms_locked()
            service_number = next(self._service_seq)
            service: dict[str, Any] = {
                "id": f"svc{service_number}",
                "order": service_number,
                "source": source_id,
                "target": target_id,
                "source_name": self._switch_name_locked(source_id),
                "target_name": self._switch_name_locked(target_id),
                "demand_mbps": demand,
                "status": "waiting",
                "path_switch_ids": None,
                "path_link_ids": None,
                "candidate_path_switch_ids": None,
                "candidate_path_link_ids": None,
                "waiting_reason": "已登记，等待按登记顺序核查实际转发路径与链路额度。",
                "notice": None,
                "created_at_clock_ms": now_clock,
                "updated_at_clock_ms": now_clock,
            }
            self.services.append(service)
            self._log_locked(
                "service-register",
                f"{service['id'].upper()} 登记业务：{service['source_name']} → "
                f"{service['target_name']}，约定带宽 {demand} Mbps（整数，双向共用额度）。",
            )
            self._reconcile_services_locked()
            return self.snapshot_locked()

    def revoke_service(self, service_id: str) -> dict[str, Any]:
        """Remove a registration, free its quota if active, then backfill."""

        with self._lock:
            self.process_due_commands()
            service = next((item for item in self.services if item["id"] == service_id), None)
            if service is None:
                raise KeyError(f"未知业务：{service_id}")

            was_reserved = service["status"] == "reserved"
            path_text = "、".join(link.upper() for link in (service.get("path_link_ids") or []))
            self.services = [item for item in self.services if item["id"] != service_id]
            if was_reserved:
                detail = f"，释放路径（{path_text}）上占用的 {service['demand_mbps']} Mbps"
            else:
                detail = "（该业务正在等待，未占用额度）"
            self._log_locked(
                "service-revoke",
                f"{service['id'].upper()} 已撤销{detail}；按同一规则和登记顺序为等待业务补位。",
            )
            self._reconcile_services_locked()
            return self.snapshot_locked()

    # Domain-language aliases for programmatic callers.
    add_service = register_service
    create_service = register_service
    remove_service = revoke_service
    cancel_service = revoke_service

    def _utilization_locked(self) -> dict[str, int]:
        """Sum reserved Mbps per link across active services (single direction bucket)."""

        used = {link["id"]: 0 for link in self.links}
        for service in self.services:
            if service["status"] != "reserved":
                continue
            for link_id in service.get("path_link_ids") or []:
                if link_id in used:
                    used[link_id] += int(service["demand_mbps"])
        return used

    def _service_path_text_locked(self, switch_ids: list[str], link_ids: list[str]) -> str:
        nodes = " → ".join(self._switch_name_locked(sid) for sid in switch_ids)
        if link_ids:
            nodes += "；经 " + "、".join(link_id.upper() for link_id in link_ids)
        return nodes

    def _reconcile_services_locked(self) -> None:
        """Apply the drill's reservation rule to every registered service.

        1. Active reservations whose actual forwarding path is unchanged are
           kept and keep consuming quota; reservations whose path disappeared
           or changed are released back to the waiting queue.
        2. Waiting services are offered admission strictly in registration
           order.  A service is admitted only when every link of its actual
           forwarding path has enough shared capacity; failure never stops
           later services from being tried.

        The method is quiet when nothing transitions, because it runs after
        every command-due drain and on periodic state snapshots.
        """

        if not getattr(self, "services", None):
            return

        now_clock = self._clock_ms_locked()
        ordered = sorted(self.services, key=lambda item: item["order"])
        used = {link["id"]: 0 for link in self.links}

        # Phase 1: preserve unchanged reservations, release invalid ones.
        for service in ordered:
            if service["status"] != "reserved":
                continue
            old_links = list(service.get("path_link_ids") or [])
            current = self._forwarding_path_locked(service["source"], service["target"])

            if current is not None and list(current[1]) == old_links:
                for link_id in old_links:
                    used[link_id] = used.get(link_id, 0) + int(service["demand_mbps"])
                continue

            old_text = "、".join(link_id.upper() for link_id in old_links) or "原路径"
            if current is None:
                cause = (
                    f"链路倒换后，{service['source_name']} 与 {service['target_name']} "
                    f"已不在同一实际转发连通段，原预留路径（{old_text}）失效"
                )
            else:
                new_text = "、".join(link_id.upper() for link_id in current[1]) or "直连"
                cause = (
                    f"链路倒换后实际转发路径由（{old_text}）变为（{new_text}），"
                    "按规则路径未保持不变"
                )

            service["status"] = "waiting"
            service["path_switch_ids"] = None
            service["path_link_ids"] = None
            service["candidate_path_switch_ids"] = None
            service["candidate_path_link_ids"] = None
            service["notice"] = cause
            service["waiting_reason"] = f"{cause}；已释放占用额度，按原登记顺序重新排队。"
            service["updated_at_clock_ms"] = now_clock
            self._log_locked(
                "service-released",
                f"{service['id'].upper()} {cause}；{service['demand_mbps']} Mbps 额度已释放并转为等待。",
            )

        # Phase 2: admit waiting services in registration order.  ``used`` now
        # contains exactly the kept reservations plus admissions made below.
        for service in ordered:
            if service["status"] == "reserved":
                continue

            demand = int(service["demand_mbps"])
            path = self._forwarding_path_locked(service["source"], service["target"])
            if path is None:
                service["candidate_path_switch_ids"] = None
                service["candidate_path_link_ids"] = None
                service["waiting_reason"] = (
                    f"无路可走：当前实际转发拓扑中 {service['source_name']} 与 "
                    f"{service['target_name']} 不在同一连通段；继续等候，"
                    "链路恢复转发后会按登记顺序自动重试。"
                )
                service["updated_at_clock_ms"] = now_clock
                continue

            path_nodes, path_links = path
            service["candidate_path_switch_ids"] = path_nodes
            service["candidate_path_link_ids"] = path_links

            bottleneck_id: Optional[str] = None
            bottleneck_available = LINK_CAPACITY_MBPS + 1
            for link_id in path_links:
                reserved = used.get(link_id, 0)
                available = LINK_CAPACITY_MBPS - reserved
                if available < demand and available < bottleneck_available:
                    bottleneck_id = link_id
                    bottleneck_available = available

            if bottleneck_id is not None:
                bottleneck_link = self._link_locked(bottleneck_id)
                reserved_here = used.get(bottleneck_id, 0)
                route_text = self._service_path_text_locked(path_nodes, path_links)
                service["waiting_reason"] = (
                    f"带宽不足：候选路径 {route_text}；其中 {bottleneck_link['name']} "
                    f"已预留 {reserved_here}/{LINK_CAPACITY_MBPS} Mbps（双向共用），"
                    f"仅剩 {bottleneck_available} Mbps，不足本业务 {demand} Mbps。"
                    "整条路径额度未全部满足，因此不占用任何额度；继续等候，"
                    "前面的业务变动或撤销后会再次补位。"
                )
                service["updated_at_clock_ms"] = now_clock
                continue

            # Every link has room -> atomic occupation.
            service["status"] = "reserved"
            service["path_switch_ids"] = path_nodes
            service["path_link_ids"] = path_links
            service["waiting_reason"] = None
            service["notice"] = None
            service["updated_at_clock_ms"] = now_clock
            for link_id in path_links:
                used[link_id] = used.get(link_id, 0) + demand

            route_text = self._service_path_text_locked(path_nodes, path_links)
            self._log_locked(
                "service-reserved",
                f"{service['id'].upper()} 已预留成功：{route_text}；"
                f"每段占用 {demand} Mbps，整条路径额度一次性锁定。",
            )

    # ------------------------------------------------------------- snapshot

    def snapshot_locked(self) -> dict[str, Any]:
        topology = self.topology_locked()
        pending_count = sum(1 for command in self.commands if command["status"] == "pending")
        used = self._utilization_locked()

        link_snapshots: list[dict[str, Any]] = []
        for link in self.links:
            item = dict(link)
            reserved_mbps = int(used.get(link["id"], 0))
            item["capacity_mbps"] = LINK_CAPACITY_MBPS
            item["reserved_mbps"] = reserved_mbps
            item["available_mbps"] = LINK_CAPACITY_MBPS - reserved_mbps
            link_snapshots.append(item)

        service_snapshots = [dict(service) for service in self.services]
        reserved_services = sum(1 for service in self.services if service["status"] == "reserved")
        waiting_services = len(self.services) - reserved_services

        return {
            "clock_ms": self._clock_ms_locked(),
            "switches": [dict(switch) for switch in self.SWITCHES],
            "links": link_snapshots,
            "commands": [dict(command) for command in self.commands],
            "events": [dict(event) for event in self.events],
            "last_probe": dict(self.last_probe) if self.last_probe else None,
            "topology": topology,
            "pending_command_count": pending_count,
            "link_capacity_mbps": LINK_CAPACITY_MBPS,
            "services": service_snapshots,
            "service_summary": {
                "total": len(self.services),
                "reserved": reserved_services,
                "waiting": waiting_services,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self.process_due_commands()
            return self.snapshot_locked()
