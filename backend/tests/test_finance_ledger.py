"""持久化账簿迁移与费用核对测试。

覆盖需求：
1. 修缮支出/变更签证迁移到带历史版本的持久化账簿，旧接口契约不变；
2. 重复引用同一张签证的存量记录只计一次实际款项，原明细可追溯；
3. 带小数退款与原支出对应，持久化前后净额精确到分；
4. 迁移与核准并发时冲减只确认一次；迟到的 43.25 元退款在切换后仍可旧接口查询核对；
5. 回滚后冲减不凭空消失、也不重复计；重新切换数据不重复；
6. 180.40 元人工支出 + 同签证一次冲减 + 并发驳回同工序另一条支出的试运行；
7. 变更签证与修缮支出两侧的有效凭据和净额核对。
"""
from __future__ import annotations

import concurrent.futures

import pytest

from app.ledger import (
    ANN_ADJ_LEDGER,
    ANN_ADJ_NET,
    ANN_DUPLICATE,
    ANN_DUP_REASON,
    ANN_LEDGER_ID,
    ANN_LINK_CHANGE,
    ANN_REFUND_OF,
    cents_to_text,
    ledger,
    to_cents,
)
from app.services.change_order import ChangeOrderService
from app.services.construction_schedule import ConstructionScheduleService
from app.services.funding_use import FundingUseService
from app.store import store

funding_service = FundingUseService()
change_service = ChangeOrderService()
schedule_service = ConstructionScheduleService()

PROCESS = "木构件修补"


# ── 辅助构造 ──

def prepare_change(process: str = PROCESS, fee: float | None = None) -> dict:
    """准备一条已签认变更签证（含排期前置），返回签证行。"""
    sched, _ = schedule_service.create_entry({
        "计划工序": process,
        "开始日期": "2026-11-01",
        "结束日期": "2026-12-01",
    })
    schedule_service.run_action(sched["id"], "发布排期")
    values = {
        "变更事项": f"{process}工艺调整",
        "变更原因": "现场勘察发现构件偏差",
        "影响工序": process,
    }
    if fee is not None:
        values["费用增减"] = fee
    change, missing = change_service.create_entry(values)
    assert missing == []
    change_service.run_action(change["id"], "提出变更")
    updated, msg = change_service.run_action(change["id"], "签认变更")
    assert updated is not None, msg
    return updated


def create_funding(
    change_id: int | None,
    amount: float,
    *,
    refund_of: int | None = None,
    project: str = "人工支出",
    category: str = "人工费",
    process: str = PROCESS,
) -> dict:
    values = {
        "支出项目": project,
        "支出类别": category,
        "预算金额": amount,
        "实际金额": amount,
        "支出日期": "2026-11-20",
        "经办人": "张工",
        "关联签证": change_id,
        "关联工序": process,
    }
    if refund_of is not None:
        values["原支出"] = refund_of
    entry, missing = funding_service.create_entry(values)
    assert missing == [], missing
    return entry


def approve(entry_id: int):
    funding_service.run_action(entry_id, "提交报销")
    return funding_service.run_action(entry_id, "核准支出")


def reject(entry_id: int):
    funding_service.run_action(entry_id, "提交报销")
    return funding_service.run_action(entry_id, "驳回支出")


def net_text() -> str:
    return ledger.totals(store)["funding"]["net_text"]


# ═══════════════════════════════════════════════════════════════════
# 1. 迁移：旧接口兼容、历史版本、净额精确到分
# ═══════════════════════════════════════════════════════════════════

def test_migration_preserves_legacy_rows_and_creates_versions():
    before = {r["id"]: dict(r) for r in store.rows("funding_use")}
    report = ledger.migrate(store)
    assert report["mode"] == "active"
    assert report["seen"] >= len(before)

    # 旧接口 GET/列表仍可查询，字段值不变且多了可追溯账簿标注
    for fid, row_before in before.items():
        row_after = funding_service.get_entry(fid)
        assert row_after is not None
        assert row_after["支出项目"] == row_before["支出项目"]
        assert row_after["实际金额"] == row_before["实际金额"]
        assert row_after["status"] == row_before["status"]
        assert ANN_LEDGER_ID in row_after
        versions = ledger.versions("funding_use", fid)
        assert versions and versions[0]["action"] == "迁移入账"
        assert versions[0]["snapshot"]["支出项目"] == row_before["支出项目"]

    # 迁移可重复执行，不产生重复分录/版本
    again = ledger.migrate(store)
    assert again["written"] == 0
    assert ledger.status()["entries"] == report["entries"]
    assert len(ledger.versions("funding_use", 1)) == 1


