"""Shared research-only forecast and value evidence presentation.

@author cylovelife
"""


def _text(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def forecast_table_md(observations: list[dict]) -> str:
    rows = [row for row in observations if isinstance(row.get("forecast"), dict)]
    if not rows:
        return ""
    lines = [
        "收益与波动分别建模，使用前复权收盘日线；均为研究观察，不改变规则评分或仓位。",
        "开发窗使用本次前复权快照；历史复权值可能修订，日期截断不代表恢复了当时可见的数据版本。",
        "收益误差范围来自开发窗残差10%/90%分位，未经独立校准，不是上涨概率或保证覆盖的置信区间。",
        "日波动为未来窗口内单日收益的均方根（RMS），不是年化波动；收益未扣交易成本。",
        "",
        "| 标的 | 数据截至 | 未来根数 | 收益估计 | 收益误差参考范围 | 日波动估计 | 选用模型（收益/波动） |",
        "|---|---|---:|---:|---|---:|---|",
    ]
    for row in rows:
        result = row["forecast"]
        name = _text(f"{row['market']}:{row['code']} {row.get('name') or ''}")
        if result["status"] != "observed":
            lines.append(
                f"| {name} | {result.get('asof', '—')} | {result['horizon']} | — | — | — | "
                f"{_text(result['status'])}：{_text(result.get('reason', ''))} |"
            )
            continue
        low, high = result["return_reference_range"]
        model = "Ridge" if result["return_model"] == "ridge" else "零收益基线"
        risk_model = "Ridge" if result["risk_model"] == "ridge_log_rms" else "历史20根RMS"
        lines.append(
            f"| {name} | {result['asof']} | {result['horizon']} | {result['expected_return']:+.2%} | "
            f"{low:+.2%}～{high:+.2%} | {result['daily_rms']:.2%} | {model}/{risk_model} |"
        )
    lines += [
        "",
        "采用零收益基线表示尚未检出稳定的收益预测增量，并不表示价格不会变动。",
        "模型选择要求各开发窗平均MAE至少改善2%、每窗退化不超过5%；这些窗口参与选型，不是独立确认集。",
        "",
    ]
    return "\n".join(lines)


VALUE_WARNINGS = {
    "financial_sector_metrics_required": "金融类需另核资本/资产质量，中性分不算财务证据",
    "nonpositive_earnings": "利润非正，估值及现金流比值需复核",
    "weak_cash_conversion": "利润现金转化偏弱",
    "earnings_declining": "利润增长为负",
    "high_nonfinancial_leverage": "非金融口径负债率偏高",
    "missing_dimensions": "存在未观测维度",
    "cheapness_requires_quality_review": "低估值需结合盈利质量核查",
}


def value_evidence_md(observations: list[dict]) -> str:
    rows = [row for row in observations if isinstance(row.get("value_evidence"), dict)]
    if not rows:
        return ""
    lines = [
        "\n**长期价值证据与待复核项**\n",
        "覆盖为质量/成长/现金流/财务安全/估值五维的可观测数，不是置信度；规则高分不替代缺失证据。\n",
        "| 标的 | 已观测维度 | 复核状态 | 依据 / 缺口 |",
        "|---|---:|---|---|",
    ]
    labels = {
        "reviewable": "可跟踪复核",
        "needs_review": "需重点复核",
        "insufficient_data": "数据不足",
    }
    for row in rows:
        evidence = row["value_evidence"]
        notes = (
            "；".join(VALUE_WARNINGS.get(w, w) for w in evidence["warnings"])
            or "当前字段未触发上述复核标记"
        )
        lines.append(
            f"| {_text(row.get('name') or row.get('code'))} | {evidence['observed_dimensions']}/5 | "
            f"{labels[evidence['status']]} | {notes} |"
        )
    return "\n".join(lines) + "\n"
