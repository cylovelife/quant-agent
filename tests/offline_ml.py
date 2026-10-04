# -*- coding: utf-8 -*-
"""走前 ML 验证层自检（离线、不联网、不碰真实数据）。

重点不是「模型准不准」，而是四条**不变量**：
1. 预测不依赖未来数据——截断未来样本后，过去同一时点的预测必须逐值相同。
   任何随机切分的 CV 都会在这里失败。
2. purge 契约——训练样本的收益窗口必须在测试截面之前**完全结束**。
   不做这一步，重叠窗口会让「样本外」名不副实。
3. 门禁只在四关同时成立时才给「建议采用」，否则必须说「维持现状」。
4. 样本外截面太少时只报数、不下结论。
"""
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import ml_eval  # noqa: E402

_fails = []


def check(name: str, cond: bool, detail: str = ""):
    flag = "OK]" if cond else "FAIL]"
    print(f"  [{flag} {name}" + (f"  << {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def _panel(n_dates=25, n_sym=20, seed=1, signal=0.0, feature="v_quality",
           step_days=22, start="2024-01-02"):
    """造一个受控面板：ret = signal × feature + 噪声。

    `step_days` 默认 22 天，与真实长期回放的截面间距（实测中位 22 天）一致——
    间距直接决定 purge 的实际跨度，用 1 天间隔造样本会让 purge 检验失去意义。
    """
    rng = np.random.default_rng(seed)
    feats = ["v_quality", "v_growth", "v_cashflow", "v_balance", "v_valuation"]
    t0 = pd.Timestamp(start)
    rows = []
    for i in range(n_dates):
        d = str((t0 + pd.Timedelta(days=step_days * i)).date())
        base = rng.normal(50, 12, size=len(feats))
        for s in range(n_sym):
            f = {k: float(v) for k, v in zip(feats, base + rng.normal(0, 6, len(feats)))}
            y = signal * f[feature] + rng.normal(0, 1.0)
            rows.append({"date": d, "code": f"S{s:03d}", "market": "cn",
                         "score": float(rng.normal(0, 1)), "ret_120": y, **f})
    return pd.DataFrame(rows)


def test_no_future_leakage():
    print("· 不变量①：预测不得依赖未来数据")
    panel = _panel(25, 20, seed=7, signal=3.0)
    full = ml_eval.walk_forward_predict(panel, target="ret_120", kind="ridge",
                                        min_train_periods=6)
    dates = sorted(panel["date"].unique())
    cut = dates[18]
    part = panel[panel["date"] <= cut].copy()
    trunc = ml_eval.walk_forward_predict(part, target="ret_120", kind="ridge",
                                         min_train_periods=6)
    a = full[part.index].to_numpy(dtype=float)
    b = trunc.to_numpy(dtype=float)
    same_mask = ~np.isnan(a) & ~np.isnan(b)
    check("截断未来后过去预测仍然存在", same_mask.sum() > 0, str(same_mask.sum()))
    same = np.allclose(a[same_mask], b[same_mask], rtol=0, atol=1e-12)
    check("截断未来后过去预测逐值相同（无泄漏）", same,
          f"最大差 {np.nanmax(np.abs(a - b)) if same_mask.sum() else 'n/a'}")
    check("截断前后 NaN 位置一致", bool(np.array_equal(np.isnan(a), np.isnan(b))))

    oof = full.dropna()
    check("样本外预测非空", len(oof) > 0, str(len(oof)))
    first_dates = set(dates[:6])
    check("训练窗口内的截面不产生预测",
          bool(oof.index.isin(panel[panel["date"].isin(first_dates)].index).sum() == 0))
    check("样本外预测覆盖到最后一个截面",
          bool(oof.index.isin(panel[panel["date"] == dates[-1]].index).any()))


def test_purge_contract():
    print("· 不变量②：purge 契约（训练窗口收益必须已了结）")
    panel = _panel(25, 20, seed=6, signal=2.0)
    dates = sorted(panel["date"].unique())

    diag = {}
    ml_eval.walk_forward_predict(panel, target="ret_120", kind="ridge",
                                 min_train_periods=6, diag=diag)
    check("默认开启 purge", diag.get("purge") is True, str(diag))
    check("purge 跨度 = 持有期折算的日历日",
          diag.get("purge_days") == ml_eval.horizon_calendar_days("ret_120"),
          str(diag.get("purge_days")))
    check("120 交易日折算约为 174 日历日", diag.get("purge_days") == 174,
          str(diag.get("purge_days")))
    check("记录了首次预测时的训练上界", bool(diag.get("first_train_max")), str(diag))
    first = diag.get("first_test_date")
    check("首个可预测截面晚于训练窗口起点", first is not None and first > dates[0],
          str(first))

    # 关键契约：**首次预测**所用训练样本的最大日期 + purge + embargo ≤ 首测日
    back = (pd.Timestamp(first) - pd.Timedelta(
        days=diag["purge_days"] + diag["embargo_periods"] * 22)).date()
    check("首次预测的训练上界不晚于「首测日 − purge − embargo」",
          pd.Timestamp(diag["first_train_max"]).date() <= back,
          f"{diag['first_train_max']} vs {back}")
    check("训练上界随迭代推进（不是被最后一次覆盖）",
          pd.Timestamp(diag["last_train_max"]) > pd.Timestamp(diag["first_train_max"]),
          f"{diag['first_train_max']} → {diag['last_train_max']}")

    # 关掉 purge 后，训练集确实会用到紧邻测试截面的样本（证明差别是真的）
    d0 = {}
    ml_eval.walk_forward_predict(panel, target="ret_120", kind="ridge",
                                 min_train_periods=6, purge=False, diag=d0)
    check("关闭 purge 时训练集贴到测试截面之前",
          d0.get("purge_days") == 0
          and pd.Timestamp(d0["first_train_max"])
          > pd.Timestamp(diag["first_train_max"]),
          str(d0.get("first_train_max")))

    # 相邻截面收益窗口的重叠比例（把「为什么必须 purge」量化出来）
    gap = (pd.Timestamp(dates[1]) - pd.Timestamp(dates[0])).days
    overlap = max(0.0, 1.0 - gap * 252.0 / 365.0 / 120.0)
    check("相邻截面 120 日收益窗口确实大幅重叠", overlap > 0.7, f"{overlap:.2f}")


def test_signal_and_noise():
    print("· 有信号应被识别，纯噪声不得虚高（否则「无改进」结论不可信）")
    strong = _panel(25, 20, seed=3, signal=4.0)
    res = ml_eval.evaluate_against_rule(strong, "ret_120", n_perm=0)
    best = res["candidates"].get(res["best"] or "", {})
    check("识别出 ML 候选", bool(res["candidates"]), str(list(res["candidates"])))
    check("强信号下样本外 IC 明显为正", (best.get("ic_mean") or 0) > 0.3,
          str(best.get("ic_mean")))
    check("强信号下门禁通过", res["gate"].get("passed") is True, str(res["gate"]))
    check("结论给出建议接入", "接入" in (res["verdict"] or ""), res["verdict"])

    noise = _panel(25, 20, seed=11, signal=0.0)
    r2 = ml_eval.evaluate_against_rule(noise, "ret_120", n_perm=0)
    nb = r2["candidates"].get(r2["best"] or "", {})
    check("纯噪声面板上样本外 IC 接近 0（不虚高）",
          abs(nb.get("ic_mean") or 1) < 0.25, str(nb.get("ic_mean")))


def test_permutation():
    print("· 置换检验：把「更好」变成「显著更好」")
    # 无预测力的面板：真实 IC 应落回原假设分布内，p 值不小
    noise = _panel(25, 20, seed=21, signal=0.0)
    r = ml_eval.evaluate_against_rule(noise, "ret_120", kinds=("ridge",),
                                      n_perm=8, seed=1)
    pm = r.get("perm") or {}
    check("置换得到原假设分布", pm.get("n_perm") == 8 and
          pm.get("null_mean") is not None, str(pm.get("n_perm")))
    check("无信号时 p 值不显著", (pm.get("p_value") or 0) > 0.10,
          str(pm.get("p_value")))
    check("无信号时门禁不通过", r["gate"].get("passed") is False, str(r["gate"]))
    check("无信号时结论为维持现状", "维持现状" in (r["verdict"] or ""), r["verdict"])

    # 有预测力的面板：真实 IC 应在原假设分布之外
    # n_perm 决定 p 值下界 = 1/(n_perm+1)：n_perm=8 时下界 0.111，永远过不了 0.10，
    # 所以这里必须用 ≥10 次，否则测的是「样本量不够」而不是「信号显著」。
    sig = _panel(25, 20, seed=22, signal=5.0)
    r2 = ml_eval.evaluate_against_rule(sig, "ret_120", kinds=("ridge",),
                                       n_perm=12, seed=1)
    pm2 = r2.get("perm") or {}
    check("有信号时 p 值显著", (pm2.get("p_value") or 1) <= 0.10, str(pm2.get("p_value")))
    check("p 值计算含 +1 修正（不会取到 0）", (pm2.get("p_value") or 0) > 0,
          str(pm2.get("p_value")))
    check("原假设分布本身不过度偏离 0",
          abs(pm2.get("null_mean") or 1) < 0.30, str(pm2.get("null_mean")))


def test_gate_and_sample_floor():
    print("· 门禁与样本底线")
    small = _panel(9, 20, seed=5, signal=4.0)
    r = ml_eval.evaluate_against_rule(small, "ret_120", min_train_periods=6, n_perm=0)
    check("样本过薄时不下结论",
          "只报数" in (r["verdict"] or "") or "预测为空" in (r["verdict"] or ""),
          r["verdict"])
    if r.get("candidates"):
        check("样本过薄时门禁未通过", r["gate"].get("passed") is False, str(r["gate"]))

    # 尾部守卫：优势只来自极少数极端截面时必须塌掉
    tm = ml_eval._trimmed_mean([0.05] * 19 + [0.95], trim=0.10)
    check("尾部守卫能削掉单点极端优势", tm is not None and tm < 0.10, str(tm))
    flat = ml_eval._trimmed_mean([0.20] * 20, trim=0.10)
    check("均匀优势不被尾部守卫误杀", flat is not None and flat > 0.19, str(flat))
    check("样本过少时尾部守卫返回 None",
          ml_eval._trimmed_mean([0.1, 0.2], trim=0.10) is None)

    check("缺列/空面板不抛错",
          bool(ml_eval.evaluate_against_rule(pd.DataFrame(), "ret_120").get("verdict")))
    check("缺目标列返回可读结论",
          "无法评估" in ml_eval.evaluate_against_rule(
              _panel(12, 8, seed=2), "ret_999").get("verdict", ""))


def test_render():
    print("· 渲染")
    res = ml_eval.evaluate_against_rule(_panel(25, 20, seed=4, signal=3.0),
                                        "ret_120", n_perm=4)
    md = ml_eval.render_md(res)
    check("含基准行", "规则分（基准）" in md)
    check("含 ML 行", "ML·" in md)
    check("含四关门禁说明", "四关" in md and "门禁" in md)
    check("含 purge 说明", "purge" in md and "174" in md, md[:120])
    check("含置换检验段", "置换检验" in md)
    check("含结论", "结论" in md)
    check("空结果也能渲染", "无结果" in ml_eval.render_md({}) or bool(
        ml_eval.render_md(None)))


def main():
    print("走前 ML 验证层自检（离线）\n")
    test_no_future_leakage()
    test_purge_contract()
    test_signal_and_noise()
    test_permutation()
    test_gate_and_sample_floor()
    test_render()
    print()
    if _fails:
        print(f"❌ {len(_fails)} 项未通过:")
        for f in _fails:
            print(f"   - {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
