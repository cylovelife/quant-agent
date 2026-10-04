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

采纳门禁（四关全过才建议采用）
----------------------------
1. 样本外 IC 均值**严格高于**规则分；
2. ICIR 不低于规则分（稳定性不许变差——均值高但忽正忽负的信号更难用）；
3. 尾部守卫：去掉 |IC| 最大的 10% 截面后优势仍在（不能只靠少数极端日撑起来）；
4. 置换检验显著：在每个截面内打乱收益后重跑整条流程，真实 IC 必须落在原假设
   分布之外（长期面板只有几十个截面、二十来个标的，IC 的标准误很大，
   「0.49 比 0.15 高」完全可能只是抽样波动）。

另设样本量底线：样本外截面数 < 8 时只报数、不给结论。样本太薄时任何「更优」
都只是在描述噪声。

purge：不做的话，「样本外」这个词是假的
------------------------------------
前向收益是**重叠窗口**。实测截面间隔中位 22 天、持有 120 交易日（≈174 日历日），
相邻截面的收益窗口重叠约 87%——把 t−1 的 (特征, 收益) 放进训练集去预测 t，
等于提前看过同一段行情。所以训练样本要求「收益窗口已在测试截面之前完全结束」，
再加一个 embargo 截面。第一版没有做这一步，GBR 样本外 IC 报出 +0.49；加上
purge 之后才是可以拿来说事的数字。
"""
import re

import numpy as np
import pandas as pd

# 长期轨六维（与 value_track.FACTORS_CORE 同源；这里显式列出以便特征与评分解耦）
FEATURES = ["v_quality", "v_growth", "v_cashflow", "v_balance", "v_valuation"]
DEFAULT_HORIZON = 120
MIN_OOF_PERIODS = 8

# A 股一年约 243 个交易日。用 252 折算「N 个交易日 ≈ 多少日历日」会**略微高估**
# 日历跨度，高估的方向是多剔除训练样本——安全的一侧。
_TRADING_DAYS_PER_YEAR = 252.0
DEFAULT_EMBARGO_PERIODS = 1
DEFAULT_N_PERM = 20


def horizon_calendar_days(target: str, horizon_days=None) -> int:
    """把「持有 N 个交易日」折算成日历天数。

    从目标列名（`ret_120`）推断，或由调用方显式给出。purge 需要日历日而不是
    交易日，因为截面日期之间的间隔不固定（实测 6~36 天）。
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
            max_depth=3, max_iter=120, learning_rate=0.06,
            min_samples_leaf=20, l2_regularization=1.0, random_state=seed)
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


def walk_forward_predict(panel: pd.DataFrame, features=None, target: str = "ret_120",
                         kind: str = "ridge", min_train_periods: int = 6,
                         min_train_rows: int = 30, seed: int = 0,
                         purge: bool = True, embargo_periods: int = None,
                         horizon_days=None, diag: dict = None) -> pd.Series:
    """expanding-window 走前预测，返回与 panel 同索引的样本外预测序列。

    样本外 = 该截面从未参与任何一次拟合。未产生预测的位置保持 NaN（不是 0，
    也不是「用规则分代替」——那会让对比失去意义）。

    **purge 是必需的，不是保守起见**
    -------------------------------
    只按信号日切分还不够。前向收益是**重叠窗口**：实测截面间隔中位 22 天、
    持有 120 交易日（≈174 日历日），则相邻截面的收益窗口重叠约 87%。若把
    t−1 的 (特征, 收益) 放进训练集去预测 t，模型等于提前看过同一段行情，
    样本外 IC 会被系统性高估。所以训练样本必须满足「其收益窗口已在测试日
    之前完全结束」：

        date(train) + horizon(日历日) + embargo ≤ date(test)

    embargo 再往前推若干截面，切断「相邻截面特征几乎相同」带来的隐性重叠。
    A 股财务六维在相邻横截面间变化很小（实测 v_quality 相邻 |Δ| 中位 0），
    这条尤其重要。
    """
    features = list(features or FEATURES)
    out = pd.Series(np.nan, index=panel.index, dtype=float)
    if panel is None or panel.empty or target not in panel.columns:
        return out
    dstr = panel["date"].astype(str)
    dates = sorted(dstr.unique())
    purge_days = horizon_calendar_days(target, horizon_days) if purge else 0
    emb = DEFAULT_EMBARGO_PERIODS if embargo_periods is None else int(embargo_periods)
    dts = pd.to_datetime(dstr, errors="coerce") if purge_days else None
    first_test, first_train_max, last_train_max = None, None, None
    for i, d in enumerate(dates):
        if i < int(min_train_periods):
            continue
        te = panel[dstr == d]
        if te.empty:
            continue
        if purge_days:
            cut = pd.Timestamp(d) - pd.Timedelta(days=purge_days)
            tr = panel[(dstr < d) & (dts <= cut)]
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
            pred = _fit_predict(_model(kind, seed), tr[features],
                                tr[target].astype(float), te[features])
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
        diag.update({"purge": bool(purge), "purge_days": purge_days,
                     "embargo_periods": emb, "first_test_date": first_test,
                     "first_train_max": first_train_max,
                     "last_train_max": last_train_max})
    return out


