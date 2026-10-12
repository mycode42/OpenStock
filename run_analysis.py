#!/usr/bin/env python3
"""
Swarm Trader AI Hedge Fund Runner

Fetches live Alpaca positions, runs multi-agent analysis, and executes trades with safety rails.

Usage:
  poetry run python run_analysis.py                    # Dry run, all holdings
  poetry run python run_analysis.py --execute          # Actually trade
  poetry run python run_analysis.py --tickers NVDA,AVGO  # Specific tickers only
  poetry run python run_analysis.py --telegram         # Telegram-friendly output
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()  # Load .env before any imports that need credentials

from src.alpaca_integration import (
    get_alpaca_account,
    get_alpaca_positions,
    convert_to_portfolio,
    format_positions_summary,
)
from src.config import resolve_mode
from src.main import run_hedge_fund


def _execute_via_canonical_executor(decisions: dict, mode: str, dry_run: bool) -> list[dict]:
    """Route legacy analysis decisions through the sole order writer."""
    from run_hedge_fund import _decisions_to_trades

    payload = _decisions_to_trades(decisions)
    if not payload["trades"]:
        return []
    repo_root = Path(__file__).parent
    command = [sys.executable, str(repo_root / "execute_trades.py"), "--mode", mode]
    if dry_run:
        command.append("--dry-run")
    completed = subprocess.run(
        command,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=90,
        check=False,
    )
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    if completed.returncode != 0:
        raise RuntimeError(f"Canonical executor exited {completed.returncode}: {completed.stderr[-500:]}")
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Canonical executor returned invalid JSON") from exc
    rows = output.get("results")
    if not isinstance(rows, list):
        raise RuntimeError("Canonical executor returned no result list")
    return [
        {
            **row,
            "success": row.get("status") in {"submitted", "would_execute"},
            "dry_run": row.get("status") == "would_execute",
            "skipped": row.get("status") == "skipped",
            "reason": row.get("reason") or row.get("error") or row.get("status", ""),
        }
        for row in rows
    ]


def main():
    parser = argparse.ArgumentParser(
        description="Run Swarm Trader AI Hedge Fund analysis and trading",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_analysis.py                           # Dry run all holdings
  python run_analysis.py --execute                 # Execute trades  
  python run_analysis.py --tickers NVDA,AVGO      # Analyze specific tickers
  python run_analysis.py --telegram               # Telegram-friendly format
  python run_analysis.py --model qwen3.5:cloud    # Use different model
        """
    )
    
    # Core flags
    parser.add_argument(
        "--execute", 
        action="store_true", 
        help="Actually execute trades (default: dry run)"
    )
    parser.add_argument(
        "--tickers", 
        type=str, 
        help="Comma-separated list of specific tickers to analyze (default: all holdings)"
    )
    parser.add_argument(
        "--mode",
        choices=["swing", "day"],
        default=None,
        help="Trading mode (defaults to trading_mode.json / TRADING_MODE).",
    )
    parser.add_argument(
        "--telegram", 
        action="store_true", 
        help="Format output for Telegram (no markdown tables)"
    )
    
    # Model configuration
    configured_provider = os.getenv("LLM_PROVIDER", "OpenAI").strip() or "OpenAI"
    parser.add_argument(
        "--model", 
        type=str, 
        default=None,
        help="Model to use (default: LLM_MODEL for the configured provider)"
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=configured_provider,
        help=f"Model provider (default: LLM_PROVIDER or {configured_provider})"
    )
    parser.add_argument(
        "--analysts", 
        type=str,
        default="apex,fundamentals_analyst,technical_analyst",
        help="Comma-separated list of analysts to use (default: apex + fundamentals + technical)"
    )
    parser.add_argument(
        "--top",
        type=int,
        default=5,
        help="Analyze only top N positions by value (default: 5, use 0 for all)"
    )
    parser.add_argument(
        "--show-reasoning", 
        action="store_true", 
        help="Show detailed reasoning from each agent"
    )
    
    args = parser.parse_args()
    model_provider = args.provider
    if args.model:
        model_name = args.model
    elif model_provider.casefold() == configured_provider.casefold() and os.getenv("LLM_MODEL", "").strip():
        model_name = os.getenv("LLM_MODEL", "").strip()
    else:
        provider_defaults = {
            "openai": "gpt-4.1",
            "anthropic": "claude-opus-4-20250514",
        }
        model_name = provider_defaults.get(model_provider.casefold()) or os.getenv("LLM_MODEL", "gpt-4.1").strip()

    mode = resolve_mode(cli_mode=args.mode)
    if mode == "auto":
        mode = "swing"
    
    # Parse inputs
    selected_analysts = [a.strip() for a in args.analysts.split(",") if a.strip()]
    specific_tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
    
    try:
        # Fetch current Alpaca state
        print(f"📡 Fetching Alpaca account and positions... [{mode.upper()}]")
        account = get_alpaca_account(mode=mode)
        positions_raw = get_alpaca_positions(mode=mode)
        
        if not args.telegram:
            print(f"✅ Found {len(positions_raw)} positions")
            print(format_positions_summary(positions_raw, account))
            print()
        
        # Determine analysis universe
        if specific_tickers:
            tickers_to_analyze = specific_tickers
            portfolio = convert_to_portfolio(positions_raw, account, specific_tickers)
        else:
            # Analyze holdings, optionally limited to top N by market value
            held = [p for p in positions_raw if float(p.get("qty", 0)) != 0]
            held.sort(key=lambda p: abs(float(p.get("market_value", 0))), reverse=True)
            if args.top and args.top > 0:
                held = held[:args.top]
            held_tickers = [p["symbol"] for p in held]
            tickers_to_analyze = held_tickers
            portfolio = convert_to_portfolio(positions_raw, account)
        
        if not tickers_to_analyze:
            print("❌ No tickers to analyze. Either specify --tickers or hold some positions.")
            return 1
            
        print(f"🔍 Analyzing {len(tickers_to_analyze)} ticker(s): {', '.join(tickers_to_analyze)}")
        print(f"🤖 Using analysts: {', '.join(selected_analysts)}")
        print(f"🧠 Model: {model_name} ({model_provider})")
        print(f"💼 Mode: {'LIVE TRADING' if args.execute else 'DRY RUN'}")
        print()
        
        # Run the AI hedge fund analysis
        result = run_hedge_fund(
            tickers=tickers_to_analyze,
            start_date=(datetime.now().replace(day=1)).strftime("%Y-%m-%d"),  # Start of month
            end_date=datetime.now().strftime("%Y-%m-%d"),
            portfolio=portfolio,
            show_reasoning=args.show_reasoning,
            selected_analysts=selected_analysts,
            model_name=model_name,
            model_provider=model_provider,
        )
        
        decisions = result.get("decisions", {})
        analyst_signals = result.get("analyst_signals", {})
        
        if not decisions:
            print("❌ No trading decisions generated")
            return 1
            
        # Execute or preview trades
        trade_results = _execute_via_canonical_executor(
            decisions, mode=mode, dry_run=not args.execute,
        )
        
        # Format output
        if args.telegram:
            output_telegram_summary(decisions, trade_results, args.execute)
        else:
            output_detailed_summary(decisions, analyst_signals, trade_results, args.execute)
        if args.execute and any(row.get("status") == "failed" for row in trade_results):
            return 2
            
    except Exception as e:
        print(f"❌ Error: {e}")
        return 1
        
    return 0


