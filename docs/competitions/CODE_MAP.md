# 首赛完整代码入口

本页覆盖本次自有模型、数据处理、实验驱动、提交复现和测试。既有投资系统不属于这次新增改动；运行原始数据、预测CSV、模型产物和凭据继续留在Git忽略的`state/`，不随代码公开。

所有自有代码文件及SHA-256见[代码清单](code_manifest.json)。

## 建议阅读顺序

| 顺序 | 文件 | 负责什么 |
|---|---|---|
| 1 | [competition_data.py](../../src/competition_data.py) | SHA/重复副本/表结构/日期/OHLC校验，分离训练标签与无标签输入 |
| 2 | [competition_validation.py](../../src/competition_validation.py) | 用实际label_end清除跨时间边界样本 |
| 3 | [competition_baseline.py](../../src/competition_baseline.py) | 原18项因果特征、星期/Logistic/浅树、选型锁定及一次保留评估 |
| 4 | [competition_metrics.py](../../src/competition_metrics.py) | Accuracy、Balanced Accuracy、混淆矩阵和原样例格式输出 |
| 5 | [competition_submission.py](../../src/competition_submission.py) | 365条服务器协议、缺失输入先验、显式互补编码 |
| 6 | [competition_reproduce.py](../../src/competition_reproduce.py) | 仅重拟合固定星期模型并复现最终CSV，不重复保留评估、不上传 |
| 7 | [competition_event_clock.py](../../src/competition_event_clock.py) | OHLC变化时钟与训练内零标签星期分支 |
| 8 | [competition_timeseries.py](../../src/competition_timeseries.py) | AR(5)/ARX(5)收益特征与固定alpha的Ridge拟合 |

第二名提交实际使用第1–6项；事件时钟和AR/ARX是未被采用的本地对照。第二名输出包含看到公开分数后的整体`1-y`选择，不能把该排名归因于AR/ARX或独立验证创新。[完整解释](rank2_2026-10-28.md)

## 实验当时真正执行的驱动

此前这些脚本写在本地`state/`，现以**原始字节快照**归档，便于检查全部实验代码：

- [audit_day2.py.txt](source_history/audit_day2.py.txt)：Excel公式、末行标签和文件审计。
- [event_clock_probe.py.txt](source_history/event_clock_probe.py.txt)：两个事件时钟候选和四窗对照。
- [arx_probe.py.txt](source_history/arx_probe.py.txt)：两个自回归候选和四窗对照。
- [kaggle_env_exec.py.txt](source_history/kaggle_env_exec.py.txt)：当时的CLI内存凭据转交和输出脱敏包装，无token值。

`.py.txt`用于逐行审阅历史，不是新的可执行入口。它们保留当时`state/`目录位置和相对路径，不能直接改名后在归档目录运行；原始版本与运行证据仍保留在本地。第三方Kaggle SDK、主办者及参考Notebook未复制进Git，按文档中的来源链接查看。

## 参数与数据契约

- [训练当时的数据契约](stock_price_predictor_contract_at_training.json)：与D3冻结记录一致，SHA为`b67a33e915ca38839ddfd7f12738d5e5238644707f839d74e2d09c04df0e7720`。
- [当前服务器契约及提交状态](stock_price_predictor_contract.json)：记录Id/ID差异、364输入/365输出、缺失输入和方向互补。
- [D3预登记协议](stock_price_predictor_protocol.json)、[事件时钟协议](event_clock_protocol.json)、[AR/ARX协议](arx_protocol.json)。
- 依赖沿用仓库[requirements.lock](../../requirements.lock)，没有为竞赛改动原锁文件；Kaggle CLI另行隔离安装为2.2.5。

## 测试代码

- [test_competition_pipeline.py](../../tests/test_competition_pipeline.py)：真实契约形状的合成xlsx、损坏拒绝、未来扰动、purge、指标、选型隔离、禁止覆盖。
- [test_competition_event_clock.py](../../tests/test_competition_event_clock.py)：事件特征因果性、重复观察及训练内分支。
- [test_competition_timeseries.py](../../tests/test_competition_timeseries.py)：AR特征、验证标签隔离、服务器365条及互补输出。

测试只生成临时合成数据，不携带原比赛样本。

```bash
.venv/bin/python -m pytest -q \
  tests/test_competition_pipeline.py tests/test_competition_event_clock.py \
  tests/test_competition_timeseries.py \
  tests/test_temporal_validation.py tests/test_research_experiments.py
```

## 从已授权下载的原始文件复现最终提交

先在本机接受赛规并官方下载、保留根目录及`StockTrends/`的三对原文件。代码不分发这些文件。下面只生成新文件，遇已有路径明确失败，不发起线上提交：

```bash
.venv/bin/python src/competition_reproduce.py \
  --raw state/competitions/stock-price-predictor/raw \
  --contract docs/competitions/stock_price_predictor_contract_at_training.json \
  --output state/competitions/stock-price-predictor/review-reproduced.csv \
  --public-lb-complement
```

已知最终SHA：`0d3e80c52bab50ab7865191b8a9f2914690f654518ae8a1e04f19b1bd06abc76`。去掉`--public-lb-complement`会保留训练标签方向，输出与当时32.054%提交对应的365条预测。这个开关是公开榜选择的历史记录，不是自动调优或独立泛化机制。

## 本次远程交付边界

推送对象为现有公开GitHub仓库的`codex/kaggle-day2`分支，不合入投资系统main。Kaggle规则要求公开竞赛代码同时在对应比赛Notebook/论坛提供，且使用允许商业使用的OSI许可证；用户已明确授权额外的Kaggle代码公开发布，以及本次新增竞赛代码采用Apache-2.0；[许可范围](CODE_LICENSE.md)逐项列明，不扩展到既有投资系统。准备包不包含原始比赛数据、提交预测、账号凭据或无关投资运行产物。

交付核验使用`git-workflow`、`finishing-a-development-branch`和`verification-before-completion`。已读项目AGENTS、skills-index、engineering-workflow、Python code-style/data-runtime及anti-hallucination规则。核实个人Git作者/提交者邮箱、GitHub SSH身份、公开可见性与远程分支；数据/账本隔离Harness充分，未修改工程指令。实际测试、复现和远程提交结果在交付时分别报告。

本轮本地验证（2026-10-28）：上述pytest命令为`44 passed in 8.74s`；竞赛模块及测试`ruff check`通过；最终CSV为365条预测、SHA与已提交版本一致，已有输出路径拒绝覆盖。15项自有源码清单哈希、4份历史快照逐字节核对通过。暂存31个文件，检查未含原始xlsx、CSV、账本或实际凭据值。远程推送与Kaggle代码公开状态需分别以平台回读为准。

[本轮交付记录](code_delivery_2026-10-28.md)记录验证、公开状态与交付边界。对应比赛的[公开代码审阅Notebook](https://www.kaggle.com/code/leon0330/stock-price-predictor-code-review-20261028)已提供同一份源码。
