# 任务4：单笔虚拟订单与成交模拟

2026-10-26。实现与离线验证完成，待本步意见。当前交付为**不写账本的单笔成交提案**：
把事前订单、证券规则、raw开盘价、交易状态和临时资金／持仓快照，解析为模拟成交或明确的未成交原因。
资金事件落账、并发／重放幂等与账户每日净值分别属于任务5–7，本轮没有初始化账户。

## 使用入口与源码

```bash
.venv/bin/python run.py --simulate-order /absolute/path/request.json
```

该分支在普通跑盘导入、运行配置和日志初始化前退出，只读指定的本地SQLite，stdout只输出JSON。
相对数据库／成本文件路径相对于请求文件目录解释；缺raw目标日行情时返回待核验，不联网补数，也不读取qfq替代。
合法的成交、拒单、未成交和待核验结果都返回进程状态0；输入／文件／数据库错误返回2，错误JSON写stderr。
不能与其他操作选项混用。无需提供券商账户或真实成交。

- `src/execution.py`：不可变订单、证券、目标日交易事实和资金／单证券持仓快照；统一成交函数 `match_order`。
- `src/execution_data.py`：复用 `LocalData` 的只读raw分区，只取目标日开盘及其获取元数据，拒绝周／月聚合行情。
- `src/backtest.py::replay_paper_order`：对显式历史订单预演同一成交函数。
- `src/portfolio.py::preview_paper_order`：对显式订单和当前快照预演同一函数，不修改原持仓、交易文件或现金。
- `params/execution_v1.json`：成本与基础规则版本 `paper_open_v1`，有效起点2026-07-06，核验日2026-10-26。
  内容指纹 `policy_id` 包括参数、来源和假设；调整成本会改变指纹。

原 `backtest_symbol`、v2标签／持仓流程继续表示未扣费的qfq价格研究。
新成交版本为 `raw_next_open_ioc_v1`，不能把原v2记录改名充当本版数量成交或扣费收益。
原收盘止损标签不是已提前提交的收盘订单；未来账户如在收盘才触发退出，应形成下一交易日开盘订单，不能知道收盘后再按该收盘成交。

## 输入与边界

以下金额及代码只是可复现构造示例，没有选择投资标的或确认虚拟本金。请求结构为：

```json
{
  "db_path": "/absolute/path/fixture.db",
  "order": {
    "order_id": "fixture-buy-1",
    "market": "cn", "code": "600000", "side": "buy", "quantity": 100,
    "signal_date": "2026-10-26", "execution_date": "2026-10-27",
    "limit_price": null
  },
  "instrument": {"market": "cn", "code": "600000", "exchange": "SSE", "profile": "cn_mainboard"},
  "session": {
    "date": "2026-10-27", "source": "fixture", "known_at": "2026-10-27@open",
    "trading_status": "trading", "instrument_status": "regular",
    "lower_limit": "9", "upper_limit": "11",
    "opening_capacity": 10000, "capacity_kind": "assumed",
    "previous_session": "2026-10-26", "market": "cn", "code": "600000"
  },
  "account": {
    "date": "2026-10-27", "market": "cn", "code": "600000",
    "available_cash": "50000", "lots": []
  }
}
```

`policy_path`为可选字段，未填时只读仓库上述成本文件。实际请求须有对应raw行情；完整构造数据库／CLI案例见测试。
金额输出为十进制字符串；不使用二进制浮点扣款。持仓批次形如 `{"quantity": 100, "acquired_date": "2026-10-26"}`。
资金快照是单笔预演的可用现金，`lots`只能属于同一市场／代码，跨证券数量不可借用。
多个订单不可各自重复使用同一现金快照并声称账户可负担；后续任务5负责顺序占款、扣款和落账。

订单必须在信号日之后的目标日开盘执行；`previous_session`须与原信号日一致。
这是调用方提供并注明出处的相邻交易日事实，**不是本步补齐了官方日历**。
目标日缺数据不顺延到后一天；停止、假日和未知原因不会通过延长窗口变成成交。
`known_at`只接受该日 `@preopen`／`@open`阶段，不能填收盘后才知道的交易状态。
这些是模型阶段标记，不是虚构的实际采集时间；行情 `fetched_at`保留真实取得时刻，历史晚抓数据不伪装成当时已送达。

