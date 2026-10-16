"""Evaluate the dedicated OpenRouter Cerebras route without placing calls or uploading recordings."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from app.coaching.replay import evaluate
from app.providers.groq import GroqCoach
from app.providers.openrouter import OpenRouterCoach

parser = argparse.ArgumentParser()
parser.add_argument("--fixtures", type=Path, default=Path("packages/evaluation-fixtures/coach.jsonl"))
parser.add_argument("--provider", choices=["openrouter", "groq"], default="openrouter")
parser.add_argument("--models", nargs="+", default=["openai/gpt-oss-120b"])
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
key_name = "OPENROUTER_API_KEY" if args.provider == "openrouter" else "GROQ_API_KEY"
if not os.getenv(key_name):
    raise SystemExit(
        f"Set {key_name} in your protected environment to run live model inference. No calls are placed."
    )
cases = [json.loads(line) for line in args.fixtures.read_text().splitlines() if line.strip()]


async def main():
    reports = []
    for model in args.models:
        reports.append(
            await evaluate(
                cases,
                (OpenRouterCoach if args.provider == "openrouter" else GroqCoach)(
                    os.environ[key_name], model
                ),
                model=model,
            )
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(reports, indent=2) + "\n")
    print("Replay report written. This measures inference and grounding, not live-call outcome lift.")


asyncio.run(main())
