#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段缓存等价性验证：证明「命中缓存」与「完整重算」结果完全一致，且确实更快。

为什么必须单独验证
------------------
**整份报告不能用来比对缓存**。候选池来自实时快照，两次运行之间标的集合可能变化
（实测三次连续运行拿到 46/47 只港股），报告内容自然不同——那是数据漂移，不是缓存
问题。要验证缓存本身，必须固定输入，只比较「同一输入下，命中缓存」与「从头重算」。

做法：固定一份 K 线（复用 tests/offline_e2e.py 的缓存），对三段重计算各跑三次：
    ① fresh  —— 绕过缓存完整计算（这是"真值"）
    ② prime  —— 未命中 → 计算 → 落盘（填充缓存）
    ③ hit    —— 命中缓存，应跳过计算
断言 ① == ② == ③（归一化后深度相等），并输出各阶段耗时与加速比。

用法：
    python tests/verify_phase_cache.py              # 复用 K 线缓存（约 4 分钟）
    python tests/verify_phase_cache.py --refresh    # 重新抓 K 线
"""
import argparse
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import offline_e2e as oe  # noqa: E402


def _elapsed(fn):
    t0 = time.time()
    try:
        return fn(), time.time() - t0
    except Exception as e:
        raise SystemExit(f"阶段执行失败：{type(e).__name__}: {e}") from e


REQUIRED_PHASES = ("exit_model", "param_scan", "select_short")


def check_run_wiring() -> list:
    """静态检查 run.py 是否把三个重阶段**都**路由到了 cached_phase。

    为什么必须有这一步：阶段级等价测试只证明「缓存机制正确」，**不证明 run.py 真的用了它**。
    实测踩过——exit_model 的调用点漏改，缓存机制全部正确、验证也全绿，但最大的 ≈155s
    阶段照样每次重算；线索只有「缓存目录里少一个文件」，极易被忽略。
    """
    import ast
    src = open(os.path.join(ROOT, "run.py"), encoding="utf-8").read()
    wired = []
    for n in ast.walk(ast.parse(src)):
        if (isinstance(n, ast.Call) and getattr(n.func, "id", None) == "cached_phase"
                and n.args and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)):
            wired.append(n.args[0].value)
    return [p for p in REQUIRED_PHASES if p not in wired]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="重新抓取 K 线")
    args = ap.parse_args()

    print("=== 路由检查：run.py 是否对三个重阶段都启用了缓存 ===")
    unwired = check_run_wiring()
    if unwired:
        print(f"❌ 以下阶段未接入 cached_phase，其耗时不会被缓存：{unwired}")
        return 1
    print(f"✅ {list(REQUIRED_PHASES)} 均已接入 cached_phase\n")

    import exit_model as em  # noqa: E402
    import model_select as msel  # noqa: E402
    import phase_cache as pc  # noqa: E402
    import run as R  # noqa: E402

    kmap = oe.build_kline_map(refresh=args.refresh)
    if not kmap:
        raise SystemExit("K 线缓存为空，先跑一次 tests/offline_e2e.py 或加 --refresh")
    params, cfg = R.load_configs()

    tmp = tempfile.mkdtemp(prefix="qt_phasecache_")
    old_dir, old_log = pc.CACHE_DIR, R.log
    pc.CACHE_DIR = tmp
    hits = []
    R.log = lambda m: hits.append(str(m)) if "命中缓存" in str(m) else None

    ecfg = cfg.get("exit_model", {}) or {}
    mcfg = cfg.get("model_select", {}) or {}
    phases = [
        ("exit_model", {
            "min_bucket_n": int(ecfg.get("min_bucket_n", 20)),
            "oos_frac": float(ecfg.get("oos_frac", 0.3)),
            "shrink_k": float(ecfg.get("shrink_k", 25)),
        }, lambda: em.fit_exit_model(
            kmap, params,
            min_bucket_n=int(ecfg.get("min_bucket_n", 20)),
            oos_frac=float(ecfg.get("oos_frac", 0.3)),
            shrink_k=float(ecfg.get("shrink_k", 25)))),
        ("param_scan", {}, lambda: em.parameter_scan(kmap, params)),
        ("select_short", {"grid": mcfg.get("shorts")},
         lambda: msel.select_short(cfg, params, kmap)),
    ]

    sig = pc.kline_signature(kmap)
    print(f"K 线缓存 {len(kmap)} 只标的，内容签名 {sig}")
    print(f"参数字典版本 {params.get('version')}\n")

    failures = []
    print(f"{'阶段':<14}{'fresh(s)':>10}{'prime(s)':>10}{'hit(s)':>9}"
          f"{'加速比':>8}   一致性")
    for name, extra, fn in phases:
        r_fresh, t_fresh = _elapsed(
            lambda: R.cached_phase(name, fn, sig, params, extra, no_cache=True))
        r_prime, t_prime = _elapsed(
            lambda: R.cached_phase(name, fn, sig, params, extra))
        hits.clear()
        r_hit, t_hit = _elapsed(
            lambda: R.cached_phase(name, fn, sig, params, extra))

        a, b, c = (pc._sanitize(x) for x in (r_fresh, r_prime, r_hit))
        ok = (a == b == c)
        speed = (t_prime / t_hit) if t_hit > 1e-6 else float("inf")
        print(f"{name:<14}{t_fresh:>10.1f}{t_prime:>10.1f}{t_hit:>9.2f}"
              f"{speed:>7.0f}x   {'✅ 一致' if ok else '❌ 不一致'}")
        if not ok:
            failures.append(name)
            # 定位第一处差异，便于排查
            if isinstance(a, dict) and isinstance(c, dict):
                for k in sorted(set(a) | set(c)):
                    if a.get(k) != c.get(k):
                        print(f"    首个差异键 {k!r}:\n      fresh={a.get(k)!r}\n      hit  ={c.get(k)!r}")
                        break
            else:
                print(f"    fresh={a!r}\n    hit  ={c!r}")
        if not hits:
            failures.append(f"{name} 未命中缓存")
            print(f"    ⚠ 第三次调用没有产生「命中缓存」日志，说明 key 不稳定")

    pc.CACHE_DIR = old_dir
    R.log = old_log
    n_files = len([f for f in os.listdir(tmp) if f.endswith(".json")])
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n缓存文件 {n_files} 个（3 个阶段各 1 个）")
    if failures:
        print(f"\n❌ 缓存验证失败：{failures}")
        return 1
    print("\n✅ 缓存等价性验证通过：命中缓存与完整重算结果一致，且确实跳过计算")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