已支持沪深主板普通A股，以及**调用方已确认类别的境内股票ETF**两种CNY、多头、不融资配置。
主板代码／交易所不符会拒绝；ETF代码前缀只做基础身份筛查，不作为ETF细分类证明。
ST、退市整理、IPO无涨跌幅期、科创板／创业板及其他特殊证券未接入本配置；债券、黄金、跨境、货币ETF、海外与场外基金不能套用股票ETF的T+1假设。
证券实际分类、当日状态、价格限制和开盘流动性尚未自动采集：调用方必须提供有来源的明确事实，未知时不成交。
代码属于研究池或raw分区，不等于证券类型及交易权限已核验。

## 规则与成本

| 项目 | 主板A股配置 | 境内股票ETF配置 | 性质 |
|---|---|---|---|
| 买入单位 | 100股整数倍 | 100份整数倍 | 交易规则 |
| 报价最小变动 | 0.01元 | 0.001元 | 交易规则 |
| 本模型单笔数量上限 | 100万 | 100万 | 模型范围上限；不宣称穷尽各市场特殊额度 |
| 可卖数量 | 仅更早日期已买入批次 | 仅更早日期已买入批次 | 首期T+1；不支持T+0品种 |
| 佣金 | 万3，最低5元 | 万1，最低0元 | **试验假设，非用户券商费率**；包含交易所／监管规费 |
| 卖出印花税 | 0.05%，买入不收 | 0 | 税制依据见下 |
| 过户费 | 双向0.001% | 0 | 普通股票交易与ETF份额二级市场交易口径 |
| 滑点 | 双向不利5bps | 双向不利5bps | **试验假设**，再按证券tick向不利方向取整 |

