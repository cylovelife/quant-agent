# -*- coding: utf-8 -*-
"""每日报告生成：Markdown + HTML。"""
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(ROOT, "reports")
os.makedirs(REPORT_DIR, exist_ok=True)

MARKET_NAMES = {"cn": "A股", "etf": "场内ETF", "hk": "港股", "us": "美股", "fund": "场外基金"}

DISCLAIMER = (
    "> ⚠️ **免责声明**：本报告由量化模型自动生成，仅供研究与学习使用，"
    "不构成任何投资建议。历史回测收益不代表未来表现，波段低吸策略存在"
    "接飞刀风险，请严格控制仓位并独立决策。\n"
)

FACTOR_COLS = ["f_boll", "f_rsi", "f_volume", "f_trend", "f_macd", "f_drawdown"]


def _pct(x, digits=2):
    if x is None or (isinstance(x, float) and x != x):
        return "-"
    return f"{x * 100:+.{digits}f}%"


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
    w = params["weights"]
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


def iter_detail_md(iter_info: dict, params: dict) -> str:
    """模型自迭代状态：相关性与显著性（让调权可解释）。"""
    lines = [weights_md(params)]
    if not iter_info.get("updated"):
        lines.append(f"- 未更新：{iter_info.get('reason', '无')}")
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
    return "\n".join(lines) + "\n"


def generate_report(today_picks: dict, summary: dict, ev: dict,
                    iter_info: dict, params: dict,
                    collinearity: "dict | None" = None,
                    factor_ic: "dict | None" = None) -> tuple[str, str]:
    """today_picks: {market: DataFrame}; 返回 (md_path, html_path)"""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    title = f"波段低买每日报告 · {date_str}"

    md = [f"# {title}\n", DISCLAIMER, "\n## 一、市场概览\n",
          market_summary_md(summary), "\n## 二、昨日推荐复盘与模型迭代\n",
          eval_md(ev)]
    md.append("\n**模型自迭代状态**\n\n")
    md.append(iter_detail_md(iter_info, params))

    md.append("\n## 三、因子健康度（共线诊断 + 预测力）\n\n")
    md.append(factor_health_md(collinearity, factor_ic))

    md.append("\n## 四、明日低买候选（按评分排序）\n")
    for mkt in ("cn", "etf", "hk", "us", "fund"):
        df = today_picks.get(mkt)
        if df is None:
            continue
        md.append(f"\n### {MARKET_NAMES.get(mkt, mkt)}\n\n")
        md.append(picks_table(df, has_bt=True))

    md.append("\n## 五、操作说明\n\n"
              "- 评分模型：布林下轨 proximity + RSI 超卖企稳 + 缩量 + 60日线上方保护 + MACD 拐点 + 适度回撤\n"
              "- 回测口径：信号次日开盘买入，持有 5 日或触发止损(-5%)/止盈(+8%)\n"
              "- 回测指标：胜率/场均/信息比率IR/年化收益Calmar（信号数≥5才统计，避免小样本误报）\n"
              "- 因子预测力：截面 IC / Rank IC（评分与回测场均收益的截面相关）\n"
              "- 权重自适应：基于相关性显著性的贝叶斯式更新；因子共线诊断防止权重双重放大\n"
              "- 建议分散至 3~5 只、单只仓位 ≤20%，跌破买入价 5% 严格执行止损\n")

    md_path = os.path.join(REPORT_DIR, f"report_{date_str}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    html_path = os.path.join(REPORT_DIR, f"report_{date_str}.html")
    body = "\n".join(md)
    # 简易 markdown 表格转 HTML（报告只用了标题/表格/列表/引用）
    html = render_html(title, body)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    return md_path, html_path


def render_html(title: str, md: str) -> str:
    import re
    lines = md.split("\n")
    out, in_table = [], False
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
                out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
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
</style></head><body>
{html_body}
</body></html>"""
