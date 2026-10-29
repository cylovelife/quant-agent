# Hull第四名：用REA的方法核对能复原什么，并实现公开风险框架

日期：2026-10-29，Asia/Shanghai。承接[外部调研与上下文实验](hull_research_2026-10-29.md)。用户已确认“REV”指 Reverse Engineer Anything（REA）。

**REA已下载，第四名公开的组合与风险公式已实现并做构造验证；完整原策略尚未复原。** 缺口是未公开的核心alpha、关键参数与原始逐日行为参照，不是增加一个逆向工具就能补出的信息。当前没有证据将“参考第四名＋微创新”换算成更高的第三名概率。对这些边界的判断置信度为高：原作者明确保留信号构造，本轮也没有获得可执行策略或逐日仓位。

## 1. REA已实际落地到哪里

上游为 [morluto/rea](https://github.com/morluto/rea)，下载至：

```text
/Users/cylove0330/Documents/tech/mydev/rea
```

固定提交 `54f5cf7e14e347d20828b07a991b644eafd080c9`，MIT许可。通过浅克隆取得源码，仓库工作区干净，未递归初始化第三方子模块。远程解析为同一上游的GitHub SSH地址，沿用本机既有URL重写配置。

本轮阅读README、AGENTS、REA入口技能的适用范围与调查／验证段、[证据工作流](https://github.com/morluto/rea/blob/54f5cf7e14e347d20828b07a991b644eafd080c9/.agents/skills/reverse-engineer-anything/references/evidence-workflows.md)及[重构方法证据笔记](https://github.com/morluto/rea/blob/54f5cf7e14e347d20828b07a991b644eafd080c9/website/evidence/touhou-reconstruction-method.md)。借鉴的内容是如何建立目标身份、保存证据、检验假设和比较重放结果。

当前是**源码下载与方法论应用**，不是已安装或调用REA MCP。没有安装其运行依赖、注册全局MCP或启动反编译器。它的目标路由面向已有程序、包或运行行为；我们没有第四名的目标程序可供分析。把Kaggle文章页面反编译，也只能得到网页展示逻辑，不能得到未向页面发送的交易信号代码。

## 2. 按REA方法建立证据和未知项

REA要求将观察、推断、未知分开，并保留覆盖范围；其 `verify_reconstruction` 只验证声明的有限可比较属性，不能由局部通过推出全系统等价。这一点直接用于本轮复原。

在它记录的历史重构案例中，验证器曾只核对引用常量的指令，却漏查常量数据，错误数值也可能被判成匹配。方法上的启发是：**验证参照本身也要检查**。金融复原同样要核对输入、参数、收益单位、状态与每日输出，不能只看一条相似净值曲线。

本轮复核了[第四名原文](https://www.kaggle.com/competitions/hull-tactical-market-prediction/writeups/4th-place-technical-model-no-learning-short-te)。正文明确表示核心信号早于比赛且不披露具体构造。原文中关于逆波动组合、波动目标和长窗口／定期更新的描述可作为结构证据。作者主页的进一步浏览受到reCAPTCHA服务连接问题限制；原文正文已经读取，原生网页检索和GitHub检索也没有取得原始策略代码。这里不把有限搜索写成“作者绝无公开代码”。

| 对象 | 公开证据 | 当前能验证什么 | 仍缺什么 |
|---|---|---|---|
| 主alpha | 属于短周期均值回归，较稀疏 | 方法家族 | 精确公式、输入、阈值、持有／退出条件 |
| 辅助信号 | 有少量用于平滑敞口的低alpha成分 | 组合设计意图 | 各信号定义、数量、选择过程与尺度 |
| 组合权重 | 按滚动波动的倒数归一化 | 逆波动公式 | 波动究竟定义在信号值还是策略收益上，具体估计窗口 |
| 波动目标 | 目标波动／组合估计波动，乘到敞口后截断 | 缩放公式及0–2边界 | 目标值、估计器、窗口、缩放前／后收益口径 |
| 更新 | 较长历史、周期性更新 | 需要状态与因果顺序 | 更新日历、暖机、零波动及缺失处理 |
| 成绩 | 原文有阶段汇总表与图 | 作者报告了这些历史结果 | 原始逐日仓位、精确区间、输入对齐及部分统计定义 |

完整机器可读清单见 [hull_fourth_reconstruction_contract.json](hull_fourth_reconstruction_contract.json)。所有未公开字段保留 `null`，`original_strategy_reproduced=false`，没有用经验参数伪装作者原值。

逆向工程依赖一个可观察对象：代码、程序、可调用行为、完整数学规格或输入输出。少量图表与汇总指标通常不能唯一识别一套交易规则。拟合一条相似曲线只增加“有一种规则可以解释它”的假设，不会证明找回了原函数。

## 3. 公开公式已经实现到哪一步

新增 [published_portfolio.py](../../src/quant_agent/competitions/hull/published_portfolio.py)。它接收调用方提供的信号敞口及历史风险数据，**不生成或冒充作者alpha**。

### 逆波动组合

$$
u_i=\frac{1/\max(\sigma_i,\epsilon)}{\sum_j1/\max(\sigma_j,\epsilon)},\qquad
b_t=\sum_i u_i a_{i,t}
$$

这里 $a_{i,t}$ 是外部输入的信号敞口，$b_t$ 是组合敞口。实现中，$\sigma_i$ 选择为调用方提供的各信号策略历史日收益的样本标准差；这是对原文歧义的一种**显式解释**，并非已证实的作者口径。不能把预测幅度的标准差与策略真实收益波动混为一谈。

$\epsilon$ 是调用方指定的正波动下限。它使零波动输入有有限结果；其数值和处理方式不是从原文恢复的。低波动信号会获得较高权重，但低波动并不保证高alpha，权重也没有利用协方差矩阵来寻找最优组合。

### 组合波动目标

$$
L_t=\frac{\sigma_{\rm target}}{\max(\hat\sigma_{\rm portfolio,t},\epsilon_{\rm annual})},
\qquad w_t=\operatorname{clip}(L_t b_t,0,2)
$$

这按公开关系缩放**整个敞口**，不同于我们上一轮围绕1倍标普收缩主动仓位的规则。例如 $b=1$、目标10%、当前组合波动20%，结果是0.5倍敞口，不是1倍。两个量都要同为年化波动，不能将日标准差直接与年化目标相除。

原文表中“实现年化波动14.3%”不等于作者将目标设成14.3%或15%。我们没有这样反推。构造测试中的15%是明确标记的测试参数。

### 状态与因果性

`RiskSettings`要求显式给出两个回看窗口、最少观测数、更新间隔、波动目标、日波动下限、暖机仓位以及缩放前／后收益口径，没有冒充原参数的默认值。

`PublishedRiskOverlay`只接收决策时已经成熟的历史：历史决策ID须早于当前ID，标签结束ID不得晚于当前决策。原API提供的一步lag收益可以在当前日使用；这和训练期为避免标签跨界而严格purge是两个不同用途。具体作者更新时点仍未公开。

实现采用样本标准差、252记录年化、按成功决策次数计算更新间隔；更新间隔内保留旧风险估计，但仍接受当前的外部信号敞口。暖机使用调用方给定仓位。非法输入导致失败，不推进内部时钟或污染已保存状态。这些选择是可检查的实现规范，不能当作原作者已经这样做的证据。

## 4. 已经跑过的构造验收

两个信号的历史收益波动比为1:2，逆波动权重应为2/3和1/3。输入敞口1.5、0.5时，组合敞口为7/6。采用一组明确的演示参数后：

| 构造决策ID | 风险是否更新 | 输出仓位 |
|---|---|---:|
| 4 | 是 | 0.60380739 |
| 5 | 否 | 0.60380739 |
| 6 | 是，传入风险历史幅度加倍 | 0.30190369 |

它验证了归一化、波动缩放和更新周期。这里只是改变构造输入来检验状态机，不是在修改真实历史，也不是策略收益或第四名复现结果。

另有一个反例检验：`[+4%,-3%,+4%,-3%]`与`[+4%,+4%,-3%,-3%]`有相同复利总收益、算术均值和标准差，却有不同回撤路径。因此即便匹配若干汇总指标，也不能替代逐日行为对齐。增加少量汇总指标也不会自动形成完整原函数的参照。

[test_hull_published_portfolio.py](../../tests/test_hull_published_portfolio.py)新增6项测试，覆盖公式、非有限值与零波动、仓位边界和float32、更新周期、暖机、未来标签拒绝、失败不推进状态、参数／单位显式性，以及上述不可由汇总结果恢复路径的反例。

## 5. 何时才可以说“复原完成”，再谈微创新

下一道门槛是取得足够的原策略材料：完整数学规格或原始代码／程序／可调用参照，信号与全部状态参数，以及同一输入上的作者逐日仓位或权威行为样例。随后先锁定输入、起始状态、时间顺序、评分和容差，逐日重放；通过后再冻结一个真正可比较的原版基线。

目前这个门槛未通过，所以没有启动“原版之上的微创新”，也没有以猜测alpha做参数搜索。本轮新增真实拟合0、候选alpha搜索0、比赛回测0；保留窗仍为 **0预测、0评分**。公开风险组件的工程工作已经完成，依赖作者未公开信息的整策略等价性仍不可验证。

微创新只有在固定基线、相同预算下改善了未用于选型的时间窗口，并能解释增量来源，才构成竞争力提升的证据。第四与第三的名次不是策略复杂度的连续刻度；改变几个参数可能改善，也可能破坏原有优势。当前无法估计前三概率，更不能把历史汇总表与我们不同区间的开发分数直接对比。

如未来一直没有原策略参照，可以另立一个**公开架构启发的原创复现研究**，为透明的短期反转信号事前登记公式和预算；它能成为我们自己的基线，但不会被改名为“完整恢复第四名”。

## 6. 交付与验证记录

本轮本地证据保存在Git忽略目录 `state/competitions/hull-tactical-market-prediction/fourth-reconstruction-20261029/`：REA下载与方法材料的SHA回执、纯构造演示输入／结果及验证记录。未向REA仓库、Kaggle或其他服务上传比赛数据，没有发送作者消息。REA源码仓库保持干净，主工程运行版本、投资账本与参数未变。

本轮相关回归 **112 passed in 18.94s**，含6项新增测试及原有Hull、首赛、时间隔离、研究登记和包结构检查：

```bash
.venv/bin/python -m pytest -q \
  tests/test_hull_published_portfolio.py tests/test_hull_context.py \
  tests/test_hull_mapping.py tests/test_hull_tree.py tests/test_hull_ridge.py \
  tests/test_hull_data.py tests/test_hull_sequential.py tests/test_hull_metrics.py \
  tests/test_competition_pipeline.py tests/test_competition_event_clock.py \
  tests/test_competition_timeseries.py tests/test_temporal_validation.py \
  tests/test_research_experiments.py tests/test_package_architecture.py
```

Hull源码及测试的`ruff check`、两个新增Python文件的格式检查、`git diff --check`、`uv pip check`均通过。REA的5份方法材料SHA一致，本轮文档本地引用已检查存在。REA与主工程工作区干净；当前Hull隔离工作区保留此前及本轮未提交改动。构造测试通过不代表原策略复原或前三竞争力已经验证。

路由：复用 `deep-research`及其TECH／证据／输出参考、`using-git-worktrees`核对已有隔离区、`web-access`读取登录页面、`test-driven-development`及其反模式、`tech-blogger`、`verification-before-completion`；新增阅读REA入口的适用范围、调查／验证段和证据工作流作为方法参考，没有声称执行REA工具。已读项目AGENTS、skills-index、engineering-workflow、Python code-style/data-runtime、anti-hallucination、研究与技术写作规则。REA的仓库AGENTS用于了解其工程边界，本轮未修改其TS／JS源码，不触发该语言实现流程。未Git提交或推送。
