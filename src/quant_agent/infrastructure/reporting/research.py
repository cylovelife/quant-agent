"""Concrete daily observations and reports derived only from frozen research bars.

@author caiyanliangbj
"""

from __future__ import annotations

import math

import pandas as pd

from quant_agent.domain.market.indicators import add_indicators
from quant_agent.domain.research.tracking_types import SIGNALS, STATUS_LABELS
from quant_agent.domain.strategies.short_term import score_row


def _finite(value: float) -> float | None:
    return float(value) if pd.notna(value) and math.isfinite(value) else None


def build_observations(
    frames: dict[tuple[str, str], pd.DataFrame],
    params: dict,
    instrument_metadata: dict | None = None,
) -> list[dict]:
    """Preserve cutoff, price adjustment and scoring conventions of the existing model."""
    observations = []
    metadata = instrument_metadata or {}
    for (market, code), bars in sorted(frames.items()):
        last = bars.iloc[-1]
        close = float(last["close"])

        def change(period: int) -> float | None:
            if len(bars) <= period:
                return None
            return _finite((close / float(bars["close"].iloc[-period - 1]) - 1) * 100)

        row = {
            "market": market,
            "code": code,
            "name": metadata.get("names", {}).get(market, {}).get(code, "名称待核实"),
            "name_source": metadata.get("source", ""),
            "name_verified_on": metadata.get("verified_on", ""),
            "date": str(last["date"])[:10],
            "first_date": str(bars["date"].iloc[0])[:10],
            "bars": len(bars),
            "adjust": "qfq",
            "source": str(last.get("src") or ""),
            "open": float(last["open"]),
            "high": float(last["high"]),
            "low": float(last["low"]),
            "close": close,
            "volume": float(last["volume"]),
            "amount": float(last["amount"]),
            "change_pct": change(1),
            "return_5d_pct": change(5),
            "return_20d_pct": change(20),
            "rsi": None,
            "volume_ratio": None,
            "boll_pb": None,
            "ma60": None,
            "macd_hist": None,
            "macd_hist_prev": None,
            "score": None,
            "factors": {},
            "at_score_threshold": False,
            "note": "预热不足（至少70根）",
        }
        if len(bars) >= 70:
            indicators = add_indicators(bars, params)
            current = indicators.iloc[-1].to_dict()
            current["macd_hist_prev"] = indicators["macd_hist"].iloc[-2]
            scores = score_row(current, params["weights"])
            row.update(
                rsi=_finite(current["rsi"]),
                volume_ratio=_finite(current["vr"]),
                boll_pb=_finite(current["boll_pb"]),
                ma60=_finite(current["ma60"]),
                macd_hist=_finite(current["macd_hist"]),
                macd_hist_prev=_finite(current["macd_hist_prev"]),
                score=float(scores["score"]),
                factors={k: float(v) for k, v in scores.items() if k != "score"},
                at_score_threshold=scores["score"] >= params["score_threshold"],
                note="仅观察池评分，未执行实时流动性粗筛",
            )
        row["analysis"] = explain_observation(row, params)
        observations.append(row)
    return observations


def _number(value: float | None, digits: int = 2, *, percent: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:+.{digits}f}%" if percent else f"{value:.{digits}f}"