def test_net_amount_exact_to_cent_before_and_after_migration():
    # 切换前内存口径
    pre = ledger.store_totals(store)["funding"]["net_text"]
    ledger.migrate(store)
    post = ledger.totals(store)["funding"]["net_text"]
    assert pre == post == "37.50"
    assert to_cents("180.40") == 18040
    assert to_cents("43.25") == 4325
    assert cents_to_text(18040 - 4325) == "137.15"


# ═══════════════════════════════════════════════════════════════════
# 2. 存量重复引用同一签证：只计一次，明细可追溯
# ═══════════════════════════════════════════════════════════════════

def test_duplicate_legacy_reference_counted_once_but_traceable():
    change = prepare_change(fee=200.0)
    cid = change["id"]
    # 模拟存量：两条已核准支出引用同一张签证的同一笔款项
    f1 = create_funding(cid, 100.00, project="木构件人工(原始凭据)")
    f2 = create_funding(cid, 100.00, project="木构件人工(重复登记)")
    approve(f1["id"])
    approve(f2["id"])

    pre_net = ledger.store_totals(store)["funding"]["net_cents"]
    # 未去重前内存直接相加：seed 已核准 37.50 + 两条 100.00
    assert pre_net == to_cents(237.50)

    report = ledger.migrate(store)
    assert set(report["duplicates"]) == {f2["id"]}

    totals = ledger.totals(store)["funding"]
    # 实际款项只计一次 100.00（seed 另有 37.50 已核准，不引用该签证，不影响签证口径）
    assert totals["net_text"] == "137.50"
    dup_ids = [d["funding_id"] for d in totals["duplicate_references"]]
    assert dup_ids == [f2["id"]]

    # 原明细仍可通过旧接口追溯，并带重复原因
    f2_after = funding_service.get_entry(f2["id"])
    assert f2_after[ANN_DUPLICATE] is True
    assert f"变更签证#{cid}" in f2_after[ANN_DUP_REASON]
    # 重复项不作为有效凭据
    valid_ids = [v["funding_id"] for v in totals["valid_credentials"]]
    assert f1["id"] in valid_ids and f2["id"] not in valid_ids

    # 回滚后同口径：仍只计一次，且明细保留
    ledger.rollback("核对回滚")
    rolled_all = ledger.totals(store)
    rolled = rolled_all["funding"]
    assert rolled_all["source"] == "store"
    assert rolled["net_text"] == "137.50"
    assert funding_service.get_entry(f2["id"])[ANN_DUPLICATE] is True


# ═══════════════════════════════════════════════════════════════════
# 3. 带小数退款对应原支出；43.25 迟到退款切换后仍可旧接口查询
# ═══════════════════════════════════════════════════════════════════

def test_decimal_refund_linked_to_original_and_queryable_after_switch():
    change = prepare_change(fee=180.40)
    cid = change["id"]

    # 180.40 元已核准人工支出
    labor = create_funding(cid, 180.40, project="大木作人工费")
    approve(labor["id"])

    # 迁移切换
    ledger.migrate(store)
    assert net_text() == "217.90"  # 37.50（seed）+ 180.40

    # 切换后财务刚批准一笔迟到的 43.25 元退款，与原支出绑定
    refund = create_funding(
        cid, 43.25, refund_of=labor["id"], project="人工退款", category="退款冲减"
    )
    updated, msg = approve(refund["id"])
    assert updated is not None, msg
    assert updated["status"] == "已核准"

    # 旧接口继续查询这次核准并核对归属
    got = funding_service.get_entry(refund["id"])
    assert got["status"] == "已核准"
    assert got[ANN_REFUND_OF] == labor["id"]
    assert got[ANN_LINK_CHANGE] == cid

    totals = ledger.totals(store)
    assert totals["funding"]["net_text"] == "174.65"  # 217.90 - 43.25
    # 签证口径只计本签证：180.40 - 43.25 = 137.15
    visa = next(v for v in totals["change_orders"] if v["change_order_id"] == cid)
    assert visa["adjustment_net_text"] == "137.15"

    # 费用调整台账回写到签证，旧接口 GET 签证可见
    change_after = change_service.get_entry(cid)
    kinds = [item["kind"] for item in change_after[ANN_ADJ_LEDGER]]
    assert "核准" in kinds and "冲减" in kinds
    assert to_cents(change_after[ANN_ADJ_NET]) == to_cents("137.15")

    # 退款分录的历史版本完整：登记 -> 提交 -> 核准（冲减确认）
    actions = [v["action"] for v in ledger.versions("funding_use", refund["id"])]
    assert actions == ["登记", "提交报销", "核准支出"]


