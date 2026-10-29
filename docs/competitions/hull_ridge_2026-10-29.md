# Hull Ridge 与固定配置映射的开发结果

日期：2026-10-29，Asia/Shanghai。承接[数据与常数基线](hull_day1_2026-10-29.md)。

**一个固定参数的 Ridge 基线已完成，两窗均未证明稳定预测增量；保留窗口没有预测或评分。** 官方原评估 API 对第二个开发窗16行的重放也已通过，输出配置与研究计算逐项一致。本轮没有根据结果追加参数、阈值、特征或映射搜索。

Hull 原赛事已经结束。当前执行的是历史数据的顺序预测与配置练习，不涉及真实账户成交，也不能取得原赛前三名次。正式 Featured / KDD Cup 前三仍是长期结果目标，当前证据不足以估计成功概率。

## 预测对象与标普基准

模型预测官方字段 `market_forward_excess_returns`：它是前瞻收益减去滚动五年均值、再按 MAD 规则限幅得到的标签。它与直接预测指数价格、涨跌分类、预测 `forward_returns - risk_free_rate` 都有区别。

标签预测值 $\hat y_t$ 经映射得到标普500敞口 $w_t$：1代表全仓市场基准，低于1降低敞口，高于1增加敞口。配置0–2是赛题边界，不代表个人账户的可用杠杆。

官方核算策略收益为：

$$
s_t=f_t(1-w_t)+w_t r_t
$$

其中 $r_t$ 为 `forward_returns`，$f_t$ 为 `risk_free_rate`。官方评分还考虑波动和相对市场收益惩罚，所以毛收益更高不一定得分更高。本轮报告未计入真实买卖价差、滑点、手续费或实际融资利率；收益为样本窗口内累计毛收益，不是年化收益或实盘收益。

## 模型与输入特征

[ridge.py](../../src/quant_agent/competitions/hull/ridge.py) 实现以下固定流水线：

```text
94项官方基础特征 + 3项已验证的一步lag字段
  ├─ 训练内中位数填补，保留训练时全缺失列 → 训练内标准化
  └─ 97项逐列缺失指示
                 ↓
           194维输入 → Ridge
                 ↓
          预测调整后的前瞻收益
                 ↓
       固定形式和强度的线性截断映射
                 ↓
             标普敞口0–2
```

基础特征分组为D类9项、E类20项、I类9项、M类18项、P类13项、S类12项、V类13项，分别对应哑变量、宏观、利率、市场技术、估值、情绪和波动。文件没有 MOM 组。多数列是匿名指标，不能把某个 M 列擅自解释成 RSI 或 MACD，也没有据此补造原始 OHLC、成交量、新闻或跨股票因子。

三个lag字段是 `lagged_forward_returns`、`lagged_risk_free_rate`、`lagged_market_forward_excess_returns`。历史训练时均按前一条实际提供记录构造；这与官方mock逐行映射一致。评价期可以使用接口已经提供的前一天标签信息，当日三项训练专有字段不进入输入。

`date_id`、`is_scored` 和当前标签不进入模型。194维由97项标准化数值加97项0/1缺失指示组成，缺失指示本身不做标准化；填补统计和数值均值/标准差均只在各训练段拟合。

Ridge 优化平方误差与L2系数惩罚，适合在相关特征较多时建立透明的线性参考。其参数在真实拟合前固定为：

```json
{"alpha": 100.0, "solver": "lsqr", "tol": 0.000001, "max_iter": 10000}
```