def explain_observation(row: dict, params: dict) -> dict:
    """Explain existing rounded factor scores; never introduce a second scoring model."""
    if row["score"] is None:
        return {
            "status": "insufficient_history",
            "factors": [],
            "drivers": [],
            "risks": [],
            "conclusion": "历史日线预热不足，暂不生成评分和研究排序。",
        }
    weights = params["weights"]
    n_volume = params.get("indicators", {}).get("vr_n", 5)
    ma60 = row.get("ma60")
    price_ma = row["close"] / ma60 if ma60 and ma60 > 0 else None
    bases = {
        "boll": f"布林%B={_number(row['boll_pb'], 3)}，越靠近下轨越符合低吸位置偏好。",
        "rsi": f"RSI={_number(row['rsi'], 1)}；当前规则偏好25–40区间，极端超卖也会降低得分。",
        "volume": f"量比={_number(row['volume_ratio'])}（成交量/此前{n_volume}根均量）；当前规则偏好缩量回踩。",
        "trend": f"收盘={_number(row['close'], 3)}，MA60={_number(ma60, 3)}，收盘/MA60={_number(price_ma, 3)}；仅衡量价格趋势保护。",
        "macd": f"MACD柱={_number(row.get('macd_hist'), 6)}，前柱={_number(row.get('macd_hist_prev'), 6)}；比较柱值变化评估动能拐点。",
        "drawdown": f"近20根日线收益={_number(row['return_20d_pct'], percent=True)}；当前规则偏好适度回撤，过浅或过深均降低得分。",
    }
    factors = []
    for key, label in (
        ("boll", "布林位置"),
        ("rsi", "RSI状态"),
        ("volume", "量能"),
        ("trend", "趋势保护"),
        ("macd", "MACD动能"),
        ("drawdown", "回撤幅度"),
    ):
        points = row["factors"]["f_" + key]
        factors.append(
            {
                "key": key,
                "label": label,
                "points": points,
                "weight": weights[key],
                "contribution": points * weights[key],
                "gap": (100 - points) * weights[key],
                "basis": bases[key],
            }
        )
    top = sorted(factors, key=lambda f: -f["contribution"])[:2]
    gaps = sorted(factors, key=lambda f: -f["gap"])[:2]
    drivers = [
        f"{f['label']}贡献{f['contribution']:.2f}分（单项{f['points']:.1f}/100）" for f in top
    ]
    risks = [f"距各项满分的主要差距：{f['label']}{f['gap']:.2f}分" for f in gaps if f["gap"] > 0]
    if price_ma is not None and price_ma < 1:
        risks.append(f"收盘价比MA60低{(1 - price_ma) * 100:.2f}%，仍需观察趋势修复。")
    vr, pct = row.get("volume_ratio"), row.get("change_pct")
    if vr is not None and vr > 1.5:
        movement = (
            "放量上涨"
            if pct is not None and pct > 0
            else ("放量下跌" if pct is not None and pct < 0 else "放量且收盘持平/变化未知")
        )
        risks.append(f"当前为{movement}，量比{vr:.2f}；这与模型的缩量回踩偏好不同。")
    hist, previous = row.get("macd_hist"), row.get("macd_hist_prev")
    if hist is not None and previous is not None and hist < 0:
        risks.append(
            "MACD负柱收窄，动能改善但尚未转正。"
            if hist > previous
            else "MACD负柱仍扩张或持平，不能认定动能已经转强。"
        )
    threshold = params["score_threshold"]
    met = row["score"] >= threshold
    limits = "、".join(f["label"] for f in gaps if f["gap"] > 0) or "尚未完成的样本外验证"
    conclusion = (
        f"{row['score']:.1f}分达到{threshold:.1f}分阈值，列入低吸研究观察；"
        f"优先核查{limits}条件及后续价格/量能确认，不能据此认定反弹已成立。"
        if met
        else f"{row['score']:.1f}分低于{threshold:.1f}分阈值，当前不列入本模型优先低吸观察；"
        f"主要受{limits}约束。低分只表示技术形态与当前策略不匹配，不是基本面或长期价值评价。"
    )
    return {
        "status": "threshold_met" if met else "below_threshold",
        "factors": factors,
        "drivers": drivers,
        "risks": risks,
        "conclusion": conclusion,
    }


