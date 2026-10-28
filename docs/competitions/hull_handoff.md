# Hull 历史训练接续卡

核实时间：2026-10-28，Asia/Shanghai。承接[第3天](day3_stock_price_predictor.md)；社区赛不追加模型搜索，下一主训练转Hull。本卡是历史练习准备，不是2026原赛事参评或获奖计划。

## 已核实的官方边界

- [Overview](https://www.kaggle.com/competitions/hull-tactical-market-prediction/overview)：官方提交截止2025-12-15 23:59 UTC，预测阶段结束2026-06-25 23:59 UTC。当前已结束，浏览器 `Late Submission` 灰显；不能承诺还能线上补交。
- 每日输出标普500配置比例0–2，通过顺序evaluation API进行Notebook推断。原训练阶段CPU/GPU均8小时、预测阶段9小时，关闭网络；这些是原平台上限，不是本机性能或当前补交可用性证明。
- [规则](https://www.kaggle.com/competitions/hull-tactical-market-prediction/rules)：数据用途限竞赛，禁止向未接受规则者分发；外部数据须符合公开、可获得及成本约束。原队伍最多5人、每日5次提交、可选2份最终提交。所有原始文件继续放本地忽略目录。
- 当前登录页面显示 `cylovelife`。用户已对新的Hull协议当场授权接受并下载，用于历史练习。按授权点击接受后，数据页仍要求Join并提示无法连接reCAPTCHA；刷新恢复后再次尝试仍未持久化，未取得成功回执或真实数据。不再盲目重试，需浏览器验证服务恢复或用户手动完成页面加入；已获授权，无须再授权同一协议。

## 数据语义：说明已读，真实文件尚未审计

[官方Data页](https://www.kaggle.com/competitions/hull-tactical-market-prediction/data)显示13文件、8.53 MB，包含train.csv、test.csv和kaggle_evaluation目录。这是页面声明，不能当作已下载的文件manifest或实际列数/行数。

`date_id`表示交易日序号，不假造公历日期。M/E/I/P/V/S/MOM/D前缀分别代表技术市场、宏观、利率、价格估值、波动、情绪、动量和哑变量；具体可用列以原文件为准，早期有较多缺失。

训练专有 `forward_returns`、`risk_free_rate`、`market_forward_excess_returns` 不作为当日预测特征；测试提供对应一日延迟字段及 `is_scored`。测试样例复制训练末180个date_id，官方明确说此公开样例得分没有泛化意义。先审计真实lag映射与API逐行行为，不能按同名拼接造成当日标签泄露。

## 官方评分器已取得

由Overview直接链接的[官方metric notebook](https://www.kaggle.com/code/metric/hull-competition-sharpe)下载，页面版本3/3、kernel version 262840772，Apache-2.0。保存在 `state/competitions/hull-tactical-market-prediction/`：

- `hull-competition-sharpe.ipynb` SHA-256：`849e5402a1766267355e830980efa785f6d4ad436572c6191725bdad68d918b5`。
- 提取代码 `official_metric_v3.py` SHA-256：`2f75bc0b1602afd3dd3d7da3415841c98ef49639b10d6dd53d261ea612c80ae6`。
- `metric_manifest.json`记录来源和版本；`metric_smoke.json`记录构造检查。

实际输出是配置，不是涨跌Accuracy。记日配置为w、指数收益r、无风险收益f，策略收益为 `s=f*(1-w)+w*r`。官方用超额收益的几何日均值、策略收益样本标准差和252日年化构造Sharpe，再除以超出市场1.2倍波动及低于市场收益的惩罚，最终上限1,000,000。精确结果以冻结源码为准，不能用普通算术均值Sharpe替代。

已做5项构造检查：全仓1与独立计算的市场Sharpe一致；低于0、高于2、非数值配置、零方差输入均被官方scorer拒绝。构造得分0.702886不是任何真实市场成绩。官方函数会修改传入solution，调用前必须copy；它不替我们完整验证长度、ID对齐、NaN/Inf，正式adapter应在边界显式拒绝这些输入，而不改写官方算法。

## 下一个工作时段：先取得真实闭环，再选复杂度

1. 确认规则持久接受并官方下载，保存ZIP/各文件哈希、实际schema、缺失分布、date_id顺序、train/mock重复与API源码身份；未取得时不伪造真实契约。
2. 将训练专有字段、可用特征、lagged标签和评分字段分离；用原评估API检查每一步能看到的信息、批大小、输出形状、状态重置。按实际标签成熟时点purge，不从序号擅自推断节假日。
3. 固定至少两个开发窗口和一个较新保留窗口；构造评分输入仅含窗口内真实成熟标签。先算配置0.5/1/1.5的常数参考，检查评分及波动约束。常数0遇零方差可不可评估，不强报0分。
4. 第一轮只做Ridge与一个受限CPU树模型预测超额收益；同一特征、窗口、预算。配置映射先固定为截断的线性函数，拟合只用开发段训练外预测；预测模型和映射分别比较，不一起无限搜索。
5. 特征先用官方可见字段加缺失指示，训练内填补/缩放；按前缀组做有限消融、逐窗口稳定性和训练外置换检查，避免单窗重要性筛选。只有互补增益在新窗口成立才加入融合或状态适应，不预设Transformer/LSTM更优。

资源沿用本机CPU与免费额度。首个工作时段1.5–2小时，先交付真实manifest、顺序adapter和常数评分，不同时追求模型排名。之后每轮只验证一个增量假设。历史练习结果单列，实际正式前三需要另外选一个仍开放、资格/规则/资源匹配的Featured赛事。

本轮浏览器观察保存在Stock Price Predictor的 `evidence/day3/hull-*.txt`，此处不复制完整条款或原数据到Git。没有向主办方或其他队伍发消息，没有创建后台任务，没有使用付费算力。

后续API核查：用户配置token后，Hull文件列表可读；DownloadDataFiles仍返回403并提示先接受规则。认证已验证有效，Hull数据访问仍未取得。日志位于 `state/competitions/hull-tactical-market-prediction/downloads/hull_files.json` 和 `hull_download.json`；没有下载成功或完成真实文件审计的声明。