这是一项待检验的基线设定，没有搜索alpha或将其称为最优模型。模型原理及缺失处理参见 [Ridge](https://scikit-learn.org/1.7/modules/generated/sklearn.linear_model.Ridge.html)、[SimpleImputer](https://scikit-learn.org/1.7/modules/generated/sklearn.impute.SimpleImputer.html) 和 [MissingIndicator](https://scikit-learn.org/1.7/modules/generated/sklearn.impute.MissingIndicator.html) 的对应版本官方说明。

## 固定映射与训练外尺度

[预登记协议](hull_ridge_protocol.json) 在真实拟合和评分前冻结。映射固定为：

$$
a=\sqrt{\frac{1}{N}\sum_{i=1}^N (\hat y_i^{\mathrm{OOF}})^2},\qquad
w_t=\operatorname{clip}\left(1+0.25\frac{\hat y_t}{a},0,2\right)
$$

这里OOF表示在时间上更早的训练外预测，不是随机交叉验证。映射中心1、强度0.25和上下界均不优化；只有预测尺度 $a$ 从历史训练外预测估计。如果 $a<10^{-8}$，回退到中性敞口1。

因此，一个历史预测RMS量级的正信号增加0.25敞口，负信号减少0.25敞口。它是固定研究规则，没有证明是最优配置或风险控制器。

每个外部开发窗之前，取三段各252个date_id跨度的连续校准窗；每段使用更早、已成熟标签的扩展训练历史拟合，再预测该段。按标签结束边界清除后每段251行，合计753条训练外预测。尺度计算不读取外部评价标签，也不优化OOF评分。

两个外部开发窗各需要三次OOF拟合、一次最终历史重拟合，共8次；只有一个候选参数配置。以相同映射处理训练目标均值作为附加参考，用于判断收益变化是否只是平均敞口改变，预测误差也单独对照训练均值。

原 API 的Python浮点标量通过protobuf float32传输。本轮将研究配置也统一量化为float32后再评分，避免线下配置与接口传输精度不一致；这是输出精度约定，不改变预测模型或搜索映射参数。

## 时间窗口与保留边界

沿用先前常数基线的区间，所有日期均为交易日序号，不假造公历日期：

| 项目 | 开发窗1 | 开发窗2 |
|---|---|---|
| 外部评价区间，右端不含 | [7536, 8040) | [8040, 8544) |
| 实际成熟评价date_id | 7536–8038 | 8040–8542 |
| 成熟评价行数 | 503 | 503 |
| 最终成熟训练行数 | 7,535 | 8,039 |
| 更早的三段OOF区间 | [6780,7032)、[7032,7284)、[7284,7536) | [7284,7536)、[7536,7788)、[7788,8040) |
| OOF RMS | 0.00275301 | 0.00375340 |
| 映射增益 $0.25/a$ | 90.80958 | 66.60632 |

保留段仍为 **[8544,9048)**。先在原始训练表中过滤掉该段，再构造模型历史和lag输入；它没有进入拟合、映射校准、预测、评分或模型API重放。文件审计仍可核对完整输入身份与mock关系，这里没有宣称全系统不可访问的封存服务。

开发窗此前用于常数参考，第二窗的拟合历史也包含第一窗已成熟标签。因此本轮属于开发对照，不是新的独立测试证据。

## 开发结果

| 指标 | Ridge 窗1 | 全仓标普 窗1 | Ridge 窗2 | 全仓标普 窗2 |
|---|---|---|---|---|
| 官方评分 | 0.950229 | 1.205100 | −0.160947 | −0.167637 |
| 样本累计毛收益 | +105.6029% | +61.7534% | −0.3079% | +0.0089% |
| 年化波动 | 29.3890% | 19.7918% | 21.3095% | 19.5154% |
| 策略/市场波动比 | 1.48491 | 1.00000 | 1.09193 | 1.00000 |
| 最大回撤 | −33.6017% | −23.8965% | −26.1805% | −24.1305% |
| 平均敞口 | 1.33433 | 1.00000 | 0.97214 | 1.00000 |

第一窗毛收益增加伴随更高敞口与波动、回撤，官方风险调整评分反而下降，不能据毛收益将其认定为稳定超额预测能力。第二窗评分数值略高，但毛收益更低、回撤更深；官方负分与惩罚机制的组合也限制了单看分数差的解释。

| 预测层指标 | 开发窗1 | 开发窗2 |
|---|---|---|
| Ridge MSE | 0.0001676022 | 0.0001602094 |
| 训练均值预测 MSE | 0.0001566525 | 0.0001511052 |
| Ridge误差相对均值参考增加 | 6.99% | 6.03% |
| 预测与目标的 Pearson 相关 | 0.10161 | 0.00279 |
| 训练均值经相同映射的官方评分 | 1.204977 | −0.168012 |

相关系数是描述统计，未做显著性或跨阶段稳定性证明。两窗MSE均差于均值参考，当前线性预测没有稳定增益；映射也未在两窗同时改善风险收益。本轮结论为保留代码和失败对照，**不提升为个人策略，不解封保留窗，不基于本次分数估计前三概率**。

## 验收与产物

主记录位于忽略目录 `state/competitions/hull-tactical-market-prediction/ridge-20261029/`：

- `frozen.json`：协议、输入manifest、源码SHA与依赖版本。
- `results.json`：逐窗预测误差、官方评分、毛收益、波动、回撤和运行成本。
- `development-1/2-calibration-oof.csv`：753条历史训练外预测及标签结束序号。
- `development-1/2-predictions.csv`：各503条开发预测和配置，不含保留段。
- `development-1/2-model.json`：194项系数、截距、求解迭代数和映射尺度。
- `api-development-replay/`：原API对date_id 8040–8055的16次单步调用、生成输入及预测Parquet。

原 API 输出与向量化研究配置逐项一致。API重放仅使用开发段生成的可见输入，没有使用与保留段重叠的官方mock文件，也没有读取评价当日标签作为回调输入。

整个真实研究命令内部计时约1.85秒，其中API重放约0.46秒；不含解释器启动、环境准备和开发时间，不能外推正式赛全流程成本。没有付费算力、线上提交或真实交易。

复现需使用新输出目录，并登记为复现，不计为独立样本：

```bash
.venv/bin/python -m quant_agent.competitions.hull.ridge_study \
  --raw state/competitions/hull-tactical-market-prediction/raw \
  --manifest state/competitions/hull-tactical-market-prediction/raw_manifest.json \
  --protocol docs/competitions/hull_ridge_protocol.json \
  --output state/competitions/hull-tactical-market-prediction/ridge-reproduction \
  --check-api
```

新增测试验证固定强度/截断/零信号回退、训练内预处理、空特征列保留、同一可见输入下改变评价标签不影响预测与映射、保留段特征和标签扰动不影响整个研究结果，以及逐行/批量配置一致。这里的标签隔离不禁止后续时点读取官方已经提供的lagged标签。

```bash
.venv/bin/python -m pytest -q tests/test_hull_ridge.py
```

新增6项测试通过。连同此前Hull、Stock Price、时间验证、研究实验与包架构测试，本轮实际结果为 **91 passed in 15.04s**：

```bash
.venv/bin/python -m pytest -q \
  tests/test_hull_ridge.py tests/test_hull_data.py \
  tests/test_hull_sequential.py tests/test_hull_metrics.py \
  tests/test_competition_pipeline.py tests/test_competition_event_clock.py \
  tests/test_competition_timeseries.py tests/test_temporal_validation.py \
  tests/test_research_experiments.py tests/test_package_architecture.py
```

新增自有源码与测试的ruff检查、文档23个本地链接、`git diff --check`及最终环境`uv pip check`通过。冻结源码与协议哈希、两窗各503条预测、OOF成熟边界、8次拟合以及16行API重放ID均已核对。`pyproject.toml`的Hull可选依赖也补齐了实际使用的scikit-learn 1.7.2，既有依赖锁未改变。本轮未提交或推送Git。

本轮使用 `executing-plans`、`test-driven-development`及测试反模式参考、`verification-before-completion`；读取项目AGENTS、skills-index、engineering-workflow、Python code-style/data-runtime、anti-hallucination、shared-code-change-safety。沿用隔离worktree及既有Harness，不修改投资账本、参数或主工程运行版本。

## 后续模型方向与前三目标

下一项依原计划建立一个受限CPU树模型，在相同可见特征、开发窗口和映射规则下对照。若其增量也不稳定，再以有限的特征组消融、训练窗口和分布变化假设定位问题，每次先登记再执行。只有互补性和新窗口增量明确时，才增加融合或状态适应复杂度。

Ridge在当前数据上尚未战胜简单预测参考；增加神经网络或更多参数不能据此被视为获奖方案。正式冲前三还需要选定仍开放的赛事、可靠隐藏评测、多个未用于选型的时间阶段上的稳定增益，以及完整赛季的实际成绩。Hull练习负责验证方法和执行能力，不替代正式名次。
