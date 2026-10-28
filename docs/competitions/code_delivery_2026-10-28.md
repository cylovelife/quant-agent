# 首赛源码交付（2026-10-28）

完整代码入口见[CODE_MAP](CODE_MAP.md)：8个模块、3个测试文件、4份原始实验驱动快照，附数据契约、参数和复盘。源码提交`a649d51`，许可范围提交`d8f6704`；新增许可仅适用[明确列出的竞赛代码与配置](CODE_LICENSE.md)。

## 交付对象

- GitHub：[codex/kaggle-day2分支](https://github.com/cylovelife/quant-agent/tree/codex/kaggle-day2)。使用个人作者和GitHub SSH身份，不合并main，不修改原投资worktree。
- Kaggle：[Stock Price Predictor Code Review 20261028](https://www.kaggle.com/code/leon0330/stock-price-predictor-code-review-20261028)，公开版本2，关联本场比赛，页面及分享面板均确认Public和Apache 2.0。

Kaggle最初因账号未完成手机验证返回403。用户完成验证后，同一份代码已成功公开；不需要更换token。最终标题通过网页修改，避免与平台已有标题冲突。

## 本轮验证证据

- 相关pytest：44 passed in 8.74s；竞赛模块及测试ruff检查通过。
- 最终CSV重新生成的SHA与此前有效提交逐字节相同；已有输出路径拒绝覆盖。
- 15项源码清单、4份历史驱动原始字节一致；JSON契约及文档链接通过。
- Kaggle版本2回读：`is_private=false`，22个Markdown单元格与本地逐字一致，0个可执行代码单元，比赛关联正确。保存传入`--no-run`；未训练模型或提交新预测。
- 暂存内容排除原始xlsx、预测CSV、真实账本及实际凭据值；作者、提交者、个人SSH身份和远程目标已核实。
- GitHub交付以`git ls-remote --heads origin codex/kaggle-day2`与本地HEAD相同为验收，远端SHA在最终回复及本地交付回执记录。

本轮未修改原模型或参数，未重跑已消耗的保留窗，未付费、未推进Hull。第二名的公开榜互补来源仍按[成绩复盘](rank2_2026-10-28.md)完整披露，代码公开不改变其证据性质。

本地回执、下载源码副本和公开状态截图保留在忽略的`state/competitions/stock-price-predictor/`，包括`code-delivery-readback.json`及`evidence/code-published.png`。

实际加载技能：git-workflow、finishing-a-development-branch、verification-before-completion、web-access。实际读取规则：项目AGENTS、skills-index、engineering-workflow、Python code-style/data-runtime、anti-hallucination。必要核验如上；没有把本地检查或保存回执替代公开状态及Git远端SHA回读。
