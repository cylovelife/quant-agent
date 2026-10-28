# quant-agent 工程边界

- CLI 真理源：`src/quant_agent/entrypoints/cli.py`；`run.py`保留源码工作区启动兼容。配置真理源：`config.json`、`params/params.json`；RSI 方向见
  `docs/design_selfloop_v1.md`（其中历史结果不代表当前验证结果）。
- 源码按 `src/quant_agent/` 的领域、应用、基础设施及入口分包；测试保留在 `tests/`。行情与财务复用 `src/quant_agent/infrastructure/persistence/store.py`
  和已有 adapter；这是个人本地 SQLite，不是公司业务数据库。
- `qfq` 行情供短线评分，`raw` 供估值分位，二者不可互转或混用。没有复权因子时
  不生成后复权数据；前复权历史会修订，不得仅追加末根。
- `state/`、`reports/`、`logs/` 为本地运行产物，不提交。测试用临时目录/独立库，
  不覆盖真实账本、参数、推荐与历史证据。
- 新研究结果仅供观察；既有 `evaluate.update_weights` 门禁继续生效。
  描述性历史 IC、同一快照重复运行均不能作为新样本或生产准入依据。
- 基础验证：`python3 tests/offline_store.py`、`python3 tests/offline_datasources.py`、
  `python3 tests/offline_long.py`、`python3 tests/offline_ml.py`。
  `offline_e2e.py` 和 `verify_phase_cache.py` 需要先有行情缓存，首次可能联网，
  不能当作无条件纯离线测试。
  `python3 tests/offline_e2e.py --synthetic` 是纯构造行情的例外入口，不联网、不读真实推荐；
  其通过仅代表集成行为，不代表真实行情或策略收益已验证。
- `python3 tests/verify_phase_cache.py --synthetic` 使用构造行情、临时缓存和固定历史日期
  验证三个重阶段的重算／首次缓存／缓存命中等价性；不读真实行情缓存或联网。
  默认缓存验证仍需有效日期边界及行情缓存，缺边界时返回不可验证，不能记为通过。
- 本轮新增能力的独立验证入口与实际运行结果见
  `docs/research/free_stockdb_adoption.md`。交付区分离线回归、真实联网采集、
  本地历史研究和长期样本外验证。
- 确认跟踪的语义真理源为 `src/quant_agent/application/research/tracking.py` 和
  `docs/research/confirmation_tracking.md`；固定原观察条件、登记后核验、不虚构历史排名。
  验证：`python3 -m pytest -q tests/test_research_tracking.py tests/test_local_research.py`。
- 数据准入与本地版本语义见 `src/quant_agent/domain/market/quality.py`、`docs/research/data_contract.md`；
  快照完整性不代表完整历史时点覆盖。验证：`python3 -m pytest -q tests/test_data_contract.py`。
- 单笔raw订单模拟见 `src/quant_agent/domain/trading/execution.py`、`docs/research/order_execution.md`；
  `run.py --simulate-order REQUEST_JSON`只读本地库并返回成交提案，不写现金／持仓账本。
  验证：`python3 -m pytest -q tests/test_execution.py`。
- 现金／数量／费用事件账本见 `src/quant_agent/application/paper/ledger.py`、`docs/research/paper_ledger.md`。
  使用 `store.paper_connection` 的独立SQLite事务，不走行情缓存的吞错／关闭降级；
  不把旧研究持仓推断为数量，不自动初始化正式账户。
  验证：`python3 -m pytest -q tests/test_paper_ledger.py`。

- 公司行动、外部资金流与收益口径见 `src/quant_agent/application/paper/actions.py`、
  `docs/research/corporate_actions.md`；公司行动覆盖未核实时不得计算总回报，
  旧账本只读，升级须在新副本完成。验证：`python3 -m pytest -q tests/test_corporate_actions.py`。

- 账户初始化入口与不可重复注资语义见 `docs/research/paper_account_initialization.md`；
  验证：`python3 -m pytest -q tests/test_account_initialization.py`。

- 逐日结算与日终不可变口径见 `src/quant_agent/application/paper/daily.py`、`docs/research/paper_daily_settlement.md`；
  日历和公司行动覆盖未核实时不可将单位净值当完整总回报。
  验证：`python3 -m pytest -q tests/test_paper_daily.py`。

- 前瞻虚拟试验与同资金流现金基准见 `src/quant_agent/application/paper/experiment.py`、
  `docs/research/paper_experiment.md`；冻结代码漂移应暂停，不回填过去的前瞻记录。
  验证：`python3 -m pytest -q tests/test_paper_experiment.py`。

- 日期隔离和标签结束时点见 `src/quant_agent/domain/research/validation.py`、
  `docs/research/temporal_validation.md`；缺固定日期边界或实际结束日不可作为封存测试。
  验证：`python3 -m pytest -q tests/test_temporal_validation.py`。

- 试验代码版本衔接见 `src/quant_agent/application/paper/runtime.py`、`docs/research/paper_runtime_handoff.md`；
  只在全新副本追加运行版本，原登记、本金和继承历史不改，缺未结算日／代码再次漂移须暂停。
  首版仅支持单实验、策略与现金基准两账户的独立账本。
  验证：`python3 -m pytest -q tests/test_paper_runtime.py`。
- ETF前复权备用来源见 `src/quant_agent/infrastructure/market_data/datasources/etf_qfq_source.py`、`docs/research/etf_qfq_sources.md`；
  必须有真实前复权因子及可校验凭据，只支持已验证的现金分红／单位比例，未知因子或拆并股拒绝。
  不从raw缓存直接改名为qfq，不用推算成交额填补缺失。
  验证：`python3 -m pytest -q tests/test_etf_qfq_source.py`。
