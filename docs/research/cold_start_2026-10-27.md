# 2026-10-27 虚拟账户冷启动

2026-10-27 10:17:18（北京时间）登记，虚拟本金人民币 50,000 元，策略账户与同本金现金对照分别空仓开立，初始现金均为 50,000 元。两账户是对照，不是两笔投资。账本位于 `state/paper/cold-start-20261027/experiment.db`，启动请求和逐日请求同目录；它们是本地运行数据，不提交。启动日收盘后才能结算，不能提前补造曲线。

## 选择及风险假设

选择华泰柏瑞沪深 300 ETF（510300）：它是项目现有观察池和比较基准中的宽基 ETF；[上交所基金资料](https://www.sse.com.cn/disclosure/fund/etflist/detail.shtml?etfClass=03&fundid=510300&type=008)可核验标的，[指数编制方案](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-04-29/510320_20260429_FYDY.pdf)说明指数覆盖沪深市场规模和流动性较大的股票。10 月 26 日未复权收盘价为 4.179 元（[历史行情](https://cn.investing.com/etfs/huataipinebridge-csi-300-historical-data)，[另一行情页面](https://content5.etninfo.com/content/cyuwebV2/asharecontent/sc/ashare_quote.php?code=510300)）；腾讯原始日线联网验证同价。较大的成交额和宽基覆盖支持其作为可观察的冷启动样本，不足以证明它收益最优或优于其他宽基。

为限制单一权益敞口，先按约 50% 本金确定固定买入数量：`floor(25000 / 4.179 / 100) * 100 = 5900` 份。按该参考价计算名义金额 24,656.10 元，实际次日价格、模拟滑点和费用会改变支出。冻结单笔总支出上限 30,000 元、最低现金储备 20,000 元；这两个值是本试验的风险假设，不是公网能证明的最优参数。上限或储备不足则拒绝／回滚，不改变数量追单。价格波动仍可能导致亏损。

依据[上交所休市安排](https://www.sse.com.cn/disclosure/dealinstruc/closed/)，2026-10-27、28 不在公布的休市日期中；登记首个可交易日为 10 月 28 日。该版本只支持一次固定数量开盘买入后持有，与持续纯现金基准比较；不会自动切换多个策略。模拟佣金 0.01%、不利滑点 5 个基点并按报价档位处理，属于固定模拟假设。

## 行情和结算

2026-10-27 全仓审查后曾因源码漂移暂停，见[审查记录](code_review_2026-10-27.md)。
同日11:01:01已完成[运行版本衔接](paper_runtime_handoff.md)：原 `experiment.db` 保持完整，
新活动账本为同目录 `experiment-runtime-v2.db`，活动指针为 `active.json`。
原始登记、启动请求、本金、策略、费用、风险和来源均未更改，新增不可变运行版本记录自10月27日生效。
代码与Python指纹匹配，运行状态已恢复为 `active/ready`。
两账户现金仍分别为50,000元、空仓、零订单，尚无日终记录；盘中推进已实际验证会被16:00门禁拒绝。

当日盘中不能把尚未完成的日线当收盘价。已在隔离临时数据库联网验证腾讯代理返回的 `day` 未复权行情，来源标识为 `tencent_proxy_ifzq_raw`；本地正式行情库启动时尚无 510300 raw 数据。用于结算前需在收盘后重新采集，并独立核实交易日、停牌、涨跌限制、开盘可成交容量及公司行动。日成交量不能作为开盘可成交容量；公司行动覆盖未核实时，总回报不可计算。不得用 qfq 或旧价替代 raw。

当日16:00后，使用新活动请求手动推进及查询：

```bash
.venv/bin/python run.py --advance-paper-experiment state/paper/cold-start-20261027/advance-active-20261027.json
.venv/bin/python run.py --compare-paper-experiment state/paper/cold-start-20261027/compare-active.json
```

明日收盘后可显式刷新未复权行情：

```bash
.venv/bin/python -c 'import sys; sys.path.insert(0,"src"); import fundamentals; print(fundamentals.fetch_kline_raw("510300","etf",limit=30,ttl_days=0).tail())'
```

之后仍需提供有来源和时点的开盘交易事实及公司行动记录，才能构造 10 月 28 日结算请求；数据不足应停止，不伪造成交。当前没有自动定时运行，登记本身不代表已产生收益或任何实盘交易。
