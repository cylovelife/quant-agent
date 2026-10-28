# -*- coding: utf-8 -*-
"""走前（walk-forward）机器学习信号验证层。

定位：**验证器，不是预测器**
---------------------------
本项目已经有一条可信的尺子：`value_track.cross_section_ic`——先按交易日分组算
截面秩相关，再对 IC 序列求均值 / ICIR / t。任何新信号要谈「更好」，必须用
**同一把尺子**量，否则「更好」只是口径差异。所以本模块只做一件事：把模型的
样本外预测值当成一个**候选因子**，塞回同一套评估里，与现有规则分 `score`
同台对比。

为什么必须走前（expanding window）
--------------------------------
时序数据上任何随机切分的交叉验证都会泄漏：训练集里含未来日期的样本，模型学到的
是「那段时间涨了什么」。只有「只用 t 之前的数据拟合、预测 t」才与实盘时点一致。
本模块的实现严格以 `date < t` 划训练集，且**同一天的横截面不参与当日预测的训练**
——同一天的行共享市场 beta，放进去等于把答案的一半提前交给模型。

为什么默认**不**接入评分
----------------------
长期轨的 120 日 IC=+0.155（t=5.24）是在「规则分」这一输入下验证出来的。把 ML
分数直接塞进六维评分，会让已成立的结论失效、收益无法归因。所以：
- 默认只输出对比结论（`run.py --ml-eval`），不动生产评分；
- 只有通过下面三重门禁，结论里才会出现「建议采用」。

这与短线选型层踩过的坑一致：那边样本内优势 198% 过拟合、样本外 −0.220pct。
「加上去看着更全」不是理由，样本外能站住才是。

研究输出只作为观察，不自动接入生产评分。候选先在验证段选取，
固定测试段只评价该候选和规则基准；跨实验封存、晋级及搜索登记属于后续任务。
实际标签结束日用于清除，按交易日数折算的日历跨度只作为展示参考。
"""

import re

import numpy as np
import pandas as pd

# 长期轨六维（与 value_track.FACTORS_CORE 同源；这里显式列出以便特征与评分解耦）
FEATURES = ["v_quality", "v_growth", "v_cashflow", "v_balance", "v_valuation"]
DEFAULT_HORIZON = 120
MIN_OOF_PERIODS = 8

# 仅作为展示参考；训练样本的成熟时点以逐行实际标签结束日为准。
_TRADING_DAYS_PER_YEAR = 252.0
DEFAULT_EMBARGO_PERIODS = 1
DEFAULT_N_PERM = 20


def horizon_calendar_days(target: str, horizon_days=None) -> int:
    """把「持有 N 个交易日」折算成日历天数。

    从目标列名（`ret_120`）推断，或由调用方显式给出。该估算仅供展示，
    不得代替真实交易日和每行标签结束日。
    """
    if horizon_days:
        return int(horizon_days)
    m = re.search(r"(\d+)\s*$", str(target))
    n = int(m.group(1)) if m else 0
    return int(round(n * 365.0 / _TRADING_DAYS_PER_YEAR))


def _model(kind: str = "ridge", seed: int = 0):
    """候选模型。

    刻意保留一个线性基线：几百个样本上 GBDT 很容易把噪声当结构，没有线性基线
    对照就无法区分「非线性真的有用」和「只是过拟合得更花哨」。
    """
    if kind == "ridge":
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        return make_pipeline(StandardScaler(), Ridge(alpha=1.0))
    if kind == "gbr":
        from sklearn.ensemble import HistGradientBoostingRegressor

        return HistGradientBoostingRegressor(
            max_depth=3,
            max_iter=120,
            learning_rate=0.06,
            min_samples_leaf=20,
            l2_regularization=1.0,
            random_state=seed,
        )
    raise ValueError(f"未知模型: {kind}")


