# Phase 1 数据通道探针报告（akshare）

> 目的：设计文档 §3.1 列的接口是「文档说存在」。本报告回答的是另一个问题——
> **在本机真实网络环境下，这些接口能不能拿到数据、拿到什么、失败是什么原因。**
>
> 探针时间：2026-09-29 22:51~23:05 ｜ 环境：macOS，出口路由 utun0（TUN）
> 证据：`output/akshare_probe_20260929.py`（脚本）、`output/akshare_probe_stdout.txt`、`output/akshare_probe_result.json`
> 状态：✅ 实测可用 ｜ ❌ 实测不可达 ｜ ⚠️ 未测完/不确定

---

## 1. 结论先行

**四类关键数据里，三类可用、一类只剩替代口径。**

| 数据类型 | 结论 | 说明 |
|---|---|---|
| 龙虎榜 | ✅ **3/3 可用** | 走 `datacenter-web.eastmoney.com`，与本机可达 |
| 融资融券 | ✅ **4/4 可用** | 走交易所官网（sse/szse/bse），与本机可达 |
| 限售解禁 | ✅ **2/2 可用** | 走 `datacenter-web`，与本机可达 |
| 退市标的 | ✅ **2/2 可用** | 走交易所官网 |
| 资金流 | ⚠️ **个股历史日线不可达**，大单/行业/概念可用 | `push2his` 与 `push2` clist 被本机出口拒绝 |

**一个必须写进设计文档的新事实**：本机出口 IP 为 **日本东京（156.146.34.69，AS60068 Datacamp）**，
**全部流量**（含国内站点）都走 utun0。国内 ipip.net 回声看到的也是这个日本 IP——

```
$ route -n get default   → interface: utun0
$ curl https://myip.ipip.net → 当前 IP：156.146.34.69 来自于：日本 东京都 东京 datacamp.co.uk
```

**推论**：东财 push 集群（`*.push2*`）对本机出口连接直接 reset / 502。
这不是「偶发网络失败」，也不是限流——它**跨进程、跨客户端、跨运行日稳定复现**。

**独立佐证（来自本仓库自己的落盘）**：`state/quant.db` 的 `data_health` 全历史画像显示
`eastmoney_clist` 长期低成功率（ETF 15.4%，港/美各 9.1%），而 `tencent_quote_list`
与 `kline_tencent` 是 **100%**。这与探针结论完全一致，可排除「沙箱假象」。

---

## 2. 实测矩阵（17 个接口，akshare 1.18.97）

### 2.1 ✅ 可用

| 接口 | 行数 | 耗时 | 上游主机 |
|---|---|---|---|
| `stock_lhb_detail_em` | 67 | 5.7s | datacenter-web |
| `stock_lhb_stock_statistic_em` | 554 | 3.8s | datacenter-web |
| `stock_lhb_jgmmtj_em` | 35 | 6.6s | datacenter-web |
| `stock_margin_detail_sse` | 2000 | 0.8s | sse.com.cn |
| `stock_margin_detail_szse` | 2106 | 1.2s | szse.cn |
| `stock_margin_sse` | 1 | 0.5s | sse.com.cn |
| `stock_margin_detail_bse` | 344 | **34.7s** | bse.cn |
| `stock_restricted_release_detail_em` | 34 | 3.4s | datacenter-web |
| `stock_restricted_release_summary_em` | 2 | 2.0s | datacenter-web |
| `stock_info_sh_delist` | 159 | 0.3s | sse.com.cn |
| `stock_info_sz_delist` | 208 | 4.7s | szse.cn |
| `stock_fund_flow_big_deal` | 5000 | 14.3s | 东财（非 push 集群） |
| `stock_fund_flow_industry` | 90 | 0.4s | push2（**间歇可用**） |
| `stock_fund_flow_concept` | 387 | 1.0s | push2（**间歇可用**） |
| `fund_etf_category_sina` | 1688 | 4.4s | **新浪** |
| `stock_hk_spot` | 2810 | 20.7s | **新浪** |
| `stock_zh_a_spot_tx` | 5571 | 42.0s | **腾讯** |

### 2.2 ❌ 不可达

| 接口 | 表现 | 上游主机 | 备注 |
|---|---|---|---|
| `stock_individual_fund_flow` | Connection reset（curl / requests / curl_cffi 三种客户端全失败） | push2his | **个股主力净流入历史日线的唯一口径** |
| `stock_individual_fund_flow_rank` | JSONDecodeError（空响应） | push2 clist | 全市场截面口径，同样被拒 |
| `fund_etf_spot_em` | HTTP 502 | push2delay | ETF 现货 |
| `stock_hk_spot_em` | Connection reset | 72.push2 | 港股现货 |
| `stock_us_spot_em` | Connection reset | 72.push2 | 美股现货 |
| `stock_us_spot`（新浪） | ⚠️ 未测完 | sina | 911 页逐页抓，预热 136/911 用 69s，主动中止 |

### 2.3 故障归因（不是猜的，是逐条对照出来的）

| 现象 | 判据 | 结论 |
|---|---|---|
| push2his / 72.push2 连接 reset | 同期 curl、requests（`trust_env=False` 直连）、curl_cffi(chrome) **三者全失败**；TCP 443 可连通（`nc -zv` 成功） | 不是本地网络/TLS/UA 问题，是**服务端在 HTTP 层拒绝本出口** |
| 同时段 datacenter-web / sse / szse 返回 200 | 同一次对照测试 | **按子系统区别对待**，不是全站封禁 |
| push2 行业/概念资金流首次成功、之后失败 | 首轮 17 接口连打后 `stock_fund_flow_industry` 短暂 502、休息 100s 后恢复 | push2 **间歇可用**，对请求密度敏感 |
| 两融 szse/sse 首轮失败、复测全过 | 两个接口**重试即成功** | 首轮失败是**瞬时**故障，不是结构性不可用 |

