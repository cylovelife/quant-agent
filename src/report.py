# -*- coding: utf-8 -*-
"""每日报告生成：Markdown + HTML。"""
import os
from datetime import datetime

import pandas as pd

import exit_model
import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(ROOT, "reports")
os.makedirs(REPORT_DIR, exist_ok=True)

MARKET_NAMES = {"cn": "A股", "etf": "场内ETF", "hk": "港股", "us": "美股", "fund": "场外基金"}

DISCLAIMER = (
    "> ⚠️ **免责声明**：本报告由量化模型自动生成，仅供研究使用，不构成投资建议；"
    "历史回测不代表未来表现，请严格控制仓位并独立决策。\n"
)

FACTOR_COLS = ["f_boll", "f_rsi", "f_volume", "f_trend", "f_macd", "f_drawdown"]

_CN = "零一二三四五六七八九十"


def _cn(n: int) -> str:
    """章节号中文化（1→一 … 11→十一）。"""
    if n <= 10:
        return _CN[n]
    return "十" + _CN[n - 10]


def _pct(x, digits=2):
    if x is None or (isinstance(x, float) and x != x):
        return "-"
    return f"{x * 100:+.{digits}f}%"


def _n(x, digits=2, suffix="", plus=False):
    """数值渲染：None/NaN → '-'，其余按位数格式化。"""
    if x is None:
        return "-"
    try:
        if isinstance(x, float) and x != x:
            return "-"
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    sign = "+" if plus else ""
    return f"{v:{sign}.{digits}f}{suffix}"


def picks_table(df, has_bt: bool) -> str:
    if df is None or df.empty:
        return "（今日该类别无可入选标的）\n"
    cols = ["code", "name", "price", "pct", "amount", "score",
            "boll_pb", "rsi", "vr"]
    head = ["代码", "名称", "现价", "今日涨幅", "成交额(亿)", "评分",
            "布林%B", "RSI", "量比"]
    if has_bt and "bt_win_rate" in df.columns:
        cols += ["bt_win_rate", "bt_avg_ret", "bt_ir", "bt_calmar", "bt_max_dd"]
        head += ["回测胜率", "回测场均收益", "回测IR", "回测Calmar", "回测最大回撤"]
    lines = ["| " + " | ".join(head) + " |",
             "|" + "---|" * len(head)]
    for _, r in df.iterrows():
        vals = []
        for c in cols:
            v = r.get(c)
            if c == "bt_win_rate":
                vals.append(_pct(v) if v is not None else "-")
            elif c in ("bt_avg_ret", "bt_max_dd"):
                vals.append(_pct(v) if v is not None else "-")
            elif c in ("bt_ir", "bt_calmar"):
                vals.append(f"{v:.2f}" if v is not None else "-")
            elif c == "pct":
                vals.append(f"{v:+.2f}%")
            elif isinstance(v, float):
                vals.append(f"{v:.3g}")
            else:
                vals.append(str(v))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines) + "\n"


def market_summary_md(summary: dict) -> str:
    lines = ["| 市场 | 样本数 | 上涨占比 | 中位涨幅 | 总成交额(亿) | 备注 |",
             "|---|---|---|---|---|---|"]
    for mkt, s in summary.items():
        # 港/美股腾讯行情成交额口径与人民币不同，不直接可比
        amt = "-" if mkt in ("hk", "us") else f"{s['total_amount']:.0f}"
        lines.append(
            f"| {MARKET_NAMES.get(mkt, mkt)} | {s['count']} | "
            f"{_pct(s['up_ratio'], 1)} | {s['median_pct']:+.2f}% | "
            f"{amt} | {s.get('note', '-')} |")
    return "\n".join(lines) + "\n"


def eval_md(ev: dict) -> str:
    if "skipped" in ev:
        return f"（{ev['skipped']}）\n"
    if "avg_ret" not in ev:
        return "（昨日推荐标的今日暂无可用行情）\n"
    lines = [
        f"- 推荐日期：**{ev['picks_date']}**，共 {ev['count']} 只",
        f"- 次日平均收益：**{_pct(ev['avg_ret'])}**，胜率：**{ev['win_rate'] * 100:.1f}%**",
        f"- 最佳：{ev['best']['name']}({ev['best']['code']}) {_pct(ev['best']['ret'])}；"
        f"最差：{ev['worst']['name']}({ev['worst']['code']}) {_pct(ev['worst']['ret'])}",
    ]
    return "\n".join(lines) + "\n"


def weights_md(params: dict) -> str:
    w = params.get("weights") or {}
    if not w:
        return f"- 参数版本：`{params.get('version', 'base')}`，因子权重：缺失\n"
    items = "，".join(f"{k} {v:.2f}" for k, v in w.items())
    return f"- 参数版本：`{params.get('version', 'base')}`，因子权重：{items}\n"


FACTOR_LABELS = {
    "boll": "布林%B", "rsi": "RSI", "volume": "量能", "trend": "趋势",
    "macd": "MACD", "drawdown": "回撤",
    "f_boll": "布林%B", "f_rsi": "RSI", "f_volume": "量能",
    "f_trend": "趋势", "f_macd": "MACD", "f_drawdown": "回撤",
}


def factor_health_md(coll: "dict | None" = None, factor_ic: "dict | None" = None) -> str:
    """因子健康度区块（借鉴 R&D-Agent(Q) 验证单元“因子去重”思想）。"""
    lines = []
    if coll and coll.get("n", 0) >= 5:
        max_abs = coll.get("max_abs", 0.0)
        flagged = coll.get("flagged", [])
        if not flagged:
            lines.append(
                f"- ✅ 六因子截面共线度低（最大 |r|={max_abs:.2f}，n={coll['n']}），"
                f"无高度冗余对，权重未叠加单一维度信息。")
        else:
            lines.append(
                f"- ⚠️ 检测到 {len(flagged)} 对高度共线因子（|r|≥0.7，n={coll['n']}），"
                f"高度联动（含负相关），权重叠加会放大该共同维度，建议审视是否构成冗余：")
            for a, b, r in flagged:
                lines.append(f"  - {FACTOR_LABELS.get(a, a)} ↔ {FACTOR_LABELS.get(b, b)}：r={r:+.2f}")
    else:
        lines.append("- 候选样本过少，跳过因子共线诊断。")
    if factor_ic and factor_ic.get("n", 0) >= 5:
        lines.append(
            f"- 当日截面因子预测力：IC={factor_ic['ic']:+.2f}（n={factor_ic['n']}），"
            f"Rank IC={factor_ic['rank_ic']:+.2f}；IC>0 表示分数越高、历史低吸表现越好。")
    return "\n".join(lines) + "\n"


ROLLING_ORDER = ["score", "f_boll", "f_rsi", "f_volume", "f_trend", "f_macd", "f_drawdown"]


def _verdict(ic: "dict | None") -> str:
    """按 IC 序列的均值/ICIR/显著性给出可读判定。"""
    if not ic:
        return "样本不足"
    if ic.get("icir") is None:
        return "样本不足"
    mean, t = ic["mean"], ic.get("t")
    if mean > 0 and t is not None and abs(t) >= 2:
        return "✅ 稳定正向"
    if mean > 0:
        return "⚠️ 正向不显著"
    if mean < 0 and t is not None and abs(t) >= 2:
        return "⚠️ 显著反向"
    return "⚠️ 噪声/无效"


def rolling_ic_md(rolling: "dict | None") -> str:
    """滚动因子预测力区块（借鉴 R&D-Agent(Q) 统一评测：IC / ICIR / Rank IC / Rank ICIR）。"""
    if not rolling or not rolling.get("factors"):
        return "- 滚动因子评估：样本不足，跳过。\n"
    lines = [
        f"- 口径：{rolling['n_symbols']} 只 × {rolling['n_periods']} 个截面"
        f"（{rolling['date_range'][0]} ~ {rolling['date_range'][1]}），"
        f"前向收益 = 次日开盘买入、持有 {rolling['horizon']} 日，共 {rolling['n_obs']} 条观测。"
        f"`ICIR = IC均值 / IC标准差`（未年化）衡量**稳定性**；`t` 因重叠窗口偏乐观，"
        f"判读以 ICIR 与胜率为主。",
        "- ⚠️ 评估池是「今日候选」的历史序列（近期跌幅居前标的），非历史当日真实候选池，"
        "横截面区分度被压缩：**相对排序可信，绝对水平偏保守**。",
    ]
    rows = ["| 因子 | IC均值 | ICIR | RankIC均值 | RankICIR | IC胜率 | t值 | 判定 |",
            "|---|---|---|---|---|---|---|---|"]
    scored = []
    for f in ROLLING_ORDER:
        d = rolling["factors"].get(f)
        if not d:
            continue
        ic, ric = d.get("ic"), d.get("rank_ic")

        def _f(v, g=2):
            return f"{v:+.{g}f}" if v is not None else "-"

        ic_mean = _f(ic["mean"], 3) if ic else "-"
        ic_ir = _f(ic.get("icir"), 2) if ic else "-"
        ric_mean = _f(ric["mean"], 3) if ric else "-"
        ric_ir = _f(ric.get("icir"), 2) if ric else "-"
        wr = f"{ic['winrate'] * 100:.0f}%" if ic and ic.get("winrate") is not None else "-"
        tv = _f(ic.get("t"), 2) if ic else "-"
        rows.append(
            f"| {FACTOR_LABELS.get(f, f)} | {ic_mean} | {ic_ir} | "
            f"{ric_mean} | {ric_ir} | {wr} | {tv} | {_verdict(ic)} |")
        if ic and ic.get("icir") is not None:
            scored.append({"name": FACTOR_LABELS.get(f, f), "icir": ic["icir"],
                           "mean": ic["mean"], "t": ic.get("t")})
    lines.append("\n".join(rows))
    if scored:
        ranked = sorted(scored, key=lambda x: -x["icir"])
        best, worst = ranked[0], ranked[-1]
        lines.append(
            f"- 稳定性排序：**{best['name']}** 最稳（ICIR={best['icir']:+.2f}，"
            f"IC均值={best['mean']:+.3f}），**{worst['name']}** 最弱"
            f"（ICIR={worst['icir']:+.2f}，IC均值={worst['mean']:+.3f}）。")
        neg = [x["name"] for x in ranked
               if x["mean"] < 0 and x["t"] is not None and abs(x["t"]) >= 2]
        if neg:
            lines.append(f"- ⚠️ 以下因子滚动 IC 显著为负（|t|≥2），方向可能反向，"
                         f"建议降权或重审：{'、'.join(neg)}。")
        # 无反向因子时不再单独写一句「都在噪声范围内」——那是一句没有动作的空话，
        # 表格里的 ICIR/t 已经说明了一切。
        strong = [x["name"] for x in ranked if x["t"] is not None and abs(x["t"]) >= 2]
        if not strong and all(abs(x["icir"]) < 0.1 for x in ranked):
            lines.append("- ⚠️ **整体预警**：全部因子 |ICIR|<0.1 且无显著项，"
                         "说明该评分体系在本回测面板上预测力接近随机，"
                         "请勿据此放大仓位。")
    return "\n".join(lines) + "\n"


