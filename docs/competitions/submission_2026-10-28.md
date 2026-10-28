# 首次平台提交：认证已通，赛方数据契约阻止评分

**后续状态已更新：有效提交57729795，官方67.945%，队伍leon0330当前第2/25。本文保留此前阶段证据；格式兼容处理、公开榜自适应方向选择及AR/ARX对照见[最新复盘](rank2_2026-10-28.md)。**

实际执行：2026-10-28，Asia/Shanghai；两次提交分别发生在15:31和15:32。用户提供的 `KAGGLE_API_TOKEN` 在交互式shell中有效；已通过官方CLI读取提交列表、上传及重新下载比赛文件。没有输出token，没有将它复制进仓库、运行日志或额外配置；仅传给本次子进程环境。此前缺少CLI认证的阻塞已解除。

**已完成两次实际上传，但均为ERROR，未取得有效评分、个人线上Accuracy或名次。** 服务器和官方文件存在两处可复核差异：样例列名为`Id`，服务器要求`ID`；真实测试与样例各364行，服务器要求365条预测。停止继续猜测格式，不补造输入、标签或未确定ID的预测。

## 平台回执与修复范围

| 提交编号 | 文件 | 预测内容 | 平台结果 |
|---|---|---|---|
| 57729186 | `day3/submission.csv` | D3冻结的364条预测 | ERROR：`ID column ID not found in submission` |
| 57729225 | `day3/submission-server-header.csv` | 仅将Id改成ID，所有364条ID值/预测值保持相同 | ERROR：`Submission has 364 rows but 365 were expected.` |

实际使用官方Kaggle CLI 2.2.5的 `competitions submit stock-price-predictor -f ... -m ... --wait 45`，随后再次读取个人提交列表确认两条均为`SubmissionStatus.ERROR`，publicScore/privateScore为空。命令非零退出来自评分失败，不能记作认证失败或有效提交。第二次输出显示今日剩余8次，不由该观察推断每日总上限。

冻结源CSV SHA-256仍为 `48fc5c42a14c5112ba14c82fbe081217e599a4fb1c3af645ddcb17430204b1d1`。仅改列名的CSV SHA-256为 `3b3e8830719b1f61c24d68fc2f217ded7d10cb8439bf52504569e8278ba56f9d`。逐行断言两份文件除表头外完全一致；没有重训、改阈值、看线上分数调参或生成测试标签。

`state/competitions/stock-price-predictor/day3/`内保存 `platform-first-attempt.json`、`platform-header-repair-attempt.json`、`server-header-repair.json`，包括UTC时间、命令结果与文件哈希。模型原`results.json`保留训练时状态，不回写为评分成功；平台状态以本记录与独立回执为准。

## 重新下载后的核实

从官方API重新列出六文件，并新下载根目录的Test与样例，未覆盖D2原始文件：

| 新下载文件 | 实际行/列 | SHA-256与D2比较 |
|---|---|---|
| Test.xlsx | 364行，Date/High/Low/Open/Close | `3319a611dead47ca837b28fc1965965a113948031233dd04e880d94fe08aec4d`，完全相同 |
| sample_submission.xlsx | 364行，Id/Target | `eca72fc4865c535b67628285d92c4f15e49ed9029ef24f29fdb43137563c1fa5`，完全相同 |

证据在 `state/competitions/stock-price-predictor/server-contract-review/`：`files.json`、`submissions.json`、两份新xlsx和`audit.json`。不是缓存旧文件造成的行数错觉。服务器尚未给出365个预期ID的完整映射，不猜多出的行位于开头还是末尾，也不把365行解释成含表头。数据契约已据回执更新，训练时旧契约副本仍保留。

D3导出器跟随官方样例输出Id的假设现已被服务器否定；其源码和训练哈希保留为当时证据，不能再称默认导出可直接被平台接受。后续若继续此题，先取得主办方明确的完整输入/ID契约，再做导出器修复和回归，避免固化不完整的服务器猜测。

## 接下来需要解决什么

比赛主办方需要提供一致的365行Test与样例，或将服务器要求改为364行，并确认ID列大小写及完整映射。未取得澄清前，继续试填一条预测不会带来可信研究证据。此前四小时社区练习预算继续有效；这场不升级为无上限排错或冲榜。最终前三目标不变，具体方法研究继续按[竞争力复核](competitiveness_review_2026-10-28.md)推进。

可发送给主办方的草稿如下，**本轮未发送或公开发布**：

> The current official Test.xlsx and sample_submission.xlsx each contain 364 data rows, with sample IDs 0–363 and header Id,Target. Submission 57729186 failed because the server requires ID. After changing only Id to ID, submission 57729225 failed because the server expects 365 rows. Could you provide a consistent test/sample pair and confirm the full ID mapping, or correct the server configuration?

同时使用已授权的同一token检查Hull：文件列表可读，官方下载返回403并提示需接受比赛规则。因此认证通路正常，但Hull数据访问授权仍未在平台生效。没有绕过规则、重新接受新条款或下载成功的声明；先前用户对Hull的接受授权仍保留，需要页面实际完成并回读成功。[Hull接续卡](hull_handoff.md)

## 路由与验证

本轮加载`token-manager`（凭据只在内存中使用）、`executing-plans`（恢复已授权提交）、`verification-before-completion`（平台回读）。token-manager连接器当前不可用，按用户明确提供的环境变量通路继续，未要求安装无关公司认证系统。沿用已读的项目AGENTS、skills-index、Python运行时与anti-hallucination规则；本轮无源码变更，不重跑无关模型/投资测试。

已验证：CLI认证请求成功、两次上传编号及错误、提交列表回读、两份新官方文件哈希/行列、仅表头修复逐行等价、原冻结提交哈希、JSON与文档引用。未验证：有效评分、服务器完整ID集合、线上名次、Hull实际数据和新模型。没有Git提交/推送，没有付费资源或对外消息。
