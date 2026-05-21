"""Alpha Miner CLI — CLI entry point.

Usage:
  alpha-miner list-operators
  alpha-miner backtest --expression "rank(close/ts_mean(close,20))" --universe small_scale
  alpha-miner score --expression "rank(close/ts_mean(close,20))"
  alpha-miner wq-submit --expression "rank(close/open)" --tag my-agent
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Load .env from project root (QuantGPT parent)
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"
if _ENV_FILE.is_file():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s", stream=sys.stderr)
logger = logging.getLogger("alpha-miner-cli")


# ---------------------------------------------------------------------------
# Shared argument helpers
# ---------------------------------------------------------------------------


def _add_backtest_args(parser: argparse.ArgumentParser) -> None:
    """Add the standard backtest argument group to a subcommand parser."""
    parser.add_argument("--expression", required=True, help="因子表达式, 如 rank(close/ts_mean(close,20))")
    parser.add_argument("--universe", default="hs300", help="股票池 (small_scale/hs300/csi500/csi1000/csi2000)")
    parser.add_argument("--start", default="2023-01-01", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default="2025-12-31", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--groups", type=int, default=5, help="分组数量")
    parser.add_argument("--holding", type=int, default=5, help="持仓周期(交易日)")
    parser.add_argument("--benchmark", default="hs300", help="基准 (hs300/zz500/sz50/csi1000)")
    parser.add_argument("--no-neutralize-industry", action="store_true", help="禁用行业中性化")
    parser.add_argument("--no-neutralize-cap", action="store_true", help="禁用市值中性化")


def _add_wq_sim_args(parser: argparse.ArgumentParser) -> None:
    """Add WQ BRAIN simulation arguments."""
    parser.add_argument("--expression", required=True, help="FASTEXPR 因子表达式")
    parser.add_argument("--tag", required=True, help="提交者标识, 用于追踪 agent")
    parser.add_argument("--region", default="USA", help="市场区域 (USA/CHN 等)")
    parser.add_argument("--wq-universe", default="TOP3000", help="WQ Universe (TOP3000/TOP500 等)")
    parser.add_argument("--delay", type=int, default=1, help="信号延迟 (0 或 1)")
    parser.add_argument("--decay", type=int, default=0, help="Alpha 衰减 (0-20)")
    parser.add_argument("--neutral", default="SUBINDUSTRY", help="中性化 (SUBINDUSTRY/INDUSTRY/SECTOR/MARKET/NONE)")
    parser.add_argument("--truncation", type=float, default=0.08, help="权重截断 (0-0.5)")
    parser.add_argument("--auto-submit", action="store_true", help="检查通过后自动提交到 WQ 审核")


# ---------------------------------------------------------------------------
# Main CLI builder
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alpha-miner",
        description="Alpha Miner CLI — QuantGPT 因子研究命令行工具",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- info ----
    sub.add_parser("list-operators", help="列出所有支持的因子表达式操作符")
    sub.add_parser("list-universes", help="列出可用股票池和基准")

    # ---- validate ----
    p_validate = sub.add_parser("validate", help="验证因子表达式语法")
    p_validate.add_argument("--expression", required=True, help="因子表达式")
    p_validate.add_argument("--mode", choices=["local", "wq"], default="local", help="验证模式 (local=本地回测, wq=WQ提交)")

    # ---- backtest ----
    p_backtest = sub.add_parser("backtest", help="执行因子回测并生成 HTML 报告")
    _add_backtest_args(p_backtest)

    # ---- score ----
    p_score = sub.add_parser("score", help="执行因子回测并返回综合评分 (0-100)")
    _add_backtest_args(p_score)

    # ---- diagnose ----
    p_diag = sub.add_parser("diagnose", help="诊断因子问题并推荐突变策略")
    p_diag.add_argument("--expression", required=True, help="当前因子表达式")
    p_diag.add_argument("--score", type=float, default=50.0, help="综合评分 (0-100)")
    p_diag.add_argument("--ic-mean", type=float, default=0.0, help="IC 均值")
    p_diag.add_argument("--ic-ir", type=float, default=0.0, help="IC 信息比率")
    p_diag.add_argument("--monotonicity", type=float, default=0.0, help="分组单调性 (0-1)")

    # ---- anti-overfit ----
    p_ao = sub.add_parser("anti-overfit", help="对因子执行反过拟合检测 (4项测试)")
    _add_backtest_args(p_ao)

    # ---- rolling ----
    p_rv = sub.add_parser("rolling", help="对因子执行滚动验证 (Walk-Forward)")
    _add_backtest_args(p_rv)

    # ---- wq-submit ----
    p_wq = sub.add_parser("wq-submit", help="提交因子到 WorldQuant BRAIN 平台模拟")
    _add_wq_sim_args(p_wq)

    # ---- wq-batch ----
    p_wqb = sub.add_parser("wq-batch", help="批量扫描因子在多个参数组合下的 WQ BRAIN 表现")
    _add_wq_sim_args(p_wqb)
    p_wqb.add_argument("--regions", nargs="+", default=None, help="市场区域列表 (默认 ['USA'])")
    p_wqb.add_argument("--delays", nargs="+", type=int, default=None, help="信号延迟列表 (默认 [1])")
    p_wqb.add_argument("--wq-universes", nargs="+", default=None, help="Universe 列表 (默认 ['TOP3000'])")
    p_wqb.add_argument("--neutrals", nargs="+", default=None, help="中性化列表 (默认 ['SUBINDUSTRY'])")

    # ---- wq-submit-ids ----
    p_wsid = sub.add_parser("wq-submit-ids", help="通过 alpha_id 批量提交到 WQ BRAIN")
    p_wsid.add_argument("--ids", nargs="+", required=True, help="alpha_id 列表 (最多 50)")

    # ---- wq-list ----
    p_wql = sub.add_parser("wq-list", help="列出 WQ BRAIN 平台上的 alpha")
    p_wql.add_argument("--limit", type=int, default=100, help="返回数量 (最大 100)")
    p_wql.add_argument("--offset", type=int, default=0, help="分页偏移")
    p_wql.add_argument("--min-fitness", type=float, default=None, help="最低 fitness 过滤")
    p_wql.add_argument("--status-filter", type=str, default=None, help="状态过滤 (UNSUBMITTED/ACTIVE)")

    # ---- wq-check ----
    p_wqc = sub.add_parser("wq-check", help="批量查询 alpha 状态")
    p_wqc.add_argument("--ids", nargs="+", required=True, help="alpha_id 列表 (最多 50)")

    # ---- wq-finalize ----
    p_wqf = sub.add_parser("wq-finalize", help="查询已提交 alpha 的最终 SC 检查结果")
    p_wqf.add_argument("--ids", nargs="+", required=True, help="alpha_id 列表 (最多 100)")

    # ---- serve (legacy) ----
    p_serve = sub.add_parser("serve", help="启动 QuantGPT 服务器")
    p_serve.add_argument("--transport", choices=["stdio", "sse", "streamable-http", "http"], default="stdio")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8003)

    # ---- prefetch (legacy) ----
    p_prefetch = sub.add_parser("prefetch", help="预下载市场数据")
    p_prefetch.add_argument("universes", nargs="+", help="股票池名称, 如 hs300 csi500 small_scale")

    return parser


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run an async coroutine and handle graceful exit."""
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