ADVICE_TAG = {"SELL": "🔴 建议卖出", "WATCH": "🟡 观察/减仓", "HOLD": "🟢 继续持有"}


def exit_advice_md(advice: "list | None", book: "dict | None" = None,
                   model: "dict | None" = None) -> str:
    """离场建议区块：先给今日硬规则平仓流水，再给明日需行动的清单。"""
    lines = []
    # 今日已由硬规则（止损/止盈/到期）平仓的标的：这些是「已经卖掉」的
    today = datetime.now().strftime("%Y-%m-%d")
    done = [p for p in ((book or {}).get("positions") or [])
            if p.get("status") == "closed" and p.get("exit_date") == today]
    if done:
        brief = "、".join(
            f"{p.get('name')}({p.get('exit_reason')} {_pct(p.get('exit_ret'))}，"
            f"持{p.get('days_held')}日)" for p in done[:10])
        more = f" 等 {len(done)} 只" if len(done) > 10 else ""
        lines.append(f"- 🔴 **今日已按硬规则平仓 {len(done)} 只**：{brief}{more}。")
    if not advice:
        lines.append("（当前无持仓或离场模型未就绪）\n")
        return "\n".join(lines) + "\n"
    act = [a for a in advice if a["advice"] in ("SELL", "WATCH")]
    hold = [a for a in advice if a["advice"] == "HOLD"]
    tau = (model or {}).get("tau", 0.0) or 0.0
    t_min = (model or {}).get("t_min")
    gate = f"t≤{t_min:+.1f}" if t_min is not None else f"t≤{exit_model.NEG_T_WEAK:+.1f}"
    if not act:
        lines.append(f"- ✅ 明日无卖出信号：{len(hold)} 只持仓的继续持有期望均为正"
                     f"（E≥τ={tau * 100:+.1f}%）。")
    else:
        lines.append(f"- 明日需行动 **{len(act)}** 只 / 共 {len(advice)} 只持仓"
                     f"（建议卖出 {sum(1 for a in act if a['advice'] == 'SELL')}、"
                     f"观察盯盘 {sum(1 for a in act if a['advice'] == 'WATCH')}）。"
                     f"判定：**幅度**（E<τ={tau * 100:+.1f}%）决定是否要动，"
                     f"**可信度**（{gate}）决定动到什么程度；"
                     f"门槛由样本内扫描选定、样本外验证（见第七章）。")
        rows = ["| 代码 | 名称 | 市场 | 持有 | 累计收益 | 买入评分 | 收益区间 | "
                "继续持有期望E | 样本n | t值 | 建议 | 理由 |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for a in act:
            e = a.get("expect_ret")
            t = a.get("expect_t")
            rows.append(
                f"| {a['code']} | {a['name']} | {MARKET_NAMES.get(a['market'], a['market'])} "
                f"| {a['days_held']}日 | {_pct(a['ret'])} "
                f"| {a['entry_score'] if a['entry_score'] is not None else '-'} "
                f"| {a.get('r_bucket', '-')} "
                f"| {_pct(e) if e is not None else '-'} "
                f"| {a['expect_n'] if a['expect_n'] is not None else '-'} "
                f"| {f'{t:+.2f}' if t is not None else '-'} "
                f"| {ADVICE_TAG.get(a['advice'], a['advice'])} | {a['reason']} |")
        lines.append("\n".join(rows))
    if hold:
        brief = "、".join(
            f"{a['name']}({_pct(a['ret'])}, E={_pct(a['expect_ret']) if a.get('expect_ret') is not None else '-'})"
            for a in hold[:12])
        more = f" 等 {len(hold)} 只" if len(hold) > 12 else ""
        lines.append(f"- 继续持有：{brief}{more}")
    return "\n".join(lines) + "\n"


def _fmt_stop(v):
    return "不止损" if v is None else f"{v * 100:.0f}%"


def _fmt_take(v):
    return "不止盈" if v is None else f"+{v * 100:.0f}%"


def parameter_scan_md(scan: "dict | None") -> str:
    """离场规则体检：止损 / 止盈 / 持有期的历史表现对比。"""
    if not scan:
        return "- 离场规则体检：样本不足，本轮跳过。\n"
    cur = scan["current"]
    lines = [
        f"- 信号样本 **{scan['n_signals']}** 笔（信号独立采样口径，用于横向比较，"
        f"不等同实盘组合收益）；当前配置：止损 {_fmt_stop(cur['stop'])}、"
        f"止盈 {_fmt_take(cur['take'])}、持有 {cur['horizon']} 日。",
        "- 读法：**平均收益**看赚钱效率，**5%分位/最差**看尾部风险，"
        "**Sharpe** 是两者权衡。",

    ]
    head = ["| 设置 | 平均收益 | 中位 | 胜率 | 5%分位 | 最差 | Sharpe |",
            "|---|---|---|---|---|---|---|"]

    def blocks(title, items, key, cur_val):
        rows = list(head)
        for it in items:
            mark = " ⬅当前" if it[key] == cur_val else ""
            sh = it.get("sharpe")
            label = _fmt_stop(it[key]) if key == "stop" else \
                ("不止盈" if it[key] is None else f"+{it[key] * 100:.0f}%")
            rows.append(
                f"| {label}{mark} | {_pct(it['avg'])} | {_pct(it['med'])} "
                f"| {it['win'] * 100:.1f}% | {_pct(it['p05'])} | {_pct(it['worst'])} "
                f"| {f'{sh:.3f}' if sh is not None else '-'} |")
        return f"\n**{title}**\n\n" + "\n".join(rows)

    lines.append(blocks(f"止损线对比（持有 {cur['horizon']} 日 / 止盈 "
                        f"{_fmt_take(cur['take'])}）", scan["by_stop"],
                        "stop", cur["stop"]))
    lines.append(blocks(f"止盈线对比（持有 {cur['horizon']} 日 / 止损 "
                        f"{_fmt_stop(cur['stop'])}）", scan["by_take"],
                        "take", cur["take"]))
    h_rows = list(head)
    for it in scan["by_horizon"]:
        mark = " ⬅当前" if it["horizon"] == cur["horizon"] else ""
        sh = it.get("sharpe")
        h_rows.append(
            f"| {it['horizon']} 日{mark} | {_pct(it['avg'])} | {_pct(it['med'])} "
            f"| {it['win'] * 100:.1f}% | {_pct(it['p05'])} | {_pct(it['worst'])} "
            f"| {f'{sh:.3f}' if sh is not None else '-'} |")
    lines.append("\n**持有期对比（止损 "
                 f"{_fmt_stop(cur['stop'])} / 止盈 {_fmt_take(cur['take'])}）**\n\n"
                 + "\n".join(h_rows))

    lines.extend(_scan_verdict_lines(scan))
    return "\n".join(lines) + "\n"


def _scan_verdict_lines(scan: dict) -> list:
    """离场规则体检的结论行：按 Sharpe 比较当前档位与更优档位。

    完整版与精简版共用同一份判定——两处各写一遍迟早分叉，
    而「离场规则要不要改」正是最不该出现两套说法的结论。
    """
    cur = scan["current"]
    out = []
    cands = [x for x in scan["by_stop"] if x.get("sharpe") is not None]
    if cands:
        best = max(cands, key=lambda x: x["sharpe"])
        cur_s = next((x for x in scan["by_stop"] if x["stop"] == cur["stop"]), None)
        if best["stop"] != cur["stop"] and cur_s and cur_s.get("sharpe") is not None \
                and best["sharpe"] > cur_s["sharpe"]:
            out.append(
                f"- ⚠️ **止损线体检**：`{_fmt_stop(best['stop'])}` 的 Sharpe "
                f"（{best['sharpe']:.3f}）优于当前 `{_fmt_stop(cur['stop'])}`"
                f"（{cur_s['sharpe']:.3f}），平均收益由 {_pct(cur_s['avg'])} "
                f"变为 {_pct(best['avg'])}。低吸策略依赖均值回归，"
                f"**过紧的止损会把标的割在反弹前**——建议评估放宽止损。")
        else:
            out.append(f"- 止损线：当前 `{_fmt_stop(cur['stop'])}` 在对比档位中表现不劣。")
    cands_t = [x for x in scan["by_take"] if x.get("sharpe") is not None]
    if cands_t:
        bt = max(cands_t, key=lambda x: x["sharpe"])
        cur_t = next((x for x in scan["by_take"] if x["take"] == cur["take"]), None)
        if bt["take"] != cur["take"] and cur_t and cur_t.get("sharpe") is not None \
                and bt["sharpe"] > cur_t["sharpe"]:
            out.append(
                f"- ⚠️ **止盈线体检**：`{_fmt_take(bt['take'])}` 的 Sharpe "
                f"（{bt['sharpe']:.3f}）优于当前 `{_fmt_take(cur['take'])}`"
                f"（{cur_t['sharpe']:.3f}）。过早止盈会截断右尾。")
        else:
            out.append(f"- 止盈线：当前 `{_fmt_take(cur['take'])}` 在对比档位中表现不劣。")
    return out


def exit_model_md(model: "dict | None") -> str:
    """离场模型说明与样本外验证结论。"""
    if not model:
        return "- 离场模型：样本不足，本次未标定。\n"
    val = model.get("validation")
    lines = [
        f"- 状态空间：`(持有天数 d, 累计收益桶 r, 买入评分桶 s)`，"
        f"d∈1~{model['horizon'] - 1}、r 六桶（≤-5%/+5% 边界）、"
        f"s 三分位（边界 {model['score_edges']}）。",
        f"- 标定样本：{model['n_symbols']} 只标的、{model['n_trades']} 笔信号路径、"
        f"{model['n_obs']} 条决策观测，区间 {model['date_range'][0]} ~ {model['date_range'][1]}；"
        f"有效状态桶 {len(model['states'])} 个。",
        "- 值函数 `E = E[从当前价继续持有到期的收益 | 状态]`（立即卖出记 0），"
        "桶内样本稀疏处向 `(d,r)` 与全局均值做两层贝叶斯收缩（k="
        f"{model.get('shrink_k', '-')}）。",
    ]
    if not val:
        lines.append("- ⚠️ 样本不足以做样本外切分，仅输出状态表，**不作为卖出依据**。")
        return "\n".join(lines) + "\n"
    t_min = val.get("t_min")
    gate = f"t ≤ {t_min:+.1f}" if t_min is not None else "不设门槛（样本内最优）"
    lines.append(
        f"- 决策规则：`E < τ*`（**幅度**）且 `t ≤ t_min*`（**可信度**）才建议离场；"
        f"两者由**样本内扫描联合选出**——`τ* = {val['tau'] * 100:+.1f}%`、"
        f"`t_min*`：{gate}；样本内/外切分日 {val['cut_date']}。")
    lines.append("  加 t 门槛是必须的：`E` 在 ±0.05% 量级的偏差全是噪声，"
                 "只按 `E<τ` 判卖会把浮盈标的也清掉。")
    b, m = val["oos"]["baseline"], val["oos"]["model"]
    loose = val["oos"].get("model_loose")
    rows = ["| 口径（样本外） | 交易数 | 平均收益 | 中位收益 | 胜率 | 5%分位 | 最差 "
            "| 平均持有 | 提前离场 |",
            "|---|---|---|---|---|---|---|---|---|"]
    pairs = [("基线：固定持有到期", b),
             (f"模型：E<τ 且 t≤{t_min:+.1f}" if t_min is not None
              else "模型：仅按 E<τ", m)]
    if loose and loose.get("n"):
        pairs.append(("对照：仅按 E<τ 离场（无 t 门槛）", loose))
    for tag, d in pairs:
        if not d.get("n"):
            continue
        rows.append(
            f"| {tag} | {d['n']} | {_pct(d['avg_ret'])} | {_pct(d['median_ret'])} "
            f"| {d['win_rate'] * 100:.1f}% | {_pct(d['p05'])} | {_pct(d['worst'])} "
            f"| {d['avg_days']:.2f}日 | {d.get('early_exit_ratio', 0) * 100:.0f}% |")
    lines.append("\n".join(rows))
    lift = val.get("lift", {})
    if lift:
        la, lw, lp = lift.get("avg_ret", 0.0), lift.get("win_rate", 0.0), lift.get("p05", 0.0)
        verdict = [f"平均收益 {la * 100:+.2f}pct", f"胜率 {lw * 100:+.1f}pct",
                   f"5%分位 {lp * 100:+.2f}pct"]
        fired = (m.get("early_exit_ratio") or 0.0) > 0
        if not fired:
            lines.append(f"- ⚠️ **模型在样本外一次都没触发**（提前离场 0%），"
                         f"与基线完全等价。说明 `t_min*={t_min}` 这组门槛在本期数据上"
                         f"不可达（状态桶 t 值整体偏小）。结论：本期模型无增量信息，"
                         f"卖出清单以硬规则（止损/止盈/到期）为主。")
        elif la > 0 and lw >= 0:
            lines.append(f"- ✅ 样本外验证通过：{'，'.join(verdict)}。"
                         "模型具备提前离场的增量价值：收益与下行风险同时改善。")
        elif lp > 0 and la > -0.002:
            # 不再复述一遍三个数字——上面的 ⚠️ 括号里已经写过了。这里只讲判断。
            head = ("收益与风险未同时改善" if la >= 0 else "收益未跑赢基线")
            lines.append(f"- ⚠️ **样本外{head}**（{'，'.join(verdict)}）。"
                         "这个口径的定位是「**风控**」而非「增收」：提前离场换来更浅的"
                         "尾部（5% 分位 +），代价是胜率下降、平均收益基本持平。"
                         "结论：🔴 卖出以**硬规则**（止损/止盈/到期）与达门槛的负期望"
                         "状态为准；模型信号只作风险控制辅助，不作为独立增收依据。")
        else:
            weak = []
            if la <= 0:
                weak.append(f"平均收益 {la * 100:+.2f}pct")
            if lp <= 0:
                weak.append(f"5%分位 {lp * 100:+.2f}pct")
            lines.append(f"- ⚠️ **样本外未能取得一致改善**（{'，'.join(verdict)}）："
                         f"{'、'.join(weak) or '收益与风险'}均未优于基线。"
                         "结论：该状态表暂无独立卖出价值，卖出清单以硬规则为主。")
    return "\n".join(lines) + "\n"


def _evidence_line(iter_info: dict) -> str:
    """证据口径一行：交易日数 / 名义笔数 / 有效样本量 / ICC / DEFF。

    同日推荐同涨同跌，名义笔数不等于独立观测数。只报名义 N 会让人误以为几百笔
    样本足够支撑调权，实际有效样本往往只有十几笔，任何相关性都是噪声。
    """
    deff = float(iter_info.get("deff") or 1.0)
    return (f"\n> **证据口径**：{iter_info['n_days']} 个有效交易日 / 名义 "
            f"{iter_info.get('nominal_samples') or iter_info.get('samples')} 笔 → "
            f"有效样本量 **{iter_info.get('n_eff')}**"
            f"（同日标的收益相关 ICC={iter_info.get('icc')}，设计效应 DEFF={deff}）。"
            f"t 值按有效样本量计算；若只用名义笔数会把 t 值放大约 "
            f"{round(deff ** 0.5, 2)} 倍。")


def iter_detail_md(iter_info: dict, params: dict) -> str:
    """模型自迭代状态：相关性与显著性（让调权可解释）。

    必须同时给出**证据口径**（交易日数 / 有效样本量 / DEFF）。同日推荐同涨同跌，
    名义笔数不等于独立观测数；只报名义 N 会让人误以为几百笔样本足够支撑调权，
    而实际情况往往是有效样本只有十几笔、任何相关性都是噪声。
    """
    lines = [weights_md(params)]
    if not iter_info.get("updated"):
        lines.append(f"- 未更新：{iter_info.get('reason', '无')}")
        if iter_info.get("degenerate_days"):
            lines.append(f"- 已剔除退化日（全天收益恒为 0，属缺失而非观测）："
                         f"{'、'.join(iter_info['degenerate_days'])}")
        if iter_info.get("n_days"):
            lines.append(_evidence_line(iter_info))
        return "\n".join(lines) + "\n"
    corrs = iter_info.get("correlations", {})
    tvals = iter_info.get("tvals", {})
    sig = iter_info.get("significant", {})
    lines.append(f"- 已基于最近 {iter_info['samples']} 笔样本更新权重；"
                 f"各因子收益相关性与统计显著性（t 值，|t|≥2 为显著）：")
    rows = ["| 因子 | 相关性 | t值 | 显著性 |", "|---|---|---|---|"]
    for key in corrs:
        t = tvals.get(key, 0.0)
        mark = "✅ 显著" if sig.get(key) else "⚠️ 不显著"
        rows.append(f"| {key} | {corrs[key]:+.3f} | {t:+.2f} | {mark} |")
    lines.append("\n".join(rows))
    if iter_info.get("n_days"):
        lines.append(_evidence_line(iter_info))
    if iter_info.get("degenerate_days"):
        lines.append(f"> 已剔除退化日（全天收益恒为 0，属缺失而非观测）："
                     f"{'、'.join(iter_info['degenerate_days'])}。")
    return "\n".join(lines) + "\n"


def book_overview_md(book: "dict | None", advice: "list | None") -> str:
    """持仓概览：未平仓数量、浮盈亏分布、已平仓历史统计。"""
    if book is None:
        return "（持仓账本未启用）\n"
    opens = [p for p in book.get("positions", []) if p.get("status") == "open"]
    closed = [p for p in book.get("positions", []) if p.get("status") == "closed"]
    if not opens and not closed:
        return "- 账本为空（首次运行将从最近一次推荐回填）。\n"
    per = {}
    for p in opens:
        per.setdefault(p.get("market"), []).append(p)
    lines = []
    if opens:
        rets = [float(p.get("ret") or 0.0) for p in opens]
        pos = sum(1 for r in rets if r > 0)
        rows = ["| 市场 | 持仓数 | 平均浮动 | 浮盈数 | 明细 |", "|---|---|---|---|---|"]
        for mkt, lst in per.items():
            rr = [float(p.get("ret") or 0.0) for p in lst]
            det = "、".join(f"{p['name']} {_pct(float(p.get('ret') or 0.0))}" for p in lst[:6])
            rows.append(f"| {MARKET_NAMES.get(mkt, mkt)} | {len(lst)} | {_pct(sum(rr) / len(rr))} "
                        f"| {sum(1 for x in rr if x > 0)}/{len(rr)} | {det} |")
        lines.append(f"- 未平仓 **{len(opens)}** 只，平均浮动 **{_pct(sum(rets) / len(rets))}**，"
                     f"浮盈 {pos}/{len(rets)}。")
        lines.append("\n".join(rows))
    if closed:
        rr = [float(p.get("exit_ret") or 0.0) for p in closed]
        reasons = {}
        for p in closed:
            reasons[p.get("exit_reason", "?")] = reasons.get(p.get("exit_reason", "?"), 0) + 1
        rs = "、".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
        lines.append(f"- 已平仓 **{len(closed)}** 笔，平均收益 **{_pct(sum(rr) / len(rr))}**，"
                     f"胜率 {sum(1 for x in rr if x > 0) / len(rr) * 100:.1f}%（{rs}）。")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 长期价值轨渲染
# ---------------------------------------------------------------------------
def macro_env_md(ms: "dict | None") -> str:
    """第八章：宏观环境。给出每一维的数据、得分与依据，缺失项明确标注。"""
    if not ms:
        return "（本次未运行长期轨，或宏观数据获取失败）\n"
    lines = []
    stance = ms.get("equity_stance")
    lines.append(
        f"- 宏观环境评分 **{ms.get('score')}** / 100 → 环境「**{ms.get('label')}**」"
        + (f"，对应建议权益仓位区间 **{stance[0] * 100:.0f}%~{stance[1] * 100:.0f}%**"
           if stance else "")
        + f"；风格倾向：**{'、'.join(ms.get('style_bias') or ['—'])}**。"
        f"（数据截至 {ms.get('date')}）")
    rows = ["| 维度 | 权重 | 得分 | 依据（数据与判断） |", "|---|---|---|---|"]
    for it in ms.get("items") or []:
        rows.append(f"| {it['name']} | {it['weight'] * 100:.0f}% | "
                    f"{it['score'] if it['score'] is not None else '—'} | {it['note']} |")
    lines.append("\n".join(rows))
    if ms.get("missing"):
        lines.append(f"- ⚠️ 缺失维度（**不参与总分**，只标注不猜）："
                     f"{'、'.join(ms['missing'])}。")
    else:
        lines.append("- 五维数据齐全，按完整权重计分。")
    # 原先在这里还列一遍「一手数据：PMI/CPI/M2…」——与上表「依据」列逐字重复，
    # 删掉。口径说明压到一句：说清「这个分数回答什么问题」就够了。
    lines.append("- 口径：宏观分不预测涨跌，只回答「当前环境对长期持有权益资产友好"
                 "到什么程度」；利率维度用 10 年国债 ETF 价格动量（比公布值及时），"
                 "并按该资产自身波动率归一。")
    return "\n".join(lines) + "\n"


def longterm_picks_md(lt: "dict | None", cfg: "dict | None" = None) -> str:
    """第九章：长期价值推荐（股票 + 场外基金 + ETF 配置载体）。"""
    if not lt or not lt.get("enabled"):
        note = (lt or {}).get("note") or (lt or {}).get("error") or "本次未运行长期轨"
        return f"（{note}）\n"
    lines = []
    stats = (lt.get("picks") or {}).get("stats") or {}
    lines.append(f"- 标的池 **{stats.get('universe')}** 只 → 完成深度分析 "
                 f"**{stats.get('analyzed')}** 只（数据不全 {stats.get('failed')} 只），"
                 f"其中达到推荐线 **{stats.get('passed')}** 只。")
    lines.append("- 打分口径：质量 26% / 成长 14% / 现金流 16% / 财务安全 8% / "
                 "估值 24% / 宏观契合 12%（可迭代）。分数为分段线性映射，可逐条回溯。")

    stocks = (lt.get("picks") or {}).get("stocks")
    top_n = int(((cfg or {}).get("longterm", {}) or {}).get("top_stocks", 10))
    if isinstance(stocks, pd.DataFrame) and not stocks.empty:
        d = stocks.head(top_n)
        lines.append("\n**长期价值候选（股票）**\n")
        head = ["代码", "名称", "评分", "现价", "PE(TTM)", "PB分位", "股息率",
                "ROE(3年)", "营收增速", "入选桶"]
        cols = ["code", "name", "score", "price", "pe_ttm", "pb_pct",
                "div_yield", "roe_avg3", "rev_yoy", "bucket"]
        rows = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        for _, r in d.iterrows():
            rows.append("| " + " | ".join([
                str(r.get("code")), str(r.get("name")), _n(r.get("score"), 1),
                _n(r.get("price"), 2), _n(r.get("pe_ttm"), 1),
                (_pct(r.get("pb_pct"), 0) if r.get("pb_pct") is not None else "-"),
                (_n(r.get("div_yield"), 2, "%") if r.get("div_yield") is not None else "-"),
                (_n(r.get("roe_avg3"), 1, "%") if r.get("roe_avg3") is not None else "-"),
                (_n(r.get("rev_yoy"), 1, "%", plus=True)
                 if r.get("rev_yoy") is not None else "-"),
                str(r.get("bucket") or "-"),
            ]) + " |")
        lines.append("\n".join(rows))

        lines.append("\n**同一批标的的六维拆解**（分数由什么构成）\n")
        h2 = ["名称", "质量", "成长", "现金流", "财务安全", "估值", "宏观契合", "风格"]
        c2 = ["v_quality", "v_growth", "v_cashflow", "v_balance",
              "v_valuation", "v_macro", "style"]
        r2 = ["| " + " | ".join(h2) + " |", "|" + "---|" * len(h2)]
        for _, r in d.iterrows():
            r2.append("| " + " | ".join(
                [str(r.get("name"))] + [_n(r.get(c), 1) for c in c2[:-1]]
                + [str(r.get("style") or "-")]) + " |")
        lines.append("\n".join(r2))
        # 逐只列一行「契合当前宏观偏向…」曾占满 10 行、且每行前缀完全相同。
        # 真正有信息量的只有「命中了哪几个标签」，压成一行。
        fits = [(str(r.get("name")), str(r.get("macro_fit") or ""))
                for _, r in d.iterrows() if r.get("macro_fit")]
        if fits:
            bias = "、".join((lt.get("macro") or {}).get("style_bias") or []) or "—"
            hit = "、".join(f"{nm}={fit.split('：')[-1].rstrip('。')}"
                            for nm, fit in fits[:top_n])
            lines.append(f"\n- 宏观偏向 **{bias}**；命中：{hit}。")
        notes = []
        for _, r in d.iterrows():
            nv = r.get("balance_note")
            if nv is None or (isinstance(nv, float) and pd.isna(nv)):
                nv = r.get("cashflow_note")
            if nv is None or (isinstance(nv, float) and pd.isna(nv)):
                continue
            notes.append(f"{r.get('name')}（{nv}）")
        if notes:
            lines.append(f"\n- 口径修正：{'；'.join(notes[:6])}"
                         f"{' 等' if len(notes) > 6 else ''}。")

        # 行业相对位置（中信建投）：回答「是行业整体便宜，还是它自己便宜」。
        # 同一批标的里，PE 低可能是行业普遍低（周期股下行），也可能是公司自身
        # 被错杀——不看行业均值分不清这两件事。
        ir = lt.get("industry_rank") or {}
        if ir:
            lines.append("\n**行业相对位置**（中信建投 · PE 口径；"
                         "区分「行业整体便宜」与「个股自身便宜」）\n")
            # 不再单列「同业家数」——`12/44` 里的分母已经是它。
            h3 = ["名称", "所属行业", "行业排名", "行业均值PE"]
            r3 = ["| " + " | ".join(h3) + " |", "|" + "---|" * len(h3)]
            for _, row in d.iterrows():
                v = ir.get(str(row.get("code")))
                if not v:
                    continue
                r3.append(f"| {row.get('name')} | {v.get('industry') or '-'} | "
                          f"{v.get('rank') or '-'} | "
                          f"{_n(v.get('industry_avg'), 1)} |")
            if len(r3) > 2:
                lines.append("\n".join(r3))
                lines.append("\n- 「行业排名」形如 `12/44` = 该股 PE 在所属行业的升序"
                             "位次（越小越便宜），分母为同业家数。行业分类来自券商"
                             "接口，与申万口径可能不同。")
    else:
        lines.append("\n（本次没有股票达到推荐线）\n")

    funds = (lt.get("picks") or {}).get("funds")
    top_f = int(((cfg or {}).get("longterm", {}) or {}).get("top_funds", 5))
    if isinstance(funds, pd.DataFrame) and not funds.empty:
        lines.append("\n**长期基金候选（场外）**\n")
        # 「近1年」与「年化」讲的是同一件事，去掉前者；年化/波动/回撤/Sharpe 才是选基依据。
        head = ["代码", "名称", "评分", "年化", "年化波动", "最大回撤",
                "Sharpe", "规模(亿)", "规模变化", "经理"]
        rows = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        for _, r in funds.head(top_f).iterrows():
            rows.append("| " + " | ".join([
                str(r.get("code")), str(r.get("name")), _n(r.get("score"), 1),
                _n(r.get("ann_ret"), 1, "%"),
                _n(r.get("ann_vol"), 1, "%"), _n(r.get("max_dd"), 1, "%"),
                _n(r.get("sharpe"), 2), _n(r.get("scale"), 1),
                (_pct(r.get("scale_chg"), 0) if r.get("scale_chg") is not None else "-"),
                str(r.get("manager") or "-"),
            ]) + " |")
        lines.append("\n".join(rows))
        lines.append("- 选基口径：主看「年化收益 ÷ 年化波动」（不看单年度排名，"
                     "高收益常来自押注单一赛道），并对规模大幅缩水扣分。")

    etfs = lt.get("etfs") or []
    if etfs:
        lines.append("\n**ETF 配置载体**（无财报数据，**不参与价值评分**，仅按主题列出）\n")
        # 去掉「20日」：与 60 日同向时无新增信息，背离时又不足以单独下结论，留着只是加宽表。
        head = ["分组", "代码", "名称", "60日"]
        rows = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        for e in etfs:
            rows.append(f"| {e.get('group')} | {e.get('code')} | {e.get('name')} | "
                        f"{_n(e.get('mom_60'), 2, '%', plus=True)} |")
        lines.append("\n".join(rows))

    # 跟踪指数估值（中信建投）：ETF 本身没有 PE/PB（腾讯行情接口对 ETF 该字段为空），
    # 这里给的是**其跟踪指数**的估值与历史分位，用于判断「现在贵不贵」。
    iv = lt.get("index_valuation") or []
    if iv:
        def _fv(x, default=1e9):
            try:
                f = float(str(x).replace("%", "").strip())
            except (TypeError, ValueError):
                return default
            return default if f != f else f

        lines.append("\n**跟踪指数估值**（中信建投 · ETF 无财报与估值字段，"
                     "此处给其跟踪指数的口径）\n")
        low = sorted(iv, key=lambda x: _fv(x.get("pePercentile")))[:12]
        # 分位是窗口统计量：成立才两年的指数，「PE 分位 4%」是在 20 个月样本上算的，
        # 与 20 年窗口的 4% 不是一回事。把年限摆到表里，别让读者自己猜。
        _min_y = float(((cfg or {}).get("datasource") or {}).get("csc_index_min_years", 5))
        # 全收益版与价格版（同一套成分股）估值完全一致，报告里是**同一个敞口**。
        # 一个便宜被数两遍会直接改变排序观感，所以合并成一行、把同源代码并排列出。
        # 注意只合并展示：库里两个代码都在，跟踪产品不同，不能丢。
        _twins = {}

        def _code_txt(x):
            c = str(x.get("securityCode") or x.get("indexCode") or "-")
            sib = [str(s) for s in (x.get("_same_exposure") or [])]
            if sib:
                _twins.setdefault(c, sib)
            return " / ".join([c] + sib)

        def _age_txt(x):
            a = x.get("_age_years")
            if a is None:
                return "-"
            return f"{a:.1f}y" + ("⚠" if a < _min_y else "")

        head = ["指数", "代码", "成立", "PE", "PE分位", "PB", "PB分位", "ROE", "股息率"]
        rows = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        for x in low:
            if x.get("_exposure_primary") is False:
                continue          # 同源对只出代表行，同源代码列在代码格内
            rows.append("| " + " | ".join([
                str(x.get("securityName") or x.get("indexName") or "-"),
                _code_txt(x),
                _age_txt(x),
                _n(x.get("pe"), 1),
                _n(x.get("pePercentile"), 1, "%"),
                _n(x.get("pb"), 2),
                _n(x.get("pbPercentile"), 1, "%"),
                _n(x.get("roe"), 2, "%"),
                _n(x.get("dividendRatio"), 2, "%"),
            ]) + " |")
        lines.append("\n".join(rows))
        lines.append(f"\n- 共 {len(iv)} 个指数，按 **PE 历史分位升序**列最低的 12 个"
                     "（越低=相对自身历史越便宜）。分位区间取决于接口口径，"
                     "不代表绝对低估；**指数低估 ≠ ETF 当下适合买入**。")
        young = [f"{x.get('securityName') or x.get('indexName')}"
                 f"（{x['_age_years']:.1f}y）" for x in low
                 if isinstance(x.get("_age_years"), (int, float))
                 and x["_age_years"] < _min_y]
        diverge = []
        for x in low:
            pe_p, pb_p = _fv(x.get("pePercentile"), None), _fv(x.get("pbPercentile"), None)
            if pe_p is not None and pb_p is not None and abs(pe_p - pb_p) >= 40:
                diverge.append(f"{x.get('securityName') or x.get('indexName')}"
                               f"（PE {_n(pe_p, 1, '%')}/PB {_n(pb_p, 1, '%')}）")
        # 两个风险合并到一行：都是「这个分位不能单独看」的同一件事。
        warn = []
        if young:
            warn.append(f"带 ⚠ 者成立不足 {_min_y:.0f} 年（{'、'.join(young[:3])}"
                        f"{' 等' if len(young) > 3 else ''}），分位样本短、与老指数不可比，"
                        f"排序时会系统性靠前")
        if diverge:
            warn.append(f"PE/PB 分位背离 ≥40pct（{'、'.join(diverge[:3])}"
                        f"{' 等' if len(diverge) > 3 else ''}）：PE 分位低常因盈利在周期"
                        f"高位、资产端并未变便宜，须两个分位一起看")
        if warn:
            lines.append("\n- ⚠️ " + "；".join(warn) + "。")
        if _twins:
            pair = "、".join(f"{k}≡{'/'.join(v)}" for k, v in _twins.items())
            lines.append(f"- 代码以 `/` 并列者为**估值同源**（全收益版/价格版，同成分股"
                         f"同成立日，估值必然相同）：{pair}。跟踪产品不同，两个代码"
                         f"都保留。")
        # 已落库的历史序列及其窗口：窗口是被动降级的结果，必须显式说明，
        # 否则「这条序列覆盖几年」无从判断。
        hist = [(x, x.get("_hist_window")) for x in iv if x.get("_hist_window")]
        if hist:
            lst = "、".join(f"{x.get('securityCode')}({w}/"
                            f"{x.get('_hist_points')}点)" for x, w in hist)
            lines.append(f"- 已落库历史序列：{lst}（窗口=该指数成立年限允许的最长"
                         f"窗口，上游对超长请求返回 rc=102）。")
    return "\n".join(lines) + "\n"


def longterm_track_md(lt: "dict | None") -> str:
    """第十章：长期跟踪与前向验证。"""
    if not lt or not lt.get("enabled"):
        return "（本次未运行长期轨）\n"
    lines = []
    track = lt.get("track") or {}
    summ = track.get("summary") or {}
    if summ:
        lines.append("**已到期里程碑的实际表现**（含基准超额）\n")
        rows = ["| 里程碑 | 样本 | 平均收益 | 胜率 | 平均超额(vs 沪深300ETF) | 超额胜率 |",
                "|---|---|---|---|---|---|"]
        for n, d in sorted(summ.items(), key=lambda kv: int(kv[0])):
            rows.append(f"| {n} 个交易日 | {d['n']} | {_n(d['avg_ret'], 2, '%', plus=True)} | "
                        f"{_n(d['win_rate'], 1, '%')} | "
                        f"{_n(d['avg_excess'], 2, '%', plus=True)} | "
                        f"{_n(d['excess_win'], 1, '%')} |")
        lines.append("\n".join(rows))
        lines.append("- 为什么看超额：长期收益里混着市场 beta，不减基准就分不清"
                     "「选股能力」和「大盘上涨」。")
    else:
        lines.append(f"- 尚无到期的里程碑。{track.get('note', '')}")
        if track.get("batches"):
            lines.append(f"- 已登记 **{track['batches']}** 批长期推荐，"
                         f"从下一交易日起自动结算 5 日里程碑。")

    rep = lt.get("replay")
    if rep and rep.get("milestones"):
        lines.append(f"\n**历史回放检验**（{rep.get('n_symbols')} 只 × "
                     f"{rep.get('n_periods')} 个截面 = {rep.get('n_obs')} 条观测，"
                     f"{rep.get('date_range', ['', ''])[0]} ~ "
                     f"{rep.get('date_range', ['', ''])[1]}）\n")
        rows = ["| 里程碑 | 综合评分 IC | ICIR | t 值 | IC>0 比例 | 解读 |",
                "|---|---|---|---|---|---|"]
        for n, h in sorted(rep["milestones"].items(), key=lambda kv: int(kv[0])):
            d = (h.get("factors") or {}).get("score")
            if not d:
                continue
            t = d.get("t")
            if t is None:
                verdict = "样本不足"
            elif abs(t) >= 2:
                verdict = "✅ 统计显著" + ("（方向为正）" if d["ic_mean"] > 0 else "（方向为负）")
            elif abs(t) >= 1.5:
                verdict = "⚠️ 边缘显著"
            else:
                verdict = "不显著"
            rows.append(f"| {n} 个交易日 | {d['ic_mean']:+.4f} | {d.get('icir')} | "
                        f"{t:+.2f} | {d['positive_rate']:.0%} | {verdict} |")
        lines.append("\n".join(rows))
        lines.append("- 无前视保证：财务只用**公告日 ≤ 时点**的报告期（用 NOTICE_DATE，"
                     "不是报告期截止日）；估值分位只用时点之前的**不复权**价；"
                     "IC 先按交易日分组再算秩相关（不分组会把当日 beta 误当预测力）。"
                     "回放不含宏观维度，权重按比例分给其余五维。")
        best = max(((int(n), (h.get("factors") or {}).get("score") or {})
                    for n, h in rep["milestones"].items()),
                   key=lambda x: abs(x[1].get("t") or 0), default=None)
        if best and abs(best[1].get("t") or 0) >= 2:
            lines.append(f"- 结论：在 **{best[0]} 个交易日**口径上，长期评分与未来收益"
                         f"的相关性统计显著（IC={best[1]['ic_mean']:+.4f}，"
                         f"t={best[1]['t']:+.2f}）。")
    else:
        lines.append("- 历史回放未产出足够样本（需要更多标的或更长 K 线）。")

    sel = (lt.get("selection") or {}).get("long")
    if sel:
        lines.append("\n**长期权重方案选型**\n")
        rows = ["| 方案 | 平均 IC | 平均 ICIR |", "|---|---|---|"]
        for c in sel.get("candidates") or []:
            mark = " ⬅最优" if c["scheme"] == sel.get("best") else ""
            rows.append(f"| {c['scheme']}{mark} | {c['ic_avg']:+.4f} | {c['icir_avg']:+.3f} |")
        lines.append("\n".join(rows))
        # 极差已在 verdict 句子里出现，不再单列一行。
        lines.append(f"- 结论：{sel.get('verdict')}")
    ml = _ml_verdict_line()
    if ml:
        lines.append(ml)
    return "\n".join(lines) + "\n"


def _ml_verdict_line() -> str:
    """走前 ML 验证的一句话结论（数据来自 `ml_eval` 表，由 `--ml-eval` 写入）。

    这里**不**现场跑验证：置换检验一轮要几十秒，而且日线报告每次重跑同一个结论
    没有意义。没跑过就如实说没跑，不假装「已评估」。
    """
    try:
        rows = store.ml_eval_history(20)
    except Exception:
        return ""
    if not rows:
        return ("\n- 走前 ML 验证：本日未运行（`python run.py --ml-eval`）。"
                "ML 层只做样本外验证，不参与生产评分。")
    latest = rows[0]["run_date"]
    day = [r for r in rows if r["run_date"] == latest and r["model"] != "rule"]
    if not day:
        return ""
    passed = any(r.get("gate_passed") for r in day)
    best = max(day, key=lambda r: r.get("ic_mean") or -9)
    verdict = ("**通过门禁，可进入接入验证**" if passed
               else "**未过门禁 → 维持现状**")
    return (f"\n- 走前 ML 验证（{latest}，purge 后的样本外）：最佳候选 "
            f"`{best['model']}` IC={_n(best.get('ic_mean'), 4, plus=True)}"
            f" vs 规则分 {_n(best.get('rule_ic_mean'), 4, plus=True)}，"
            f"样本外 {best.get('n_periods')} 个截面 → {verdict}。"
            f"ML 层只验证不接入，评分口径未变。")


def model_selection_md(sl: "dict | None") -> str:
    """短线模型选型（离场规则组合的 walk-forward 结论）。"""
    if not sl:
        return "（未产出：历史信号样本不足）\n"
    lines = []
    cur, rec = sl.get("current") or {}, sl.get("recommend") or {}

    def _fmt_rule(d):
        st = "不止损" if d.get("stop_loss") is None else f"{d['stop_loss'] * 100:.0f}%"
        tk = "不止盈" if d.get("take_profit") is None else f"{d['take_profit'] * 100:.0f}%"
        return f"持有 {d.get('hold_days')} 日 / 止损 {st} / 止盈 {tk}"

    lines.append(f"- 样本：{sl.get('n_signals')} 笔信号（独立采样口径，用于**相对比较**，"
                 f"不代表实盘组合收益）。")
    rows = ["| | 规则 | 平均收益 | **日均** | 平均持有 | 胜率 | 5%分位 | Sharpe |",
            "|---|---|---|---|---|---|---|---|"]
    cs, rs = sl.get("current_stats") or {}, sl.get("recommend_stats") or {}
    rows.append(f"| 当前生产配置 | {_fmt_rule(cur)} | "
                f"{_n((cs.get('avg') or 0) * 100, 3, '%', plus=True)} | "
                f"**{_n((cs.get('avg_per_day') or 0) * 100, 4, '%', plus=True)}** | "
                f"{_n(cs.get('avg_days'), 1, '日')} | "
                f"{_n((cs.get('win') or 0) * 100, 1, '%')} | "
                f"{_n((cs.get('p05') or 0) * 100, 2, '%')} | {_n(cs.get('sharpe'), 3)} |")
    rows.append(f"| 全样本最优 | {_fmt_rule(rec)} | "
                f"{_n((rs.get('avg') or 0) * 100, 3, '%', plus=True)} | "
                f"**{_n((rs.get('avg_per_day') or 0) * 100, 4, '%', plus=True)}** | "
                f"{_n(rs.get('avg_days'), 1, '日')} | "
                f"{_n((rs.get('win') or 0) * 100, 1, '%')} | "
                f"{_n((rs.get('p05') or 0) * 100, 2, '%')} | {_n(rs.get('sharpe'), 3)} |")
    lines.append("\n".join(rows))
    lines.append("\n> 决策量是**日均收益**而非平均收益：网格里持有期不同（3/5/8 日），"
                 "持有更久的组合天然多吃几天市场漂移，用原始均值比会把「敞口更大」"
                 "误当「策略更优」。同时设尾部守卫——建议参数若把 5% 分位明显拖深，"
                 "一律维持现配置。")

    folds = sl.get("folds") or []
    if folds:
        # 样本内/外的聚合增益不再单列一行——结论句里已有同样两个数字（曾因量化精度
        # 不同显示出 -0.0280 与 -0.0275 两个值）。这里只补一句衰减幅度。
        _decay = sl.get("decay")
        _decay_txt = (f"；样本内优势衰减 {_decay * 100:.0f}%"
                      if _decay is not None else "")
        lines.append(f"\n**锚定式 walk-forward**（前段选优 → 后段验证{_decay_txt}）\n")
        rows = ["| 切分点 | 样本内选出 | 样本内增益 | 样本外增益 | 样本外：选优 / 当前（日均）|",
                "|---|---|---|---|---|"]
        for f in folds:
            gs = (f["is_picked"].get("avg_per_day") or 0) - \
                 (f["is_current"].get("avg_per_day") or 0)
            go = (f["oos_picked"].get("avg_per_day") or 0) - \
                 (f["oos_current"].get("avg_per_day") or 0)
            sl_ = f["picked"]["stop_loss"]
            rule = (f"持有{f['picked']['hold_days']}日/止损"
                    + ("不止损" if sl_ is None else f"{sl_ * 100:.0f}%"))
            rows.append(f"| {f['split'] * 100:.0f}% | {rule} | {gs * 100:+.4f}pct/日 | "
                        f"{go * 100:+.4f}pct/日 | "
                        f"{(f['oos_picked'].get('avg_per_day') or 0) * 100:+.4f}% / "
                        f"{(f['oos_current'].get('avg_per_day') or 0) * 100:+.4f}% |")
        lines.append("\n".join(rows))
    lines.append(f"- 结论：{sl.get('verdict')}")
    return "\n".join(lines) + "\n"



def data_health_md(health: "dict | None") -> str:
    """数据完整性披露（放在报告最前面）。

    为什么必须显式写进报告，而不是只写日志：行情抓取超预算时该市场的标的会被跳过，
    但报告仍会照常生成——曾出现 **cn / etf 两个市场被完全掐掉（评分通过 0 只）、
    而报告看不出任何异常** 的情况。缺数据而不自知，比跑得慢危险得多：
    用户会以为「A股今天没有符合条件的标的」，而不是「今天没取到 A 股数据」。

    除了「缺口」，这里也披露「降级取数」（`_fallbacks`）：财务改用备用源时
    公告日会退化成法定披露截止日，收益率口径没变但**时点口径变了**，
    不写出来就没人知道这批数字的来历与主源不同。

    还有一类必须分开写：**新数据源本轮没取到**（`_missing_sources`，资金流 /
    龙虎榜 / 两融 / 解禁 / 退市清单）。它和上面两类的影响面不同——行情缺口会直接
    影响今天的结论，而这些新表是逐日累积的**因子原料**：少一天不影响今天的操作清单，
    但会让将来的因子检验样本缺一块。混在一起说，要么把「今天照样能操作」说成
    「今天的数据不可信」，要么把「样本永久缺一块」淹没在行情缺口里。
    """
    if not health:
        return ""
    tripped = {k: v for k, v in health.items()
               if isinstance(v, dict) and v.get("tripped")}
    partial = {k: v for k, v in health.items()
               if isinstance(v, dict) and not v.get("tripped")
               and (v.get("missing") or 0) >= 3}
    fallbacks = health.get("_fallbacks") or []
    missing_src = health.get("_missing_sources") or []
    if not tripped and not partial and not fallbacks and not missing_src:
        return ""
    lines = ["> ⚠️ **本轮数据完整性提示**（缺口会直接影响结论，请先看这里）\n>"]
    for mkt, v in tripped.items():
        miss = v.get("missing") or 0
        lines.append(
            f"> - **{MARKET_NAMES.get(mkt, mkt)}**：行情抓取累计 {v.get('spent')}s 已超预算 "
            f"{v.get('budget')}s，剩余标的被跳过"
            + (f"，实际未取到 **{miss} 只**。" if miss else "。"))
    for mkt, v in partial.items():
        lines.append(f"> - **{MARKET_NAMES.get(mkt, mkt)}**："
                     f"{v.get('missing')} 只标的未取到行情。")
    if fallbacks:
        lines.append(f"> - **降级取数 {len(fallbacks)} 笔**（数据可用，但来源口径与主源不同）：")
        grouped = {}
        for f in fallbacks:
            grouped.setdefault(str(f.get("reason") or "未说明"), []).append(
                f"{f.get('market', '?')}/{f.get('code', '?')}")
        for reason, items in grouped.items():
            shown = "、".join(items[:6]) + ("…" if len(items) > 6 else "")
            lines.append(f">   - {reason} —— 共 {len(items)} 只（{shown}）")
    if missing_src:
        grouped = {}
        for m in missing_src:
            grouped.setdefault(str(m.get("reason") or "未说明"), []).append(
                str(m.get("source") or "?"))
        lines.append(f"> - **新数据源本轮未取到 {len(missing_src)} 条**"
                     f"（不影响今日操作，但会让将来的因子样品缺一块）：")
        for reason, items in grouped.items():
            uniq = sorted(set(items))
            shown = "、".join(uniq[:4]) + ("…" if len(uniq) > 4 else "")
            lines.append(f">   - {reason} —— {shown}")
    lines.append(">")
    lines.append("> 要拿到完整数据：确认网络与数据源正常后重跑（必要时加 `--no-cache`），"
                 "或上调 `config.json` 的 `fetch_budget_sec` 用更长的等待换完整样本。")
    return "\n".join(lines) + "\n\n"


BRIEF_BUY_N = 5            # 精简版买入候选条数（分散 3~5 只的纪律上限）
BRIEF_BUY_PER_MARKET = 2   # 同一市场最多几只：全给港股/ETF 就不是分散了
BRIEF_SELL_N = 5
BRIEF_WATCH_N = 3
BRIEF_LT_STOCKS = 3
BRIEF_LT_FUNDS = 2


def _brief_buy_rows(today_picks: dict, per_market: int = BRIEF_BUY_PER_MARKET,
                    top_n: int = BRIEF_BUY_N) -> list:
    """跨市场按评分取买入候选，同市场限流。

    为什么限流：评分最高的前 5 只经常全部来自同一个市场（例如清一色港股 ETF），
    照抄就变成单一敞口，与「分散 3~5 只」的执行纪律相违背。
    """
    pool = []
    for mkt, df in (today_picks or {}).items():
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            try:
                sc = float(r.get("score") or 0)
            except (TypeError, ValueError):
                sc = 0.0
            pool.append((sc, mkt, r))
    pool.sort(key=lambda x: -x[0])
    out, cnt = [], {}
    for sc, mkt, r in pool:
        if cnt.get(mkt, 0) >= per_market:
            continue
        cnt[mkt] = cnt.get(mkt, 0) + 1
        out.append((mkt, r))
        if len(out) >= top_n:
            break
    return out


def brief_action_rows(today_picks: dict, advice: "list | None",
                      per_market: int = BRIEF_BUY_PER_MARKET,
                      top_n: int = BRIEF_BUY_N,
                      sell_n: int = BRIEF_SELL_N,
                      watch_n: int = BRIEF_WATCH_N) -> list:
    """把「买什么 / 卖什么 / 盯什么」压成一张动作表。

    排序：买入按评分降序；卖出按继续持有期望 E 升序（最该先走的排最前）；
    观察按 t 值升序（证据相对最硬的排前）。
    """
    # 买入清单剔除两类：① 已持有 ≥1 日的老仓重复入选（手上本来就有，
    # 再列一次就是「加仓」暗示，而模型并不支持加仓）；② 明确建议卖出的。
    # 今日刚建仓（days_held=0）的保留——它们就是今天的推荐本身；即便离场
    # 模型给了「观察」，那也是买入之后的事，不能反过来把买入吞掉。
    skip = {(str(a.get("market")), str(a.get("code")))
            for a in (advice or [])
            if (a.get("days_held") or 0) > 0 or a.get("advice") == "SELL"}
    rows = []
    for mkt, r in _brief_buy_rows(today_picks, per_market, top_n):
        if (mkt, str(r.get("code"))) in skip:
            continue
        pct = r.get("pct")
        rows.append({
            "act": "买入", "code": str(r.get("code")), "name": str(r.get("name")),
            "market": MARKET_NAMES.get(mkt, mkt),
            "score": _n(r.get("score"), 1),
            "price": _n(r.get("price"), 2),
            "chg": f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "-",
            # 买入行的执行要点对所有标的都相同（纪律行统一说明），
            # 备注列只标「这是新开仓」，避免把同一句话重复 5 遍。
            "note": "新开仓",
        })
    # 卖出/观察只针对「已持有 ≥1 日」的仓位：今天刚买的不该当天就谈离场，
    # 硬规则（止损/止盈/到期）自然会管住它们。
    held = [a for a in (advice or []) if (a.get("days_held") or 0) > 0]
    sells = sorted([a for a in held if a.get("advice") == "SELL"],
                   key=lambda a: a.get("expect_ret") if a.get("expect_ret") is not None else 0)
    for a in sells[:sell_n]:
        rows.append({
            "act": "卖出", "code": str(a.get("code")), "name": str(a.get("name")),
            "market": MARKET_NAMES.get(a.get("market"), a.get("market")),
            "score": _n(a.get("entry_score"), 1),
            "price": _pct(a.get("ret")),
            "chg": f"持{a.get('days_held')}日",
            "note": f"E={_pct(a.get('expect_ret'))}"
                    + (f"（t={a['expect_t']:+.2f}）" if a.get("expect_t") is not None else ""),
        })
    watches = sorted([a for a in held if a.get("advice") == "WATCH"],
                     key=lambda a: a.get("expect_t") if a.get("expect_t") is not None else 0)
    for a in watches[:watch_n]:
        rows.append({
            "act": "观察", "code": str(a.get("code")), "name": str(a.get("name")),
            "market": MARKET_NAMES.get(a.get("market"), a.get("market")),
            "score": _n(a.get("entry_score"), 1),
            "price": _pct(a.get("ret")),
            "chg": f"持{a.get('days_held')}日",
            "note": "盯盘/收紧止损，不构成独立卖出依据",
        })
    return rows


def brief_action_md(rows: "list | None") -> str:
    if not rows:
        return "（今日无可执行的买入/卖出动作）\n"
    head = ["动作", "代码", "名称", "市场", "评分", "现价 / 累计", "今日 / 持有", "备注"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        lines.append("| " + " | ".join([
            r["act"], r["code"], r["name"], str(r["market"]), r["score"],
            r["price"], r["chg"], r["note"]]) + " |")
    return "\n".join(lines) + "\n"


def brief_longterm_md(lt: "dict | None", n_stocks: int = BRIEF_LT_STOCKS,
                      n_funds: int = BRIEF_LT_FUNDS) -> str:
    """长期轨精简版：只留「买什么 + 一句依据 + 超额收益状态」。"""
    if not lt or not lt.get("enabled"):
        return "（本次未运行长期轨）\n"
    lines = []
    ms = lt.get("macro") or {}
    if ms.get("score") is not None:
        stance = ms.get("equity_stance")
        lines.append(
            f"- 宏观：**{ms['score']}** 分「{ms.get('label')}」"
            + (f"，建议权益仓位 {stance[0] * 100:.0f}%~{stance[1] * 100:.0f}%"
               if stance else "")
            + f"；风格偏向 {'、'.join(ms.get('style_bias') or ['—'])}。")
    rows = ["| 类别 | 代码 | 名称 | 评分 | 一句话依据 |", "|---|---|---|---|---|"]
    stocks = (lt.get("picks") or {}).get("stocks")
    if isinstance(stocks, pd.DataFrame) and not stocks.empty:
        for _, r in stocks.head(n_stocks).iterrows():
            bits = []
            if r.get("pe_ttm") is not None:
                bits.append(f"PE {_n(r.get('pe_ttm'), 1)}")
            if r.get("pb_pct") is not None:
                bits.append(f"PB分位 {_pct(r.get('pb_pct'), 0)}")
            if r.get("div_yield") is not None:
                bits.append(f"股息 {_n(r.get('div_yield'), 2, '%')}")
            if r.get("bucket"):
                bits.append(str(r.get("bucket")))
            rows.append("| 股票 | " + " | ".join([
                str(r.get("code")), str(r.get("name")), _n(r.get("score"), 1),
                " · ".join(bits) or "-"]) + " |")
    funds = (lt.get("picks") or {}).get("funds")
    if isinstance(funds, pd.DataFrame) and not funds.empty:
        for _, r in funds.head(n_funds).iterrows():
            bits = []
            if r.get("ann_ret") is not None:
                bits.append(f"年化 {_n(r.get('ann_ret'), 1, '%')}")
            if r.get("ann_vol") is not None:
                bits.append(f"波动 {_n(r.get('ann_vol'), 1, '%')}")
            if r.get("sharpe") is not None:
                bits.append(f"Sharpe {_n(r.get('sharpe'), 2)}")
            rows.append("| 基金 | " + " | ".join([
                str(r.get("code")), str(r.get("name")), _n(r.get("score"), 1),
                " · ".join(bits) or "-"]) + " |")
    if len(rows) > 2:
        lines.append("\n" + "\n".join(rows))
    # 长期只看超额：绝对收益里混着市场 beta，分不清选股与大盘
    summ = ((lt.get("track") or {}).get("summary") or {})
    if summ:
        parts = []
        for n, d in sorted(summ.items(), key=lambda kv: int(kv[0])):
            parts.append(f"{n}日 超额 {_n(d.get('avg_excess'), 2, '%', plus=True)}"
                         f"（胜率 {_n(d.get('excess_win'), 0, '%')}，{d.get('n')} 只）")
        lines.append(f"- **超额收益**（减沪深300ETF）：{'；'.join(parts)}。")
    else:
        tail = f"（已登记 {(lt.get('track') or {}).get('batches')} 批，自动结算里程碑）" \
            if (lt.get("track") or {}).get("batches") else ""
        lines.append(f"- **超额收益**（减沪深300ETF）：尚无到期里程碑{tail}。")
    rep = lt.get("replay") or {}
    d120 = ((rep.get("milestones") or {}).get("120") or {}).get("factors", {}).get("score")
    if d120 and d120.get("t") is not None:
        lines.append(f"- 历史回放 120 日：评分 IC={d120['ic_mean']:+.4f}（t={d120['t']:+.2f}）。")
    return "\n".join(lines) + "\n"


def brief_md(today_picks: dict, ev: dict, iter_info: dict, params: dict,
             exit_advice_list: "list | None", book: "dict | None",
             param_scan: "dict | None", longterm: "dict | None",
             model_selection: "dict | None", run_short: bool, run_long: bool,
             full_name: str = "") -> list:
    """精简版正文：一屏看完「今天要动手的每一件事」+ 两处结论。

    完整版回答「为什么」，精简版只回答「做什么」——证据、因子检验、行业排名、
    指数估值一律留在完整版，避免每天为了 5 个动作读 400 行。
    """
    md = []
    if run_short:
        rows = brief_action_rows(today_picks, exit_advice_list)
        md.append("\n## 今日操作清单\n")
        md.append(brief_action_md(rows))
        if not any(r["act"] == "买入" for r in rows):
            md.append("- 今日无新增买入候选（高评分标的均已在持仓中），只执行下面的动作\n")
        md.append(
            "- 买入纪律：分散 3~5 只、单只仓位 ≤20%；次日开盘买入，"
            "跌破买入价 5% 止损、+8% 止盈、持有 5 日到期（硬规则，离场模型不改）\n"
            "- 卖出/观察只对**已有持仓**生效；不在清单里的持仓按硬规则照常持有\n")
        if "avg_ret" in (ev or {}):
            md.append(
                f"- 昨日推荐（{ev['picks_date']}，{ev['count']} 只）复盘：平均 "
                f"**{_pct(ev['avg_ret'])}**、胜率 **{ev['win_rate'] * 100:.1f}%**"
                f"（最佳 {ev['best']['name']} {_pct(ev['best']['ret'])} / "
                f"最差 {ev['worst']['name']} {_pct(ev['worst']['ret'])}）\n")
        opens = [p for p in ((book or {}).get("positions") or []) if p.get("status") == "open"]
        today = datetime.now().strftime("%Y-%m-%d")
        done = [p for p in ((book or {}).get("positions") or [])
                if p.get("status") == "closed" and p.get("exit_date") == today]
        act = [a for a in (exit_advice_list or [])
               if a.get("advice") in ("SELL", "WATCH") and (a.get("days_held") or 0) > 0]
        n_sell = sum(1 for a in act if a.get("advice") == "SELL")
        n_watch = sum(1 for a in act if a.get("advice") == "WATCH")
        if opens:
            rets = [p.get("ret") for p in opens if p.get("ret") is not None]
            avg = sum(rets) / len(rets) if rets else 0.0
            win = sum(1 for r in rets if r > 0)
            md.append(
                f"- 持仓 **{len(opens)}** 只，平均浮动 **{_pct(avg)}**（浮盈 {win}/{len(opens)}）；"
                f"今日硬规则平仓 {len(done)} 只；明日需行动 {len(act)} 只"
                f"（卖出 {n_sell} / 观察 {n_watch}）\n")
        md.append("\n## 结论（两处）\n")
        if not iter_info.get("updated"):
            md.append(f"- **权重未更新**：{iter_info.get('reason', '无')}"
                      f"（当前版本 `{params.get('version', 'base')}`）\n")
        else:
            md.append(f"- **权重已更新**：基于最近 {iter_info.get('samples')} 笔样本，"
                      f"版本 `{params.get('version', 'base')}`\n")
        if param_scan:
            vs = [v.lstrip("- ").strip() for v in _scan_verdict_lines(param_scan)]
            md.append("- **离场规则**：" + "；".join(vs) + "\n")
        if model_selection and model_selection.get("verdict"):
            md.append(f"- **参数选型**：{model_selection['verdict']}\n")
    if run_long:
        md.append("\n## 长期价值轨\n")
        md.append(brief_longterm_md(longterm))
    if full_name:
        md.append(f"\n> 完整版（因子检验 / 离场规则体检 / 行业排名 / 指数估值）：`{full_name}`\n")
    return md


def generate_report(today_picks: dict, summary: dict, ev: dict,
                    iter_info: dict, params: dict,
                    collinearity: "dict | None" = None,
                    factor_ic: "dict | None" = None,
                    rolling: "dict | None" = None,
                    exit_advice_list: "list | None" = None,
                    exit_model: "dict | None" = None,
                    book: "dict | None" = None,
                    param_scan: "dict | None" = None,
                    longterm: "dict | None" = None,
                    model_selection: "dict | None" = None,
                    cfg: "dict | None" = None,
                    data_health: "dict | None" = None,
                    mode: str = "all",
                    brief: bool = False) -> tuple[str, str]:
    """today_picks: {market: DataFrame}; 返回 (md_path, html_path)

    章节号动态生成：短线轨与长期轨的章节随 mode 增减，不出现空章节。

    brief=True 时：`report_DATE.*` 只放「操作清单 + 结论」，完整证据另存为
    `report_DATE.full.*`——每天只需 5 分钟执行的人不该被迫读 400 行。
    """
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    run_short = mode in ("short", "all")
    run_long = mode in ("long", "all")
    if run_short and run_long:
        title = f"投资助手每日报告 · 短线低吸 + 长期价值 · {date_str}"
    elif run_long:
        title = f"长期价值投资报告 · {date_str}"
    else:
        title = f"波段低买每日报告 · {date_str}"

    ch = [0]

    def h2(name: str) -> str:
        ch[0] += 1
        return f"\n## {_cn(ch[0])}、{name}\n"

    md = [f"# {title}\n", DISCLAIMER]
    _dh = data_health_md(data_health)
    if _dh:
        md.append(_dh)
    if run_short:
        md.append(h2("市场概览"))
        md.append(market_summary_md(summary))
        md.append(h2("昨日推荐复盘与模型迭代"))
        md.append(eval_md(ev))
        md.append("\n**模型自迭代状态**\n\n")
        md.append(iter_detail_md(iter_info, params))
        md.append(h2("因子健康度（共线 + 单期预测力 + 滚动稳定性）"))
        md.append(factor_health_md(collinearity, factor_ic))
        md.append("\n**滚动因子预测力（回测口径，含 ICIR / Rank ICIR）**\n\n")
        md.append(rolling_ic_md(rolling))
        md.append(h2("明日低买候选（按评分排序）"))
        for mkt in ("cn", "etf", "hk", "us", "fund"):
            df = today_picks.get(mkt)
            if df is None:
                continue
            md.append(f"\n### {MARKET_NAMES.get(mkt, mkt)}\n\n")
            md.append(picks_table(df, has_bt=True))
        md.append(h2("卖出建议（硬规则平仓 + 模型离场信号）"))
        md.append(exit_advice_md(exit_advice_list, book, exit_model))
        md.append(h2("持仓账本与离场规则体检"))
        md.append(book_overview_md(book, exit_advice_list))
        md.append("\n**离场规则体检（止损 / 止盈 / 持有期）**\n")
        md.append(parameter_scan_md(param_scan))
        md.append(h2("离场模型（马尔可夫状态 + 期望值函数）"))
        md.append(exit_model_md(exit_model))
        md.append(h2("短线模型选型（walk-forward 参数选择）"))
        md.append(model_selection_md(model_selection))
    if run_long:
        md.append(h2("宏观环境"))
        md.append(macro_env_md((longterm or {}).get("macro")))
        md.append(h2("长期价值推荐（股票 / 基金 / ETF 载体）"))
        md.append(longterm_picks_md(longterm, cfg))
        md.append(h2("长期跟踪与前向验证"))
        md.append(longterm_track_md(longterm))
    md.append(h2("操作说明"))
    # 这一章原本是十几条方法学说明，逐日重复、且 README 里已有一份完整版。
    # 日报只留「今天要不要动手、按什么纪律动手」这一类可执行信息。
    if run_short:
        md.append(
            "**短线低吸轨**\n\n"
            "- 执行纪律：分散 3~5 只、单只仓位 ≤20%；跌破买入价 5% 严格执行止损。"
            "离场模型只调整「观察/减仓/建议卖出」的档位，止损/止盈/到期是硬规则\n"
            "- 口径：六因子加权评分 → 次日开盘买入 → 持有 5 日或触发 ±5%/−8%；"
            "权重自适应要求新样本，无新样本不调权\n")
    if run_long:
        md.append(
            "\n**长期价值轨**\n\n"
            "- 只给「候选与依据」，不设虚拟持仓——长期仓位属你的资产配置决策。"
            "看**超额收益**（减沪深300ETF），里程碑 5/20/60/120/250 个交易日\n"
            "- 口径：宏观 → 标的池 → 六维评分（质量/成长/现金流/财务安全/估值/宏观契合）"
            "→ 跟踪；估值分位只用 PB；金融类负债率与现金流两维取中性分\n")
    md.append(
        "\n**通用**\n\n"
        "- 参数调整一律要求**样本外支持**，不支持的写「维持现配置」；"
        "ML 层只做走前验证，默认**不接入**评分\n"
        "- 完整口径、数据来源与坑位记录见 README；运行快照 state/run_manifest_YYYY-MM-DD.json；"
        "结构化数据 state/quant.db（`python run.py --db-stats`）\n")

    body_full = "\n".join(md)
    md_path = os.path.join(REPORT_DIR, f"report_{date_str}.md")
    html_path = os.path.join(REPORT_DIR, f"report_{date_str}.html")

    if brief:
        # 完整版不丢：同名 + `.full` 后缀，需要查证据/因子检验时有地方翻。
        full_md = os.path.join(REPORT_DIR, f"report_{date_str}.full.md")
        full_html = os.path.join(REPORT_DIR, f"report_{date_str}.full.html")
        with open(full_md, "w", encoding="utf-8") as f:
            f.write(body_full)
        with open(full_html, "w", encoding="utf-8") as f:
            f.write(render_html(f"{title} · 完整版", body_full))
        body = "\n".join(
            [f"# {title} · 精简版\n", DISCLAIMER]
            + ([_dh] if _dh else [])
            + brief_md(today_picks, ev, iter_info, params, exit_advice_list, book,
                       param_scan, longterm, model_selection, run_short, run_long,
                       full_name=os.path.basename(full_html)))
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(body)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(render_html(f"{title} · 精简版", body, brief=True))
        return md_path, html_path

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(body_full)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(title, body_full))
    return md_path, html_path


ACTION_CLASS = {"买入": "buy", "卖出": "sell", "观察": "watch"}


def render_html(title: str, md: str, brief: bool = False) -> str:
    import re
    lines = md.split("\n")
    out, in_table = [], False

    def _cell(c: str) -> str:
        # 动作标签用色块标出：扫一眼就知道哪些行要动手。不用红绿——
        # 红绿在国内语境里是涨跌，动作标签借用它会被读反。
        if brief and c in ACTION_CLASS:
            return f'<td><span class="tag {ACTION_CLASS[c]}">{c}</span></td>'
        return f"<td>{c}</td>"

    for ln in lines:
        if ln.startswith("|"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(_cell(c) for c in cells) + "</tr>")
            continue
        elif in_table:
            out.append("</table>")
            in_table = False
        if ln.startswith("# "):
            out.append(f"<h1>{ln[2:]}</h1>")
        elif ln.startswith("### "):
            out.append(f"<h3>{ln[4:]}</h3>")
        elif ln.startswith("## "):
            out.append(f"<h2>{ln[3:]}</h2>")
        elif ln.startswith("> "):
            out.append(f"<blockquote>{ln[2:]}</blockquote>")
        elif ln.startswith("- "):
            out.append(f"<li>{ln[2:]}</li>")
        elif ln.strip():
            out.append(f"<p>{ln}</p>")
    if in_table:
        out.append("</table>")
    html_body = "\n".join(out)
    html_body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html_body)
    html_body = re.sub(r"`(.+?)`", r"<code>\1</code>", html_body)
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 1080px; margin: 24px auto; padding: 0 20px; color: #1a1a2e;
         background: #fafafa; line-height: 1.65; }}
  h1 {{ border-bottom: 3px solid #c0392b; padding-bottom: 8px; }}
  h2 {{ color: #c0392b; margin-top: 32px; border-left: 4px solid #c0392b; padding-left: 10px; }}
  h3 {{ margin-top: 20px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px;
           background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  th {{ background: #2c3e50; color: #fff; padding: 7px 9px; white-space: nowrap; }}
  td {{ border-bottom: 1px solid #eee; padding: 6px 9px; text-align: center; }}
  tr:hover td {{ background: #fdf3f2; }}
  blockquote {{ background: #fff6f0; border-left: 4px solid #e67e22;
                margin: 12px 0; padding: 10px 14px; font-size: 13px; color: #7a4a12; }}
  li {{ margin: 4px 0; }}
  code {{ background: #eee; padding: 1px 5px; border-radius: 3px; }}
  .tag {{ display: inline-block; padding: 2px 10px; border-radius: 10px;
          font-size: 12px; font-weight: 700; color: #fff; }}
  .tag.buy {{ background: #2d6cdf; }}
  .tag.sell {{ background: #c0392b; }}
  .tag.watch {{ background: #e67e22; }}
</style></head><body>
{html_body}
</body></html>"""
