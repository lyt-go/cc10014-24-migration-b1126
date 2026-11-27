"""修缮支出领域服务：支出审批与变更签证费用联动，超出预算后联动排期异常。

费用数据双写到带历史版本的持久化账簿（app.ledger）：
- 旧的提交/核准/驳回/查询接口契约保持不变；
- 核准结果同步版本历史，退款冲减幂等确认，并回写签证费用调整台账；
- 回滚迁移后接口仍可用，冲减流水保留在账簿中不丢失、不重复。
"""
from __future__ import annotations

from typing import Any

from app.ledger import ANN_DUPLICATE, ledger
from app.services.base import BaseModuleService
from app.store import store


class FundingUseService(BaseModuleService):
    MODULE = "funding_use"
    ENTITY = "支出记录"
    OPERATION_TERM = "提交报销"
    # 追加可选关联字段，旧调用方不传也不受影响
    LIST_FIELDS = [
        "支出项目", "支出类别", "预算金额", "实际金额", "支出日期", "经办人",
        "审批状态", "备注说明", "凭据类型", "关联签证", "原支出", "关联工序",
    ]
    REQUIRED_FIELDS = ["支出项目", "支出类别", "预算金额"]
    STATUS_ORDER = ["待提交", "已提交", "已核准", "已驳回"]
    ACTION_RULES = {"提交报销": "已提交", "核准支出": "已核准", "驳回支出": "已驳回"}
    NEGATIVE_ACTIONS = ["驳回支出"]

    def _ensure_ledger(self) -> None:
        ledger.ensure_migrated(store)

    # ── 旧接口保持签名不变，内部接入持久化账簿 ──
    def list_entries(self, *args: Any, **kwargs: Any):
        self._ensure_ledger()
        return super().list_entries(*args, **kwargs)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        self._ensure_ledger()
        return super().get_entry(entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        self._ensure_ledger()
        entry, missing = super().create_entry(values)
        if entry is not None:
            ledger.sync_funding_upsert(entry)
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        self._ensure_ledger()
        before = store.find(self.MODULE, entry_id)
        status_from = before.get("status", "") if before else ""
        entry, message = super().run_action(entry_id, action)
        if entry is not None and entry.get("status") != status_from:
            # 仅在状态真正流转时追加账簿版本；基类幂等短路的重复请求不产生新版本
            ledger.sync_funding_action(entry, action, status_from, store)
        return entry, message

    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        if action == "核准支出":
            actual = float(entry.get("实际金额", 0))
            budget = float(entry.get("预算金额", 0))
            # 超额检查
            if actual > budget * 1.1:
                # 检查是否有变更签证支撑超额
                cs = [
                    r for r in store.rows("change_order")
                    if r.get("status") == "已签认" and "费用增减" in str(r.keys())
                ]
                has_support = any(
                    float(c.get("费用增减", 0)) >= (actual - budget)
                    for c in cs
                )
                if not has_support:
                    return False, f"超出预算{actual - budget:.2f}元，需变更签证支撑"
        if action == "驳回支出":
            # 驳回后检查是否有已核准的支出引用了此驳回项
            pass  # 无特殊前置校验
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        effects = []
        if action == "核准支出":
            # 核准后更新竣工总结中的费用汇总
            for summary in store.rows("completion_summary"):
                if summary.get("status") in ("编制中", "已提交"):
                    summary["费用已核准"] = True
                    effects.append(dict(summary))
        return effects

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        # 附带账簿口径的净额与有效凭据，看板与旧接口共用同一口径
        snapshot["finance_totals"] = ledger.totals(store)
        snapshot["duplicate_reference_count"] = sum(
            1 for row in self._snapshot_rows() if row.get(ANN_DUPLICATE)
        )
        return snapshot

    def _snapshot_rows(self) -> list[dict[str, Any]]:
        return store.rows(self.MODULE)
