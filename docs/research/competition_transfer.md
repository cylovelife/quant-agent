# 竞赛方法迁移到双轨主程序

日期：2026-11-04。目标是补足可解释特征、短期预测和反证能力；本次不声称策略收益或真实预测精度已经提高。

## 已核对的来源与取舍

| 来源 | 迁移的方法 | 不迁移的内容 |
|---|---|---|
| Stock Price Predictor `baseline.py` / `validation.py` | 当时可见的多尺度收益、K线形态；训练内填补/标准化；实际标签结束日 purge；复杂模型须胜过简单基线 | 自然日星期标签规则、公开榜驱动的预测互补、该数据集参数 |
| Hull `context.py` / `reversal.py` / `ablation.py` | 用历史波动标准化涨跌、短长波动比、分开评估预测与风险；分窗与组件比较 | 杠杆映射、15%波动目标、比赛匿名特征和第四名未公开 alpha |

上述两套代码的历史结果分别见 `docs/competitions/stock_price_model_feature_walkthrough_2026-10-28.md` 与 `hull_lessons_and_kaggle_plan_2026-10-29.md`。本轮不重训比赛、不解析比赛原数据、不消费 Hull `[8544,9048)`。运行时领域模块不依赖 `competitions`。

## 行为与不变量

- 短期仅使用显式 qfq 日线，以实际观察记录为时钟，不造交易日、补价格或从 raw 转 qfq。收盘后第 t 根观察产生未来 h 根收盘收益与日收益 RMS；这不是可成交收益、年化波动或总回报。
- 固定 Ridge 与零收益／过去20根 RMS 基线。三个按时间前推的开发窗，训练标签结束日严格小于窗首，评价标签结束日严格小于窗尾；所有预处理仅在训练集拟合。平均 MAE 至少改善2%、每窗最多退化5%才在研究展示中选 Ridge；收益与波动各自选择。全部仍是开发证据，`promotion_allowed=false`。
- 经验残差的10%/90%分位只作误差参考范围，不提供独立校准覆盖率或上涨概率。历史不足、数据错误和可选 sklearn 缺失给出明确状态，不伪造预测。
- 长期增加连续财务特征与质量×估值交互；保留现有规则分。未知值保留缺失；金融类现金流/负债中性分不冒充实际质量证据；负利润不能因负现金流相除而变成优质现金流。历史回放只使用真实公告日期早于信号日的财报。
- 修正原有季度累计EPS乘增长率冒充TTM的算法：改为上年全年＋本期累计－上年同期累计，缺报告时返回未知，同报告期取已提供数据中的最新公告修订。这里只是每股数值的算术重建，股本变化未校验，历史PE仍须标记为代理；不能称为精确TTM。[IAS 33](https://www.ifrs.org/issued-standards/list-of-standards/ias-33-earnings-per-share/)规定EPS以加权平均股数为分母，因此不同期间的每股口径不能自动视为可比。本轮没有将其晋级为真实估值或交易信号。
- 长期 ML 增加一个可选的 `ridge_enriched` 消融候选，旧面板缺字段时明确跳过。训练内缺失处理；验证选型与测试评价继续分离。测试不是跨实验强制封存，既有登记/晋级限制保留。
- 日常投资报告和 `--daily --offline` 使用同一短期领域实现；长期报告显示数据覆盖与复核原因。旧参数、旧实验和账本不自动迁移。

## 执行与验证计划

1. 新增因果行情特征和固定预算短期预测；测试前缀/未来扰动、真实标签结束日、同窗基线、退化回退、坏输入与缺失数据。
2. 新增长期连续特征及证据诊断，接入现价分析、历史回放和 ML 消融；测试未知财务、金融口径、负利润、公告边界及旧面板兼容。
3. 接入两条主报告路径与缓存版本；测试日常输出、冻结记录、领域依赖和原门禁。
4. 执行相关 pytest、四项基础离线、synthetic e2e/cache；在独立输出目录只读探查已有本地市场数据，完整记录失败和未验证项。

## 路由清单

- 实际 Skill：agentic-kaggle-skill（validation / tabular / ensembling references）、brainstorming、using-git-worktrees、writing-plans、scikit-learn（pipelines reference）、test-driven-development、verification-before-completion。
- 规则：项目 `AGENTS.md`、`skills-index.md`、`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`shared-code-change-safety.md`、`anti-hallucination.md`。
- Harness：当前结构、源码真理源、数据与实验边界可执行，判定 adequate；本轮无需重写工程指令。
- 未加载：deep-research（没有独立文献综述任务）、统计推断/SHAP/专用时序框架（没有相应实现）、Git 远程交付 Skill（本轮未要求远程发布）。
- 版本：项目现有 Python 3.11.14、scikit-learn 1.7.2；不按 Skill 示例升级依赖。训练内 Pipeline 参考 [sklearn 1.7 数据泄漏说明](https://scikit-learn.org/1.7/common_pitfalls.html)，缺失列保持参考 [SimpleImputer](https://scikit-learn.org/1.7/modules/generated/sklearn.impute.SimpleImputer.html)。

## 本轮本地数据结果

只读现有观察池（6只股票、6只ETF），每只最多300根qfq日线，先冻结再计算。11只数据截至2026-11-03，510500仅到2026-10-27；原始快照与逐窗结果保存在本地 `state/competition-transfer-20261104/`，不提交。未联网补行情，未打开比赛数据或封存窗口。

- 每只三个开发窗、每窗15条标签在窗内成熟的评价记录；这些5根收益仍有重叠，45条不是45个独立样本。
- 收益Ridge仅512880通过预先固定的增益/退化门槛：平均绝对误差约1.694个百分点，对照零收益基线约1.984个百分点。其余11只回退零收益基线；不是宣称这些资产不会上涨。
- 波动Ridge在部分标的平均误差下降，但没有任何标的同时通过各窗口退化约束，12只全部保留历史20根RMS基线。没有看完结果后放宽门槛或继续调参。
- 六只股票各有16条带可用公告日期的缓存报告；这只证明当前缓存可做公告过滤，不证明财报历史修订、股本调整、退市/成分或全市场覆盖完整。
- 连续财务特征的独立市场样本外增益尚未验证。现有配置没有固定长期验证/测试边界，本轮没有为得到一个分数擅自填入边界或复用旧测试段。新增消融和测试标签扰动检查使用构造数据。

## 使用与交付

日常本地入口保持不变：`.venv/bin/python run.py --daily --offline`。报告增加短期研究段，JSON观察记录保存模型、特征、设置、逐窗误差及输入版本。普通 `run.py --mode short/all` 的候选报告同样展示预测；这条原有命令仍有行情采集和研究记录的既有副作用，本轮没有对真实账户执行它。

长期现价分析和历史回放使用同一连续特征函数；`config.json` 的ML候选增加 `ridge_enriched`。旧回放缓存缺新schema版本时拒绝当作新特征面板；按既有日期协议重建后才能评价。预测收益使用qfq收盘变化，长期标签仍是raw价格变化；二者都不自动等于可成交/含分红总回报。

新源码会改变既有冻结实验的代码指纹；旧paper实验需要原有新副本运行版本衔接流程，本轮没有自动激活。首次实现以未提交diff同步至主工程；随后用户要求完整评审、修复并交付远程，见[评审与交付记录](competition_transfer_review_2026-11-04.md)。旧运行记录保留原版本，不回写成修复后的证据。

评审后，新预测的应用入口仅接纳已有完成日策略的CN/ETF，结果标记 `history_basis=current_qfq_snapshot`、`point_in_time_complete=false`。长期两个Ridge使用完全相同的训练内插补、缺失标志和缩放；新增特征须先胜过相同模型的原特征对照，再胜过最优原模型，才进入测试。财务年报要求12月31日期末，三年CAGR必须找到精确的三年前年报端点；特征缓存版本升为2。规则权重和评分公式未增加新权重，但修正财务输入可能改变新计算的历史规则分；冻结记录不变。

## 验证证据

- 新增测试覆盖因果前缀、未来扰动不变性、真实标签结束日、分窗退化、缺失数据、raw来源拒绝伪装qfq、报告接线、金融口径、负利润现金流、公告时点、特征消融测试隔离、TTM算术和同报告期修订。
- 主工程完整pytest：**529 passed in 73.03s**，在已有Hull环境运行（主工程环境缺可选polars）；新增行情/价值专测24项通过。四项基础离线、synthetic端到端、synthetic缓存等价均通过。基础数据源脚本首次因新worktree缺 `state/cache` 失败，补齐目录后通过；未修改该脚本或真实缓存。
- 实际CLI使用仅含行情的独立SQLite副本，先前一次隔离配置误用了相对DB路径而读到空库；改为副本绝对路径后通过。原行情库只经LocalData只读读取，副本测试不写真实账本或参数。
- 本轮追加使用 `systematic-debugging` 排查上述环境/夹具问题；没有安装或更新全局依赖。Ruff与 `git diff --check` 作为静态检查，不能替代前述行为验证。

构造测试、同一行情快照重复运行和本次开发窗结果都不构成新的独立盈利证据。

实际验证命令（在主工程或同源码隔离副本运行；`$HULL_PYTHON`指现有Hull环境，不新装依赖）：

```bash
$HULL_PYTHON -m pytest -q
$HULL_PYTHON tests/offline_store.py
$HULL_PYTHON tests/offline_datasources.py
$HULL_PYTHON tests/offline_long.py
$HULL_PYTHON tests/offline_ml.py
$HULL_PYTHON tests/offline_e2e.py --synthetic
$HULL_PYTHON tests/verify_phase_cache.py --synthetic
.venv/bin/ruff check src/quant_agent tests
git diff --check
```

本地[报告样例](../../state/competition-transfer-20261104/cli-final/research_2026-11-04.md)、[逐窗预测](../../state/competition-transfer-20261104/probe-final.json)、[CLI回执](../../state/competition-transfer-20261104/cli-final-receipt.json)及 `pytest-final.log` 均在忽略的运行目录；源行情快照ID为 `2d6fb6fa874a277d4dd4ec5beb412edf6e185a3be1c64013db9a8945b4f0d880`。这些本地链接不随Git源码发布。
