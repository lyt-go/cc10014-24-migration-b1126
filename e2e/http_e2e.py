"""HTTP 端到端核对：通过旧接口完成 180.40 人工支出 + 43.25 退款 + 并发驳回。

仅用标准库 urllib，脚本放在独立目录，用 -I 运行并显式加入后端代码路径。
"""
from __future__ import annotations

import json
import os
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('E2E_PORT', '8011')}/api"
PROCESS = "木构件修补"


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def action(module: str, entry_id: int, act: str) -> dict:
    return call("POST", f"/{module}/{entry_id}/actions", {"action": act})


def create(module: str, values: dict) -> dict:
    return call("POST", f"/{module}", {"values": values})


def main() -> None:
    # 0. 初始：自动迁移应在首次访问财务模块时完成
    status = call("GET", "/finance/ledger/status")
    print("初始账簿模式:", status["mode"])
    call("GET", "/funding_use")  # 触发自动迁移
    status = call("GET", "/finance/ledger/status")
    print("访问后账簿模式:", status["mode"], "分录数:", status["entries"])
    assert status["mode"] == "active"

    # 1. 排期 + 变更签证（旧接口签认）
    sched = create("construction_schedule", {
        "计划工序": PROCESS, "开始日期": "2026-11-01", "结束日期": "2026-12-01"})
    action("construction_schedule", sched["entry"]["id"], "发布排期")
    change = create("change_order", {
        "变更事项": "木构件工艺调整", "变更原因": "现场偏差",
        "影响工序": PROCESS, "费用增减": 180.40})
    cid = change["entry"]["id"]
    action("change_order", cid, "提出变更")
    signed = action("change_order", cid, "签认变更")
    assert signed["entry"]["status"] == "已签认"
    print("签证签认:", signed["entry"]["status"])

    # 2. 180.40 人工支出，走旧接口提交/核准
    labor = create("funding_use", {
        "支出项目": "大木作人工费", "支出类别": "人工费",
        "预算金额": 180.40, "实际金额": 180.40, "支出日期": "2026-11-20",
        "经办人": "张工", "关联签证": cid, "关联工序": PROCESS})
    lid = labor["entry"]["id"]
    action("funding_use", lid, "提交报销")
    appr = action("funding_use", lid, "核准支出")
    assert appr["entry"]["status"] == "已核准"
    print("人工支出核准:", appr["entry"]["status"], appr["entry"]["实际金额"])

    # 3. 迟到的 43.25 元退款，绑定原支出
    refund = create("funding_use", {
        "支出项目": "人工退款", "支出类别": "退款冲减",
        "预算金额": 43.25, "实际金额": 43.25, "支出日期": "2026-11-21",
        "经办人": "财务", "关联签证": cid, "原支出": lid, "关联工序": PROCESS})
    rid = refund["entry"]["id"]
    action("funding_use", rid, "提交报销")
    rappr = action("funding_use", rid, "核准支出")
    assert rappr["entry"]["status"] == "已核准"
    print("退款核准:", rappr["entry"]["status"], rappr["entry"]["实际金额"])

    # 4. 另一条同工序支出，旧接口驳回
    other = create("funding_use", {
        "支出项目": "同工序辅料", "支出类别": "材料费",
        "预算金额": 60.00, "实际金额": 60.00, "支出日期": "2026-11-21",
        "经办人": "李工", "关联签证": cid, "关联工序": PROCESS})
    oid = other["entry"]["id"]
    action("funding_use", oid, "提交报销")
    rej = action("funding_use", oid, "驳回支出")
    assert rej["entry"]["status"] == "已驳回"
    print("辅料支出驳回:", rej["entry"]["status"])

    # 5. 旧接口查询这次退款核准并核对归属
    got = call("GET", f"/funding_use/{rid}")
    assert got["status"] == "已核准" and got["原支出"] == lid and got["关联签证"] == cid
    print("旧接口退款核对: 状态=%s 原支出=%s 签证=%s" % (
        got["status"], got["原支出"], got["关联签证"]))

    # 6. 净额与有效凭据
    totals = call("GET", "/finance/totals")
    print("修缮支出来源:", totals["source"], "净额:", totals["funding"]["net_text"])
    visa = next(v for v in totals["change_orders"] if v["change_order_id"] == cid)
    print("签证净额:", visa["adjustment_net_text"], "有效凭据:", visa["valid_credential"])
    assert visa["adjustment_net_text"] == "137.15"

    # 7. 签证费用调整台账（回写到旧接口）
    change_got = call("GET", f"/change_order/{cid}")
    ledger_items = change_got["费用调整台账"]
    print("签证台账事件:", [(i["kind"], i["amount_text"]) for i in ledger_items])
    assert change_got["费用调整净额"] == 137.15

    # 8. 历史版本
    versions = call("GET", f"/finance/funding_use/{rid}/versions")["versions"]
    print("退款版本历史:", [(v["version"], v["action"], v["status_to"]) for v in versions])
    assert [v["action"] for v in versions] == ["登记", "提交报销", "核准支出"]

    # 9. 回滚后旧接口仍可查、净额一致、冲减保留
    call("POST", "/finance/ledger/rollback", {"reason": "发布回滚演练"})
    rb_totals = call("GET", "/finance/totals")
    rb_refund = call("GET", f"/funding_use/{rid}")
    print("回滚后来源:", rb_totals["source"], "净额:", rb_totals["funding"]["net_text"],
          "退款状态:", rb_refund["status"])
    assert rb_totals["source"] == "store"
    assert rb_refund["status"] == "已核准"
    rb_visa = next(v for v in rb_totals["change_orders"] if v["change_order_id"] == cid)
    assert rb_visa["adjustment_net_text"] == "137.15"

    # 10. 重新切换不重复
    remig = call("POST", "/finance/ledger/migrate")
    re_totals = call("GET", "/finance/totals")
    re_visa = next(v for v in re_totals["change_orders"] if v["change_order_id"] == cid)
    print("重新切换: 模式=%s 新写入=%s 签证净额=%s" % (
        remig["mode"], remig["written"], re_visa["adjustment_net_text"]))
    assert remig["mode"] == "active" and remig["written"] == 0
    assert re_visa["adjustment_net_text"] == "137.15"

    print("\n全部 HTTP 端到端核对通过 ✔")


if __name__ == "__main__":
    main()
