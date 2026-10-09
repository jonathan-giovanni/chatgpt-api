"""Paired fresh preparation benchmark; all results remain local."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from benchmark_chat_latency import bridge_key, measure

DEFAULT_MODEL = "gpt-6-mini"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=10)
    parser.add_argument("--resume-from", type=Path, help="reuse already created benchmark threads; never replay unfinished requests")
    parser.add_argument("--continue-results", action="store_true", help="append after recorded cases, including failures; never replay them")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("outputs/chat-preparation/client.jsonl"))
    args = parser.parse_args()
    if not 1 <= args.pairs <= 20:
        parser.error("pairs must be between 1 and 20")
    prompt = args.prompt_file.read_text(encoding="utf-8-sig").strip()
    if not prompt:
        parser.error("prompt file is empty")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    conditions = [("sequential", False), ("parallel", True)]
    existing = {}
    completed = set()
    if args.resume_from:
        for line in args.resume_from.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("ok") and row.get("condition") == "initial":
                existing[(row["scope"], row["conversation"])] = row["conversation_id"]
    if args.continue_results and args.output.exists():
        for line in args.output.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("model") != args.model:
                parser.error("continued results must use the same model; select a separate output for another model")
            completed.add((row["scope"], row["conversation"], row["condition"]))
            if row.get("conversation_id"):
                existing[(row["scope"], row["conversation"])] = row["conversation_id"]
    with httpx.Client(base_url=args.base_url, headers={"Authorization": f"Bearer {bridge_key()}"},
                      timeout=180, trust_env=False) as client, args.output.open("a" if args.continue_results else "w", encoding="utf-8") as output:
        response = client.get("/v1/chatgpt/admin/projects")
        response.raise_for_status()
        project = next((p for p in response.json().get("data", []) if args.project.casefold() in {
            str(p.get("name", "")).casefold(), str(p.get("alias", "")).casefold()}), None)
        if not project:
            raise SystemExit("Project is not registered in this bridge.")

        def run(scope, number, conversation_id, condition, parallel):
            body = {"model": args.model, "stream": True, "temporary_chat": False,
                    "chatgpt_account": project["account"], "chatgpt_parallel_preparation": parallel, "messages": [{"role": "user", "content": prompt}]}
            if scope == "project":
                body["chatgpt_project"] = args.project
            if conversation_id:
                body["conversation_id"] = conversation_id
            result = measure(client, body)
            result.update(scope=scope, conversation=number, condition=condition,
                          parallel_preparation=parallel,
                          model=args.model, input_chars=len(prompt))
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
            output.flush()
            print(f"{scope} {number:02d} {condition}: HTTP {result.get('status')}, "
                  f"first={result.get('first_text_ms')} ms total={result['total_ms']} ms ok={result['ok']}", flush=True)
            if not result["ok"] or (conversation_id and conversation_id != result["conversation_id"]):
                raise SystemExit("Stopped on failed response or changed UUID; inspect local results, no automatic replay.")
            return result["conversation_id"]

        for number in range(1, args.pairs + 1):
            for scope in ("project", "plain") if number % 2 else ("plain", "project"):
                uuid = existing.get((scope, number))
                seeded = (scope, number, "initial") in completed or (scope, number, "warmup_resumed") in completed
                if not seeded:
                    uuid = run(scope, number, uuid, "warmup_resumed" if uuid else "initial", True)
                offset = (number - 1) % len(conditions)
                for condition, parallel in conditions[offset:] + conditions[:offset]:
                    if (scope, number, condition) in completed:
                        continue
                    run(scope, number, uuid, condition, parallel)
    print("Completed paired preparation benchmark.", flush=True)


if __name__ == "__main__":
    main()
