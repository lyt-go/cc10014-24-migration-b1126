"""真实 HTTP 并发验证：迁移与退款核准交叠时冲减只确认一次。

通过运行中的 uvicorn 服务发起并发请求，覆盖网络/线程层面的竞态。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('E2E_PORT', '8011')}/api"
PROCESS = "木构件修补并发"


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def action(module: str, eid: int, act: str) -> dict:
    return call("POST", f"/{module}/{eid}/actions", {"action": act})


def create(module: str, values: dict) -> dict:
    return call("POST", f"/{module}", {"values": values})


def main() -> None:
    # 先回滚，制造“迁移尚未切换”的起点（数据保留）
    call("POST", "/finance/ledger/rollback", {"reason": "并发演练起点"})

    sched = create("construction_schedule", {
        "计划工序": PROCESS, "开始日期": "2026-11-01", "结束日期": "2026-12-01"})
    action("construction_schedule", sched["entry"]["id"], "发布排期")
    change = create("change_order", {
        "变更事项": "并发工艺调整", "变更原因": "偏差",
        "影响工序": PROCESS, "费用增减": 180.40})
    cid = change["entry"]["id"]
    action("change_order", cid, "提出变更")
    action("change_order", cid, "签认变更")

    labor = create("funding_use", {
        "支出项目": "并发人工费", "支出类别": "人工费",
        "预算金额": 180.40, "实际金额": 180.40, "支出日期": "2026-11-20",
        "经办人": "张工", "关联签证": cid, "关联工序": PROCESS})
    lid = labor["entry"]["id"]
    action("funding_use", lid, "提交报销")
    action("funding_use", lid, "核准支出")

    refund = create("funding_use", {
        "支出项目": "并发退款", "支出类别": "退款冲减",
        "预算金额": 43.25, "实际金额": 43.25, "支出日期": "2026-11-21",
        "经办人": "财务", "关联签证": cid, "原支出": lid, "关联工序": PROCESS})
    rid = refund["entry"]["id"]
    action("funding_use", rid, "提交报销")

    # 并发：显式迁移 vs 退款核准，再叠加两次重复核准重放
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = [
            pool.submit(call, "POST", "/finance/ledger/migrate"),
            pool.submit(action, "funding_use", rid, "核准支出"),
            pool.submit(action, "funding_use", rid, "核准支出"),
            pool.submit(action, "funding_use", rid, "核准支出"),
        ]
        results = [f.result() for f in futs]

    print("并发结果条数:", len(results))
    got = call("GET", f"/funding_use/{rid}")
    assert got["status"] == "已核准"

    totals = call("GET", "/finance/totals")
    visa = next(v for v in totals["change_orders"] if v["change_order_id"] == cid)
    print("并发后签证净额:", visa["adjustment_net_text"], "总账净额:",
          totals["funding"]["net_text"])
    assert visa["adjustment_net_text"] == "137.15"

    # 冲减流水只能有一条：直接核对台账中冲减事件数
    adj = call("GET", f"/finance/change-orders/{cid}/adjustments")
    reversals = [i for i in adj["items"] if i["kind"] == "冲减"]
    approves = [i for i in adj["items"] if i["kind"] == "核准"]
    print("冲减事件数:", len(reversals), "核准事件数:", len(approves))
    assert len(reversals) == 1
    assert len(approves) == 1
    print("\n并发冲减幂等核对通过 ✔")


if __name__ == "__main__":
    main()
