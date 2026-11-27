"""持久化账簿管理与财务核对接口。

这些是迁移新增的只读/管理接口；既有 /api/funding_use、/api/change_order
提交、核准、驳回、签认、查询接口的契约保持不变。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.ledger import ledger
from app.services import service_for
from app.store import store

router = APIRouter(prefix="/api/finance", tags=["持久化账簿"])


class RollbackPayload(BaseModel):
    reason: str = ""


@router.get("/ledger/status")
def ledger_status() -> dict[str, Any]:
    """账簿状态：模式（active/rolled_back）、分录用量、迁移历史。"""
    return ledger.status()


@router.post("/ledger/migrate")
def ledger_migrate() -> dict[str, Any]:
    """显式把修缮支出与变更签证存量迁移进持久化账簿（可重复执行、断点续迁）。"""
    return ledger.migrate(store)


@router.post("/ledger/rollback")
def ledger_rollback(payload: RollbackPayload) -> dict[str, Any]:
    """回滚迁移：切回内存读源；账簿数据与冲减流水保留，不删除、不重复计。"""
    return ledger.rollback(payload.reason)


@router.get("/totals")
def finance_totals() -> dict[str, Any]:
    """有效凭据、重复引用、退款冲减与净额（激活时来自账簿，回滚后同口径来自内存）。"""
    return ledger.totals(store)


@router.get("/{module}/{entry_id}/versions")
def entry_versions(module: str, entry_id: int) -> dict[str, Any]:
    """某条支出/签证在账簿中的完整历史版本。"""
    if module not in ("funding_use", "change_order"):
        return {"module": module, "entry_id": entry_id, "versions": []}
    service = service_for(module)
    entry = service.get_entry(entry_id) if service else None
    return {
        "module": module,
        "entry_id": entry_id,
        "entry": entry,
        "versions": ledger.versions(module, entry_id),
    }


@router.get("/change-orders/{change_id}/adjustments")
def change_adjustments(change_id: int) -> dict[str, Any]:
    """变更签证费用调整台账：签认基线、历次核准/冲减/驳回与累计净额。"""
    return ledger.adjustment_ledger(change_id)
