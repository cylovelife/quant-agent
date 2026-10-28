# 第3天：真实契约、时间验证与首个分类基线

**后续状态已更新：有效提交57729795，官方67.945%，队伍leon0330当前第2/25。本文保留此前阶段证据；格式兼容处理、公开榜自适应方向选择及AR/ARX对照见[最新复盘](rank2_2026-10-28.md)。**

实际执行：2026-10-28，Asia/Shanghai。D3是任务序号。本轮合并完成原D3–D5的本地工作；D6线上随后已实际上传两次，均因赛方文件/评分契约冲突ERROR；未取得有效得分，见[回执](submission_2026-10-28.md)。继续在同一quant-agent仓库的隔离worktree开发，不另建仓库，不修改原投资系统参数或账本。社区练习只保留一次有效提交，后续主训练转Hull。

## 本次预测什么、采用什么模型

本题预测下一条**自然日**记录收盘是否高于当日，而非预测价格数值。训练末行的Excel公式引用空白，排除其标签；其价格仍可作为之后的合法历史。保留周末重复行，遵守官方目标；不能把此题的Accuracy当成真实交易日预测或投资收益。

训练前冻结[实验协议](stock_price_predictor_protocol.json)，只比较三个固定候选与训练多数类。星期模型按训练窗口各星期的上涨比例，以0.5为分类阈值；没有查测试答案。Logistic和浅树使用下述18个固定特征，未做目标驱动的特征搜索。

| 模型 | 选择理由与固定限制 | 开发窗1 | 开发窗2 | 平均Accuracy |
|---|---|---:|---:|---:|
| 多数类参考 | 只取各训练窗多数类 | 62.78% | 60.56% | 61.67% |
| 星期条件频率 | 检验自然日重复数据的结构性基线 | 62.78% | 66.11% | **64.44%** |
| LogisticRegression | C=0.1，训练内中位数填补与标准化 | 60.56% | 67.78% | 64.17% |
| HistGradientBoostingClassifier | 深度2、4叶、60轮、叶最少40行、L2=10 | 63.33% | 65.56% | 64.44% |

选型规则事先登记：依复杂度从简到繁，复杂模型平均Accuracy须增加至少1个百分点，且最差窗口退化不超过1个百分点才替换。**最终选择星期模型**；浅树未提供足以增加复杂度的增益。没有根据保留测试或排行榜再修改特征、参数、阈值。

锁定后只评估所选模型一次：2019年保留窗324行中正确215行，**Accuracy=66.3580%**，多数类为192/324=59.2593%，差7.10个百分点；Balanced Accuracy=65.5777%。混淆矩阵按真实类别为行、预测类别为列，为 `[[134,58],[51,81]]`。这只是一个含日历重复的历史窗口，不报告显著性或真实投资alpha。

## 特征与时间边界

令C/O/H/L分别为当日收盘/开盘/最高/最低，只使用当行及更早记录：

| 特征组 | 定义 | 数量 |
|---|---|---:|
| 星期 | weekday 0–6 one-hot | 7 |
| 过去收益 | C(t)/C(t-k)-1，k=1/3/7/14 | 4 |
| 日内、跳空 | C/O-1；O(t)/C(t-1)-1 | 2 |
| 振幅与位置 | (H-L)/C；(C-L)/(H-L)，零振幅位置设0.5 | 2 |
| 波动与均线偏离 | 过去7条单期收益的样本标准差；C/过去14条均价-1 | 2 |
| 重复行情 | 当日OHLC是否全部等于上一行 | 1 |