def _fit_predict(model, xtr: pd.DataFrame, ytr: pd.Series, xte: pd.DataFrame):
    """拟合 + 预测。缺失值用**训练集**中位数填充。

    用全样本中位数填充就是泄漏：那一刻的中位数里含未来信息。这是很容易忽略的
    一类泄漏——它不在 y 上做手脚，而是在预处理里把未来带了进来。
    """
    xtr, xte = xtr.astype(float).copy(), xte.astype(float).copy()
    if xtr.isna().to_numpy().any() or xte.isna().to_numpy().any():
        med = xtr.median()
        med = med.where(med.notna(), 0.0)
        xtr = xtr.fillna(med)
        xte = xte.fillna(med)
    model.fit(xtr, ytr)
    return model.predict(xte)


def walk_forward_predict(
    panel: pd.DataFrame,
    features=None,
    target: str = "ret_120",
    kind: str = "ridge",
    min_train_periods: int = 6,
    min_train_rows: int = 30,
    seed: int = 0,
    purge: bool = True,
    embargo_periods: int = None,
    horizon_days=None,
    diag: dict = None,
) -> pd.Series:
    """expanding-window 走前预测，返回与 panel 同索引的样本外预测序列。

    样本外 = 该截面从未参与任何一次拟合。未产生预测的位置保持 NaN（不是 0，
    也不是「用规则分代替」——那会让对比失去意义）。

    **purge 是必需的，不是保守起见**
    -------------------------------
    只按信号日切分还不够。前向收益是**重叠窗口**：实测截面间隔中位 22 天、
    持有 120 交易日（≈174 日历日），则相邻截面的收益窗口重叠约 87%。若把
    t−1 的 (特征, 收益) 放进训练集去预测 t，模型等于提前看过同一段行情，
    样本外 IC 会被系统性高估。所以训练样本必须满足「其收益窗口已在测试日
    之前完全结束」：实际标签结束日须严格早于预测日期。

    embargo 再往前推若干截面，切断「相邻截面特征几乎相同」带来的隐性重叠。
    A 股财务六维在相邻横截面间变化很小（实测 v_quality 相邻 |Δ| 中位 0），
    这条尤其重要。
    """
    features = list(features or FEATURES)
    out = pd.Series(np.nan, index=panel.index if panel is not None else [], dtype=float)
    if panel is None or panel.empty or target not in panel.columns:
        return out
    dstr = panel["date"].astype(str)
    dates = sorted(dstr.unique())
    purge_days = horizon_calendar_days(target, horizon_days) if purge else 0
    emb = DEFAULT_EMBARGO_PERIODS if embargo_periods is None else int(embargo_periods)
    end_col = f"label_end_{target}"
    if purge and end_col not in panel:
        if diag is not None:
            diag.update(
                {
                    "purge": True,
                    "label_end_status": "missing",
                    "purge_days": purge_days,
                    "embargo_periods": emb,
                    "first_test_date": None,
                }
            )
        return out
    ends = pd.to_datetime(panel[end_col], errors="coerce") if purge else None
    starts = pd.to_datetime(dstr, errors="coerce") if purge else None
    first_test, first_train_max, last_train_max = None, None, None
    for i, d in enumerate(dates):
        if i < int(min_train_periods):
            continue
        te = panel[dstr == d]
        if te.empty:
            continue
        if purge:
            tr = panel[
                (dstr < d)
                & ends.notna()
                & starts.notna()
                & (ends >= starts)
                & (ends < pd.Timestamp(d))
            ]
        else:
            tr = panel[dstr < d]
        tr = tr.dropna(subset=[target])
        # embargo：丢掉训练集里最近 emb 个截面
        if emb and not tr.empty:
            tdates = sorted(tr["date"].astype(str).unique())
            drop = set(tdates[-emb:])
            tr = tr[~tr["date"].astype(str).isin(drop)]
        if len(tr) < int(min_train_rows):
            continue
        try:
            pred = _fit_predict(
                _model(kind, seed), tr[features], tr[target].astype(float), te[features]
            )
        except Exception:
            continue
        out.loc[te.index] = pred
        tmax = str(tr["date"].astype(str).max())
        if first_test is None:
            first_test, first_train_max = d, tmax
        last_train_max = tmax
    if diag is not None:
        # 两个训练上界分开记：只有「首次预测时的训练上界」能用来核对 purge 契约，
        # 用最后一次迭代的值去核对会得到「训练日期晚于测试日期」的假失败。
        diag.update(
            {
                "purge": bool(purge),
                "purge_days": purge_days,
                "label_end_status": "actual" if purge else "disabled",
                "embargo_periods": emb,
                "first_test_date": first_test,
                "first_train_max": first_train_max,
                "last_train_max": last_train_max,
            }
        )
    return out