# ═══════════════════════════════════════════════════════════════════
# 4. 迁移与核准并发：冲减只确认一次
# ═══════════════════════════════════════════════════════════════════

def test_concurrent_migration_and_refund_approval_confirms_reversal_once():
    change = prepare_change(fee=180.40)
    cid = change["id"]
    labor = create_funding(cid, 180.40, project="大木作人工费")
    approve(labor["id"])
    refund = create_funding(
        cid, 43.25, refund_of=labor["id"], project="迟到退款", category="退款冲减"
    )
    funding_service.run_action(refund["id"], "提交报销")

    import threading
    start = threading.Event()

    def do_migrate():
        start.wait()
        return ledger.migrate(store)

    def do_approve():
        start.wait()
        # 让迁移先跑起来，制造真实交叠
        import time
        time.sleep(0.02)
        return funding_service.run_action(refund["id"], "核准支出")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_mig = pool.submit(do_migrate)
        f_app = pool.submit(do_approve)
        start.set()
        report = f_mig.result(timeout=15)
        app_entry, app_msg = f_app.result(timeout=15)

    assert report["mode"] == "active"
    assert app_entry is not None, app_msg
    assert app_entry["status"] == "已核准"

    # 冲减流水恰好一条
    assert ledger.count_reversals(refund["id"]) == 1
    assert ledger.reversal_already_confirmed(refund["id"]) is True

    # 再次核准（幂等窗口/重放）不能产生第二次冲减，也不能追加账簿版本
    versions_before = len(ledger.versions("funding_use", refund["id"]))
    again, _ = funding_service.run_action(refund["id"], "核准支出")
    assert again is not None
    assert ledger.count_reversals(refund["id"]) == 1
    assert len(ledger.versions("funding_use", refund["id"])) == versions_before

    totals = ledger.totals(store)
    # 37.50 seed + 180.40 - 43.25
    assert totals["funding"]["net_text"] == "174.65"
    visa = next(v for v in totals["change_orders"] if v["change_order_id"] == cid)
    assert visa["adjustment_net_text"] == "137.15"


# ═══════════════════════════════════════════════════════════════════
# 5. 回滚：冲减不消失、不重复；重新切换不重复
# ═══════════════════════════════════════════════════════════════════

def test_rollback_keeps_reversal_and_remigration_does_not_double_count():
    change = prepare_change(fee=180.40)
    cid = change["id"]
    labor = create_funding(cid, 180.40, project="大木作人工费")
    approve(labor["id"])
    refund = create_funding(cid, 43.25, refund_of=labor["id"],
                            project="人工退款", category="退款冲减")
    approve(refund["id"])
    ledger.migrate(store)

    active_net = ledger.totals(store)["funding"]["net_text"]
    assert active_net == "174.65"

    # 回滚：旧接口仍能查到这次核准与归属
    ledger.rollback("发布回滚演练")
    got = funding_service.get_entry(refund["id"])
    assert got["status"] == "已核准"
    assert got[ANN_REFUND_OF] == labor["id"]
    rolled = ledger.totals(store)
    assert rolled["source"] == "store"
    assert rolled["funding"]["net_text"] == "174.65"  # 冲减没有凭空消失，也没计两次

    # 账簿里冲减流水仍保留
    assert ledger.reversal_already_confirmed(refund["id"]) is True
    assert ledger.adjustment_ledger(cid)["net_text"] == "137.15"

    # 重新切换：断点续迁，不重复写分录/冲减
    report = ledger.migrate(store)
    assert report["mode"] == "active"
    assert report["written"] == 0
    again = ledger.totals(store)
    assert again["funding"]["net_text"] == "174.65"
    visa = next(v for v in again["change_orders"] if v["change_order_id"] == cid)
    assert visa["adjustment_net_text"] == "137.15"
    assert len(ledger.versions("funding_use", refund["id"])) == 3


# ═══════════════════════════════════════════════════════════════════
# 6. 试运行：180.40 人工支出 + 同签证冲减，并并发驳回同工序另一条支出
# ═══════════════════════════════════════════════════════════════════

