# 任务5 虚拟现金数量与费用账本

2026-10-26。任务5的事件账本与离线验证已完成，待本步验收。
显式模拟订单现在可以在同一SQLite事务内形成成交、现金、费用和数量事件，并从这些事件重建余额与持仓。
本轮只使用构造账户，没有选择或初始化用户的正式虚拟账户；五万元仍是示例。
公司行动与入提款由任务6实现，正式启动与每日净值由任务7实现。

## 文件与调用边界

- `src/paper_ledger.py`：`PaperLedger`提供账户底座、订单记账、查询对账、待核验订单取消和旧账本副本预检。
- `src/store.py`：`create_paper_store`独占新建空库，`paper_connection`提供独立严格事务。
  新库以应用标识`QPL1`和结构版本1识别，不能作为行情缓存打开，也不能从行情库自动迁移。
- `src/portfolio.py::record_paper_order`：统一记账入口，调用账本持有的资金和数量。
  原`positions.json`、v2价格标签及其结算行为保持独立。
- `src/execution.py`：继续复用任务4的成交模型；成本定义随账户完整保存，后续修改外部成本文件不会改变该账户的费率。
- `src/execution_cli.py`和`run.py`：显式记账与只读查询在普通跑盘导入、配置和日志初始化前分流。
  普通跑盘仍维护研究记录，不自动向新现金账本发单。

这里是个人本地SQLite，不使用公司数据库平台。行情缓存原有的吞错、文件回退和`QUANT_STORE`关闭机制不作用于现金账本。
账本打不开、结构不支持或对账失败时向上返回错误，不能降级成空仓或重新注资。
新建库遇到既有文件即拒绝；建表失败清理本次新建文件，不覆盖旧库。

## 事件与资金关系

| 表 | 保存内容 | 写入约束 |
|---|---|---|
| `paper_accounts` | 账户标识、CNY、起始日期、本金分值、成本指纹及完整定义 | 起点不可改写；同参数重复登记不重复注资，不同参数冲突 |
| `paper_orders` | 账户内唯一订单ID、冻结意图、目标日和最新处理结果 | 账户＋订单ID唯一；证券、方向、数量、限价等意图不能借重试改变 |
| `paper_attempts` | 每次实际核验的行情／资金输入、结果和时间 | 只追加；相同输入与状态复用原核验，不重复落账 |
| `paper_events` | 初始现金、成交本金、佣金、印花税、过户费和数量变化 | 只追加；订单＋事件种类唯一，金额和数量强制整数 |

每个实际模拟成交产生五类事件：成交本金、三项费用、证券数量；零额费用也保留对应组件，便于与成本规则逐项核对。
滑点已含在成交价，不再增加一次现金扣款。未成交、拒单、待核验和取消只有处理记录，不产生经济事件。

现金以整数分保存，金额接口使用十进制字符串；不足一分、布尔值、非有限值或超出SQLite整数范围的金额均拒绝。
不会先把极长小数舍入成合法分值再接受。余额以整数事件求和，证券数量按市场和代码分别重建，禁止负现金和超卖。
卖出按FIFO消耗较早买入的批次数量，当日买入数量不参与可卖量；这用于数量对账，不代表已实现分红或税务成本核算。

每次读写都核对起始现金、冻结政策、订单意图、尝试输入指纹、费用分项、成交金额、现金前后值、数量前后值及事件归属。
任一组件缺失或多出不明事件，账本拒绝继续记账。只读状态查询把余额和事件放在同一个读事务中，避免两次读取之间发生变化。

## 顺序重试与故障恢复

记账在`BEGIN IMMEDIATE`取得SQLite写锁后，重新读取并校验最新余额与持仓，再运行任务4模型。
请求不能提供可用现金、持仓或临时费率覆盖账户。订单、处理记录及全部经济事件一起提交；中途失败全部回滚，不以日志或内存结果冒充已记账。
测试使用真实SQLite触发器在费用写入处制造错误，确认此前插入的成交本金、订单和尝试均没有残留，去除故障后原订单可成功重试一次。
这是已测的软件事务失败恢复，不是断电或存储介质故障验证。

| 情况 | 行为 |
|---|---|
| 已成交或明确拒单／未成交后重复提交 | 返回首次终态及`replayed=true`，不重新成交；新行情修订不改旧价格和现金 |
| 相同订单ID改变数量、方向、证券或限价 | 返回冲突，不动账本 |
| 尚未拿到可核验数据 | 保存`unavailable`核验，不扣款；数据恢复后原意图可重试 |
| 同一待核验输入再次运行 | 返回原核验，不新增经济事件 |
| 旧待核验订单未处理，却试图推进到更后交易日 | 返回冲突，先重试或明确取消旧订单，防止拿未来余额补造过去交易 |
| 取消待核验订单 | 保留取消原因，资金数量不变；不能取消已成交订单来伪造退款 |
| 已处理较后日期后补造较早新订单，或使用账户启动前信号 | 返回冲突；已经记账的旧订单仍可幂等查询 |

不同证券共享一个账户现金，两个订单不能各自借同一份初始余额成交。
同日多个订单按实际提交顺序串行记账；同日待核验订单恢复时，也要使用其他已处理订单之后的余额。
同证券的批次消耗按经济处理顺序重建，不按最早创建订单的ID排序。
开盘容量仍沿用任务4的单笔显式模型条件，本步没有实现盘口撮合或跨账户流动性仿真。

## 命令入口

```bash
.venv/bin/python run.py --record-order /absolute/path/record.json
.venv/bin/python run.py --ledger-status /absolute/path/status.json
```