def output_detailed_summary(decisions, analyst_signals, trade_results, executed):
    """Output detailed analysis and trade results."""
    print("=" * 80)
    print("🎯 TRADING DECISIONS")
    print("=" * 80)
    
    for ticker, decision in decisions.items():
        action = decision.get("action", "hold")
        qty = decision.get("quantity", 0)
        confidence = decision.get("confidence", 0)
        reasoning = decision.get("reasoning", "")
        
        print(f"\n📊 {ticker}")
        print(f"   Action: {action.upper()}")
        print(f"   Quantity: {qty}")
        print(f"   Confidence: {confidence:.1f}%")
        print(f"   Reasoning: {reasoning}")
    
    print("\n" + "=" * 80)
    print("📋 TRADE RESULTS")
    print("=" * 80)
    
    successful_trades = 0
    failed_trades = 0
    
    for result in trade_results:
        ticker = result["ticker"]
        action = result["action"]
        qty = result["qty"]
        success = result.get("success", False)
        
        if result.get("skipped"):
            print(f"⏸️  {ticker}: SKIPPED - {result.get('reason', '')}")
        elif success:
            successful_trades += 1
            if result.get("dry_run"):
                print(f"✅ {ticker}: WOULD {action.upper()} {qty} shares (DRY RUN)")
            else:
                order_id = result.get("order_id", "")
                print(f"✅ {ticker}: {action.upper()} {qty} shares submitted (Order: {order_id})")
        else:
            failed_trades += 1
            reason = result.get("reason", "Unknown error")
            print(f"❌ {ticker}: FAILED - {reason}")
    
    print(f"\nSummary: {successful_trades} successful, {failed_trades} failed")


def output_telegram_summary(decisions, trade_results, executed):
    """Output Telegram-friendly summary (no markdown tables)."""
    print("🤖 Swarm Trader AI Hedge Fund Analysis")
    print(f"⏰ {datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M %Z')}")
    print()
    
    # Count signals
    signals_count = {"bullish": 0, "bearish": 0, "neutral": 0}
    trades_count = {"buy": 0, "sell": 0, "hold": 0}
    
    for decision in decisions.values():
        signal = decision.get("signal", "neutral").lower()
        action = decision.get("action", "hold").lower()
        signals_count[signal] = signals_count.get(signal, 0) + 1
        trades_count[action] = trades_count.get(action, 0) + 1
    
    print(f"📈 Signals: {signals_count['bullish']} Bullish, {signals_count['bearish']} Bearish, {signals_count['neutral']} Neutral")
    print(f"💼 Actions: {trades_count.get('buy', 0)} Buy, {trades_count.get('sell', 0)} Sell, {trades_count.get('hold', 0)} Hold")
    print()
    
    # Show only actionable trades
    actionable_trades = [
        (ticker, decision) for ticker, decision in decisions.items()
        if decision.get("action", "hold") != "hold" and decision.get("quantity", 0) > 0
    ]
    
    if actionable_trades:
        print("🎯 Trade Recommendations:")
        for ticker, decision in actionable_trades:
            action = decision["action"].upper()
            qty = decision["quantity"]
            confidence = decision["confidence"]
            print(f"  • {ticker}: {action} {qty} shares ({confidence:.0f}% confidence)")
    else:
        print("🎯 No trades recommended (all hold)")
    
    # Show execution results
    executed_count = len([r for r in trade_results if r.get("success") and not r.get("skipped")])
    failed_count = len([r for r in trade_results if not r.get("success") and not r.get("skipped")])
    
    if executed:
        print(f"\n✅ Submitted: {executed_count} orders, {failed_count} failed")
    else:
        print(f"\n🧪 DRY RUN: {executed_count} trades would execute, {failed_count} blocked by safety rails")


if __name__ == "__main__":
    sys.exit(main())