交易单位、报价与不能当日回转的基础依据为[沪市2026规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/fund/trading/c/c_20260424_10817739.shtml)和
[深市2026规则](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)
3.1.4–3.1.5、3.3.8–3.3.13等条款；深市列举可回转品种，首期普通股票／境内股票ETF未套用这些例外。
沪市公告正文说明2026-07-06起施行，但网页时效性标签仍显示“尚未施行”；本模型不采用另行通知实施的特殊业务条款。
ETF100份、0.001元和不收印花税另有[深交所投教说明](https://investor.szse.cn/warning/activities/fundIntroduction/t20210104_584079.html)支持。

[印花税法第三条及税率表](https://fgk.chinatax.gov.cn/zcfgk/c100009/c5193058/content.html)定义股票／存托凭证交易及卖方征收，
结合[财政部、税务总局2023年第39号公告](https://m.mof.gov.cn/czxw/202308/t20230827_3904226.htm)的减半征收，配置股票卖出0.05%。
[上交所股票投资费用说明](https://one.sse.com.cn/onething/gptz/)提供股票双向0.01‰过户费及佣金包含规费的依据。
费用说明页面部分旧税率须与后续财政部减半公告合读，不直接照搬旧1‰。
[上交所现行收费表](https://www.sse.com.cn/services/tradingservice/charge/ssecharge/)列出基金经手费；本配置将其包含在模拟佣金中，不再重复加收。
本轮没有核验任何用户特定券商合同，佣金最低额及逐项四舍五入方式均为明确的模拟假设。

买入 `open×(1+5/10000)` 后向上取tick；卖出相反，向下取tick。
滑点已经体现在成交价，`slippage_cost`只披露影响，不能再次从现金扣除。
成交金额、佣金、印花税、过户费各自四舍五入到分，最后求费用和；低额费用可能舍入到0。
政策有效日前的数据不套本费率；更早历史研究必须登记对应时期的规则和成本版本。

## 成交、未成交与不变量

- 只检查目标开盘及事前声明的状态，不使用当天收盘、最高／最低或全天成交量决定开盘成交。
  在测试中扰动这些字段和后续日线，开盘提案逐值不变。
- 涨跌停价由调用方提供，不从前复权价或未经核验的昨收机械推导；这样不会在除权、特殊幅度日猜限制价。
  本模型在开盘涨停买入／跌停卖出时，因队列未知而保守不成交；不是断言市场绝无成交可能。
- 容量必须显式给出，注明 `observed`或`assumed`。假设容量只是冻结的模型参数，不是实际盘口。
  数量超过开盘容量整笔未成交，不做部分成交；不借全日成交量假设开盘能买到。
- 不利滑点越过限制价时未成交，不强行压回涨跌停价；限价买入不得高于限价、卖出不得低于限价。
- 买入不能因资金不足静默减量；实际开盘跳空后，须重新比较全部成交额及费用与可用现金。
- 卖出不能超过已可卖数量，不能使用当日新买入数量；零股余量必须一次带走其余数，不能任意拆零。
- `filled`才产生数量与现金变化提案；`rejected`／`unfilled`／`unavailable`均为零成交、零费用、零资金／数量变化。
- 输入对象不可变，相同输入结果一致；这不是持久化幂等或抗崩溃对账证明，后者由任务5负责。
- 无论结果如何，均不写真实或虚拟持仓、交易文件、参数与账户现金；用户运行预演不等于启动投资试验。

构造核对：raw开盘10元买100股，滑点取整成交10.01元，成交额1001.00元，佣金5.00元、过户费0.01元，
现金提案 **-1006.01元**。可用现金1006.01元可成交，1006.00元即拒绝；不借入1分钱。
同开盘卖出此前100股时，成交9.99元，含佣金5.00、印花税0.50、过户费0.01，现金提案 **+993.49元**。
ETF开盘3.456元买100份，成交3.458元，含假设佣金0.03元，现金提案 **-345.83元**。
这些只是工程算例，不能作为策略收益或五万元账户已经运行的证据。

## 验证与路由

```bash
.venv/bin/python -m pytest -q tests/test_execution.py tests/test_data_contract.py tests/test_trade_semantics.py tests/test_indicator_backtest_boundaries.py tests/test_local_research.py tests/test_research_tracking.py
.venv/bin/python tests/offline_store.py
.venv/bin/python tests/offline_datasources.py
.venv/bin/python tests/offline_long.py
.venv/bin/python tests/offline_ml.py
.venv/bin/python tests/offline_e2e.py --synthetic
```

本轮组合回归 **175 passed**，其中订单模拟 **44项**；四个基础离线自检通过。
纯构造行情集成自检通过，包含交易标签／数据契约／订单模拟 **107项**子回归。
所有订单及行情来自构造输入或独立临时SQLite，CLI子进程核对读取raw而不是qfq、数据库字节不变、缺raw不回退。
也覆盖不同证券身份、同日与未来持仓、涨跌停、容量、现金差1分钱、费用、跳空、日期、未来数据扰动和旧研究路径兼容。
初版CLI复现了普通主流程计时日志污染JSON的问题，已提前分流并验证纯JSON输出。

新增模块与订单测试Ruff检查通过；已改Python文件对照任务4开始时内容／Git HEAD无新增诊断。
保留既有 `run.py` E402／E731、`fundamentals.py` F401、`report.py` F841、`offline_e2e.py` E741。
源码语法、文档链接、配置及既有文件保护、`git diff --check`均通过。
未运行联网行情采集、默认缓存E2E或完整 `verify_phase_cache.py`，未操作现存账本，未提交／推送。

本步主流程 `executing-plans`，测试 `test-driven-development`及其测试反模式引用，收尾 `verification-before-completion`。
已读取 `skills-index.md`、项目 `AGENTS.md`、`engineering-workflow.md`、`python/code-style.md`、
`python/data-runtime.md`、`shared-code-change-safety.md`、`anti-hallucination.md`。
Harness原有工程边界可用，只增加本步真理源及验证入口。没有增加依赖或委派子Agent。
本步是工程实现和针对规则的原文核验，不是投资分析；未加载深度调研、Agent应用、公司数据库或Git交付流程。
