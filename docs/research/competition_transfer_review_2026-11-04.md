# 竞赛迁移改动：时间隔离评审与交付

日期：2026-11-04。评审基线 `4f8eb4e33f77b43aabe8b6e72bdced891e201824`，覆盖竞赛迁移全部18个改动路径（含7个新增文件），以及数据读取、完成日截断、财报时点、缓存、主报告和ML评价调用链。主Agent完整检查，并安排一个只读Agent独立检查；修复后对变更点作定向复核。

## 发现与处理

| 问题 | 影响与复现 | 修复 |
|---|---|---|
| P2：海外市场绕过完成日策略 | 主流程预测循环覆盖HK/US，而现有完成日截断和数据准入仅覆盖CN/ETF；在市场尚未完成日线时可能产生预测 | `attach_forecasts` 统一限制到现有 `SUPPORTED_MARKETS`，未验证市场不生成新预测 |
| P2：连续特征消融混入预处理增益 | 原Ridge无缺失标志，新增Ridge有；11个新增特征全为零时仍通过消融，构造数据平均IC增量约0.67643 | 两个Ridge统一训练内Pipeline；同Ridge特征比较与最优原模型比较分开，双门槛；常数新增特征用例修复后增量为0且拒绝采用 |
| P2：财务年报和三年窗口误识别（沿用旧逻辑） | “半年报”包含“年报”，会进入年报序列；四条稀疏年报可能跨六年却用三次方根计算三年增长，新连续特征继承了错误 | 年报需12月31日期末；近三年统计按日历年限制，CAGR取精确year−3端点；缺失不外推，拒绝非有限数值；缓存schema升至2 |
| 证据披露缺口：qfq快照不等于历史时点档案 | 日期截断只能保证本次快照的行时间边界，不能恢复每个历史时点当时的复权版本 | 新预测JSON与两条报告路径明确 `current_qfq_snapshot` / `point_in_time_complete=false`，保留研究状态及禁止晋级 |

五个新增用例在修复前失败（市场边界、PIT字段、缺失值混杂、缺同模型对照、财年窗口），修复后通过。再增加最优原模型保护、验证极值不改变训练预处理、同报告期修订日边界、缓存版本拒绝等覆盖。只读评审复核未发现新的阻断问题。

## 时间与泄漏检查结论

- 行情特征前缀不变，未来价格扰动不改变截止日预测；目标使用实际第h根观察结束日，无未来标签列进入特征。
- 短线训练标签结束日严格早于开发窗起点，评价标签在窗尾前成熟，最终拟合标签早于预测日。每个Pipeline只在训练集拟合。
- 长期按固定日期分组，训练/验证标签不得跨越下一段；测试标签扰动不会改变模型选择或预测。连续特征比较只读取开发验证段。
- 历史财报先要求实际公告日期早于信号日，再按报告期选择可用修订；当日新公告或修订从次日参与。同报告期未来修订不能提前影响特征。
- raw/qfq分工、数据准入和原权重晋级限制继续保留；旧schema回放缓存不能被作为新特征面板读取。

这些检查不能证明完整历史PIT覆盖：现下载财报可能已有未保留版本的修订，股票池来自当前观察池，EPS算术重建未核对历史股本，qfq可重写历史。开发窗口重复用于选择、收益标签重叠，也不构成独立确认或收益增益证明。未读取比赛封存数据、未重新选择封存区间、未执行真实交易或修改原实验账本。

## 验证证据

- 修复相关专测及时间隔离/数据契约/日常集成：`pytest -q tests/test_market_forecast.py tests/test_value_features.py tests/test_temporal_validation.py tests/test_data_contract.py tests/test_local_research.py`：116通过。
- 全量 `python -m pytest -q`：537通过，79.03秒。四项基础离线 `offline_store.py`、`offline_datasources.py`、`offline_long.py`、`offline_ml.py` 及 `offline_e2e.py --synthetic`、`verify_phase_cache.py --synthetic` 全部退出0；日志保存在本地交付回执旁。未将这些检查当作联网市场数据验证。
- Ruff静态检查通过；静态检查不替代行为测试。测试使用现有Hull虚拟环境，Python 3.11 / sklearn 1.7.2；没有安装全局依赖。
- 新测试与构造行情均使用独立临时数据；前轮真实行情快照和报告保留原版本，本轮没有把重复运行当作新市场样本。

## 路由与交付约束

- 本次实际Skill：`requesting-code-review`、`dispatching-parallel-agents`、`receiving-code-review`、`git-workflow`、`finishing-a-development-branch`、`verification-before-completion`；修复复用已加载的 `test-driven-development`、`scikit-learn` 和竞赛时间验证规范。
- 已读取规则：项目 `AGENTS.md`、`skills-index.md`、`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`shared-code-change-safety.md`、`anti-hallucination.md`。Harness核对为adequate，无需扩写工程指令。
- 未加载Java/TS/Rust规则、公司GitLab/数据库流程：实际工程与交付均为Python、个人GitHub及本地SQLite。
- 目标remote核实为 `git@github.com:cylovelife/quant-agent.git`；author/committer为个人 `cylovelife@foxmail.com`，SSH独立核验为 `cylovelife` 的个人key且 `IdentitiesOnly=yes`。
- 用户明确要求提交远程并同步主工程。项目规则未要求PR，受版本管理的仓库中没有CI工作流；采用普通非强制fast-forward推送，不重写远端历史，远端拒绝时不绕过保护。主工程原有18文件未提交成果先完整保全，再同步远端main；运行目录不参与提交。
- 同步后的SHA、干净状态、保全位置及逐文件校验写本地 `state/competition-transfer-review-20261104/delivery.json`，不把运行回执提交源码。旧paper运行版本仍需按既有衔接流程启用。
