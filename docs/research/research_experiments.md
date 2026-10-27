# 研究实验登记（任务 9）

## 已实现的边界

新增独立 SQLite 研究登记，显式冻结快照、代码和运行环境、股票池、特征、标签、成本、参数、切分、随机种子和预算。登记时检查快照与股票池一致；登记后的候选、新尝试和锁定前再次核查快照、代码及环境。相同身份或候选重复登记幂等，重复快照不增加独立样本，也不自动修改现行参数或晋级。候选和尝试次数受预算约束，超时记录会阻断后续尝试与锁定；耗时为事后记录，不是操作系统层面的强制中止。

新增程序内 `ExperimentRegistry.run_attempt`：先登记尝试，再调用传入的实际计算；正常异常记录为失败，成功前再次核查代码、环境和快照。同一尝试 ID 重试返回已有受控结果，不重新计算；手工录入的结果不能冒充受控执行结果。进程中断可能留下未完成记录，需要显式处理。本方法尚未接入现有模型研究入口，也不能单独保证输入与登记快照的衍生关系。

一次手工验证尝试先 `begin` 再 `finish`：成功、失败和未完成都保留；未完成会阻断新尝试，应如实标记失败。只有已有成功验证、无未完成任务且预算尚允许时才能锁定候选；之后登记入口关闭验证搜索，仅锁定候选可消费一次测试。为避免同一登记库把重叠测试时间段重复当独立测试，新的实验不能重用重叠测试日期。结果一律为观察证据，未自动晋级。数据库通过追加式表、事务和唯一索引保护这些操作。

**能力限制：**手工登记记录的是调用者声明的尝试和结果；新增受控执行函数能够运行和记录传入的计算，但尚不会自动接管现有研究脚本；旧的 `daily_research`、`model_select`、`exit_model`、`ml_eval` 独立入口尚未强制接入该登记，因此门禁不阻止绕开登记直接读取测试段，也无法阻止另建登记库复用测试段。现阶段不能声称测试集隔离已在全项目强制实施，也不能把自行填写的成功结果当成已验证试验。待后续工作把所有研究入口接入受控执行后，才能把该规则作为全局门禁。

`evaluate.update_weights` 按日期从近期向前选取至少 200 行的完整交易日，边界日整体保留；原 DEFF、最低样本量、幂等水位和现行参数保护不变。

## 显式调用

`.venv/bin/python run.py --research-experiment REQUEST_JSON` 操作指定的独立登记库；相对路径相对请求文件。JSON 的 `operation` 分别为：

- `register`：`registry_path`、`snapshot_path`、`spec`；`spec` 包含 `universe`、`parameters`、`features`、`label`、`cost`、`split`、`seed`、`budget`。
- `candidate`：`registry_path`、`experiment_id`、候选 `definition`。
- `begin`：以上身份加 `candidate_id`、唯一 `attempt_id`、`phase`（`validation` 或 `test`）。
- `finish`：`registry_path`、`attempt_id`、`outcome`（`succeeded` 或 `failed`）、`details`。
- `lock`：`registry_path`、`experiment_id`、`candidate_id`。
- `status`：`registry_path`、`experiment_id`。

`tests/test_research_experiments.py` 的构造快照和协议提供可执行输入示例；不使用真实账本、参数和研究历史。已有旧研究结果的语义不变。