前14行预热不参与拟合，最终训练1445行。没有成交量字段，不发明量价特征；不使用测试相邻价格反推标签。所有特征直接由因果窗口计算，不后向填补；Logistic预处理只在对应训练窗fit，推断不重拟合。树关闭随机早停切分，避免将随机内部验证冒充时间验证。参数行为以本地scikit-learn 1.7.2为准。[官方数据泄露说明](https://scikit-learn.org/stable/common_pitfalls)

开发窗分别为 `[2017-01-01,2017-07-01)` 和 `[2018-01-01,2018-07-01)`，各180条成熟标签；最终保留窗为 `[2019-01-01,2019-11-22)`，324条。训练要求 `Date < start` 且实际 `label_end < start`；评估要求 `start <= Date < end` 且 `label_end < end`，严格清除跨界标签。`label_end`来自下一条真实提供记录，未假定交易所或用固定交易日推算。

选择函数先排除保留窗及跨入保留窗的标签，写入 `locked_selection.json` 后才评估保留窗。该窗现已消费，不再把它当未见测试。单次输出目录防覆盖、冻结哈希和计数用于本地纪律，**不是全系统强制封存或不可篡改审计服务**。

## 指标、提交状态与前三目标

官方Accuracy为 `正确预测数 / 评分行数`，硬标签只能是0或1；本地同时报告多数类、Balanced Accuracy和混淆矩阵，识别类别不平衡。平台实际评分行数以有效回执为准。本地66.36%不能与线上第三名65.205%直接比较，因为两者样本不同。

已生成364条预测，Id原序0–363、208条预测为1，CSV表头 `Id,Target`，无额外索引。提交文件SHA-256：

`48fc5c42a14c5112ba14c82fbe081217e599a4fb1c3af645ddcb17430204b1d1`

完整文件：`state/competitions/stock-price-predictor/day3/submission.csv`。D3观察当前登录页面显示账号 `cylovelife`；D2接受规则时记录显示 `leon0330`，本轮没有独立验证是否账号改名，不能据此声称换号或同号。当前页面允许提交，不重复接受旧协议。

**D3建模当时尚未上传或评分**（之后两次ERROR见[回执](submission_2026-10-28.md)）：扩展要求“Allow access to file URLs”，`setFiles`失败；原生系统窗口接管也失败，未更改权限。用户当时选择自行开启权限；后续改用用户配置的API token完成两次上传，不重训。平台文案原文“365 rows and a header”与实际364条样例尚未通过服务器核对；D2将其解释为365行含表头的说法已撤回，不能补造第365条输入。上传失败不记成有效提交。

提交前榜单快照：第一0.69041、第二0.65479、第三0.65205，页面说明用全部测试数据；仅作任务背景，不据此调参，不宣称获得个人名次。完整回执会单独保留，不改写原训练结果。

冲正式前三的路线是：先证实协议和验证能对应未来评测；以训练外预测和逐窗口增量选择特征，再探索树模型、状态适应和互补融合。特征按语义组消融及窗口稳定性筛选，不能全样本看相关性后筛选；阈值和配置映射也算调参。只有在多个新窗口稳定超过强基线，才增加复杂模型与搜索预算。此社区题不继续冲榜；真正的目标仍是开放Featured与官方Cup最终前三，当前证据不足以承诺成功率。

## 代码、产物与复现

新增独立模块：

- `src/competition_data.py`：先验SHA/副本/列/sheet/行数校验；日期和OHLC约束；价格、成熟训练标签、样例分离，只读源文件。
- `src/competition_validation.py`：实际标签结束日的两段时间窗口与purge，不修改既有投资验证器。
- `src/competition_metrics.py`：二分类计数、Accuracy、Balanced Accuracy和原序Id输出，拒绝覆盖。
- `src/competition_baseline.py`：18特征、三个固定候选、训练内预处理、选型锁定、保留测试与提交生成。
- `tests/test_competition_pipeline.py`：临时合成xlsx、文件/语义破坏、未来扰动、边界清除、预处理不重拟合、选型隔离和重复运行拒绝。

实际执行命令：

```bash
.venv/bin/python src/competition_baseline.py \
  --raw state/competitions/stock-price-predictor/raw \
  --contract docs/competitions/stock_price_predictor_contract.json \
  --protocol docs/competitions/stock_price_predictor_protocol.json \
  --output state/competitions/stock-price-predictor/day3
```

该目录已存在，再运行会失败。需要审计时读现有产物；未来复现须使用新目录及 `day3/contract-at-training.json`，且登记为复现，不再作为新的未见测试。当前文档契约已修正文案证据，因此不再与训练前契约具有相同哈希。

`day3/`保存 `frozen_protocol.json`、`locked_selection.json`、`holdout_predictions.csv`、`results.json`、`contract-at-training.json` 和提交文件。冻结记录包含各源码SHA、模型参数、环境和时间；训练时契约SHA为 `b67a33e915ca38839ddfd7f12738d5e5238644707f839d74e2d09c04df0e7720`。原始数据身份见[数据契约](stock_price_predictor_contract.json)。这些运行产物在Git忽略目录，不等于已通过Git备份。

真实运行仅3个候选配置/3次完整候选评估，含6次开发拟合、1次所选模型保留评估、1次全训练重拟合，低于6次候选评估上限。脚本内测量0.417秒，不含解释器启动、环境准备和开发时间；不能用它代表总人时。本轮无大范围搜索，无付费资源，未达到4小时人工投入上限。

## 验证证据与下一步

已通过：

```bash
.venv/bin/python -m pytest -q tests/test_competition_pipeline.py tests/test_temporal_validation.py tests/test_research_experiments.py
# 38 passed in 11.77s
.venv/bin/ruff check src/competition_*.py tests/test_competition_pipeline.py
# All checks passed
```

新测试17例；首次单独通过用68.14秒，随后上述38例通过。合成测试与真实训练分开，不把绿灯当泛化或线上提交成功。数据哈希和冻结源码在真实运行后核对一致；原始文件未修改，原工作区无新增竞赛源码，现有参数、锁文件、真实行情和账本未写入。未运行无关行情同步、投资回测、全量业务测试或线上交易。未提交/推送Git。

实际加载技能：`executing-plans`（延续任务）、`test-driven-development`及测试反模式参考（新增行为测试）、`systematic-debugging`（检查首次测试耗时）、`web-access`（已登录动态页面）、`verification-before-completion`。读取规则：`AGENTS.md`、`skills-index.md`、`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`shared-code-change-safety.md`、`anti-hallucination.md`。Python独立入口与数据隔离Harness足够，本轮不改工程指令。没有调用子Agent、Git交付或Web应用规则；不涉及这些交付动作。

下一项唯一主动作：取得这一文件的一次有效线上回执后结束社区练习；若浏览器仍受阻，只保留明确缺口，不用加模型掩盖。Hull只读调研、官方评分器下载与规则访问准备已并行推进，见[Hull接续卡](hull_handoff.md)。

后续用户要求先复核榜单及创新空间，已在独立协议下补做一个两个配置的探索对照，未复用2019保留窗、未修改D3四模块与提交文件。事件特征未通过增益门槛；CLI已隔离安装但认证/浏览器权限仍缺。更新见[竞争力复核](competitiveness_review_2026-10-28.md)，本页上文训练记录不改写成新的测试证据。