def main():
    from alpha_miner_cli import commands

    parser = _build_parser()
    args = parser.parse_args()

    cmd = args.command

    if cmd == "serve":
        if args.transport == "http":
            import uvicorn
            from quantgpt.api_server import app
            uvicorn.run(app, host=args.host, port=args.port)
        else:
            from quantgpt.mcp_server import mcp
            if args.transport in ("sse", "streamable-http"):
                os.environ.setdefault("FASTMCP_HOST", args.host)
                os.environ.setdefault("FASTMCP_PORT", str(args.port))
            mcp.run(transport=args.transport)
        return

    if cmd == "prefetch":
        from quantgpt.market_data import MarketDataFetcher, fetch_benchmark_returns, get_universe

        fetcher = MarketDataFetcher()
        for name in args.universes:
            logger.info("Prefetching universe: %s", name)
            codes = get_universe(name)
            logger.info("  %d stocks, fetching data...", len(codes))
            df = fetcher.fetch_stocks(codes, "2020-01-01", "2025-12-31")
            if df is not None:
                logger.info("  Done: %d records cached", len(df))
            else:
                logger.warning("  No data fetched for %s", name)

        for bm in ("hs300", "zz500"):
            logger.info("Prefetching benchmark: %s", bm)
            fetch_benchmark_returns(bm, "2020-01-01", "2025-12-31")

        logger.info("Prefetch complete.")
        return

    # All other commands are async
    dispatch = {
        "list-operators": commands.cmd_list_operators,
        "list-universes": commands.cmd_list_universes,
        "validate": commands.cmd_validate,
        "backtest": commands.cmd_backtest,
        "score": commands.cmd_score,
        "diagnose": commands.cmd_diagnose,
        "anti-overfit": commands.cmd_anti_overfit,
        "rolling": commands.cmd_rolling,
        "wq-submit": commands.cmd_wq_submit,
        "wq-batch": commands.cmd_wq_batch,
        "wq-submit-ids": commands.cmd_wq_submit_ids,
        "wq-list": commands.cmd_wq_list,
        "wq-check": commands.cmd_wq_check,
        "wq-finalize": commands.cmd_wq_finalize,
    }

    fn = dispatch.get(cmd)
    if fn is None:
        parser.print_help()
        sys.exit(1)

    _run_async(fn(args))


if __name__ == "__main__":
    main()