def _ic_series(panel: pd.DataFrame, col: str, ret_col: str,
               min_n: int = 5) -> list:
    """逐交易日的截面秩相关序列。

    相关原语直接复用 `value_track._rank_corr`，不另写一份——同一把尺子的前提是
    连最底层的一致性计算都只有一处实现。
    """
    import value_track as vtrack
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


def _summarize(panel: pd.DataFrame, col: str, ret_col: str,
               min_n: int = 5, trim: float = 0.10) -> "dict | None":
    """用与规则分完全相同的口径汇总一个候选因子的样本外表现。"""
    ics = _ic_series(panel, col, ret_col, min_n=min_n)
    if len(ics) < 3:
        return None
    a = np.asarray(ics, dtype=float)
    mean, sd = float(a.mean()), float(a.std(ddof=1))
    t = float(mean / (sd / np.sqrt(a.size))) if sd > 1e-12 else None
    tm = _trimmed_mean(ics, trim=trim)
    return {"n_periods": int(a.size),
            "ic_mean": round(mean, 4),
            "ic_std": round(sd, 4),
            "icir": round(mean / sd, 3) if sd > 1e-12 else None,
            "t": round(t, 2) if t is not None else None,
            "positive_rate": round(float((a > 0).mean()), 3),
            "ic_mean_trimmed": round(tm, 4) if tm is not None else None}


def permutation_null(panel: pd.DataFrame, ret_col: str = "ret_120",
                     kind: str = "ridge", n_perm: int = DEFAULT_N_PERM,
                     features=None, seed: int = 0, **kw) -> dict:
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
        sh[ret_col] = sh.groupby("date")[ret_col].transform(
            lambda v: rng.permutation(v.to_numpy()))
        oof = walk_forward_predict(sh, features=features, target=ret_col,
                                  kind=kind, seed=seed, **kw)
        sh["_perm_pred"] = oof
        s = _summarize(sh, "_perm_pred", ret_col)
        if s is not None:
            nulls.append(s["ic_mean"])
    if not nulls:
        return {"n_perm": 0}
    a = np.asarray(nulls, dtype=float)
    return {"n_perm": int(a.size),
            "null_mean": round(float(a.mean()), 4),
            "null_std": round(float(a.std(ddof=1)) if a.size > 1 else 0.0, 4),
            "null_max": round(float(a.max()), 4),
            "null_min": round(float(a.min()), 4),
            "nulls": [round(float(x), 4) for x in a]}