def _ic_series(panel: pd.DataFrame, col: str, ret_col: str, min_n: int = 5) -> list:
    """逐交易日的截面秩相关序列。

    相关原语直接复用 `value_track._rank_corr`，不另写一份——同一把尺子的前提是
    连最底层的一致性计算都只有一处实现。
    """
    import quant_agent.domain.research.statistics as vtrack

    ics = []
    for _, g in panel.groupby("date"):
        sub = g[[col, ret_col]].dropna()
        if len(sub) < min_n:
            continue
        if sub[col].nunique() < 3 or sub[ret_col].nunique() < 3:
            continue
        r = vtrack._rank_corr(sub[col].to_numpy(), sub[ret_col].to_numpy())
        if r is not None and np.isfinite(r):
            ics.append(float(r))
    return ics


def _trimmed_mean(ics: list, trim: float = 0.10):
    """去掉 |IC| 最大的 `trim` 比例截面后的均值。

    优势若只来自少数极端交易日，去掉它们就会塌掉。这是选型层 TAIL_TOL 的同款
    思路：先问「优势是不是只在尾部存在」，再谈要不要采用。
    """
    a = np.asarray(ics, dtype=float)
    if a.size < 5:
        return None
    k = max(1, int(round(a.size * float(trim))))
    keep = a[np.argsort(np.abs(a))[: max(0, a.size - k)]]
    return float(keep.mean()) if keep.size else None


def _summarize(
    panel: pd.DataFrame, col: str, ret_col: str, min_n: int = 5, trim: float = 0.10
) -> "dict | None":
    """用与规则分完全相同的口径汇总一个候选因子的样本外表现。"""
    ics = _ic_series(panel, col, ret_col, min_n=min_n)
    if len(ics) < 3:
        return None
    a = np.asarray(ics, dtype=float)
    mean, sd = float(a.mean()), float(a.std(ddof=1))
    t = float(mean / (sd / np.sqrt(a.size))) if sd > 1e-12 else None
    tm = _trimmed_mean(ics, trim=trim)
    return {
        "n_periods": int(a.size),
        "ic_mean": round(mean, 4),
        "ic_std": round(sd, 4),
        "icir": round(mean / sd, 3) if sd > 1e-12 else None,
        "t": round(t, 2) if t is not None else None,
        "positive_rate": round(float((a > 0).mean()), 3),
        "ic_mean_trimmed": round(tm, 4) if tm is not None else None,
    }