> 诚实标注：push2his 在本轮探针的**第一次请求是成功的（200，1.44s）**，此后持续失败。
> 所以「日本出口 → 地域风控」是**高度可能但带反例**的解释。可确定的只有一条：
> **该接口在本机不可作为可靠性依赖**。`stock_fund_flow_industry` 的恢复说明 push 集群
> 至少存在一条「间歇可用」的通道，不值得为了它把资金流写成强依赖。

---

## 3. 对设计文档的三处修正建议

### 3.1 【必改】消除东财单点，不能用 akshare 的东财系接口

Phase 1 验收标准写的是「东财单点依赖消除（ETF/HK/US 有第二源）」。
但 akshare 的 `fund_etf_spot_em` / `stock_hk_spot_em` / `stock_us_spot_em`
**全部走东财 push 集群**——而东财本来就是那个坏掉的点。用它们做第二源等于没做。

**可行的第二源**（实测可达，且与东财、腾讯**异构**）：

| 市场 | 第二源 | 实测 |
|---|---|---|
| ETF | `fund_etf_category_sina`（新浪） | 1688 行 / 4.4s ✅ |
| 港股 | `stock_hk_spot`（新浪） | 2810 行 / 20.7s ✅ |
| A股 | `stock_zh_a_spot_tx`（腾讯） | 5571 行 / 42s ✅（**与现用腾讯同源，只能算带宽冗余，不算异构**） |
| 美股 | `stock_us_spot`（新浪） | ⚠️ 未测完，成本高（911 页） |

### 3.2 【必改】个股资金流改口径，改用大单明细自聚合

`stock_individual_fund_flow`（个股主力净流入日线）与 `..._rank`（全市场截面）**都不可达**。
但有两条替代路径，都是实测可用的：

1. **`stock_fund_flow_big_deal`**（5000 行 / 14.3s）——大单成交明细，
   **可按股票代码自聚合出「个股大单净额」**，一次请求覆盖全市场，请求数 N→1；
2. `stock_fund_flow_industry` / `..._concept`（行业/概念级资金流）——**间歇可用**，
   只能做展示，不能做时序依赖。

### 3.3 【新增】adapter 不能裸调 akshare

akshare 内部自建 `requests` session，会**拉取环境变量里的代理设置**（本机存在
`HTTP_PROXY=http://127.0.0.1:58594`，而 TUN 模式下这条代理是多余且有害的），
并且**绕过了本项目已有的限速、30 分钟熔断（`fetcher._em_blocked`）、
耗时预算与 `data_health` 上报**。

新 adapter 必须：
- 显式 `trust_env=False` 直连（与 `fetcher._raw_get` 第一档一致）；
- 走本项目的限速 + 熔断 + 耗时预算；
- 每次抓取写 `data_health`（源名、耗时、条目数、是否降级）；
- 失败分类：`Connection reset` / `502` 归「出口被拒」（**不可重试**，按 30 分钟熔断），
  `SSLError` / 超时归「瞬时」（可重试）。

---

## 4. 附：本机依赖安装的一个坑（可复现）

`pip install akshare` 在本机**稳定失败**：

```
ERROR: Could not install packages due to an OSError:
EEXIST: file already exists, mkdir '.../pip-install-xxx/jsonpath_xxxx'
```

原因定位：`jsonpath-0.82.2` 只有 sdist，pip 解包阶段触发 `EEXIST`（换 TMPDIR 无效）。
可复现的绕法：

```bash
# 1) 装构建后端（该 venv 原本没有 setuptools）
pip install setuptools wheel
# 2) 手工取 sdist 并走 --no-build-isolation 安装
curl -sL -o jsonpath.tar.gz \
  https://files.pythonhosted.org/packages/cf/a1/693351acd0a9edca4de9153372a65e75398898ea7f8a5c722ab00f464929/jsonpath-0.82.2.tar.gz
tar -xzf jsonpath.tar.gz
pip install --no-build-isolation --no-deps ./jsonpath-0.82.2
# 3) 再装 akshare（依赖已满足，不会再触发 sdist 构建）
pip install akshare        # → akshare 1.18.97
```

安装位置：项目运行所用的 venv `/Users/mfx/.workbuddy/binaries/python/envs/default`。
akshare 新增依赖：`beautifulsoup4 / html5lib / xlrd / tqdm / tabulate / mini-racer /
decorator / soupsieve / webencodings / jsonpath`（**未升级 pandas / numpy**）。

---

## 5. 待验证（不阻塞 Phase 1 开工）

| 项 | 何时处理 |
|---|---|
| push2his 是否在更长的冷却期（>30 分钟）后恢复 | Step 2 落地资金流时顺带复测 |
| `stock_us_spot`（新浪）能否在可接受耗时内完成 | 美股第二源落地时评估；预计不可接受（911 页） |
| tushare / baostock 可用性（Phase 0 遗留 P1 项） | 已确认二者**均未安装**；是否引入待定 |
| push2his 若确认长期不可达，是否需要一个**非东财的个股资金流口径** | 若 Step 2 自聚合方案精度不足，则回到此项 |