def _p_value(real: float, nulls: list) -> "float | None":
    """单侧 p 值：原假设下 ≥ 真实值的比例（含真实值自身，避免 p=0 的假精确）。"""
    a = np.asarray([x for x in (nulls or []) if np.isfinite(x)], dtype=float)
    if a.size == 0 or real is None or not np.isfinite(real):
        return None
    return float((1 + int((a >= real).sum())) / (1 + a.size))


def evaluate_against_rule(panel: pd.DataFrame, ret_col: str = "ret_120",
                          features=None, kinds=("ridge", "gbr"),
                          min_train_periods: int = 6, min_train_rows: int = 30,
                          min_oof_periods: int = MIN_OOF_PERIODS, min_n: int = 5,
                          trim: float = 0.10, seed: int = 0, purge: bool = True,
                          embargo_periods: int = None, horizon_days=None,
                          n_perm: int = DEFAULT_N_PERM,
                          p_threshold: float = 0.10) -> dict:
    """把 ML 样本外预测与规则分放到同一把尺子上比较，并给出采纳结论。

    返回：`{ret_col, rule, candidates, best, gate, perm, verdict, walk_forward}`
    """
    out = {"ret_col": ret_col, "features": list(features or FEATURES),
           "n_rows": 0, "n_periods": 0, "rule": None, "candidates": {},
           "best": None, "gate": {}, "perm": {}, "walk_forward": {},
           "p_threshold": float(p_threshold), "verdict": ""}
    if panel is None or panel.empty or ret_col not in panel.columns:
        out["verdict"] = f"面板缺失或没有 {ret_col} 列，无法评估"
        return out
    out["n_rows"] = int(len(panel))
    out["n_periods"] = int(panel["date"].nunique())

    work = panel.copy()
    out["rule"] = _summarize(work, "score", ret_col, min_n=min_n, trim=trim)
    if out["rule"] is None:
        out["verdict"] = "规则分样本不足，无法作为基准"
        return out

    kw = {"min_train_periods": min_train_periods, "min_train_rows": min_train_rows,
          "purge": purge, "embargo_periods": embargo_periods,
          "horizon_days": horizon_days}
    for kind in kinds:
        diag = {}
        oof = walk_forward_predict(work, features=features, target=ret_col,
                                   kind=kind, seed=seed, diag=diag, **kw)
        col = f"_ml_{kind}"
        work[col] = oof
        s = _summarize(work, col, ret_col, min_n=min_n, trim=trim)
        if s is not None:
            s["n_oof"] = int(oof.notna().sum())
            s["model"] = kind
            s.update({f"wf_{k}": v for k, v in diag.items()})
            out["candidates"][kind] = s

    if not out["candidates"]:
        out["verdict"] = "样本外预测为空（训练窗口不足或 purge 后无可用训练样本）"
        return out
    best_kind = max(out["candidates"], key=lambda k: out["candidates"][k]["ic_mean"])
    best = out["candidates"][best_kind]
    out["best"] = best_kind
    out["walk_forward"] = {k[3:]: v for k, v in best.items() if k.startswith("wf_")}

    r = out["rule"]
    # 样本外截面太薄时，「更优」只是在描述噪声，不给结论。
    if best["n_periods"] < int(min_oof_periods):
        out["gate"] = {"passed": False, "reason": "oof_too_few"}
        out["verdict"] = (f"样本外仅 {best['n_periods']} 个截面（需 ≥"
                          f"{int(min_oof_periods)}），只报数、不下结论")
        return out

    # 置换检验：先回答「这个 IC 是不是抽样波动」。
    if n_perm and int(n_perm) > 0:
        out["perm"] = permutation_null(work, ret_col=ret_col, kind=best_kind,
                                       n_perm=n_perm, features=features, seed=seed,
                                       **kw)
        out["perm"]["best"] = best_kind
        out["perm"]["real_ic"] = best["ic_mean"]
        out["perm"]["p_value"] = _p_value(best["ic_mean"],
                                          out["perm"].get("nulls"))

    g = {
        "ic_higher": best["ic_mean"] > r["ic_mean"],
        "icir_not_lower": (best["icir"] is not None and r["icir"] is not None
                           and best["icir"] >= r["icir"]),
        "tail_holds": (best["ic_mean_trimmed"] is not None
                       and r["ic_mean_trimmed"] is not None
                       and best["ic_mean_trimmed"] > r["ic_mean_trimmed"]),
    }
    p = out["perm"].get("p_value")
    if p is None:
        g["perm_significant"] = None      # 未做置换检验时不作为否决项
    else:
        g["perm_significant"] = bool(p <= float(p_threshold))
    g["passed"] = all(v for v in g.values() if v is not None)
    out["gate"] = g
    if g["passed"]:
        out["verdict"] = (
            f"「{best_kind}」四关全过（IC {best['ic_mean']:+.4f} vs 规则 "
            f"{r['ic_mean']:+.4f}；ICIR {best['icir']} vs {r['icir']}；去尾部 "
            f"{best['ic_mean_trimmed']:+.4f} vs {r['ic_mean_trimmed']:+.4f}；"
            f"置换 p={p}）：可作为候选接入，接入前仍需一次独立样本外复现")
    else:
        miss = [k for k in ("ic_higher", "icir_not_lower", "tail_holds",
                            "perm_significant") if g.get(k) is False]
        extra = "" if p is not None else "（未做置换检验，显著性未知）"
        out["verdict"] = (f"未过门禁（{','.join(miss) or '—'}）{extra}："
                          f"维持现状，不把 ML 分数接入评分")
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
        f"- 切分：expanding window + **purge {wf.get('purge_days')} 日历日**"
        f"（= 持有期，防重叠窗口泄漏）+ embargo {wf.get('embargo_periods')} 个截面；"
        f"首个可预测截面 {wf.get('first_test_date')}",
        "- **四关门禁**：样本外 IC 更高 + ICIR 不降 + 去尾部后优势仍在 + 置换检验显著。",
        "",
        "| 候选 | 样本外截面 | IC 均值 | ICIR | t | 正比例 | 去尾部10% IC |",
        "|---|---|---|---|---|---|---|",
    ]

    def row(label, d):
        return ("| " + " | ".join([
            label, str(d.get("n_periods")),
            f"{d['ic_mean']:+.4f}",
            "-" if d.get("icir") is None else f"{d['icir']:+.3f}",
            "-" if d.get("t") is None else f"{d['t']:+.2f}",
            f"{d['positive_rate']:.2f}",
            "-" if d.get("ic_mean_trimmed") is None else f"{d['ic_mean_trimmed']:+.4f}",
        ]) + " |")

    lines.append(row("**规则分（基准）**", r))
    for k in sorted(res.get("candidates", {})):
        mark = " ★" if k == res.get("best") else ""
        lines.append(row(f"ML·{k}{mark}", res["candidates"][k]))
    lines.append("")
    if perm.get("null_mean") is not None:
        lines.append(
            f"- 置换检验（打乱截面内收益后重跑整条流程 {perm.get('n_perm')} 次）："
            f"原假设 IC 均值 {perm['null_mean']:+.4f}、标准差 {perm['null_std']:.4f}、"
            f"最大 {perm['null_max']:+.4f}；真实值 {perm.get('real_ic')} → "
            f"**p = {perm.get('p_value')}**（阈值 {res.get('p_threshold')}）。"
            f"若流程本身泄漏，打乱后的 IC 也会显著偏离 0。")
        lines.append("")
    lines += [f"**结论**：{res['verdict']}", ""]
    lines.append("> purge 的含义：训练样本的收益窗口必须在测试截面之前**完全结束**。"
                 "只按信号日切分不够——相邻截面前向窗口重叠约 87%，等于提前看过同一段"
                 "行情。样本外截面数不足 8 时只报数、不下结论；本层只做验证，"
                 "不接入生产评分。")
    return "\n".join(lines)