def permutation_null(
    panel: pd.DataFrame,
    ret_col: str = "ret_120",
    kind: str = "ridge",
    n_perm: int = DEFAULT_N_PERM,
    features=None,
    seed: int = 0,
    **kw,
) -> dict:
    """置换检验：在每个截面内打乱收益，重跑整条走前流程，得到原假设下的 IC 分布。

    为什么必须有它：长期回放面板只有几十个截面、二十来个标的，IC 的标准误很大，
    「0.49 比 0.15 高」完全可能只是抽样波动。置换把「无预测力」变成一条可比较的
    分布，p 值才有意义。

    顺带它也是**整条流程的泄漏自检**：如果流程本身泄漏，打乱收益后依然会得到
    显著偏离 0 的 IC，`null_std` 会异常小、`p_value` 会异常大而 IC 分布不居中。
    """
    rng = np.random.default_rng(seed)
    nulls = []
    for _ in range(max(1, int(n_perm))):
        sh = panel.copy()
        end_col = f"label_end_{ret_col}"
        pair = [ret_col] + ([end_col] if end_col in sh else [])
        for indices in sh.groupby("date", sort=False).indices.values():
            values = sh.iloc[indices][pair].to_numpy()[rng.permutation(len(indices))]
            for i, column in enumerate(pair):
                sh.iloc[indices, sh.columns.get_loc(column)] = values[:, i]
        oof = walk_forward_predict(
            sh, features=features, target=ret_col, kind=kind, seed=seed, **kw
        )
        sh["_perm_pred"] = oof
        s = _summarize(sh, "_perm_pred", ret_col)
        if s is not None:
            nulls.append(s["ic_mean"])
    if not nulls:
        return {"n_perm": 0}
    a = np.asarray(nulls, dtype=float)
    return {
        "n_perm": int(a.size),
        "null_mean": round(float(a.mean()), 4),
        "null_std": round(float(a.std(ddof=1)) if a.size > 1 else 0.0, 4),
        "null_max": round(float(a.max()), 4),
        "null_min": round(float(a.min()), 4),
        "nulls": [round(float(x), 4) for x in a],
    }


def _p_value(real: float, nulls: list) -> "float | None":
    """单侧 p 值：原假设下 ≥ 真实值的比例（含真实值自身，避免 p=0 的假精确）。"""
    a = np.asarray([x for x in (nulls or []) if np.isfinite(x)], dtype=float)
    if a.size == 0 or real is None or not np.isfinite(real):
        return None
    return float((1 + int((a >= real).sum())) / (1 + a.size))


