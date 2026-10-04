# Phase 0 外部资源核验台账

> Phase 0 目标：把设计方案 v1.0 中所有 ⚠️ 项核实为带链接的确证事实。
> 核验时间：2026-09-25 ｜ 核验方式：本机 curl 直连（TUN VPN 透明接管）
> 状态：✅ 已核实 ｜ ⚠️ 仍待核 ｜ ❌ 已证伪

---

## 0. 本次核验的一个意外发现

**文献前沿比预期活跃**：2025-08 至 2026-09 一年间，公式化因子挖掘方向出现了至少 10 篇新工作
（GM/Self-evolving Agent/GFlowNets/LLM Chain 路线），且出现专门的**因子评估框架**（AlphaEval）。
这意味着我们不必从零设计，「站在巨人肩膀上」在工程上是可行的；同时评估框架的成熟
验证了我们把「验证门禁作为一等公民」的设计方向是对的。

---

## 1. 数据源：akshare ✅

| 项 | 事实 | 链接 |
|---|---|---|
| 维护活跃度 | ★22739，末次 push **2026-09-23**（两天前） | github.com/akfamily/akshare |
| 资金流 | `stock_individual_fund_flow`、`stock_fund_flow_big_deal`、`stock_fund_flow_industry/concept` | ✅ 已确认接口存在 |
| 龙虎榜 | `stock_lhb_detail_em`、`stock_lhb_jgmmtj_em`、`stock_lhb_hyyyb_em`、`stock_lhb_jgstatistic_em` | ✅ |
| 融资融券 | `stock_margin_detail_sse` / `stock_margin_detail_szse` / `stock_margin_detail_bse` | ✅ |
| 限售解禁 | `stock_restricted_release_detail_em`、`..._queue_em`、`..._summary_em` | ✅ |
| 退市股（幸存者偏差用） | `stock_info_sh_delist`、`stock_info_sz_delist`；退市股财报 `stock_profit_sheet_by_report_delisted_em` 等 | ✅ **超额完成**：可解 §5.3 幸存者偏差风险 |
| 接口总量 | 资金流/龙虎榜相关接口 29 个 | 逐项 grep `akshare/__init__.py` 确认 |

**结论**：设计方案 §3.1 「优先 akshare 补数据类型」的判断成立，且四类关键数据全部可用，
另附送退市标的接口（原本列为单独风险项，现在一并解决）。

**仍待核**：tushare 积分门槛、baostock 可用性（均为 P1 冗余源，Phase 1 开工时再核）。

---

## 2. 因子库：经典来源 ✅

| 因子库 | 事实 | 链接 / 位置 |
|---|---|---|
| Qlib Alpha158 | ✅ `Alpha158` handler，本地源码已核实 | `third_party/qlib/qlib/contrib/data/handler.py:98` |
| Qlib Alpha360 | ✅ 同上 | `handler.py:48` |
| WorldQuant 101 Alphas | ✅ 原题：*101 Formulaic Alphas*，**arXiv:1601.00991** | arxiv.org/abs/1601.00991 |
| 101 开源实现 | ✅ `yli188/WorldQuant_alpha101_code` ★873（官方 Quantigic 实现移植） | ⚠️ 末次 push 2019，实现较老但公式完整 |
| 101 A股回测实现 | ℹ️ `kunboyao-Bo/WorldQuant-101-factors-backtest-Chinese-A-share`（2026-05 更新） | 可作为 A 股口径适配参考 |
| GTJA 191 | ✅ `wpwpwpwpwpwpwpwpwp/Alpha-101-GTJA-191` ★148；`Daic115/alpha191` | 2019-2023 老库，公式本身稳定够用 |
| 现成 A 股因子实验室 | ℹ️ `Haoyu-tech/astock-alpha-factor-lab`（2026-07）**Alpha158 + GTJA-191 + walk-forward LightGBM** | 与我们 Phase 2+4 路线高度重合，**可作起步脚手架** |

**关键判断**：GTJA 191 原生 A 股口径 → 移植优先级应**高于** 101（101 是美股口径，
`cap`/`adv` 需改造）。这与 v1.0 方案中的排序相反，需在 v1.1 修订。

---

## 3. 模型 / 自动研发：Qlib 生态与 RD-Agent ✅