def test_trial_run_180_40_with_concurrent_reject_same_process():
    change = prepare_change(fee=180.40)
    cid = change["id"]

    labor = create_funding(cid, 180.40, project="大木作人工费")
    refund = create_funding(cid, 43.25, refund_of=labor["id"],
                            project="人工退款", category="退款冲减")
    # 同工序的另一条支出，将被并发驳回
    other = create_funding(cid, 60.00, project="同工序辅料支出", category="材料费")

    ledger.migrate(store)

    import threading
    start = threading.Event()

    def approve_labor_then_refund():
        start.wait()
        approve(labor["id"])
        return approve(refund["id"])

    def reject_other():
        start.wait()
        return reject(other["id"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(approve_labor_then_refund)
        f2 = pool.submit(reject_other)
        start.set()
        refund_entry, refund_msg = f1.result(timeout=15)
        other_entry, other_msg = f2.result(timeout=15)

    assert refund_entry["status"] == "已核准", refund_msg
    assert other_entry["status"] == "已驳回", other_msg

    totals = ledger.totals(store)
    # 修缮支出侧有效凭据：seed 37.50 + 人工 180.40 - 退款 43.25；驳回项不计
    assert totals["funding"]["net_text"] == "174.65"
    valid_ids = {v["funding_id"] for v in totals["funding"]["valid_credentials"]}
    rejected_ids = {v["funding_id"] for v in totals["funding"]["rejected_credentials"]}
    assert labor["id"] in valid_ids and refund["id"] in valid_ids
    assert other["id"] in rejected_ids

    # 变更签证侧有效凭据：已签认 + 调整台账净额 137.15
    visa = next(v for v in totals["change_orders"] if v["change_order_id"] == cid)
    assert visa["valid_credential"] is True
    assert visa["adjustment_net_text"] == "137.15"

    # 台账中驳回事件留痕但金额为 0，不参与净额
    adj = ledger.adjustment_ledger(cid)
    kinds = [(i["kind"], i["amount_text"]) for i in adj["items"]]
    assert ("驳回", "0.00") in kinds
    assert ("核准", "180.40") in kinds
    assert ("冲减", "-43.25") in kinds
    assert adj["net_text"] == "137.15"

    # 回滚后两侧净额保持一致
    ledger.rollback("试运行回滚")
    rb = ledger.totals(store)
    assert rb["funding"]["net_text"] == "174.65"
    rb_visa = next(v for v in rb["change_orders"] if v["change_order_id"] == cid)
    assert rb_visa["adjustment_net_text"] == "137.15"


# ═══════════════════════════════════════════════════════════════════
# 7. 旧接口契约：动作接口返回 {ok,message,entry}，状态流转不变
# ═══════════════════════════════════════════════════════════════════

def test_legacy_action_contract_unchanged_after_switch():
    change = prepare_change(fee=50.0)
    entry = create_funding(change["id"], 50.0, project="检测费")
    ledger.migrate(store)

    e1, m1 = funding_service.run_action(entry["id"], "提交报销")
    assert e1["status"] == "已提交" and "提交报销" in m1
    e2, m2 = funding_service.run_action(entry["id"], "核准支出")
    assert e2["status"] == "已核准" and "核准支出" in m2

    # 列表接口分页结构不变
    items, total = funding_service.list_entries(page=1, size=5)
    assert len(items) <= 5 and total >= 1

    # 签证侧签认历史可追溯
    versions = ledger.versions("change_order", change["id"])
    actions = [v["action"] for v in versions]
    assert "登记" in actions and "签认变更" in actions


def test_rejected_funding_is_invalid_credential_and_visible_in_ledger():
    change = prepare_change(fee=80.0)
    entry = create_funding(change["id"], 80.0, project="待驳回支出")
    ledger.migrate(store)
    updated, msg = reject(entry["id"])
    assert updated["status"] == "已驳回", msg

    totals = ledger.totals(store)
    rejected = {v["funding_id"] for v in totals["funding"]["rejected_credentials"]}
    assert entry["id"] in rejected
    valid = {v["funding_id"] for v in totals["funding"]["valid_credentials"]}
    assert entry["id"] not in valid
    # 驳回事件进入签证台账，净额不变
    adj = ledger.adjustment_ledger(change["id"])
    assert any(i["kind"] == "驳回" for i in adj["items"])
    assert adj["net_text"] == "0.00"