def evaluate_against_rule(
    panel: pd.DataFrame,
    ret_col: str = "ret_120",
    features=None,
    kinds=("ridge", "gbr"),
    min_train_periods: int = 6,
    min_train_rows: int = 30,
    min_oof_periods: int = MIN_OOF_PERIODS,
    min_n: int = 5,
    trim: float = 0.10,
    seed: int = 0,
    purge: bool = True,
    embargo_periods: int = None,
    horizon_days=None,
    n_perm: int = DEFAULT_N_PERM,
    p_threshold: float = 0.10,
    boundaries: dict | None = None,
) -> dict:
    """Choose a model on validation only, then evaluate one locked test block."""
    from quant_agent.domain.research.validation import configured_boundaries, split_three_way

    columns = list(features or FEATURES)
    out = {
        "ret_col": ret_col,
        "features": columns,
        "n_rows": 0,
        "n_periods": 0,
        "rule": None,
        "candidates": {},
        "selection_candidates": {},
        "best": None,
        "gate": {"passed": False},
        "perm": {},
        "walk_forward": {},
        "p_threshold": float(p_threshold),
        "verdict": "",
    }
    if panel is None or panel.empty or ret_col not in panel.columns:
        out["verdict"] = f"面板缺失或没有 {ret_col} 列，无法评估"
        return out
    out["n_rows"], out["n_periods"] = len(panel), int(panel["date"].nunique())
    if not purge:
        out["verdict"] = "清除跨界标签已关闭，不能用于独立测试或准入"
        return out
    boundaries = configured_boundaries({"temporal_validation": boundaries})
    if boundaries is None:
        out["verdict"] = "未提供已结束的固定验证和测试日期边界，无法评估"
        return out
    end_col = f"label_end_{ret_col}"
    try:
        parts = split_three_way(panel, date_col="date", end_col=end_col, **boundaries)
    except (ValueError, TypeError) as exc:
        out["verdict"] = f"实际标签结束日或日期分区不足，无法评估：{exc}"
        return out
    train = parts.train.dropna(subset=[ret_col])
    val = parts.validation.dropna(subset=[ret_col])
    test = parts.test.dropna(subset=[ret_col])
    emb = DEFAULT_EMBARGO_PERIODS if embargo_periods is None else int(embargo_periods)
    if emb < 0:
        raise ValueError("embargo must be nonnegative")

    def remove_recent(frame):
        dates = sorted(frame["date"].astype(str).unique())
        return frame[~frame["date"].astype(str).isin(dates[-emb:])] if emb else frame

    train = remove_recent(train)
    final_train = remove_recent(pd.concat([parts.train, val]).dropna(subset=[ret_col]))
    if (
        len(train) < min_train_rows
        or train["date"].nunique() < min_train_periods
        or len(val) < min_n
        or len(test) < min_n
    ):
        out["verdict"] = "训练、验证或封存测试样本不足，预测为空，维持现状"
        return out
    for kind in kinds:
        try:
            pred = _fit_predict(
                _model(kind, seed), train[columns], train[ret_col].astype(float), val[columns]
            )
            score = _summarize(val.assign(_pred=pred), "_pred", ret_col, min_n=min_n, trim=trim)
        except (ValueError, KeyError, TypeError):
            continue
        if score:
            out["selection_candidates"][kind] = score
    if not out["selection_candidates"]:
        out["verdict"] = "验证集候选预测为空，维持现状"
        return out
    best_kind = max(
        out["selection_candidates"], key=lambda k: out["selection_candidates"][k]["ic_mean"]
    )
    out["best"] = best_kind
    try:
        predicted = _fit_predict(
            _model(best_kind, seed),
            final_train[columns],
            final_train[ret_col].astype(float),
            test[columns],
        )
    except (ValueError, KeyError, TypeError) as exc:
        out["verdict"] = f"封存测试预测失败，维持现状：{exc}"
        return out
    held = test.assign(_pred=predicted)
    comparison_columns = ["score", "_pred", ret_col]
    numeric = held[comparison_columns].apply(pd.to_numeric, errors="coerce")
    common_rows = np.isfinite(numeric.to_numpy()).all(axis=1)
    held = held.loc[common_rows].copy()
    held[comparison_columns] = numeric.loc[common_rows]
    # Both predictors must be measurable on the same rows and dates. Otherwise
    # missing rule scores or a constant prediction compare different markets.
    common_dates = [
        day
        for day, group in held.groupby("date")
        if len(group) >= min_n and (group[comparison_columns].nunique() >= 3).all()
    ]
    held = held.loc[held["date"].isin(common_dates)].copy()
    rule = _summarize(held, "score", ret_col, min_n=min_n, trim=trim)
    result = _summarize(held, "_pred", ret_col, min_n=min_n, trim=trim)
    if not rule or not result:
        out["verdict"] = "封存测试截面不足，维持现状"
        return out
    out["rule"] = rule
    out["comparison"] = {
        "n_rows": len(held),
        "n_periods": len(common_dates),
        "excluded_rows": len(test) - len(held),
        "protocol": "same_finite_rows_and_evaluable_dates",
    }
    out["candidates"][best_kind] = {**result, "n_oof": len(held), "model": best_kind}
    out["walk_forward"] = {
        "purge": True,
        "purge_days": horizon_calendar_days(ret_col, horizon_days),
        "label_end_status": "actual",
        "embargo_periods": emb,
        "first_test_date": parts.test_start,
        "validation_start": parts.validation_start,
        "test_start": parts.test_start,
        "protocol": "sealed_test_v1",
    }
    if n_perm and int(n_perm) > 0:
        rng = np.random.default_rng(seed)
        nulls = []
        for _ in range(int(n_perm)):
            shuffled = held.copy()
            shuffled[ret_col] = shuffled.groupby("date")[ret_col].transform(
                lambda x: rng.permutation(x.to_numpy())
            )
            stat = _summarize(shuffled, "_pred", ret_col, min_n=min_n, trim=trim)
            if stat:
                nulls.append(stat["ic_mean"])
        if nulls:
            arr = np.asarray(nulls)
            out["perm"] = {
                "n_perm": len(nulls),
                "null_mean": round(float(arr.mean()), 4),
                "null_std": round(float(arr.std(ddof=1)), 4) if len(arr) > 1 else 0.0,
                "null_max": round(float(arr.max()), 4),
                "null_min": round(float(arr.min()), 4),
                "nulls": nulls,
                "best": best_kind,
                "real_ic": result["ic_mean"],
                "p_value": _p_value(result["ic_mean"], nulls),
            }
    out["gate"] = {
        "passed": False,
        "reason": "research_only_pending_registration",
        "test_periods": result["n_periods"],
    }
    out["verdict"] = (
        "封存测试截面不足，只报数，维持现状"
        if result["n_periods"] < min_oof_periods
        else "验证集选型、独立测试仅作研究观察，维持现状；候选登记和准入在后续任务完成。"
    )
    return out


