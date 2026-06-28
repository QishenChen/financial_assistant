#!/usr/bin/env python3
"""
Chat CLI — development interface for the financial document intelligence platform.

Usage:
    python3 chat.py "中国建筑有哪些施工项目城市？"
    python3 chat.py "Compare R&D spending of BYD vs CATL 2022-2024"
    python3 chat.py  (interactive mode)
"""

import sys
import json
import os

# Ensure cwd is project root
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from agent.executor import execute
from agent.llm_reasoner import get_llm_config


def format_response(result: dict) -> str:
    """Format execution result for terminal display."""
    lines = []
    lines.append("=" * 60)
    lines.append(f"Pipeline:  {result.get('task_type', 'unknown')}")
    lines.append(f"Objective: {result.get('overall_objective', result.get('objective', ''))}")
    lines.append(f"Confidence: {result.get('confidence', 0.0):.0%}")
    lines.append("=" * 60)

    # Steps summary
    steps_log = result.get("steps_log", [])
    if steps_log:
        lines.append("")
        lines.append("EXECUTION STEPS:")
        for s in steps_log:
            icon = "✓" if s.get("verdict") == "PASS" else "✗"
            lines.append(f"  [{icon}] Step {s['step']} ({s['task_type']}) — {s.get('audit_detail','')[:80]}")
            if s.get("retries", 0) > 0:
                lines.append(f"       Retries: {s['retries']}")
        lines.append("")

    lines.append("ANSWER:")
    lines.append("-" * 40)
    lines.append(result.get("answer", "(no answer)")[:2000])
    fp = result.get("file_path", "")
    if fp:
        lines.append(f"\n📄 Full report saved to: {fp}")
    lines.append("-" * 40)

    # Token usage
    tokens = result.get("token_usage", {})
    if tokens:
        total_p = tokens.get("plan_prompt", 0) + tokens.get("exec_prompt", 0)
        total_c = tokens.get("plan_completion", 0) + tokens.get("exec_completion", 0)
        lines.append(f"\nTokens: {total_p} prompt + {total_c} completion = {total_p + total_c} total")

    error = result.get("error")
    if error:
        lines.append(f"\n⚠ Error: {error}")

    return "\n".join(lines)


def interactive_mode():
    """Run an interactive REPL loop."""
    config = get_llm_config()
    print(f"Financial Document Intelligence Platform")
    print(f"Model: {config.get('model', 'unknown')}")
    print(f"API:   {config.get('api_base', 'unknown')}")
    print(f"Key:   {'configured' if config.get('api_key') else 'NOT CONFIGURED — set LLM_API_KEY in .env'}")
    print()
    print("Type a question and press Enter. Type 'quit' or Ctrl+C to exit.")
    print()

    while True:
        try:
            query = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not query:
            continue
        if query.lower() in ("quit", "exit", "q"):
            print("Goodbye.")
            break

        print(f"\nProcessing: {query[:80]}{'...' if len(query) > 80 else ''}")
        print()

        result = execute(query)
        print(format_response(result))
        print()


def single_query(query: str):
    """Run a single query and exit."""
    print(f"Processing: {query[:80]}{'...' if len(query) > 80 else ''}")
    print()
    result = execute(query)
    print(format_response(result))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Single query mode
        query = " ".join(sys.argv[1:])
        single_query(query)
    else:
        # Interactive mode
        interactive_mode()