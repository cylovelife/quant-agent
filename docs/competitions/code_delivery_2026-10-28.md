# 首赛源码交付状态（2026-10-28）

本地源码提交`a649d51`（31个文件），许可范围提交`d8f6704`。完整代码入口见[CODE_MAP](CODE_MAP.md)，包含8个模块、3个测试文件及4份原始实验驱动快照。新增许可仅适用[明确列出的竞赛代码与配置](CODE_LICENSE.md)。

## 本地已验证

- 相关pytest：44 passed in 8.74s；竞赛模块及测试ruff检查通过。
- 最终CSV重新生成的SHA与此前有效提交逐字节相同；已有输出路径拒绝覆盖。
- 15项源码清单、4份历史驱动原始字节一致；JSON契约及文档链接通过。
- 暂存内容排除原始xlsx、预测CSV、真实账本及实际凭据值；Git作者、提交者、GitHub个人SSH身份及远程目标已核实。

## Kaggle私有版本已保存，公开及GitHub推送尚未完成

用户已授权对应比赛代码公开、限定范围Apache-2.0及GitHub分支推送。

保存回执为版本1，传入`--no-run`。从[账号内私有Notebook](https://www.kaggle.com/code/leon0330/notebookb734f4d0d0)重新下载，22个Markdown单元格与本地逐字一致，0个可执行代码单元，关联比赛Stock Price Predictor；未训练模型或提交新预测。Notebook标题仍为平台生成标题，不能把预期标题当作已完成重命名。

公开请求的真实HTTP响应为403，原因是`Phone verification is required to make a notebook public.`；token有效、账号为leon0330，授权范围`resources.admin:*`。不是缺少GitHub权限，也不要求用户重新提供token。

用户需自行在Kaggle个人设置完成手机验证，不将验证码或凭据发送到聊天。完成后可继续将同一份代码公开并核对可访问状态，再推送现有公开GitHub的`codex/kaggle-day2`分支并回读SHA。比赛规则§6.b要求公开竞赛代码同时分享至对应Kaggle Notebook/论坛，因此此刻不声称已推送远程，不合并main、不改原投资worktree、不推进Hull。

本地平台回执与下载副本保留在忽略的`state/competitions/stock-price-predictor/`，包括`code-delivery-readback.json`及`notebook-save-diagnostic.txt`。

本轮实际加载：git-workflow、finishing-a-development-branch、verification-before-completion、web-access；规则为项目AGENTS、skills-index、engineering-workflow、Python code-style/data-runtime、anti-hallucination。验证证据及未完成层次如上。