def render_md(res: dict) -> str:
    """把评估结果渲染成一段可直接放进报告的 Markdown。"""
    if not res or not res.get("rule"):
        return f"## 走前 ML 验证\n\n{(res or {}).get('verdict') or '无结果'}\n"
    r = res["rule"]
    wf = res.get("walk_forward") or {}
    perm = res.get("perm") or {}
    lines = [
        "## 走前 ML 验证（样本外）",
        "",
        f"- 面板：{res['n_rows']} 观测 / {res['n_periods']} 个截面，"
        f"目标 {res['ret_col']}，特征 {len(res['features'])} 维",
        f"- 切分：按实际标签结束日 purge（{wf.get('purge_days')} 日历日为参考）"
        f"+ embargo {wf.get('embargo_periods')} 个截面；"
        f"首个可预测截面 {wf.get('first_test_date')}",
        "- **四关门禁**目前不执行晋级；候选登记与独立准入由后续任务处理。",
        "",
        "| 候选 | 样本外截面 | IC 均值 | ICIR | t | 正比例 | 去尾部10% IC |",
        "|---|---|---|---|---|---|---|",
    ]

    def row(label, d):
        return (
            "| "
            + " | ".join(
                [
                    label,
                    str(d.get("n_periods")),
                    f"{d['ic_mean']:+.4f}",
                    "-" if d.get("icir") is None else f"{d['icir']:+.3f}",
                    "-" if d.get("t") is None else f"{d['t']:+.2f}",
                    f"{d['positive_rate']:.2f}",
                    "-" if d.get("ic_mean_trimmed") is None else f"{d['ic_mean_trimmed']:+.4f}",
                ]
            )
            + " |"
        )

    lines.append(row("**规则分（基准）**", r))
    for k in sorted(res.get("candidates", {})):
        mark = " ★" if k == res.get("best") else ""
        lines.append(row(f"ML·{k}{mark}", res["candidates"][k]))
    lines.append("")
    if perm.get("null_mean") is not None:
        lines.append(
            f"- 置换检验（固定测试预测，打乱测试截面内收益 {perm.get('n_perm')} 次）："
            f"原假设 IC 均值 {perm['null_mean']:+.4f}、标准差 {perm['null_std']:.4f}、"
            f"最大 {perm['null_max']:+.4f}；真实值 {perm.get('real_ic')} → "
            f"**p = {perm.get('p_value')}**（阈值 {res.get('p_threshold')}）；该检验不证明训练流程无泄漏。"
        )
        lines.append("")
    lines += [f"**结论**：{res['verdict']}", ""]
    lines.append(
        "> purge 的含义：训练样本的收益窗口必须在测试截面之前**完全结束**。"
        "只按信号日切分不够——相邻截面前向窗口重叠约 87%，等于提前看过同一段"
        "行情。样本外截面数不足 8 时只报数、不下结论；本层只做验证，"
        "不接入生产评分。"
    )
    return "\n".join(lines)
