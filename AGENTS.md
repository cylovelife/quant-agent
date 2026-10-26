# quant-agent 工程边界

- 入口与配置真理源：`run.py`、`config.json`、`params/params.json`；RSI 方向见
  `docs/design_selfloop_v1.md`（其中历史结果不代表当前验证结果）。
- 延续 `src/` 平铺 Python 模块和 `tests/` 的现有布局。行情与财务复用 `store.py`
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
- 本轮新增能力的独立验证入口与实际运行结果见
  `docs/research/free_stockdb_adoption.md`。交付区分离线回归、真实联网采集、
  本地历史研究和长期样本外验证。
- 确认跟踪的语义真理源为 `src/research_tracking.py` 和
  `docs/research/confirmation_tracking.md`；固定原观察条件、登记后核验、不虚构历史排名。
  验证：`python3 -m pytest -q tests/test_research_tracking.py tests/test_local_research.py`。
