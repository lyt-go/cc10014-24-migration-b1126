# 古建筑修缮管理平台

文保工程师、工匠和监理共同管理古建筑修缮工序，审核修缮方案，签收变更后按确认版施工。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 建筑勘查 | `building_survey` | 勘查记录 | 建筑名称、勘查部位、病害类型 |
| 修缮方案 | `restoration_plan` | 修缮方案 | 方案编号、对应勘查、修缮工艺 |
| 材料核验 | `material_verify` | 材料核验单 | 材料名称、规格型号、供应商 |
| 工匠分配 | `craftsman_assign` | 工匠分配单 | 工匠姓名、技艺类别、资质等级 |
| 工序编排 | `work_sequence` | 工序卡 | 工序名称、前置工序、所需工匠 |
| 质量检查 | `quality_inspect` | 检查记录 | 检查工序、检查项目、技术标准 |
| 历史图纸 | `historical_drawing` | 历史图纸 | 图纸编号、绘制年代、绘制内容 |
| 构件登记 | `component_registry` | 构件档案 | 构件名称、所属建筑、材质类型 |
| 环境监测 | `env_monitor` | 监测数据 | 监测位置、温度读数、湿度读数 |
| 修缮日志 | `restoration_log` | 施工日志 | 施工日期、对应工序、施工内容 |
| 验收记录 | `acceptance_record` | 验收报告 | 验收工序、验收标准、验收结论 |
| 修缮支出 | `funding_use` | 支出记录 | 支出项目、支出类别、预算金额 |
| 档案归档 | `archive_mgmt` | 修缮档案 | 档案名称、档案类别、形成日期 |
| 专家评审 | `expert_review` | 评审意见 | 评审方案、专家姓名、专业领域 |
| 安全交底 | `safety_briefing` | 交底记录 | 交底工序、危险源、防护措施 |
| 施工排期 | `construction_schedule` | 排期计划 | 计划工序、开始日期、结束日期 |
| 变更签证 | `change_order` | 变更签证单 | 变更事项、变更原因、影响工序 |
| 竣工总结 | `completion_summary` | 竣工报告 | 修缮项目、开工日期、竣工日期 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 持久化财务账簿（修缮支出 / 变更签证）

`backend/app/ledger.py` 把修缮支出（`funding_use`）与变更签证（`change_order`）的
核准费用迁移到带历史版本的 SQLite 持久化账簿，默认库文件
`backend/data/finance_ledger.sqlite3`（可用环境变量 `LEDGER_PATH` 覆盖，
`LEDGER_AUTO_MIGRATE=0` 关闭首次访问自动迁移）。

- **旧接口不变**：`/api/funding_use`、`/api/change_order` 的登记、提交、核准、驳回、
  签认、查询契约保持原样；新增字段（`关联签证`、`原支出`、`关联工序`、`凭据类型`、
  `费用调整台账`、`费用调整净额`）均为可选项。
- **历史版本**：每条分录在 `ledger_entry_versions` 留全量版本（登记/提交/核准/驳回/冲减），
  金额以整数分存储、Decimal 换算，净额持久化前后精确到分。
- **重复引用去重**：存量中重复引用同一张签证同一笔款项的记录，最早一条计为实际款项，
  其余标注 `重复引用` 与原因、保留可追溯明细，不重复计入净额。
- **退款冲减**：退款（负金额/退款类别/绑定 `原支出`）与原支出对应，核准即确认冲减；
  迁移与核准并发交叠时，凭流水唯一键 `approve:funding:<id>` 只确认一次，重放也不重复。
- **费用调整台账**：核准、冲减、驳回结果回写到对应变更签证的 `费用调整台账` 字段，
  旧接口 GET 签证即可核对累计净额。
- **迁移与回滚**：`POST /api/finance/ledger/migrate` 可重复执行（断点续迁、不重复入账）；
  `POST /api/finance/ledger/rollback` 切回内存读源，账簿数据与冲减流水全部保留，
  回滚后冲减不凭空消失、也不重复计。

新增的账簿管理/核对接口（前缀 `/api/finance`，注册在通用 `/api/{module}` 之前）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/finance/ledger/status` | 账簿模式、分录/版本/流水用量、迁移历史 |
| POST | `/api/finance/ledger/migrate` | 显式迁移（幂等，可在回滚后重新切换） |
| POST | `/api/finance/ledger/rollback` | 回滚迁移，保留账簿数据 |
| GET | `/api/finance/totals` | 有效凭据、重复引用、退款冲减与净额 |
| GET | `/api/finance/{module}/{id}/versions` | 某条支出/签证的历史版本 |
| GET | `/api/finance/change-orders/{id}/adjustments` | 签证费用调整台账与累计净额 |

相关测试：`backend/tests/test_finance_ledger.py`（去重、小数退款、并发冲减幂等、
回滚不丢不重、180.40 元人工支出 + 43.25 元退款试运行）。

领域：应用开发
子方向：web前端/全栈开发
