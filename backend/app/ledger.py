"""带历史版本的持久化财务账簿。

把修缮支出（funding_use）与变更签证（change_order）的核准费用从内存仓库迁移到
SQLite 持久化账簿，提供：

- 每条凭据的完整版本历史（登记/提交/核准/驳回/冲减，逐版本快照）；
- 整数分金额存储，Decimal 换算，持久化前后净额精确到分；
- 重复引用同一张签证的存量记录只计一次实际款项，原明细仍可追溯；
- 退款/冲减与原支出绑定，冲减确认幂等（迁移与核准并发也只确认一次）；
- 核准结果回写到变更签证的费用调整台账；
- 迁移可回滚：回滚只切换读模式，账簿数据与冲减流水全部保留。

线程模型：单连接 + WAL，所有写操作在进程内 RLock 下的短事务中完成。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from app.config import settings

_CENT = Decimal("0.01")

FUNDING_MODULE = "funding_use"
CHANGE_MODULE = "change_order"

# 内存行上的可追溯标注字段（对旧接口是增量字段，不破坏既有契约）
ANN_LEDGER_ID = "账簿分录ID"
ANN_DUPLICATE = "重复引用"
ANN_DUP_REASON = "重复引用原因"
ANN_LINK_CHANGE = "关联签证"
ANN_REFUND_OF = "原支出"
ANN_ADJ_LEDGER = "费用调整台账"
ANN_ADJ_NET = "费用调整净额"

# 费用调整台账中的冲减/驳回事件类型
EVENT_APPROVE = "核准"
EVENT_REJECT = "驳回"
EVENT_REVERSAL = "冲减"
EVENT_SIGN = "签证签认"
EVENT_MIGRATE = "迁移入账"


# ── 金额工具：一切金额落库前都换算成整数分 ──

def to_cents(value: Any) -> int:
    """把 float/str/int/Decimal 金额换算为整数分（四舍五入）。"""
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return value * 100
    dec = Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)
    return int(dec.scaleb(2))


def cents_to_decimal(cents: int) -> Decimal:
    return Decimal(cents) / Decimal(100)


def cents_to_text(cents: int) -> str:
    return f"{cents_to_decimal(cents):.2f}"


def cents_to_float(cents: int) -> float:
    return float(cents_to_decimal(cents))


def is_refund_row(row: dict[str, Any]) -> bool:
    """识别退款/冲减类支出。

    判定依据（任一成立）：显式凭据类型为退款/冲减、绑定了原支出、金额为负、
    类别或项目以“退款/冲减”开头。注意不能用任意子串匹配，否则“大木作人工费”
    这类正常凭据会被误伤。
    """
    if row.get(ANN_REFUND_OF):
        return True
    cred_type = str(row.get("凭据类型", "")).strip()
    if cred_type in ("退款", "冲减", "退款冲减"):
        return True
    try:
        if float(row.get("实际金额", 0) or 0) < 0:
            return True
    except (TypeError, ValueError):
        pass
    for text in (str(row.get("支出类别", "")), str(row.get("支出项目", ""))):
        head = text.strip()
        if head.startswith("退款") or head.startswith("冲减"):
            return True
    return False


def signed_amount_cents(row: dict[str, Any]) -> int:
    """带符号金额：退款/冲减为负，普通支出为正。"""
    cents = to_cents(row.get("实际金额", 0))
    return -abs(cents) if is_refund_row(row) else abs(cents)


def linked_change_id(row: dict[str, Any]) -> int | None:
    """解析支出行引用的变更签证（显式字段或自动触发备注）。"""
    raw = row.get(ANN_LINK_CHANGE)
    if raw not in (None, ""):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    note = str(row.get("备注说明", ""))
    marker = "变更签证#"
    if marker in note:
        head = note.split(marker, 1)[1]
        digits = ""
        for ch in head:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            return int(digits)
    return None


class Ledger:
    """持久化账簿：版本化分录 + 冲减流水 + 签证费用调整台账。"""

    def __init__(self, db_path: str, *, auto_migrate: bool = True) -> None:
        self.db_path = db_path
        self.auto_migrate = auto_migrate
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._prepared = False

    # ── 连接与建表 ──
    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._conn = conn
            self._prepare(conn)
        return self._conn

    def _prepare(self, conn: sqlite3.Connection) -> None:
        if self._prepared:
            return
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ledger_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_module TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                entry_type TEXT NOT NULL,
                project TEXT,
                category TEXT,
                amount_cents INTEGER NOT NULL,
                budget_cents INTEGER NOT NULL DEFAULT 0,
                occurred_on TEXT,
                handler TEXT,
                status TEXT NOT NULL,
                linked_change_id INTEGER,
                linked_refund_of INTEGER,
                process_name TEXT,
                is_duplicate_ref INTEGER NOT NULL DEFAULT 0,
                duplicate_reason TEXT,
                effective INTEGER NOT NULL DEFAULT 0,
                origin_snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_module, source_id)
            );

            CREATE TABLE IF NOT EXISTS ledger_entry_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                action TEXT NOT NULL,
                status_from TEXT,
                status_to TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                snapshot TEXT NOT NULL,
                operator TEXT,
                note TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(entry_id, version)
            );

            CREATE TABLE IF NOT EXISTS ledger_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id INTEGER,
                source_module TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                change_id INTEGER,
                funding_id INTEGER,
                note TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fee_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                change_order_id INTEGER NOT NULL,
                funding_id INTEGER,
                entry_id INTEGER,
                kind TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                cumulative_cents INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ledger_migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                detail TEXT,
                entries_seen INTEGER NOT NULL DEFAULT 0,
                entries_written INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ledger_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        conn.commit()
        self._prepared = True

    def reset(self) -> None:
        """测试支持：清空账簿并回到未迁移状态。"""
        with self._lock:
            conn = self._connect()
            conn.executescript(
                """
                DELETE FROM ledger_entries;
                DELETE FROM ledger_entry_versions;
                DELETE FROM ledger_journal;
                DELETE FROM fee_adjustments;
                DELETE FROM ledger_migrations;
                DELETE FROM ledger_meta;
                """
            )
            conn.commit()

    # ── 模式与迁移 ──
    def is_active(self) -> bool:
        """账簿是否为当前生效数据源（回滚后保留数据但不再作为读源）。"""
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT value FROM ledger_meta WHERE key='mode'"
            ).fetchone()
            return bool(row and row["value"] == "active")

    def has_ever_migrated(self) -> bool:
        """是否曾执行过迁移（含已回滚），用于阻止回滚后被自动切换覆盖。"""
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT value FROM ledger_meta WHERE key='mode'"
            ).fetchone()
            return bool(row and row["value"] in ("active", "rolled_back"))

    def mode(self) -> str:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT value FROM ledger_meta WHERE key='mode'"
            ).fetchone()
            return row["value"] if row else "inactive"

    def status(self) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            meta = {
                r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM ledger_meta")
            }
            entries = conn.execute("SELECT COUNT(*) c FROM ledger_entries").fetchone()["c"]
            versions = conn.execute("SELECT COUNT(*) c FROM ledger_entry_versions").fetchone()["c"]
            journal = conn.execute("SELECT COUNT(*) c FROM ledger_journal").fetchone()["c"]
            adjustments = conn.execute("SELECT COUNT(*) c FROM fee_adjustments").fetchone()["c"]
            migrations = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, kind, detail, entries_seen, entries_written, created_at "
                    "FROM ledger_migrations ORDER BY id"
                )
            ]
            return {
                "db_path": self.db_path,
                "mode": meta.get("mode", "inactive"),
                "migrated_at": meta.get("migrated_at"),
                "entries": entries,
                "versions": versions,
                "journal_events": journal,
                "fee_adjustments": adjustments,
                "migrations": migrations,
            }

    def ensure_migrated(self, store) -> dict[str, Any] | None:
        """服务入口调用：开启自动迁移时，首次访问把存量一次性迁入账簿。

        已回滚（rolled_back）状态不会被自动迁移覆盖，只能通过显式 migrate 重新切换。
        """
        if not self.auto_migrate or self.is_active() or self.has_ever_migrated():
            return None
        with self._lock:
            if self.is_active() or self.has_ever_migrated():
                return None
            return self._migrate_locked(store)

    def migrate(self, store) -> dict[str, Any]:
        """显式迁移（管理接口），可在回滚后重新切换。"""
        with self._lock:
            return self._migrate_locked(store)

    def rollback(self, reason: str = "") -> dict[str, Any]:
        """回滚迁移：切回内存读源，账簿数据与冲减流水全部保留。"""
        with self._lock:
            conn = self._connect()
            now = self._now()
            conn.execute(
                "INSERT INTO ledger_migrations(kind, detail, entries_seen, entries_written, created_at) "
                "VALUES('rollback', ?, 0, 0, ?)",
                (reason or "手动回滚", now),
            )
            conn.execute(
                "INSERT INTO ledger_meta(key, value) VALUES('mode', 'rolled_back') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            conn.commit()
            return self.status()

    def _migrate_locked(self, store) -> dict[str, Any]:
        conn = self._connect()
        now = self._now()
        seen = written = 0
        funding_rows = [dict(r) for r in store.rows(FUNDING_MODULE)]
        change_rows = [dict(r) for r in store.rows(CHANGE_MODULE)]

        # 断点续迁：已在账簿里的来源行（含迁移后新登记的正常分录）不重复入账，
        # 但其重复标注仍按下述统一规则识别并同步，保证首次/自动/显式迁移结论一致。
        existing = {
            (r["source_module"], r["source_id"])
            for r in conn.execute(
                "SELECT source_module, source_id FROM ledger_entries"
            )
        }

        # 1. 重复引用识别：同一签证、同金额、同凭据性质的多条存量支出，
        #    最早一条有效，其余标注。对当前内存中的全部存量行统一计算。
        #    签认自动生成的「变更费用」调整通知不是实际款项凭据，不参与去重。
        seen_ref: dict[tuple[int, int, str], int] = {}
        duplicate_ids: set[int] = set()
        for row in sorted(funding_rows, key=lambda r: int(r.get("id", 0))):
            change_id = linked_change_id(row)
            if change_id is None or str(row.get("审批状态", "")) == "变更费用":
                row.setdefault(ANN_DUPLICATE, False)
                continue
            amount = signed_amount_cents(row)
            kind = "refund" if is_refund_row(row) else "expense"
            key = (change_id, amount, kind)
            if key in seen_ref:
                duplicate_ids.add(int(row["id"]))
                row[ANN_DUPLICATE] = True
                row[ANN_DUP_REASON] = (
                    f"与支出#{seen_ref[key]}重复引用变更签证#{change_id}的同一笔款项，"
                    "迁移后仅计一次实际款项，本明细保留可追溯"
                )
            else:
                seen_ref[key] = int(row["id"])
                row.setdefault(ANN_DUPLICATE, False)

        try:
            # 2. 支出分录入账（断点续迁：已存在的来源行跳过）
            for row in funding_rows:
                seen += 1
                identity = (FUNDING_MODULE, int(row["id"]))
                change_id = linked_change_id(row)
                refund_of = self._opt_int(row.get(ANN_REFUND_OF))
                entry_type = "冲减" if is_refund_row(row) else "支出"
                amount = signed_amount_cents(row)
                is_dup = int(row["id"]) in duplicate_ids
                if identity in existing:
                    # 已存在分录不重复入账，但同步本轮统一识别的重复标注与有效性
                    conn.execute(
                        "UPDATE ledger_entries SET is_duplicate_ref=?, duplicate_reason=?, "
                        "effective=? WHERE source_module=? AND source_id=?",
                        (
                            int(is_dup),
                            row.get(ANN_DUP_REASON, ""),
                            int(row.get("status") == "已核准" and not is_dup),
                            FUNDING_MODULE,
                            int(row["id"]),
                        ),
                    )
                    continue
                cur = conn.execute(
                    "INSERT INTO ledger_entries(source_module, source_id, entry_type, project, "
                    "category, amount_cents, budget_cents, occurred_on, handler, status, "
                    "linked_change_id, linked_refund_of, process_name, is_duplicate_ref, "
                    "duplicate_reason, effective, origin_snapshot, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        FUNDING_MODULE,
                        int(row["id"]),
                        entry_type,
                        str(row.get("支出项目", "")),
                        str(row.get("支出类别", "")),
                        amount,
                        to_cents(row.get("预算金额", 0)),
                        str(row.get("支出日期", "")),
                        str(row.get("经办人", "")),
                        str(row.get("status", "")),
                        change_id,
                        refund_of,
                        str(row.get("关联工序", "")),
                        int(is_dup),
                        row.get(ANN_DUP_REASON, ""),
                        int(row.get("status") == "已核准" and not is_dup),
                        self._snapshot(row),
                        now,
                        now,
                    ),
                )
                entry_id = int(cur.lastrowid)
                row[ANN_LEDGER_ID] = entry_id
                conn.execute(
                    "INSERT INTO ledger_entry_versions(entry_id, version, action, status_from, "
                    "status_to, amount_cents, snapshot, operator, note, created_at) "
                    "VALUES(?,1,'迁移入账',NULL,?,?,?,?,?,?)",
                    (
                        entry_id,
                        str(row.get("status", "")),
                        amount,
                        self._snapshot(row),
                        str(row.get("经办人", "")),
                        "存量迁移：保留原始明细可追溯",
                        now,
                    ),
                )
                self._insert_journal(
                    conn,
                    entry_id,
                    FUNDING_MODULE,
                    int(row["id"]),
                    EVENT_MIGRATE,
                    amount,
                    f"migrate:funding:{row['id']}",
                    change_id,
                    int(row["id"]),
                    "存量迁移入账",
                )
                written += 1

            # 3. 变更签证分录入账
            for row in change_rows:
                seen += 1
                identity = (CHANGE_MODULE, int(row["id"]))
                fee_cents = to_cents(row.get("费用增减", 0))
                if identity in existing:
                    continue
                cur = conn.execute(
                    "INSERT INTO ledger_entries(source_module, source_id, entry_type, project, "
                    "category, amount_cents, budget_cents, occurred_on, handler, status, "
                    "linked_change_id, linked_refund_of, process_name, is_duplicate_ref, "
                    "duplicate_reason, effective, origin_snapshot, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        CHANGE_MODULE,
                        int(row["id"]),
                        "变更费用",
                        str(row.get("变更事项", "")),
                        "变更签证",
                        fee_cents,
                        fee_cents,
                        str(row.get("签认日期", "")),
                        str(row.get("审批人", "")),
                        str(row.get("status", "")),
                        int(row["id"]),
                        None,
                        str(row.get("影响工序", "")),
                        0,
                        "",
                        int(row.get("status") == "已签认"),
                        self._snapshot(row),
                        now,
                        now,
                    ),
                )
                entry_id = int(cur.lastrowid)
                row[ANN_LEDGER_ID] = entry_id
                conn.execute(
                    "INSERT INTO ledger_entry_versions(entry_id, version, action, status_from, "
                    "status_to, amount_cents, snapshot, operator, note, created_at) "
                    "VALUES(?,1,'迁移入账',NULL,?,?,?,?,?,?)",
                    (
                        entry_id,
                        str(row.get("status", "")),
                        fee_cents,
                        self._snapshot(row),
                        str(row.get("审批人", "")),
                        "存量迁移：保留原始明细可追溯",
                        now,
                    ),
                )
                if row.get("status") == "已签认":
                    self._add_adjustment(
                        conn,
                        int(row["id"]),
                        None,
                        entry_id,
                        EVENT_SIGN,
                        fee_cents,
                        # 与运行期签认同键，重迁移/补登不会产生第二条基线
                        f"change:{row['id']}:sign",
                    )
                written += 1

            # 4. 历史核准结果补登签证费用调整台账（含迟到退款的冲减流水）
            for row in sorted(funding_rows, key=lambda r: int(r.get("id", 0))):
                if int(row["id"]) in duplicate_ids:
                    continue
                change_id = linked_change_id(row)
                status = row.get("status")
                if change_id is None or status not in ("已核准", "已驳回"):
                    continue
                entry_id = row.get(ANN_LEDGER_ID)
                amount = signed_amount_cents(row)
                if status == "已核准":
                    kind = EVENT_REVERSAL if is_refund_row(row) else EVENT_APPROVE
                    # 与运行期核准同键，补登/重迁移/并发交叠都只落一笔
                    key = f"adjust:funding:{row['id']}:approve"
                    self._add_adjustment(
                        conn, change_id, int(row["id"]), entry_id, kind, amount, key
                    )
                    if kind == EVENT_REVERSAL:
                        # 冲减确认流水落库，并发/重放时凭唯一键只确认一次
                        self._insert_journal(
                            conn,
                            entry_id,
                            FUNDING_MODULE,
                            int(row["id"]),
                            EVENT_REVERSAL,
                            amount,
                            f"approve:funding:{row['id']}",
                            change_id,
                            int(row["id"]),
                            "迁移补登：退款冲减确认",
                        )
                else:
                    self._add_adjustment(
                        conn,
                        change_id,
                        int(row["id"]),
                        entry_id,
                        EVENT_REJECT,
                        0,
                        f"adjust:funding:{row['id']}:reject",
                    )

            # 5. 回写签证费用调整台账到内存真实行（旧接口 GET 即可见）
            for real_row in store.rows(CHANGE_MODULE):
                self._write_back_change(conn, real_row)

            # 5.1 把重复引用标注写回 store 真实行（副本上的标注需同步）
            dup_map = {int(r["id"]): r for r in funding_rows if r.get(ANN_DUPLICATE)}
            for real_row in store.rows(FUNDING_MODULE):
                src = dup_map.get(int(real_row["id"]))
                if src is not None:
                    real_row[ANN_DUPLICATE] = True
                    real_row[ANN_DUP_REASON] = src.get(ANN_DUP_REASON, "")
                elif int(real_row["id"]) in {int(r["id"]) for r in funding_rows}:
                    real_row.setdefault(ANN_DUPLICATE, False)

            # 6. 统一回填内存行的账簿分录 ID（断点续迁时已存在的分录也要标注）。
            #    注意必须写回 store 中的真实行，而不是上面的快照副本。
            id_map = {
                (r["source_module"], r["source_id"]): r["id"]
                for r in conn.execute(
                    "SELECT id, source_module, source_id FROM ledger_entries"
                )
            }
            for real_row in store.rows(FUNDING_MODULE):
                mapped = id_map.get((FUNDING_MODULE, int(real_row["id"])))
                if mapped is not None:
                    real_row[ANN_LEDGER_ID] = mapped
            for real_row in store.rows(CHANGE_MODULE):
                mapped = id_map.get((CHANGE_MODULE, int(real_row["id"])))
                if mapped is not None:
                    real_row[ANN_LEDGER_ID] = mapped

            conn.execute(
                "INSERT INTO ledger_migrations(kind, detail, entries_seen, entries_written, created_at) "
                "VALUES('migrate', ?, ?, ?, ?)",
                (f"迁移至 {self.db_path}", seen, written, now),
            )
            conn.execute(
                "INSERT INTO ledger_meta(key, value) VALUES('mode', 'active') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            conn.execute(
                "INSERT INTO ledger_meta(key, value) VALUES('migrated_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (now,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        report = {"seen": seen, "written": written, "duplicates": sorted(duplicate_ids)}
        report.update(self.status())
        return report

    # ── 运行期双写：登记 / 动作 ──
    def sync_funding_upsert(self, row: dict[str, Any]) -> int | None:
        """新登记的支出同步为账簿分录（版本1）。未迁移时为空操作。"""
        if not self.is_active():
            return None
        with self._lock:
            conn = self._connect()
            now = self._now()
            amount = signed_amount_cents(row)
            change_id = linked_change_id(row)
            cur = conn.execute(
                "INSERT INTO ledger_entries(source_module, source_id, entry_type, project, "
                "category, amount_cents, budget_cents, occurred_on, handler, status, "
                "linked_change_id, linked_refund_of, process_name, is_duplicate_ref, "
                "duplicate_reason, effective, origin_snapshot, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    FUNDING_MODULE,
                    int(row["id"]),
                    "冲减" if is_refund_row(row) else "支出",
                    str(row.get("支出项目", "")),
                    str(row.get("支出类别", "")),
                    amount,
                    to_cents(row.get("预算金额", 0)),
                    str(row.get("支出日期", "")),
                    str(row.get("经办人", "")),
                    str(row.get("status", "")),
                    change_id,
                    self._opt_int(row.get(ANN_REFUND_OF)),
                    str(row.get("关联工序", "")),
                    0,
                    "",
                    0,
                    self._snapshot(row),
                    now,
                    now,
                ),
            )
            entry_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO ledger_entry_versions(entry_id, version, action, status_from, "
                "status_to, amount_cents, snapshot, operator, note, created_at) "
                "VALUES(?,1,'登记',NULL,?,?,?,?,?,?)",
                (
                    entry_id,
                    str(row.get("status", "")),
                    amount,
                    self._snapshot(row),
                    str(row.get("经办人", "")),
                    "新登记凭据",
                    now,
                ),
            )
            conn.commit()
            row[ANN_LEDGER_ID] = entry_id
            return entry_id

    def sync_funding_action(
        self,
        row: dict[str, Any],
        action: str,
        status_from: str,
        store,
    ) -> dict[str, Any] | None:
        """支出动作（提交/核准/驳回）同步版本历史，并处理冲减与台账回写。"""
        if not self.is_active():
            return None
        with self._lock:
            conn = self._connect()
            now = self._now()
            source_id = int(row["id"])
            entry = self._get_entry_conn(conn, FUNDING_MODULE, source_id)
            if entry is None:
                # 回滚后重新切换等场景：动作到达时分录缺失，先补建
                self.sync_funding_upsert(row)
                conn = self._connect()
                entry = self._get_entry_conn(conn, FUNDING_MODULE, source_id)
            entry_id = int(entry["id"])
            amount = signed_amount_cents(row)
            status_to = str(row.get("status", ""))
            is_dup = bool(entry["is_duplicate_ref"])
            next_version = int(entry["version_count"]) + 1

            conn.execute(
                "UPDATE ledger_entries SET status=?, amount_cents=?, effective=?, "
                "updated_at=? WHERE id=?",
                (
                    status_to,
                    amount,
                    int(status_to == "已核准" and not is_dup),
                    now,
                    entry_id,
                ),
            )
            conn.execute(
                "INSERT INTO ledger_entry_versions(entry_id, version, action, status_from, "
                "status_to, amount_cents, snapshot, operator, note, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id,
                    next_version,
                    action,
                    status_from,
                    status_to,
                    amount,
                    self._snapshot(row),
                    str(row.get("经办人", "")),
                    str(row.get("备注说明", "")),
                    now,
                ),
            )

            change_id = linked_change_id(row)
            summary: dict[str, Any] | None = None
            if action == "核准支出" and not is_dup:
                idem_key = f"approve:funding:{source_id}"
                already = conn.execute(
                    "SELECT 1 FROM ledger_journal WHERE idempotency_key=?", (idem_key,)
                ).fetchone()
                if already is None:
                    if is_refund_row(row):
                        # 退款冲减：只确认一次，迁移与核准并发也不会重复
                        self._insert_journal(
                            conn,
                            entry_id,
                            FUNDING_MODULE,
                            source_id,
                            EVENT_REVERSAL,
                            amount,
                            idem_key,
                            change_id,
                            source_id,
                            f"退款冲减对应原支出#{row.get(ANN_REFUND_OF) or '-'}",
                        )
                        if change_id is not None:
                            self._add_adjustment(
                                conn,
                                change_id,
                                source_id,
                                entry_id,
                                EVENT_REVERSAL,
                                amount,
                                f"adjust:funding:{source_id}:approve",
                            )
                    else:
                        self._insert_journal(
                            conn,
                            entry_id,
                            FUNDING_MODULE,
                            source_id,
                            EVENT_APPROVE,
                            amount,
                            idem_key,
                            change_id,
                            source_id,
                            "支出核准确认",
                        )
                        if change_id is not None:
                            self._add_adjustment(
                                conn,
                                change_id,
                                source_id,
                                entry_id,
                                EVENT_APPROVE,
                                amount,
                                f"adjust:funding:{source_id}:approve",
                            )
            elif action == "驳回支出" and change_id is not None:
                self._add_adjustment(
                    conn,
                    change_id,
                    source_id,
                    entry_id,
                    EVENT_REJECT,
                    0,
                    f"adjust:funding:{source_id}:reject",
                )

            if change_id is not None:
                change_row = store.find(CHANGE_MODULE, change_id)
                if change_row is not None:
                    summary = self._write_back_change(conn, change_row)
            conn.commit()
            row[ANN_LEDGER_ID] = entry_id
            return summary

    def sync_change_upsert(self, row: dict[str, Any]) -> int | None:
        if not self.is_active():
            return None
        with self._lock:
            conn = self._connect()
            now = self._now()
            fee_cents = to_cents(row.get("费用增减", 0))
            cur = conn.execute(
                "INSERT INTO ledger_entries(source_module, source_id, entry_type, project, "
                "category, amount_cents, budget_cents, occurred_on, handler, status, "
                "linked_change_id, linked_refund_of, process_name, is_duplicate_ref, "
                "duplicate_reason, effective, origin_snapshot, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    CHANGE_MODULE,
                    int(row["id"]),
                    "变更费用",
                    str(row.get("变更事项", "")),
                    "变更签证",
                    fee_cents,
                    fee_cents,
                    str(row.get("签认日期", "")),
                    str(row.get("审批人", "")),
                    str(row.get("status", "")),
                    int(row["id"]),
                    None,
                    str(row.get("影响工序", "")),
                    0,
                    "",
                    0,
                    self._snapshot(row),
                    now,
                    now,
                ),
            )
            entry_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO ledger_entry_versions(entry_id, version, action, status_from, "
                "status_to, amount_cents, snapshot, operator, note, created_at) "
                "VALUES(?,1,'登记',NULL,?,?,?,?,?,?)",
                (
                    entry_id,
                    str(row.get("status", "")),
                    fee_cents,
                    self._snapshot(row),
                    str(row.get("提出人", "")),
                    "新登记变更签证",
                    now,
                ),
            )
            conn.commit()
            row[ANN_LEDGER_ID] = entry_id
            return entry_id

    def sync_change_action(
        self,
        row: dict[str, Any],
        action: str,
        status_from: str,
        new_funding_rows: list[dict[str, Any]],
    ) -> None:
        """签证动作同步版本；签认时登记费用基线，并接管自动生成的支出分录。"""
        if not self.is_active():
            return
        with self._lock:
            conn = self._connect()
            now = self._now()
            source_id = int(row["id"])
            entry = self._get_entry_conn(conn, CHANGE_MODULE, source_id)
            entry_id = (
                int(entry["id"])
                if entry is not None
                else int(self.sync_change_upsert(row) or 0)
            )
            if entry is None:
                conn = self._connect()
                entry = self._get_entry_conn(conn, CHANGE_MODULE, source_id)
            fee_cents = to_cents(row.get("费用增减", 0))
            status_to = str(row.get("status", ""))
            next_version = int(entry["version_count"]) + 1
            conn.execute(
                "UPDATE ledger_entries SET status=?, amount_cents=?, effective=?, updated_at=? "
                "WHERE id=?",
                (
                    status_to,
                    fee_cents,
                    int(status_to == "已签认"),
                    now,
                    entry_id,
                ),
            )
            conn.execute(
                "INSERT INTO ledger_entry_versions(entry_id, version, action, status_from, "
                "status_to, amount_cents, snapshot, operator, note, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id,
                    next_version,
                    action,
                    status_from,
                    status_to,
                    fee_cents,
                    self._snapshot(row),
                    str(row.get("审批人", "")),
                    str(row.get("备注说明", "")),
                    now,
                ),
            )
            if action == "签认变更":
                self._add_adjustment(
                    conn,
                    source_id,
                    None,
                    entry_id,
                    EVENT_SIGN,
                    fee_cents,
                    f"change:{source_id}:sign",
                )
            # 签认副作用自动生成的支出行同步入账（待提交，不计实际款项）
            for funding_row in new_funding_rows:
                if self._get_entry_conn(conn, FUNDING_MODULE, int(funding_row["id"])) is None:
                    self.sync_funding_upsert(funding_row)
            conn.commit()
            row[ANN_LEDGER_ID] = entry_id

    def reversal_already_confirmed(self, funding_id: int) -> bool:
        """供服务层并发判定：该退款的冲减是否已确认过。"""
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT 1 FROM ledger_journal WHERE idempotency_key=? AND event=?",
                (f"approve:funding:{funding_id}", EVENT_REVERSAL),
            ).fetchone()
            return row is not None

    def count_reversals(self, funding_id: int) -> int:
        """某条退款支出在账簿中确认的冲减次数（幂等正确性核对用）。"""
        with self._lock:
            conn = self._connect()
            return conn.execute(
                "SELECT COUNT(*) c FROM ledger_journal "
                "WHERE idempotency_key=? AND event=?",
                (f"approve:funding:{funding_id}", EVENT_REVERSAL),
            ).fetchone()["c"]

    def journal_events(self, module: str | None = None) -> list[dict[str, Any]]:
        """查询冲减/核准等流水，供管理接口与测试核对。"""
        with self._lock:
            conn = self._connect()
            if module:
                rows = conn.execute(
                    "SELECT source_module, source_id, event, amount_cents, "
                    "idempotency_key, change_id, funding_id, note, created_at "
                    "FROM ledger_journal WHERE source_module=? ORDER BY id",
                    (module,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT source_module, source_id, event, amount_cents, "
                    "idempotency_key, change_id, funding_id, note, created_at "
                    "FROM ledger_journal ORDER BY id"
                ).fetchall()
            return [dict(r) for r in rows]

    # ── 查询：版本历史、台账、有效凭据与净额 ──
    def versions(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            entry = self._get_entry_conn(conn, module, entry_id)
            if entry is None:
                return []
            rows = conn.execute(
                "SELECT version, action, status_from, status_to, amount_cents, snapshot, "
                "operator, note, created_at FROM ledger_entry_versions "
                "WHERE entry_id=? ORDER BY version",
                (entry["id"],),
            ).fetchall()
            return [
                {
                    "version": r["version"],
                    "action": r["action"],
                    "status_from": r["status_from"],
                    "status_to": r["status_to"],
                    "amount_cents": r["amount_cents"],
                    "amount": cents_to_float(r["amount_cents"]),
                    "amount_text": cents_to_text(r["amount_cents"]),
                    "snapshot": json.loads(r["snapshot"]),
                    "operator": r["operator"],
                    "note": r["note"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def adjustment_ledger(self, change_id: int) -> dict[str, Any]:
        """变更签证费用调整台账（含累计额，精确到分）。"""
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT id, change_order_id, funding_id, kind, amount_cents, cumulative_cents, "
                "created_at FROM fee_adjustments WHERE change_order_id=? ORDER BY id",
                (change_id,),
            ).fetchall()
            items = [
                {
                    "id": r["id"],
                    "change_order_id": r["change_order_id"],
                    "funding_id": r["funding_id"],
                    "kind": r["kind"],
                    "amount_cents": r["amount_cents"],
                    "amount": cents_to_float(r["amount_cents"]),
                    "amount_text": cents_to_text(r["amount_cents"]),
                    "cumulative_cents": r["cumulative_cents"],
                    "cumulative_text": cents_to_text(r["cumulative_cents"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
            # 台账净额只认实际款项（核准/冲减），签证签认基线单列
            realized = sum(
                r["amount_cents"] for r in rows if r["kind"] in (EVENT_APPROVE, EVENT_REVERSAL)
            )
            baseline = sum(r["amount_cents"] for r in rows if r["kind"] == EVENT_SIGN)
            return {
                "change_order_id": change_id,
                "baseline_cents": baseline,
                "baseline_text": cents_to_text(baseline),
                "net_cents": realized,
                "net_text": cents_to_text(realized),
                "net": cents_to_float(realized),
                "items": items,
            }

    def ledger_totals(self) -> dict[str, Any]:
        """以持久化账簿为源的有效凭据与净额。"""
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT source_id, entry_type, amount_cents, status AS entry_status, "
                "is_duplicate_ref, linked_change_id, linked_refund_of, project, category "
                "FROM ledger_entries WHERE source_module=?",
                (FUNDING_MODULE,),
            ).fetchall()
            valid, invalid, duplicates = [], [], []
            net = 0
            for r in rows:
                item = {
                    "funding_id": r["source_id"],
                    "project": r["project"],
                    "category": r["category"],
                    "amount_cents": r["amount_cents"],
                    "amount_text": cents_to_text(r["amount_cents"]),
                    "status": r["entry_status"],
                    "linked_change_id": r["linked_change_id"],
                    "linked_refund_of": r["linked_refund_of"],
                }
                if r["is_duplicate_ref"]:
                    duplicates.append(item)
                elif r["entry_status"] == "已核准":
                    valid.append(item)
                    net += r["amount_cents"]
                elif r["entry_status"] == "已驳回":
                    invalid.append(item)
            change_rows = conn.execute(
                "SELECT source_id, project, amount_cents, status AS entry_status "
                "FROM ledger_entries WHERE source_module=? ORDER BY source_id",
                (CHANGE_MODULE,),
            ).fetchall()
            visas = []
            for r in change_rows:
                adj = self.adjustment_ledger(r["source_id"])
                visas.append(
                    {
                        "change_order_id": r["source_id"],
                        "change_item": r["project"],
                        "status": r["entry_status"],
                        "fee_change_cents": r["amount_cents"],
                        "fee_change_text": cents_to_text(r["amount_cents"]),
                        "adjustment_net_cents": adj["net_cents"],
                        "adjustment_net_text": adj["net_text"],
                        "valid_credential": r["entry_status"] == "已签认",
                    }
                )
            return {
                "source": "ledger",
                "funding": {
                    "net_cents": net,
                    "net_text": cents_to_text(net),
                    "net": cents_to_float(net),
                    "valid_credentials": valid,
                    "rejected_credentials": invalid,
                    "duplicate_references": duplicates,
                },
                "change_orders": visas,
            }

    def store_totals(self, store) -> dict[str, Any]:
        """回滚/未迁移时以内存行为源，规则与账簿完全一致，保证净额衔接。"""
        valid, invalid, duplicates = [], [], []
        net = 0
        for raw in store.rows(FUNDING_MODULE):
            row = dict(raw)
            item = {
                "funding_id": int(row["id"]),
                "project": row.get("支出项目", ""),
                "category": row.get("支出类别", ""),
                "amount_cents": signed_amount_cents(row),
                "amount_text": cents_to_text(signed_amount_cents(row)),
                "status": row.get("status", ""),
                "linked_change_id": linked_change_id(row),
                "linked_refund_of": self._opt_int(row.get(ANN_REFUND_OF)),
            }
            if row.get(ANN_DUPLICATE):
                duplicates.append(item)
            elif row.get("status") == "已核准":
                valid.append(item)
                net += item["amount_cents"]
            elif row.get("status") == "已驳回":
                invalid.append(item)
        visas = []
        for raw in store.rows(CHANGE_MODULE):
            row = dict(raw)
            change_id = int(row["id"])
            realized = 0
            for f_raw in store.rows(FUNDING_MODULE):
                f = dict(f_raw)
                if linked_change_id(f) != change_id or f.get(ANN_DUPLICATE):
                    continue
                if f.get("status") == "已核准":
                    realized += signed_amount_cents(f)
            visas.append(
                {
                    "change_order_id": change_id,
                    "change_item": row.get("变更事项", ""),
                    "status": row.get("status", ""),
                    "fee_change_cents": to_cents(row.get("费用增减", 0)),
                    "fee_change_text": cents_to_text(to_cents(row.get("费用增减", 0))),
                    "adjustment_net_cents": realized,
                    "adjustment_net_text": cents_to_text(realized),
                    "valid_credential": row.get("status") == "已签认",
                }
            )
        return {
            "source": "store",
            "funding": {
                "net_cents": net,
                "net_text": cents_to_text(net),
                "net": cents_to_float(net),
                "valid_credentials": valid,
                "rejected_credentials": invalid,
                "duplicate_references": duplicates,
            },
            "change_orders": visas,
        }

    def totals(self, store) -> dict[str, Any]:
        """统一净额入口：激活走账簿，否则走内存（同口径，精确到分）。"""
        if self.is_active():
            return self.ledger_totals()
        return self.store_totals(store)

    # ── 内部工具 ──
    def _get_entry_conn(self, conn: sqlite3.Connection, module: str, source_id: int):
        return conn.execute(
            "SELECT e.*, "
            "(SELECT COUNT(*) FROM ledger_entry_versions v WHERE v.entry_id=e.id) version_count "
            "FROM ledger_entries e WHERE e.source_module=? AND e.source_id=?",
            (module, source_id),
        ).fetchone()

    def _insert_journal(
        self,
        conn: sqlite3.Connection,
        entry_id: int | None,
        module: str,
        source_id: int,
        event: str,
        amount_cents: int,
        idem_key: str,
        change_id: int | None,
        funding_id: int | None,
        note: str,
    ) -> bool:
        """登记流水；幂等键冲突时忽略。返回 True 表示本次新插入。"""
        cur = conn.execute(
            "INSERT OR IGNORE INTO ledger_journal(entry_id, source_module, source_id, event, "
            "amount_cents, idempotency_key, change_id, funding_id, note, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                entry_id,
                module,
                source_id,
                event,
                amount_cents,
                idem_key,
                change_id,
                funding_id,
                note,
                self._now(),
            ),
        )
        return cur.rowcount > 0

    def _add_adjustment(
        self,
        conn: sqlite3.Connection,
        change_id: int,
        funding_id: int | None,
        entry_id: int | None,
        kind: str,
        amount_cents: int,
        idem_key: str,
    ) -> None:
        """登记费用调整台账项；唯一键保证重放/并发下同一事件只落一笔。"""
        exists = conn.execute(
            "SELECT 1 FROM fee_adjustments WHERE idempotency_key=?", (idem_key,)
        ).fetchone()
        if exists:
            return
        cumulative = conn.execute(
            "SELECT COALESCE(MAX(cumulative_cents), 0) c FROM fee_adjustments "
            "WHERE change_order_id=?",
            (change_id,),
        ).fetchone()["c"]
        # 累计只随实际款项（核准/冲减）变动
        if kind in (EVENT_APPROVE, EVENT_REVERSAL):
            cumulative += amount_cents
        conn.execute(
            "INSERT INTO fee_adjustments(change_order_id, funding_id, entry_id, kind, "
            "amount_cents, cumulative_cents, idempotency_key, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                change_id,
                funding_id,
                entry_id,
                kind,
                amount_cents,
                cumulative,
                idem_key,
                self._now(),
            ),
        )

    def _write_back_change(
        self, conn: sqlite3.Connection, change_row: dict[str, Any]
    ) -> dict[str, Any]:
        """把费用调整台账与净额回写到签证内存行（旧接口直接可见）。"""
        change_id = int(change_row["id"])
        rows = conn.execute(
            "SELECT funding_id, kind, amount_cents, cumulative_cents, created_at "
            "FROM fee_adjustments WHERE change_order_id=? ORDER BY id",
            (change_id,),
        ).fetchall()
        ledger = [
            {
                "funding_id": r["funding_id"],
                "kind": r["kind"],
                "amount": cents_to_float(r["amount_cents"]),
                "amount_text": cents_to_text(r["amount_cents"]),
                "cumulative_text": cents_to_text(r["cumulative_cents"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        realized = sum(
            r["amount_cents"] for r in rows if r["kind"] in (EVENT_APPROVE, EVENT_REVERSAL)
        )
        change_row[ANN_ADJ_LEDGER] = ledger
        change_row[ANN_ADJ_NET] = cents_to_float(realized)
        return {
            "change_order_id": change_id,
            "net_cents": realized,
            "net_text": cents_to_text(realized),
            "items": ledger,
        }

    @staticmethod
    def _snapshot(row: dict[str, Any]) -> str:
        return json.dumps(row, ensure_ascii=False, default=str, sort_keys=True)

    @staticmethod
    def _opt_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")


ledger = Ledger(settings.ledger_path, auto_migrate=settings.ledger_auto_migrate)
