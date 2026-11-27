"""变更签证领域服务：签认签证后直接更新施工排期里程碑和费用联动。

签证的费用增减、签认版本与费用调整台账同步到持久化账簿；
核准/冲减结果回写到签证的「费用调整台账」字段，旧查询接口可直接核对。
"""
from __future__ import annotations

from typing import Any

from app.ledger import ANN_ADJ_LEDGER, ANN_ADJ_NET, ledger
from app.services.base import BaseModuleService
from app.store import store


class ChangeOrderService(BaseModuleService):
    MODULE = "change_order"
    ENTITY = "变更签证单"
    OPERATION_TERM = "提出变更"
    LIST_FIELDS = [
        "变更事项", "变更原因", "影响工序", "费用增减", "提出人", "审批人",
        "签认日期", "备注说明", "费用调整台账", "费用调整净额",
    ]
    REQUIRED_FIELDS = ["变更事项", "变更原因", "影响工序"]
    STATUS_ORDER = ["待提出", "已提出", "已签认", "已驳回"]
    ACTION_RULES = {"提出变更": "已提出", "签认变更": "已签认", "驳回变更": "已驳回"}
    NEGATIVE_ACTIONS = ["驳回变更"]

    def _ensure_ledger(self) -> None:
        ledger.ensure_migrated(store)

    def list_entries(self, *args: Any, **kwargs: Any):
        self._ensure_ledger()
        return super().list_entries(*args, **kwargs)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        self._ensure_ledger()
        entry = super().get_entry(entry_id)
        if entry is not None and ledger.is_active():
            # 旧接口 GET 单条签证也能看到最新费用调整台账与净额
            ledger.adjustment_ledger(entry_id)
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        self._ensure_ledger()
        entry, missing = super().create_entry(values)
        if entry is not None:
            ledger.sync_change_upsert(entry)
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        self._ensure_ledger()
        before = store.find(self.MODULE, entry_id)
        status_from = before.get("status", "") if before else ""
        funding_before = {int(r["id"]) for r in store.rows("funding_use")}

        entry, message = super().run_action(entry_id, action)
        if entry is not None and entry.get("status") != status_from:
            # 仅在状态真正流转时追加版本；幂等短路的重复请求不产生新版本
            new_funding = [
                r for r in store.rows("funding_use") if int(r["id"]) not in funding_before
            ]
            ledger.sync_change_action(entry, action, status_from, new_funding)
        return entry, message

    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        if action == "签认变更":
            process = entry.get("影响工序", "")
            # 1. 校验被影响的工序/排期确实存在
            schedules = store.query_by_field("construction_schedule", "计划工序", process)
            if not schedules:
                return False, f"受影响工序「{process}」在施工排期中不存在"
            # 2. 不能对已完成验收的工序签认变更
            accs = store.query_by_field("acceptance_record", "验收工序", process)
            if any(a.get("status") == "已签署" for a in accs):
                return False, f"工序「{process}」已有签署验收，不能签认变更"
            # 3. 费用增减不能为负且超过已核准总支出
            fee_change_val = entry.get("费用增减", 0)
            if fee_change_val is None:
                fee_change_val = 0
            fee_change = float(fee_change_val)
            if fee_change < 0:
                approved_total = sum(
                    float(r.get("实际金额", 0)) for r in store.rows("funding_use")
                    if r.get("status") == "已核准"
                )
                if abs(fee_change) > approved_total:
                    return False, "费用扣减超过已核准总支出"
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        effects = []
        if action == "签认变更":
            process = entry.get("影响工序", "")
            # 1. 更新施工排期里程碑节点
            for sched in store.rows("construction_schedule"):
                if sched.get("计划工序", "") == process:
                    sched["里程碑节点"] = f"变更签证#{entry_id}：{entry.get('变更事项', '')}"
                    sched["abnormal"] = True
                    effects.append(dict(sched))

            # 2. 如果费用有变化，创建修缮支出预警记录
            fee_change_val = entry.get("费用增减", 0)
            if fee_change_val is None:
                fee_change_val = 0
            fee_change = float(fee_change_val)
            if fee_change != 0:
                funding_rows = store.rows("funding_use")
                new_funding = {
                    "id": max((int(r.get("id", 0)) for r in funding_rows), default=0) + 1,
                    "支出项目": f"变更签证#{entry_id}费用调整",
                    "支出类别": "变更追加",
                    "预算金额": fee_change,
                    "实际金额": fee_change,
                    "支出日期": entry.get("签认日期", ""),
                    "经办人": entry.get("审批人", ""),
                    "审批状态": "变更费用",
                    "备注说明": f"由变更签证#{entry_id}自动触发",
                    "关联签证": entry_id,
                    "关联工序": process,
                    "status": "待提交",
                    "pending": True,
                    "abnormal": False,
                }
                funding_rows.append(new_funding)
                effects.append(new_funding)

            # 3. 更新关联工序异常标记
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process:
                    ws_row["abnormal"] = True
                    effects.append(dict(ws_row))
        return effects

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        totals = ledger.totals(store)
        snapshot["finance_totals"] = totals
        snapshot["fee_adjustment_fields"] = [ANN_ADJ_LEDGER, ANN_ADJ_NET]
        return snapshot
