"""Command composition root; importing the package starts no runtime work.

@author cylovelife
"""

import argparse
import contextlib
import os
import socket
import sys
import time


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    import quant_agent.entrypoints.research as research_cli

    ap = argparse.ArgumentParser()
    research_cli.add_arguments(ap)
    ap.add_argument(
        "--research-experiment", metavar="REQUEST_JSON", help="显式登记或检查本地研究实验"
    )
    ap.add_argument(
        "--start-paper-experiment", metavar="REQUEST_JSON", help="显式登记前瞻虚拟试验及现金基准"
    )
    ap.add_argument(
        "--transition-paper-experiment",
        metavar="REQUEST_JSON",
        help="在全新账本副本登记运行版本衔接，不重置账户",
    )
    ap.add_argument(
        "--advance-paper-experiment", metavar="REQUEST_JSON", help="按冻结规则同步推进试验与基准"
    )
    ap.add_argument(
        "--compare-paper-experiment", metavar="REQUEST_JSON", help="只读试验与基准的资金对照"
    )
    ap.add_argument("--settle-paper-days", metavar="REQUEST_JSON", help="按日原子结算虚拟账户")
    ap.add_argument("--paper-curve", metavar="REQUEST_JSON", help="只读虚拟账户每日资金曲线")
    ap.add_argument("--init-account", metavar="REQUEST_JSON", help="显式初始化独立虚拟账户")
    ap.add_argument(
        "--simulate-order",
        metavar="REQUEST_JSON",
        help="只读本地raw行情，预演单笔虚拟订单，不写账本或启动账户",
    )
    ap.add_argument(
        "--record-order",
        metavar="REQUEST_JSON",
        help="将显式虚拟订单原子写入指定的既有现金账本，不初始化账户",
    )
    ap.add_argument(
        "--ledger-status",
        metavar="REQUEST_JSON",
        help="只读指定虚拟账户的现金、数量、费用和对账事件",
    )
    ap.add_argument(
        "--record-action", metavar="REQUEST_JSON", help="记账已声明的公司行动或虚拟资金流"
    )
    ap.add_argument(
        "--migrate-ledger-copy", metavar="REQUEST_JSON", help="将v1账本复制升级到全新v2文件"
    )
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--no-eval", action="store_true")
    ap.add_argument("--no-replay", action="store_true", help="跳过长期历史回放（省约 1 分钟）")
    ap.add_argument(
        "--no-cache",
        action="store_true",
        help="忽略阶段缓存，强制重算（用于验证缓存与原算结果一致）",
    )
    ap.add_argument(
        "--mode",
        choices=["short", "long", "all"],
        default="all",
        help="short=只跑短线低吸轨, long=只跑长期价值轨",
    )
    ap.add_argument("--market", type=str, default=None)
    ap.add_argument(
        "--db-stats",
        action="store_true",
        help="打印落盘概览（各表行数 / 行情覆盖 / 数据源健康）后退出",
    )
    ap.add_argument("--csc-probe", action="store_true", help="自检中信建投数据源连通性后退出")
    ap.add_argument(
        "--ml-eval",
        action="store_true",
        help="走前 ML 验证：样本外预测 vs 规则分（只读，不接入评分）",
    )
    ap.add_argument(
        "--fund-flow",
        action="store_true",
        help="只抓当日个股资金流截面并落库（Phase 1 新数据源，独立可跑）",
    )
    ap.add_argument(
        "--new-sources",
        action="store_true",
        help="跑全部 Phase 1 新数据源（资金流 + 龙虎榜/两融/解禁/退市清单）",
    )
    ap.add_argument(
        "--spot-probe", action="store_true", help="逐档实测 ETF/港股/美股现货降级链（只读，不落库）"
    )
    ap.add_argument(
        "--akshare-probe",
        action="store_true",
        help="自检 akshare 数据源连通性与字段口径后退出（不写库）",
    )
    ap.add_argument(
        "--brief",
        action="store_true",
        default=None,
        help="报告只留操作清单与结论（完整版另存 report_DATE.full.*）",
    )
    ap.add_argument(
        "--full", action="store_true", help="输出完整版报告，覆盖 config.json 的 report.brief"
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    options = {arg.split("=", 1)[0] for arg in argv}
    if "--research-experiment" in options:
        from quant_agent.entrypoints.research_experiment import main as experiment_main

        parser = argparse.ArgumentParser(description="本地研究实验登记")
        parser.add_argument("--research-experiment", required=True)
        return experiment_main(parser.parse_args(argv).research_experiment)
    paper = {
        "--start-paper-experiment",
        "--transition-paper-experiment",
        "--advance-paper-experiment",
        "--compare-paper-experiment",
        "--settle-paper-days",
        "--paper-curve",
        "--init-account",
        "--simulate-order",
        "--record-order",
        "--ledger-status",
        "--record-action",
        "--migrate-ledger-copy",
    }
    if paper & options:
        from quant_agent.entrypoints.execution import main as execution_main

        return execution_main(argv)
    args = parse_args(argv)
    socket.setdefaulttimeout(20)
    from quant_agent.application.investing import workflow

    if any(
        (
            args.daily,
            args.sync_data,
            args.data_query,
            args.verify_snapshot,
            args.sync_snapshot,
            args.offline,
            args.codes,
            args.start,
            args.end,
            args.research_dir,
            args.bars is not None,
        )
    ):
        from quant_agent.entrypoints import research

        params, cfg = workflow.load_configs()
        workflow.apply_runtime_config(cfg)
        return research.run_command(args, cfg, params, workflow.ROOT)
    os.makedirs(workflow.LOG, exist_ok=True)
    started = time.time()
    try:
        return workflow.run(args) or 0
    finally:
        with contextlib.redirect_stdout(sys.stderr if "--data-query" in argv else sys.stdout):
            workflow.log(f"总耗时 {time.time() - started:.1f}s")