`record.json`在任务4的订单／证券／交易事实字段之外，明确指定已经存在的账本和账户：

```json
{
  "ledger_path": "/absolute/path/fixture-paper.db",
  "account_id": "fixture",
  "db_path": "/absolute/path/fixture-prices.db",
  "order": {
    "order_id": "buy-1", "market": "cn", "code": "600000", "side": "buy", "quantity": 100,
    "signal_date": "2026-10-26", "execution_date": "2026-10-27", "limit_price": null
  },
  "instrument": {"market": "cn", "code": "600000", "exchange": "SSE", "profile": "cn_mainboard"},
  "session": {
    "date": "2026-10-27", "source": "fixture", "known_at": "2026-10-27@open",
    "trading_status": "trading", "instrument_status": "regular",
    "lower_limit": "9", "upper_limit": "11", "opening_capacity": 10000,
    "capacity_kind": "assumed", "previous_session": "2026-10-26", "market": "cn", "code": "600000"
  }
}
```

`status.json`只包含`ledger_path`和`account_id`，返回现金、批次数量、费用、待核验订单及对账事件。
相对文件路径按请求文件目录解释。合法处理结果的进程状态为0；输入、存储、冲突或对账错误为2，错误JSON写stderr。
记账请求若携带`account`或`policy_path`覆盖字段会被拒绝。
已经完成的重复订单先查询既有结果，即使原行情文件后来不可用，也不会被迫重新获取价格或重复扣款。

账本创建和初始资金的底座API为`PaperLedger.create(...)`及`create_account(...)`，本轮在临时目录中测试其一致性。
任务7a新增显式初始化入口，详见[初始化与启动登记](paper_account_initialization.md)；策略、风险及可交易范围在后续任务冻结。
`--record-order`不会创建缺失账户；`--simulate-order`继续只读预演，不向现金账本写入。

## 旧账本副本预检

`stage_legacy_book(source, target)`只把旧JSON复制到一个全新目录，逐字节检查复制前后源文件，记录文件SHA-256和预检结果。
现有研究持仓不能证明证券数量、可用现金和执行费用，预检明确返回`migration_allowed=false`，不从参考价格倒推股数或虚构资金。
这不阻止未来从空仓启动新账户；旧研究记录与新现金账户分别保留。

测试确认：源文件不变；已有目标目录不覆盖；无效JSON和复制中途失败不留下半成品目录。
本轮没有对用户现存账本执行迁移或副本操作，预检验证使用独立构造文件。

## 构造账户核对结果

初始50,000元仅为测试夹具；沿用任务4示例成本与raw开盘10元：

| 操作 | 现金余额 | 持仓数量 | 累计费用 |
|---|---:|---:|---:|
| 构造初始现金 | 50,000.00 | 0 | 0.00 |
| 模拟买100股，成交10.01元 | 48,993.99 | 100 | 5.01 |
| 当日再卖100股 | 48,993.99 | 100，拒单未变 | 5.01 |
| 下一交易日卖100股，成交9.99元 | 49,987.48 | 0 | 10.52 |

往返现金减少12.52元，包含2.00元成交价差和10.52元费用；事件求和与余额相等。
重复提交、重开进程或行情重抓不再次扣费；可用现金恰好1006.01元时，两笔各需1006.01元的订单最多成交一笔。
这些是工程行为证据，不是前瞻资金曲线或策略收益验证。

## 本轮验证与规则清单

```bash
.venv/bin/python -m pytest -q tests/test_paper_ledger.py tests/test_execution.py tests/test_data_contract.py tests/test_trade_semantics.py tests/test_indicator_backtest_boundaries.py tests/test_local_research.py tests/test_research_tracking.py
.venv/bin/python tests/offline_store.py
.venv/bin/python tests/offline_datasources.py
.venv/bin/python tests/offline_long.py
.venv/bin/python tests/offline_ml.py
.venv/bin/python tests/offline_e2e.py --synthetic
```

组合回归 **204 passed**，本步账本新增 **29项**；四个基础离线自检通过。
纯构造行情集成自检通过，包含交易标签／数据契约／订单／账本 **136项**子回归。
重点覆盖跨日买卖对账、共享现金、FIFO、同日新买不能卖、并发重复／竞争订单、意图冲突、失败回滚、
原订单恢复、取消、禁止倒填、初始资金幂等、成本冻结、金额精度、账本损坏拦截、缓存隔离和旧账本副本预检。

新增模块与测试Ruff检查通过；所有已改Python对照Git HEAD无新增诊断，保留此前5项旧诊断。
文档链接、JSON／语法、作者、`git diff --check`及受保护文件指纹核对通过。
没有运行联网采集、默认缓存E2E、完整`verify_phase_cache.py`、正式账户或前瞻验证；未提交或推送。

主流程使用`executing-plans`；回归使用`test-driven-development`及其测试反模式引用；
业务键、唯一约束和事务去重参考`idempotency-patterns`，收尾使用`verification-before-completion`。
本地文档沿用`pages:write-page`及其写作质量引用，保存于既定仓库目录，未写云端。
实际读取`skills-index.md`、项目`AGENTS.md`、`engineering-workflow.md`、`python/code-style.md`、
`python/data-runtime.md`、`shared-code-change-safety.md`、`anti-hallucination.md`及幂等技能引用的`data-consistency.md`。
SQLite事务、外键、唯一约束和只追加事件按本项目个人账本执行，不套用公司MySQL／Redis／MQ示例。
Harness原有边界可用，仅补入本步真理源与验证入口；没有增加依赖或委派子Agent。
