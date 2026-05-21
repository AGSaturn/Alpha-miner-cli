"""CLI command implementations.

Each command mirrors an MCP tool from quantgpt.mcp_server, calling the same
business logic directly without the MCP transport layer or task tracking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import traceback
from typing import TYPE_CHECKING

from quantgpt.expression_parser import __doc__ as _expr_doc
from quantgpt.expression_parser import parse_expression
from quantgpt.market_data import BENCHMARK_CODES, UNIVERSES
from quantgpt.report import generate_report
from quantgpt.task_executor import _run_backtest_in_process, get_executor

# Reuse MCP server helpers (private import is intentional — same codebase)
from quantgpt.mcp_server import (
    _VALIDATION_DUMMY,
    _enrich_with_fundamentals,
    _fetch_benchmark_for_market,
    _fetch_data_for_market,
)

if TYPE_CHECKING:
    import argparse

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s", stream=sys.stderr)
logger = logging.getLogger("alpha-miner-cli")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_json(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _die(msg: str) -> None:
    print(json.dumps({"error": msg}, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Info commands
# ---------------------------------------------------------------------------


async def cmd_list_operators(args: argparse.Namespace) -> None:
    print(_expr_doc)


async def cmd_list_universes(args: argparse.Namespace) -> None:
    a_share_info = {
        "small_scale": f"5 只蓝筹股（快速测试）: {UNIVERSES['small_scale']}",
        "hs300": "沪深300成分股（动态获取）",
        "csi500": "中证500成分股（动态获取）",
        "csi1000": "中证1000成分股（派生: 全A - HS300 - CSI500, 取前1000）",
        "csi2000": "中证2000成分股（派生: 全A - HS300 - CSI500 - CSI1000, 取前2000）",
    }
    a_share_benchmarks = {k: v["name"] for k, v in BENCHMARK_CODES.items()}
    _print_json({"universes": a_share_info, "benchmarks": a_share_benchmarks})


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


async def cmd_validate(args: argparse.Namespace) -> None:
    expression: str = args.expression
    mode: str = args.mode

    depth = 0
    for i, ch in enumerate(expression):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                print(f"ERROR: 括号不平衡：位置 {i} 处多余的右括号 ')'")
                return
    if depth > 0:
        print(f"ERROR: 括号不平衡：缺少 {depth} 个右括号 ')'")
        return

    try:
        func = parse_expression(expression, mode=mode)
        if mode == "wq":
            print("OK: expression is valid for WQ BRAIN submission")
        else:
            func(_VALIDATION_DUMMY)
            print("OK: expression is valid")
    except Exception as e:
        print(f"ERROR: {e}")


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


async def cmd_backtest(args: argparse.Namespace) -> None:
    expression: str = args.expression
    universe: str = args.universe
    start_date: str = args.start
    end_date: str = args.end
    n_groups: int = args.groups
    holding_period: int = args.holding
    benchmark: str = args.benchmark
    neutralize_industry: bool = not args.no_neutralize_industry
    neutralize_cap: bool = not args.no_neutralize_cap

    try:
        market_df, stock_codes = await asyncio.to_thread(_fetch_data_for_market, universe, start_date, end_date)
        if market_df is None or len(market_df) == 0:
            _die("No market data available. Check date range and stock codes.")

        market_df = await asyncio.to_thread(
            _enrich_with_fundamentals, expression, market_df, stock_codes, start_date, end_date
        )

        executor = get_executor()
        future = executor.submit_cpu_work(
            _run_backtest_in_process, market_df, expression, n_groups, holding_period,
            neutralize_industry=neutralize_industry, neutralize_cap=neutralize_cap,
        )
        result = await asyncio.to_thread(future.result, 600)

        # Anti-overfit
        anti_overfit_result = None
        factor_df = result.get("_factor_df")
        if factor_df is not None and len(factor_df) > 100:
            try:
                from quantgpt.anti_overfit import run_anti_overfit as _run_ao
                anti_overfit_result = await asyncio.to_thread(_run_ao, factor_df, holding_period)
            except Exception as e:
                logger.warning("Anti-overfit analysis failed: %s", e)

        # Benchmark returns
        bm_returns = None
        try:
            bm_returns = await asyncio.to_thread(_fetch_benchmark_for_market, benchmark, start_date, end_date)
        except Exception as e:
            logger.warning("Benchmark fetch failed: %s", e)

        # Report
        report_result = await asyncio.to_thread(
            generate_report,
            result["ls_returns"],
            benchmark_returns=bm_returns,
            title=f"Factor: {expression}",
        )

        _print_json({
            "report_path": report_result["report_path"],
            "metrics": report_result["metrics"],
            "backtest_summary": {
                "long_short_sharpe": result["long_short_sharpe"],
                "long_short_annual": result.get("long_short_annual", 0),
                "top_group_sharpe": result.get("top_group_sharpe", 0),
                "monotonicity_score": result["monotonicity_score"],
                "spread": result["spread"],
                "group_returns": result["group_returns"],
                "ic_mean": result.get("ic_mean", 0),
                "rank_ic_mean": result.get("rank_ic_mean", 0),
                "ic_ir": result.get("ic_ir", 0),
                "ic_win_rate": result.get("ic_win_rate", 0),
                "turnover": result.get("turnover", 0),
                "wq_fitness": result.get("wq_fitness", 0),
                "cost_adjusted": result.get("cost_adjusted", False),
                "cost_rate": result.get("cost_rate", 0),
                "total_cost_drag": result.get("total_cost_drag", 0),
            },
            "wq_brain": result.get("wq_brain", {}),
            "anti_overfit": anti_overfit_result,
            "params": {
                "expression": expression,
                "universe": universe,
                "start_date": start_date,
                "end_date": end_date,
                "n_groups": n_groups,
                "holding_period": holding_period,
                "benchmark": benchmark,
                "neutralize_industry": neutralize_industry,
                "neutralize_cap": neutralize_cap,
                "stock_count": len(stock_codes),
            },
        })

    except Exception as e:
        logger.error("Backtest failed: %s", traceback.format_exc())
        _die(str(e))


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


async def cmd_score(args: argparse.Namespace) -> None:
    from quantgpt.iteration import compute_factor_score

    expression: str = args.expression
    universe: str = args.universe
    start_date: str = args.start
    end_date: str = args.end
    n_groups: int = args.groups
    holding_period: int = args.holding
    benchmark: str = args.benchmark
    neutralize_industry: bool = not args.no_neutralize_industry
    neutralize_cap: bool = not args.no_neutralize_cap

    try:
        market_df, stock_codes = await asyncio.to_thread(_fetch_data_for_market, universe, start_date, end_date)
        if market_df is None or len(market_df) == 0:
            _die("No market data available.")

        market_df = await asyncio.to_thread(
            _enrich_with_fundamentals, expression, market_df, stock_codes, start_date, end_date
        )

        executor = get_executor()
        future = executor.submit_cpu_work(
            _run_backtest_in_process, market_df, expression, n_groups, holding_period,
            neutralize_industry=neutralize_industry, neutralize_cap=neutralize_cap,
        )
        result = await asyncio.to_thread(future.result, 600)

        bm_returns = None
        try:
            bm_returns = await asyncio.to_thread(_fetch_benchmark_for_market, benchmark, start_date, end_date)
        except Exception:
            pass

        report_result = await asyncio.to_thread(
            generate_report, result["ls_returns"], benchmark_returns=bm_returns, title="Factor Score",
        )

        scoring = compute_factor_score(
            backtest_summary={
                "long_short_sharpe": result["long_short_sharpe"],
                "monotonicity_score": result["monotonicity_score"],
                "spread": result["spread"],
                "ic_mean": result.get("ic_mean", 0),
                "rank_ic_mean": result.get("rank_ic_mean", 0),
                "ic_ir": result.get("ic_ir", 0),
                "ic_win_rate": result.get("ic_win_rate", 0),
            },
            report_metrics=report_result["metrics"],
        )

        _print_json({
            "score": scoring["score"],
            "grade": scoring["grade"],
            "component_scores": scoring["component_scores"],
            "key_metrics": {
                "ic_mean": result.get("ic_mean", 0),
                "ic_ir": result.get("ic_ir", 0),
                "monotonicity": result["monotonicity_score"],
                "top_group_sharpe": result.get("top_group_sharpe", 0),
                "turnover": result.get("turnover", 0),
                "wq_fitness": result.get("wq_fitness", 0),
                "sharpe": report_result["metrics"].get("sharpe", 0),
                "max_drawdown": report_result["metrics"].get("max_drawdown", 0),
            },
            "interpretation": {"rating": scoring["grade"]},
        })

    except Exception as e:
        logger.error("Score failed: %s", traceback.format_exc())
        _die(str(e))


# ---------------------------------------------------------------------------
# Diagnose
# ---------------------------------------------------------------------------


async def cmd_diagnose(args: argparse.Namespace) -> None:
    from quantgpt.mutation_engine import MutationEngine

    expression: str = args.expression

    try:
        engine = MutationEngine(
            expression=expression,
            metrics={
                "backtest_summary": {
                    "ic_mean": args.ic_mean,
                    "ic_ir": args.ic_ir,
                    "monotonicity_score": args.monotonicity,
                },
                "report_metrics": {},
            },
            score=args.score,
        )
        diagnosis = engine.diagnose_failure()
        sys_prompt, user_prompt = engine.build_mutation_prompt()

        _print_json({
            "strategy": diagnosis.strategy.value,
            "reason": diagnosis.reason,
            "details": diagnosis.details,
            "mutation_prompt": {
                "system": sys_prompt[:500] + "..." if len(sys_prompt) > 500 else sys_prompt,
                "user": user_prompt,
            },
        })

    except Exception as e:
        logger.error("Diagnose failed: %s", traceback.format_exc())
        _die(str(e))


# ---------------------------------------------------------------------------
# Anti-overfit
# ---------------------------------------------------------------------------


async def cmd_anti_overfit(args: argparse.Namespace) -> None:
    from quantgpt.anti_overfit import run_anti_overfit as _run_ao

    expression: str = args.expression
    universe: str = args.universe
    start_date: str = args.start
    end_date: str = args.end
    holding_period: int = args.holding
    neutralize_industry: bool = not args.no_neutralize_industry
    neutralize_cap: bool = not args.no_neutralize_cap

    try:
        market_df, stock_codes = await asyncio.to_thread(_fetch_data_for_market, universe, start_date, end_date)
        if market_df is None or len(market_df) == 0:
            _die("No market data available.")

        market_df = await asyncio.to_thread(
            _enrich_with_fundamentals, expression, market_df, stock_codes, start_date, end_date
        )

        executor = get_executor()
        future = executor.submit_cpu_work(
            _run_backtest_in_process, market_df, expression,
            holding_period=holding_period, cost_rate=0,
            neutralize_industry=neutralize_industry, neutralize_cap=neutralize_cap,
        )
        result = await asyncio.to_thread(future.result, 600)
        factor_df = result.get("_factor_df")
        if factor_df is None or len(factor_df) < 100:
            _die("Insufficient factor data for anti-overfit analysis.")

        ao_result = await asyncio.to_thread(_run_ao, factor_df, holding_period)
        _print_json(ao_result)

    except Exception as e:
        logger.error("Anti-overfit failed: %s", traceback.format_exc())
        _die(str(e))


# ---------------------------------------------------------------------------
# Rolling validation
# ---------------------------------------------------------------------------


async def cmd_rolling(args: argparse.Namespace) -> None:
    from quantgpt.rolling_validator import run_rolling_validation as _run_rv

    expression: str = args.expression
    universe: str = args.universe
    start_date: str = args.start
    end_date: str = args.end
    holding_period: int = args.holding
    neutralize_industry: bool = not args.no_neutralize_industry
    neutralize_cap: bool = not args.no_neutralize_cap

    try:
        market_df, stock_codes = await asyncio.to_thread(_fetch_data_for_market, universe, start_date, end_date)
        if market_df is None or len(market_df) == 0:
            _die("No market data available.")

        market_df = await asyncio.to_thread(
            _enrich_with_fundamentals, expression, market_df, stock_codes, start_date, end_date
        )

        executor = get_executor()
        future = executor.submit_cpu_work(
            _run_backtest_in_process, market_df, expression,
            holding_period=holding_period, cost_rate=0,
            neutralize_industry=neutralize_industry, neutralize_cap=neutralize_cap,
        )
        result = await asyncio.to_thread(future.result, 600)
        factor_df = result.get("_factor_df")
        if factor_df is None or len(factor_df) < 100:
            _die("Insufficient factor data for rolling validation.")

        rv_result = await asyncio.to_thread(_run_rv, factor_df, holding_period)
        _print_json(rv_result)

    except Exception as e:
        logger.error("Rolling validation failed: %s", traceback.format_exc())
        _die(str(e))


# ---------------------------------------------------------------------------
# WQ BRAIN commands
# ---------------------------------------------------------------------------


def _safe_float(val: object) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


async def cmd_wq_submit(args: argparse.Namespace) -> None:
    from quantgpt.wq_brain_client import WQBrainClient, is_configured

    if not is_configured():
        _die("WQ BRAIN 未配置 — 请设置 WQ_BRAIN_EMAIL 和 WQ_BRAIN_PASSWORD")

    try:
        client = WQBrainClient()
        authenticated = await asyncio.to_thread(client.authenticate)
        if not authenticated:
            _die("WQ BRAIN 认证失败")

        result = await asyncio.to_thread(
            client.simulate,
            args.expression, region=args.region, universe=args.wq_universe,
            delay=args.delay, decay=args.decay, neutralization=args.neutral,
            truncation=args.truncation,
        )

        if not result.get("ok"):
            _die(result.get("error", "Simulation failed"))

        alpha_id = result.get("alpha_id")
        is_data = result.get("is", {})
        fitness = _safe_float(is_data.get("fitness"))

        if fitness is not None and fitness >= 1.0:
            rating = "A"
        elif fitness is not None and fitness >= 0.5:
            rating = "B"
        elif fitness is not None and fitness >= 0.25:
            rating = "C"
        else:
            rating = "D"

        submitted = False
        if args.auto_submit and alpha_id and rating == "A":
            submit_result = await asyncio.to_thread(client.submit_alpha, alpha_id)
            submitted = submit_result.get("ok", False)

        sharpe = _safe_float(is_data.get("sharpe"))
        returns_val = _safe_float(is_data.get("returns"))
        turnover = _safe_float(is_data.get("turnover"))

        await asyncio.to_thread(client.close)

        _print_json({
            "expression": args.expression,
            "alpha_id": alpha_id,
            "is_metrics": is_data,
            "oos_metrics": result.get("oos", {}),
            "settings": result.get("settings", {}),
            "submitted": submitted,
            "simulation_id": result.get("simulation_id"),
            "backtest_summary": {
                "long_short_sharpe": sharpe,
                "wq_fitness": fitness,
                "wq_rating": rating,
            },
            "wq_brain": {
                "wq_sharpe": sharpe,
                "wq_fitness": fitness,
                "wq_returns": returns_val,
                "wq_turnover": turnover,
                "wq_rating": rating,
            },
            "interpretation": {"rating": rating},
        })

    except Exception as e:
        logger.error("WQ BRAIN submit failed: %s", traceback.format_exc())
        _die(str(e))


async def cmd_wq_batch(args: argparse.Namespace) -> None:
    import itertools

    from quantgpt.wq_brain_client import WQBrainClient, is_configured

    if not is_configured():
        _die("WQ BRAIN 未配置 — 请设置 WQ_BRAIN_EMAIL 和 WQ_BRAIN_PASSWORD")

    regions = args.regions or ["USA"]
    delays = args.delays or [1]
    universes = args.wq_universes or ["TOP3000"]
    neutralizations = args.neutrals or ["SUBINDUSTRY"]

    combos = list(itertools.product(regions, delays, universes, neutralizations))
    if len(combos) > 36:
        _die(f"组合数 {len(combos)} 超过上限 36")

    try:
        client = WQBrainClient()
        authenticated = await asyncio.to_thread(client.authenticate)
        if not authenticated:
            _die("WQ BRAIN 认证失败")

        best_fitness = -999.0
        best_key: str | None = None
        submittable_count = 0
        sub_results: dict = {}

        for region, delay, universe, neut in combos:
            key = f"{region}_D{delay}_{universe}_{neut}"
            result = await asyncio.to_thread(
                client.simulate,
                args.expression, region=region, universe=universe,
                delay=delay, decay=args.decay, neutralization=neut,
                truncation=args.truncation,
            )

            sub: dict = {"key": key, "region": region, "delay": delay, "universe": universe, "neutralization": neut}

            if not result.get("ok"):
                sub["status"] = "failed"
                sub["error"] = result.get("error", "unknown")
            else:
                alpha_id = result.get("alpha_id")
                is_data = result.get("is", {})
                submitted = False
                if args.auto_submit and alpha_id:
                    submit_result = await asyncio.to_thread(client.submit_alpha, alpha_id)
                    submitted = submit_result.get("ok", False)

                fitness = _safe_float(is_data.get("fitness"))
                sub["status"] = "completed"
                sub["alpha_id"] = alpha_id
                sub["sharpe"] = _safe_float(is_data.get("sharpe"))
                sub["fitness"] = fitness
                sub["returns"] = _safe_float(is_data.get("returns"))
                sub["turnover"] = _safe_float(is_data.get("turnover"))
                sub["submitted"] = submitted

                if fitness is not None and fitness >= 1.0:
                    submittable_count += 1
                if fitness is not None and fitness > best_fitness:
                    best_fitness = fitness
                    best_key = key

            sub_results[key] = sub

        await asyncio.to_thread(client.close)

        best_sub = sub_results.get(best_key, {}) if best_key else {}
        best_fit = round(best_fitness, 4) if best_fitness > -999 else None
        if best_fit is not None and best_fit >= 1.0:
            best_rating = "A"
        elif best_fit is not None and best_fit >= 0.5:
            best_rating = "B"
        elif best_fit is not None and best_fit >= 0.25:
            best_rating = "C"
        else:
            best_rating = "D"

        _print_json({
            "expression": args.expression,
            "total_combinations": len(combos),
            "best_fitness": best_fit,
            "best_key": best_key,
            "submittable_count": submittable_count,
            "sub_results": sub_results,
            "backtest_summary": {
                "long_short_sharpe": best_sub.get("sharpe"),
                "wq_fitness": best_fit,
                "wq_rating": best_rating,
            },
            "wq_brain": {
                "wq_sharpe": best_sub.get("sharpe"),
                "wq_fitness": best_fit,
                "wq_returns": best_sub.get("returns"),
                "wq_turnover": best_sub.get("turnover"),
                "wq_rating": best_rating,
            },
            "interpretation": {"rating": best_rating},
        })

    except Exception as e:
        logger.error("WQ BRAIN batch failed: %s", traceback.format_exc())
        _die(str(e))


async def cmd_wq_submit_ids(args: argparse.Namespace) -> None:
    from quantgpt.wq_brain_client import WQBrainClient, is_configured

    if not is_configured():
        _die("WQ BRAIN 未配置")

    alpha_ids: list[str] = args.ids
    if len(alpha_ids) > 50:
        _die(f"alpha_ids 数量 {len(alpha_ids)} 超过上限 50")

    try:
        client = WQBrainClient()
        authenticated = await asyncio.to_thread(client.authenticate)
        if not authenticated:
            _die("WQ BRAIN 认证失败")

        results = {}
        active = sc_fail = timeout = 0

        for alpha_id in alpha_ids:
            result = await asyncio.to_thread(client.submit_alpha, alpha_id)
            entry = {
                "ok": result.get("ok", False),
                "detail": result.get("detail", ""),
                "platform_status": result.get("platform_status", ""),
            }
            if result.get("sc_value") is not None:
                entry["sc_value"] = result["sc_value"]
                entry["sc_limit"] = result.get("sc_limit")

            if result.get("ok"):
                active += 1
            elif "SC FAIL" in result.get("detail", ""):
                sc_fail += 1
            elif result.get("platform_status") == "TIMEOUT":
                timeout += 1

            results[alpha_id] = entry

        await asyncio.to_thread(client.close)

        _print_json({
            "total": len(alpha_ids),
            "active": active,
            "sc_fail": sc_fail,
            "timeout": timeout,
            "results": results,
        })

    except Exception as e:
        logger.error("WQ submit-by-ids failed: %s", traceback.format_exc())
        _die(str(e))


async def cmd_wq_list(args: argparse.Namespace) -> None:
    from quantgpt.wq_brain_client import WQBrainClient, is_configured

    if not is_configured():
        _die("WQ BRAIN 未配置")

    try:
        client = WQBrainClient()
        authenticated = await asyncio.to_thread(client.authenticate)
        if not authenticated:
            _die("WQ BRAIN 认证失败")

        s = client._get_session()
        r = await asyncio.to_thread(
            s.get,
            "https://api.worldquantbrain.com/users/self/alphas",
            params={"limit": min(args.limit, 100), "offset": args.offset, "order": "-dateCreated"},
        )
        await asyncio.to_thread(client.close)

        if r.status_code != 200:
            _die(f"HTTP {r.status_code}: {r.text[:300]}")

        data = r.json()
        raw_alphas = data if isinstance(data, list) else data.get("results", [])

        alphas = []
        for a in raw_alphas:
            code = a.get("regular", {})
            expr = code.get("code", "") if isinstance(code, dict) else str(code)
            is_data = a.get("is", {})

            fitness = _safe_float(is_data.get("fitness"))
            alpha_status = a.get("status", "")

            if args.min_fitness is not None and (fitness is None or fitness < args.min_fitness):
                continue
            if args.status_filter and alpha_status.upper() != args.status_filter.upper():
                continue

            alphas.append({
                "alpha_id": a.get("id"),
                "expression": expr,
                "status": alpha_status,
                "dateCreated": a.get("dateCreated"),
                "neutralization": a.get("settings", {}).get("neutralization"),
                "sharpe": is_data.get("sharpe"),
                "fitness": fitness,
                "returns": is_data.get("returns"),
                "turnover": is_data.get("turnover"),
            })

        _print_json({"total": len(alphas), "alphas": alphas})

    except Exception as e:
        logger.error("WQ list failed: %s", traceback.format_exc())
        _die(str(e))


async def cmd_wq_check(args: argparse.Namespace) -> None:
    from quantgpt.wq_brain_client import WQBrainClient, is_configured

    if not is_configured():
        _die("WQ BRAIN 未配置")

    alpha_ids: list[str] = args.ids
    if len(alpha_ids) > 50:
        _die(f"alpha_ids 数量 {len(alpha_ids)} 超过上限 50")

    try:
        client = WQBrainClient()
        authenticated = await asyncio.to_thread(client.authenticate)
        if not authenticated:
            _die("WQ BRAIN 认证失败")

        results = {}
        for alpha_id in alpha_ids:
            data = await asyncio.to_thread(client.check_alpha_status, alpha_id)
            if not data.get("ok"):
                results[alpha_id] = {"ok": False, "error": data.get("error", "not found")}
                continue

            is_data = data.get("is", {})
            checks = is_data.get("checks", [])
            sc_check = next((c for c in checks if c.get("name") == "SELF_CORRELATION"), None)

            results[alpha_id] = {
                "ok": True,
                "status": data.get("status"),
                "grade": data.get("grade"),
                "sharpe": _safe_float(is_data.get("sharpe")),
                "fitness": _safe_float(is_data.get("fitness")),
                "returns": _safe_float(is_data.get("returns")),
                "turnover": _safe_float(is_data.get("turnover")),
                "sc_result": sc_check.get("result") if sc_check else None,
                "sc_value": sc_check.get("value") if sc_check else None,
            }

        await asyncio.to_thread(client.close)

        summary = {
            "total": len(alpha_ids),
            "active": sum(1 for r in results.values() if r.get("status") == "ACTIVE"),
            "unsubmitted": sum(1 for r in results.values() if r.get("status") == "UNSUBMITTED"),
            "sc_fail": sum(1 for r in results.values() if r.get("sc_result") == "FAIL"),
            "sc_pending": sum(1 for r in results.values() if r.get("sc_result") == "PENDING"),
        }
        _print_json({"summary": summary, "alphas": results})

    except Exception as e:
        logger.error("WQ check failed: %s", traceback.format_exc())
        _die(str(e))


async def cmd_wq_finalize(args: argparse.Namespace) -> None:
    from quantgpt.routes.wq_brain_batch import _finalize_alpha_statuses
    from quantgpt.wq_brain_client import WQBrainClient, is_configured

    if not is_configured():
        _die("WQ BRAIN 未配置")

    alpha_ids: list[str] = args.ids
    if len(alpha_ids) > 100:
        _die(f"alpha_ids 数量 {len(alpha_ids)} 超过上限 100")

    try:
        client = WQBrainClient()
        authenticated = await asyncio.to_thread(client.authenticate)
        if not authenticated:
            _die("WQ BRAIN 认证失败")

        result = await asyncio.to_thread(_finalize_alpha_statuses, client, alpha_ids, None)
        await asyncio.to_thread(client.close)
        _print_json(result)

    except Exception as e:
        logger.error("WQ finalize failed: %s", traceback.format_exc())
        _die(str(e))