| 项 | 事实 | 链接 |
|---|---|---|
| RD-Agent | ✅ ★14748，末次 push **2026-09-23**（活跃维护） | github.com/microsoft/RD-Agent |
| RD-Agent 量化场景 | ✅ `rdagent/scenarios/qlib/`，含 `developer/` `experiment/` `proposal/` `factor_experiment_loader/` `prompts.yaml` | ✅ 结构与我们的「假设→实验→分析」闭环一致 |
| Qlib 本地模型库 | ✅ 34 个 contrib 模型（LightGBM/XGBoost/HIST/GATs/ADD/TRA/TabNet…） | `third_party/qlib/qlib/contrib/model/` |
| Qlib RL 示例 | ✅ `examples/rl`、`examples/rl_order_execution`、`examples/portfolio` | 本地已核实 |

**结论**：RD-Agent 的 factor_experiment_loader + proposal/experiment/developer 三层结构，
正是我们 §3.4 「闭环控制器」的现成参照。建议 Phase 5 直接**读它的 proposal 数据结构**
（不一定要引整个框架——我们已有 store.py 与 rolling_factor_ic，重造成本不划算）。

---

## 4. 因子挖掘前沿文献 ✅（按时间倒序，全部 arXiv 一手核实）

| arXiv ID | 日期 | 标题 | 对我们的用处 |
|---|---|---|---|
| 2609.08581 | 2026-09-08 | AlphaRJM: Reward-Jump Memory for Stochastic Return-Guided Alpha Discovery | **收益方差/随机性处理** |
| 2608.01789 | 2026-08-03 | Towards Autonomous Formulaic Alpha Discovery: An Evolutionary Computation Perspective | **演化计算路线综述** → Phase 3 首选方法论依据 |
| 2602.14670 | 2026-02-16 | FactorMiner: A Self-Evolving Agent with Skills and Experience Memory | **自进化 Agent + 技能/经验记忆** → 直接对应我们的「实验台账」设计 |
| 2601.22119 | 2026-01-29 | Alpha Discovery via Grammar-Guided Learning and Search | 语法约束搜索 → 表达式引擎的算子设计参考 |
| 2509.25055 | 2025-09-29 | AlphaSAGE: Structure-Aware Alpha Mining via GFlowNets | GFlowNets 多样探索 → 缓解「挖掘出一堆高度相似因子」 |
| 2509.01393 | 2025-09-01 | Adaptive Alpha Weighting with PPO | PPO 因子加权 → 我们 `update_weights` 的升级参考 |
| 2508.13174 | 2025-08-10 | **AlphaEval: 公式因子挖掘评估框架** | ⭐ **评估口径** → 对照我们的四关门禁，检查遗漏维度 |
| 2508.06312 | 2025-08-08 | Chain-of-Alpha: LLM 链式推理挖因子 | LLM 假设生成的具体实现范式 |
| 2508.04975 | 2025-08-07 | Sentiment-Aware Prediction + LLM-Generated Alpha | LLM 因子在情感场景的落地 |
| 2507.20263 | 2025-07-27 | Learning from Expert Factors: Trajectory-level Reward Shaping | **从专家因子学习奖励** → 冷启动方法 |
| — (NeurIPS'23) | — | AlphaGen：**arXiv:2306.12964** ✅ 已核实标题 | 「用 RL 生成协同的公式化因子组合」→ 注意是**协同**而非单因子最优 |

**重要方法论修正**：AlphaGen 的原始目标是**因子集合的协同性**（synergistic collections），
不是「单因子 IC 最大化」。这正好呼应我们 §5.3 的「过拟合换皮」风险——
设计 v1.1 应把准入逻辑从「单因子够强」扩展为「对现有因子池有增量贡献」（边际 IC）。

---

## 5. 待办与下一阶段入口

| 项 | 状态 | 何时处理 |
|---|---|---|
| tushare / baostock 可用性 | ⚠️ P1 冗余源 | Phase 1 开工前 10 分钟即可核 |
| 分析师一致预期在 A 股的有效性 | ⚠️ 未在本次核验范围内 | Phase 2 文献补充 |
| 同花顺 / 万得商业源成本 | ℹ️ 维持 v1.0 判断：**暂不引入**（商业成本 > 收益） | 除非需求变化 |
| AlphaEval 评估维度对照 | ⚠️ 需精读 | **建议 Phase 2 前完成**，用于校验四关门禁是否够严 |

---

## 版本

- 2026-09-25 初版：完成 Phase 0 全部核心核验，设计方案 v1.0 中标记 ⚠️ 的
  akshare / 101 / 191 / AlphaGen / RD-Agent 五项全部转为 ✅。
