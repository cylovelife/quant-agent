# 时间分区与标签成熟

任务 8；2026-10-27。训练、验证和测试按完整日期分组；训练标签结束日必须严格早于验证起点，验证标签结束日必须严格早于测试起点，测试标签必须在测试截止日或之前结束。缺失或无效的结束日不会按平均日历天数猜测。

## 运行边界

在决定研究协议后，可向运行配置顶层提供固定的历史日期（以下仅为格式示例，不是已登记实验）：

```json
"temporal_validation": {
  "validation_start": "2024-01-01",
  "test_start": "2025-01-01",
  "test_end": "2025-12-31"
}
```

三项必须完整、顺序正确，且 `test_end` 早于北京时间今天。测试分区限制信号日期与实际标签结束日均不晚于 `test_end`；信号采集和长期历史回放先截断原始行情到该日期；追加截止之后的数据不会改变该次评估的训练／验证／测试样本。长期回放缓存同时绑定截止日期。单次评估内三段日期互不重叠。用户或后续实验登记系统需事前冻结边界；本步尚未建立跨试验的封存登记、重复使用检测或搜索预算，那些属于任务 9。不能声称多次运行之间的测试集自动独立。

当前配置没有添加日期边界：短线选型、长期权重选型及离场模型验证会保守跳过，ML 评估返回不可评估；离场建议回退原有硬规则。旧长期历史面板没有每个收益目标的实际结束日时也不进入评估。必要时用已有历史回放重新生成有结束日期的面板；这不是对旧证据的静默补造。现有生产参数和 `evaluate.update_weights` 门禁没有更改。

## 各路径

- 短线：收集每个信号及其最长候选持有期对应的实际 K 线结束日。用最长路径的实际结束日统一清除，比按止损／止盈的实际提早结束更保守。训练段计算候选表现，验证段选候选，固定测试段只评估已选候选与当前规则；不再把多个重叠 OOS 尾段合并当作独立测试，也不再用全样本重选建议。当前结果仅供观察，不直接建议调权。
- 离场模型：每个持仓路径附真实到期日，训练段计算评分分桶与状态表，验证段选离场阈值，固定测试段评估。交付的状态表与测试使用同一训练段。原 `oos_frac` 保留接口兼容，实际分区以显式日期边界为准。
- 长期回放：分别记录 `label_end_ret_20`、`label_end_ret_60` 和 `label_end_ret_120`；多里程碑选型采用这些实际结束日的最大值清除跨界。验证段选权重，测试段只展示已选权重及默认权重，不以测试表现自动切换。
- ML：验证段只使用训练段拟合和填补缺失值，选择模型；之后仅用测试起点前已成熟的训练／验证数据重新拟合所选模型，固定预测整个测试段。规则分与模型在同一测试样本上比较。测试段置换固定预测后只打乱该截面收益，不是训练管线的泄漏证明。未登记的结果固定为研究观察，不直接晋级。
- 低层 `walk_forward_predict` 用逐行 `label_end_<目标列>` 判断成熟，实际结束日必须严格早于预测日期；`purge=False` 仅供有标记的诊断，正式评估入口拒绝关闭清除。报告中的日历跨度只是参考，不作为清除依据。

`phase_cache` 语义版本由 3 升至 4，旧离场／选型缓存不会冒充新口径。当前未建立历史 point-in-time 行情全覆盖或自动调参晋级；本轮测试为构造数据，不构成策略盈利证据。

## 本步验证

2026-10-27，项目 `.venv`：

```text
.venv/bin/python -m pytest -q tests/test_temporal_validation.py tests/test_indicator_backtest_boundaries.py tests/test_trade_semantics.py tests/test_data_contract.py tests/test_local_research.py tests/test_research_tracking.py
# 142 passed
.venv/bin/python tests/offline_store.py
.venv/bin/python tests/offline_datasources.py
.venv/bin/python tests/offline_long.py
.venv/bin/python tests/offline_ml.py
# 四项通过
.venv/bin/python tests/offline_e2e.py --synthetic
# 构造行情端到端通过，内含 161 项回归
.venv/bin/ruff check src/temporal_validation.py tests/test_temporal_validation.py src/model_select.py src/exit_model.py src/ml_eval.py src/value_track.py
# 通过
```

构造测试覆盖同日分组、跨界及未成熟标签清除、缺结束日与未结束测试日期拒绝、短线／长期权重／离场阈值及 ML 的测试段扰动、ML 未来截断、原始行情截止后截断、历史实际标签结束日和报告渲染。未做真实联网数据、历史 point-in-time 完整性或正式策略晋级验证。先前历史结果及缺结束日的旧缓存不作为新口径下的测试证据。

本步路由：`executing-plans`、`test-driven-development`、`systematic-debugging`、`verification-before-completion`。读取项目 `AGENTS.md`、`skills-index.md`、`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`shared-code-change-safety.md`。项目当前 `run.py`、`src/report.py`、`tests/offline_e2e.py` 的四条既有 Ruff 告警仍在，本步相关新增模块及模型模块检查通过；没有修改生产参数或现有 `evaluate.update_weights` 门禁。