def _text(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("`", "")


def build_research_summary(record: dict, max_per_market: int = 3) -> dict:
    """Compress the existing score ordering; confirmation checks remain unvalidated research hypotheses."""
    if max_per_market < 1:
        raise ValueError("max_per_market must be positive")
    groups = {"etf": [], "cn": []}
    if record["status"] == "data_blocked":
        return {"groups": groups, "max_per_market": max_per_market}
    for market in groups:
        candidates = sorted(
            (
                r
                for r in record.get("observations", [])
                if r["market"] == market and r["score"] is not None and r["at_score_threshold"]
            ),
            key=lambda r: (-r["score"], r["code"]),
        )
        for priority, row in enumerate(candidates[:max_per_market], 1):
            analysis = row["analysis"]
            risks = [r for r in analysis["risks"] if not r.startswith("距各项满分")]
            if not risks:
                risks = analysis["risks"][:2] or ["尚未证明具有稳定的样本外收益优势。"]
            signals = [
                f"价格：后续收盘突破观察日高点{row['high']:.3f}，并守住低点{row['low']:.3f}（尚待后续完整日线）",
            ]
            hist = row.get("macd_hist")
            if hist is not None:
                signals.append(
                    "动能：后续MACD负柱继续收窄并转正"
                    if hist < 0
                    else "动能：后续MACD保持正柱、避免重新转负"
                )
            if row.get("ma60") is not None:
                position = "站回" if row["close"] < row["ma60"] else "保持在"
                signals.append(f"趋势：后续{position}动态MA60之上（现值{row['ma60']:.3f}）")
            signals.append("量能：若回踩，跟踪量比回落到现模型偏好的0.4–0.9，且低点不失守")
            groups[market].append(
                {
                    "priority": priority,
                    "market": market,
                    "code": row["code"],
                    "name": row["name"],
                    "score": row["score"],
                    "reason": f"低吸评分{row['score']:.1f}达标；RSI {_number(row['rsi'], 1)}、"
                    f"布林%B {_number(row['boll_pb'], 3)}、近20日{_number(row['return_20d_pct'], percent=True)}。",
                    "risks": risks[:3],
                    "confirmation_signals": signals,
                }
            )
    return {"groups": groups, "max_per_market": max_per_market}


def render_report(record: dict) -> str:
    """Self-contained reading view: priorities, risks, pending checks and compact facts."""
    observations = record.get("observations", [])
    summary = record.get("research_summary") or build_research_summary(record)
    stamp = record.get("checked_at") or record["created_at"]
    threshold = record.get("parameters", {}).get("score_threshold")
    details_path = record.get("daily_details_path") or record.get("details_path")
    lines = [
        f"# 每日研究报告 · {stamp[:10]}",
        "",
        f"报告生成：{stamp}；行情交易日：{record['data_date'] or '无有效行情'}。",
        "价格来自冻结快照中的前复权收盘日线，不是实时报价。",
        "",
        "## 结论汇总",
        "",
        f"研究优先级采用现有低吸评分：股票与ETF分别降序排列，仅展示达到{_number(threshold, 1)}分阈值的标的，"
        f"每类最多{summary['max_per_market']}只，不凑数。相同评分按代码排列。",
        "这是固定观察池的研究顺序，不代表全市场排名或预期收益排序。",
        "",
        "后续确认信号是待复核的技术观察条件，尚未验证为交易规则；价格、动能、趋势与回踩量能分别跟踪。",
        "",
    ]
    if record["status"] == "data_blocked":
        lines += ["**数据校验未通过，本次不生成优先关注清单。**", ""]
    contract = record.get("data_contract")
    if contract:
        admitted = sum(
            item.get("new_signal_allowed", False) for item in contract["series"].values()
        )
        lines += [
            f"数据契约：{admitted} 个已加载标的满足本次新增模拟信号的数据条件；"
            "其余只保留历史观察或待核验状态。",
            "抓取来源与时间随冻结快照留档；发布时间、历史成分／退市／停牌覆盖不完整，"
            "快照完整性通过不等于完整历史当时可见数据。",
            "",
        ]
    for market, label in (("etf", "ETF优先关注"), ("cn", "股票优先关注")):
        lines += [
            f"### {label}",
            "",
            "| 研究优先级 | 标的 / 评分 | 关注理由 | 主要风险 | 后续确认信号 |",
            "|---|---|---|---|---|",
        ]
        selected = summary["groups"][market]
        for item in selected:
            lines.append(
                f"| {item['priority']} | {_text(item['name'])}（{item['code']}） / {item['score']:.1f} | "
                f"{_text(item['reason'])} | {_text('；'.join(r.rstrip('。；') for r in item['risks']))} | "
                f"{_text('；'.join(item['confirmation_signals']))} |"
            )
        if not selected:
            lines += [
                "",
                "当前没有可列入本类优先关注的标的；低分或预热不足均不强行入选。",
            ]
        lines.append("")
    lines.extend(render_tracking(record))
    metrics = record.get("metrics")
    if metrics:
        rank = metrics.get("factors", {}).get("score", {}).get("rank_ic") or {}
        lines += [
            f"现有综合评分的历史 Rank IC={_number(rank.get('mean'), 4)}、"
            f"Rank ICIR={_number(rank.get('icir'), 3)}，仅为描述性历史体检。",
            "上述关注名单仍处于观察阶段，尚无通过样本外等准入门禁的新增结论。",
            "",
        ]
    else:
        lines += ["历史有效截面不足，尚未形成可评估的IC序列。", ""]
    lines += [
        "## 逐标的行情与观察池评分",
        "",
        "涨跌幅按相邻收盘价计算；近5日/近20日按交易记录根数计算。未进入本页优先清单的标的也保留在下表。",
        "",
        "| 市场 | 代码 | 名称 | 评分 | 收盘价 | 当日涨跌 | 近5日 | 近20日 | RSI | 量比 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in sorted(
        observations,
        key=lambda r: (
            r["market"],
            -(r["score"] if r["score"] is not None else -1),
            r["code"],
        ),
    ):
        score = _number(row["score"], 1) if row["score"] is not None else "预热不足"
        lines.append(
            f"| {row['market']} | {row['code']} | {_text(row['name'])} | {score} | {_number(row['close'], 3)} | "
            f"{_number(row['change_pct'], percent=True)} | {_number(row['return_5d_pct'], percent=True)} | "
            f"{_number(row['return_20d_pct'], percent=True)} | {_number(row['rsi'], 1)} | {_number(row['volume_ratio'])} |"
        )
    lines += [
        "",
        f"行情覆盖：{len(observations)}只，共{sum(r['bars'] for r in observations)}根日线；"
        f"不同交易日观察次数{record['evidence_days']}（不是独立统计样本量）。",
        "评分只衡量当前低吸模型的技术适配度，低分不等于基本面或长期价值差。",
        "",
    ]
    for kind in ("missing", "invalid", "stale", "lagging", "regressed"):
        if record["quality"][kind]:
            lines.append(f"- 数据提示 {kind}: {', '.join(record['quality'][kind])}")
    if details_path:
        lines += [
            "",
            f"[查看逐标的评分依据、完整风险说明与数据来源]({details_path})",
            "",
        ]
    return "\n".join(lines)


def render_tracking(record: dict, *, detailed: bool = False) -> list[str]:
    """Show actual recorded checks; never derive old priorities from today's prices."""
    tracking = record.get("tracking")
    if not tracking:
        return []
    changes = tracking["priority_changes"]
    lines = ["## 相比上一交易日的研究优先级变化", "", changes["note"], ""]
    if changes["available"]:
        lines += [
            f"对比交易日：{changes['previous_date']}；基准留档时间：{changes['baseline_registered_at']}。",
            "",
            "| 标的 | 上次优先级 | 本次优先级 | 变化 | 评分变化 |",
            "|---|---:|---:|---|---:|",
        ]
        labels = {
            "up": "上升",
            "down": "下降",
            "entered": "新增关注",
            "exited": "退出关注",
            "unchanged": "持平",
            "unavailable": "数据缺失，暂不可比较",
        }
        for row in changes["rows"]:
            delta = f"{row['score_delta']:+.1f}" if row["score_delta"] is not None else "—"
            lines.append(
                f"| {_text(row['name'])}（{row['market']}:{row['code']}） | "
                f"{row['previous_priority'] or '—'} | {row['current_priority'] or '—'} | "
                f"{labels[row['status']]} | {delta} |"
            )
        if changes["method_changed"]:
            lines += ["", "参数口径已变化，上述排名变化不能全部归因于行情变化。"]
        lines.append("")
    counts = {key: 0 for key in STATUS_LABELS}
    for watch in tracking["watches"]:
        for value in watch["evaluation"]["signals"].values():
            counts[value["status"]] += 1
    lines += [
        "## 确认信号逐日跟踪",
        "",
        "原观察高低点固定，登记后才核验。已满足表示最近适用日条件成立；已失效表示曾满足后回落，或原低点失守使整轮失效。",
        "量能只核对回踩日（收盘≤前收），其他日沿用最近适用日结果；缺行情或历史价格／复权修订暂停核验。",
        "成交量修订只影响量能：与取整兼容时按新旧输入区间核对，跨阈值或实质修订仍暂不可判定；不阻断可信的低点失守检查。成交额变化留痕但不参与四项条件。",
        "",
        "本次核验："
        + "，".join(f"{STATUS_LABELS[key]}{count}项" for key, count in counts.items())
        + "。",
        "",
        "| 标的 | 原观察日 / 登记日 | 价格突破 | MACD | MA60 | 回踩量能 | 观察说明 |",
        "|---|---|---|---|---|---|---|",
    ]
    for watch in tracking["watches"]:
        values = []
        for key in SIGNALS:
            signal = watch["evaluation"]["signals"][key]
            text = STATUS_LABELS[signal["status"]]
            if signal["checked_on"]:
                text += f"（核验{signal['checked_on']}）"
            if signal["first_met_on"]:
                text += f"（首次{signal['first_met_on']}）"
            if signal["invalidated_on"]:
                text += f"（失效{signal['invalidated_on']}）"
            values.append(text)
        note = ("已退出本轮优先清单；" if watch["closed_date"] else "") + watch["evaluation"][
            "note"
        ]
        lines.append(
            f"| {_text(watch['name'])}（{watch['code']}） | {watch['origin_date']} / {watch['registered_at'][:10]} | "
            + " | ".join(values)
            + f" | {_text(note)} |"
        )
    if detailed:
        for watch in tracking["watches"]:
            lines += [
                "",
                f"### 跟踪依据：{_text(watch['name'])}（{watch['code']}）",
                "",
                f"观察id：`{watch['watch_id']}`；原高点{watch['high']:.3f} / 低点{watch['low']:.3f}；"
                f"只核验{watch['eligible_after']}之后的完整日线。",
                "",
            ]
            for key, signal in watch["evaluation"]["signals"].items():
                lines.append(
                    f"- {SIGNALS[key]}：{STATUS_LABELS[signal['status']]}；"
                    f"最近适用日{signal['checked_on'] or '尚无'}；{signal['evidence']}"
                )
            for field, revision in watch["evaluation"].get("revision_review", {}).items():
                lines.append(
                    f"- 历史输入修订 {field}：{revision['changed_rows']}根；"
                    f"最大绝对差{_number(revision['max_absolute_change'], 9)}"
                    + (
                        f"；分类{revision['classification']}"
                        if "classification" in revision
                        else ""
                    )
                    + "。"
                )
            lines.append("")
    if not tracking["watches"]:
        lines += ["", "本次没有可推进的观察记录。"]
    lines.append("")
    return lines


def render_details(record: dict) -> str:
    """Render visible market facts before evaluation metadata and provenance."""
    observations = record.get("observations", [])
    metrics = record.get("metrics")
    parameters = record.get("parameters", {})
    threshold = parameters.get("score_threshold")
    stamp = record.get("checked_at") or record["created_at"]
    lines = [
        f"# 每日研究明细 · {stamp[:10]}",
        "",
        f"[返回主报告]({record.get('daily_report_path') or record['report_path']})",
        "",
        f"报告生成：{stamp}；行情交易日：{record['data_date'] or '无有效行情'}。",
        "下列价格均为冻结快照中的前复权日线收盘价，不是实时报价。",
        f"有效观察标的 {len(observations)} 只；"
        f"达到评分阈值 {_number(threshold, 1)} 的 {sum(r['at_score_threshold'] for r in observations)} 只。",
        "评分排序仅覆盖固定观察池，未执行实时流动性粗筛，不等同于正式买入清单。",
        "",
        "## 逐标的行情",
        "",
        "涨跌幅按相邻两根日线收盘价计算；近5日/近20日按交易记录根数计算。",
        "",
        "| 市场 | 代码 | 名称 | 行情日期 | 收盘价 | 当日涨跌 | 近5日 | 近20日 | 成交额(亿元) |",
        "|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    market_names = {"cn": "A股", "etf": "ETF"}
    for row in observations:
        lines.append(
            f"| {market_names.get(row['market'], row['market'])} | {row['code']} | {_text(row['name'])} | {row['date']} | "
            f"{_number(row['close'], 3)} | {_number(row['change_pct'], percent=True)} | "
            f"{_number(row['return_5d_pct'], percent=True)} | {_number(row['return_20d_pct'], percent=True)} | "
            f"{_number(row['amount'] / 1e8)} |"
        )
    if not observations:
        lines += ["", "没有通过数据质量检查的行情，不能展示价格和评分。"]
    lines += [
        "",
        "## 观察池评分与指标",
        "",
        "| 代码 | 名称 | 评分 | RSI | 量比 | 布林%B | 达到评分阈值 |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    ranked = sorted(
        observations,
        key=lambda r: (
            -(r["score"] if r["score"] is not None else -1),
            r["market"],
            r["code"],
        ),
    )
    for row in ranked:
        decision = (
            ("是（研究观察）" if row["at_score_threshold"] else "否")
            if row["score"] is not None
            else "预热不足"
        )
        lines.append(
            f"| {row['code']} | {_text(row['name'])} | {_number(row['score'], 1)} | {_number(row['rsi'], 1)} | "
            f"{_number(row['volume_ratio'])} | {_number(row['boll_pb'], 3)} | {decision} |"
        )
    lines += [
        "",
        "## 逐标的评分依据与研究结论",
        "",
        "总分是现有低吸策略的适配分，不是上涨概率、目标收益或综合估值。",
        "总分=各项原始得分×参数权重之和；下表单项分已四舍五入，展示贡献相加可能与总分末位有差异。",
        "评分和技术结论使用同一冻结快照；名称只作识别标签，不参与评分。",
        "",
    ]
    for row in ranked:
        analysis = row["analysis"]
        lines += [f"### {_text(row['name'])}（{row['market']}:{row['code']}）", ""]
        if analysis["factors"]:
            lines += [
                "| 因子 | 指标依据 | 单项分/100 | 权重 | 加权贡献 |",
                "|---|---|---:|---:|---:|",
            ]
            for factor in analysis["factors"]:
                lines.append(
                    f"| {factor['label']} | {_text(factor['basis'])} | "
                    f"{factor['points']:.1f} | {factor['weight'] * 100:.2f}% | "
                    f"{factor['contribution']:.2f} |"
                )
            lines += ["", "主要贡献：" + "；".join(analysis["drivers"]) + "。", ""]
            for risk in analysis["risks"]:
                lines.append(f"- {risk}")
            lines.append("")
        lines += ["研究结论：" + analysis["conclusion"], ""]
    lines += ["## 历史因子体检", ""]
    if metrics:
        lines += [
            f"评估面板：{metrics['n_symbols']} 只、{metrics['n_obs']} 行、"
            f"{metrics['n_periods']} 个市场/日期截面；信号日期 "
            f"{metrics['date_range'][0]} 至 {metrics['date_range'][1]}，持有期 {metrics['horizon']} 根日线。",
            "",
            "| 因子 | 历史 Rank IC | Rank ICIR | 截面数 |",
            "|---|---:|---:|---:|",
        ]
        for name, values in metrics["factors"].items():
            rank = values.get("rank_ic") or {}
            lines.append(
                f"| {name} | {_number(rank.get('mean'), 4)} | "
                f"{_number(rank.get('icir'), 3)} | {rank.get('n', 0)} |"
            )
    else:
        lines.append("有效截面不足，未形成可评估的 IC 序列。")
    lines += [
        "",
        "以上是描述性历史体检。固定小观察池存在选择/幸存者偏差，前复权为当前版本；",
        "重叠收益窗口不能当作独立样本。未通过 OOS、FDR、置换、边际增量和影子准入验证，",
        "因此 promotion_allowed=false，不修改生产参数。",
        "",
        "## 行情覆盖与数据质量",
        "",
        "| 市场/代码 | 名称 | 日线根数 | 起始日期 | 最新日期 | 来源 |",
        "|---|---|---:|---|---|---|",
    ]
    for row in observations:
        lines.append(
            f"| {row['market']}:{row['code']} | {_text(row['name'])} | {row['bars']} | {row['first_date']} | "
            f"{row['date']} | {row['source']} |"
        )
    metadata = record.get("instrument_metadata", {})
    source = _text(metadata.get("source") or "未配置")
    source_label = f"[名称来源]({source})" if source.startswith("https://") else source
    lines += [
        "",
        f"名称核对日期：{metadata.get('verified_on') or '未核对'}；来源：{source_label}。",
        "名称是核对日的展示标签，未声称具备历史更名的点时数据。",
        "",
    ]
    for kind in ("missing", "invalid", "stale", "lagging", "regressed"):
        lines.append(f"- {kind}: {', '.join(record['quality'][kind]) or '无'}")
    lines += [
        "",
        f"运行状态：{record['status']}；变化：{record['change']}。",
        f"不同交易日观察次数：{record['evidence_days']}（不是独立统计样本量）。",
        f"输入指纹：`{record['run_id']}`",
        f"冻结数据：`{record['snapshot_path']}`",
        "",
    ]
    lines.extend(render_tracking(record, detailed=True))
    return "\n".join(lines)
