#!/usr/bin/env python3
"""Cron-ready Algo report: a prompt and data files go to the configured LLM, and the reply can go to Telegram.

It uses the analysis provider settings in .env (LLM_PROVIDER and LLM_MODEL), so OpenAI, Anthropic, Groq, or a local
Ollama or OpenAI-compatible server all work, and OpenClaw is not needed. The signal snapshot is read from the database.
A stale or missing snapshot produces a notice and no model call.

Examples:
  poetry run python scripts/llm_report.py --prompt ops/prompts/llm-digest.md --snapshot --telegram
  poetry run python scripts/llm_report.py --prompt ops/prompts/equity-research.md \
      --context data/research/algo-auto/latest.json --telegram
  poetry run python scripts/llm_report.py --prompt ops/prompts/llm-digest.md --snapshot --dry-run
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage

from runtime.paths import ROOT
from strategies.llm_driven.ai import DEFAULT_AI_SIGNAL_DB, LLMSignalBook, read_latest_llm_signal_payload
from strategies.llm_driven.analysis.providers import ModelProvider, get_model

TELEGRAM_LIMIT = 4000
MODEL_TIMEOUT_SECONDS = 120.0
SYSTEM_PROMPT = (
    "You write a scheduled Algo trading report from the data supplied in this message only. "
    "This run has no web access, so do not claim that any news source was checked, and do not invent links. "
    "Steps in the prompt that ask you to run a command, read a file or search are already done: the data is below. "
    "Never place, modify or cancel an order, and do not say that an order was placed."
)


def render_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("{ROOT}", str(ROOT)).strip()


def snapshot_status(now: datetime) -> tuple[bool, str]:
    """Whether the newest snapshot is fresh, and the text that describes it for the report."""
    book = LLMSignalBook(DEFAULT_AI_SIGNAL_DB, now=now)
    if book.status != "ready":
        detail = f" ({book.error})" if book.error else ""
        return False, f"The LLM signal snapshot is {book.status}{detail}."
    payload = read_latest_llm_signal_payload(DEFAULT_AI_SIGNAL_DB)
    return True, "Latest LLM signal snapshot (JSON):\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def build_message(prompt: str, contexts: list[tuple[str, str]]) -> str:
    sections = [prompt]
    sections.extend(f"--- {name} ---\n{text}" for name, text in contexts)
    return "\n\n".join(sections)


def ask_model(user_text: str, provider: str, model: str) -> str:
    client = get_model(model, ModelProvider(provider), timeout_seconds=MODEL_TIMEOUT_SECONDS)
    reply = client.invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user_text)])
    return str(reply.content).strip()


def split_for_telegram(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            head, line = line[:limit], line[limit:]
            chunks.append(head)
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks or [""]


def send_telegram(text: str, token: str, chat_id: str) -> None:
    for chunk in split_for_telegram(text):
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}).encode()
        request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            # The URL carries the bot token, so only the exception type is reported.
            raise RuntimeError(f"Telegram delivery failed: {type(exc).__name__}") from None
        if not body.get("ok"):
            raise RuntimeError("Telegram delivery was refused")


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT.parent / ".env", override=False)
    parser = argparse.ArgumentParser(description="Run an Algo report prompt through the configured LLM")
    parser.add_argument("--prompt", type=Path, required=True, help="prompt file; {ROOT} is replaced with the checkout path")
    parser.add_argument("--context", type=Path, action="append", default=[], help="data file to include (repeatable)")
    parser.add_argument("--snapshot", action="store_true", help="include the newest LLM signal snapshot; skip the model if it is not fresh")
    parser.add_argument("--telegram", action="store_true", help="also send the reply to ALGO_REPORT_TELEGRAM_TO (or TELEGRAM_CHAT_ID)")
    parser.add_argument("--dry-run", action="store_true", help="print the assembled message and call no model")
    args = parser.parse_args(argv)

    chat_id = os.getenv("ALGO_REPORT_TELEGRAM_TO") or os.getenv("TELEGRAM_CHAT_ID") or ""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if args.telegram and not (chat_id and token):
        print("error: --telegram needs TELEGRAM_BOT_TOKEN and ALGO_REPORT_TELEGRAM_TO (or TELEGRAM_CHAT_ID) in .env", file=sys.stderr)
        return 2

    def deliver(text: str) -> None:
        if args.telegram:
            send_telegram(text, token, chat_id)

    contexts = [(str(path), path.read_text(encoding="utf-8")) for path in args.context]
    if args.snapshot:
        fresh, section = snapshot_status(datetime.now(timezone.utc))
        if not fresh:
            notice = f"No briefing was produced. {section}"
            print(notice)
            deliver(notice)
            return 0
        contexts.append(("signal snapshot", section))

    message = build_message(render_prompt(args.prompt), contexts)
    if args.dry_run:
        print(message)
        return 0

    provider = os.getenv("LLM_PROVIDER", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    if not provider or not model:
        print("error: LLM_PROVIDER and LLM_MODEL must be set in .env", file=sys.stderr)
        return 2

    try:
        reply = ask_model(message, provider, model)
    except Exception as exc:
        # The detail goes to the local log only; Telegram gets the exception type, in case the message names a URL or key.
        print(f"Report failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.telegram:
            send_telegram(f"Report failed: {type(exc).__name__}. Check the model settings and the API key in .env.", token, chat_id)
        return 1

    print(reply)
    deliver(reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
