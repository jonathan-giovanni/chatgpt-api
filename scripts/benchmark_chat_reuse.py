"""Paired live requirements/stream benchmark; all results remain local."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx

from benchmark_chat_latency import bridge_key, measure


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--model", default="auto")
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=10)
    parser.add_argument("--age-probes", action="store_true", help="test original tokens at 5/10/~15 min and after expiry")
    parser.add_argument("--resume-from", type=Path, help="reuse already created benchmark threads; never replay unfinished requests")
    parser.add_argument("--parallel-controls", action="store_true", help="compare sequential/parallel fresh preparation when cache is ineligible")
    parser.add_argument("--continue-results", action="store_true", help="append after recorded cases, including failures; never replay them")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("outputs/requirements-cache/client.jsonl"))
    args = parser.parse_args()
    if args.parallel_controls and args.age_probes:
        parser.error("age probes require the requirements-reuse experiment, not parallel controls")
    if not 1 <= args.pairs <= 20:
        parser.error("pairs must be between 1 and 20")
    prompt = args.prompt_file.read_text(encoding="utf-8-sig").strip()
    if not prompt:
        parser.error("prompt file is empty")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    conditions = [("fresh_eof", False, False), ("cached_eof", True, False),
                  ("fresh_done", False, True), ("cached_done", True, True)]
    if args.parallel_controls:
        conditions = [("sequential_eof", False, False), ("parallel_eof", True, False),
                      ("sequential_done", False, True), ("parallel_done", True, True)]
    probes = []
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
    if args.continue_results and args.age_probes:
        parser.error("age probes cannot resume across unknown cache seed times")
    with httpx.Client(base_url=args.base_url, headers={"Authorization": f"Bearer {bridge_key()}"},
                      timeout=180, trust_env=False) as client, args.output.open("a" if args.continue_results else "w", encoding="utf-8") as output:
        response = client.get("/v1/chatgpt/admin/projects")
        response.raise_for_status()
        project = next((p for p in response.json().get("data", []) if args.project.casefold() in {
            str(p.get("name", "")).casefold(), str(p.get("alias", "")).casefold()}), None)
        if not project:
            raise SystemExit("Project is not registered in this bridge.")

        def run(scope, number, conversation_id, condition, reuse, close):
            body = {"model": args.model, "stream": True, "temporary_chat": False,
                    "chatgpt_account": project["account"], "chatgpt_reuse_requirements": reuse,
                    "chatgpt_stream_close_on_done": close, "messages": [{"role": "user", "content": prompt}]}
            if args.parallel_controls:
                body["chatgpt_reuse_requirements"] = False
                body["chatgpt_parallel_preparation"] = reuse
            if scope == "project":
                body["chatgpt_project"] = args.project
            if conversation_id:
                body["conversation_id"] = conversation_id
            result = measure(client, body)
            result.update(scope=scope, conversation=number, condition=condition,
                          reuse=body["chatgpt_reuse_requirements"], parallel_preparation=body.get("chatgpt_parallel_preparation"),
                          close_on_done=close,
                          model=args.model, input_chars=len(prompt))
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
            output.flush()
            print(f"{scope} {number:02d} {condition}: HTTP {result.get('status')}, "
                  f"first={result.get('first_text_ms')} ms total={result['total_ms']} ms ok={result['ok']}", flush=True)
            if not result["ok"] or (conversation_id and conversation_id != result["conversation_id"]):
                raise SystemExit("Stopped on failed response or changed UUID; inspect local results, no automatic replay.")
            return result["conversation_id"]

        def due_probes():
            for probe in probes:
                while probe["ages"] and time.monotonic() >= probe["start"] + probe["ages"][0]:
                    age = probe["ages"].pop(0)
                    run(probe["scope"], 1, probe["uuid"], f"age_{age}s", True, True)

        for number in range(1, args.pairs + 1):
            for scope in ("project", "plain") if number % 2 else ("plain", "project"):
                started = time.monotonic()
                uuid = existing.get((scope, number))
                seeded = (scope, number, "initial") in completed or (scope, number, "warmup_resumed") in completed
                if not seeded:
                    uuid = run(scope, number, uuid, "warmup_resumed" if uuid else "initial", True, True)
                if args.age_probes and number == 1:
                    probes.append({"scope": scope, "uuid": uuid, "start": started, "ages": [300, 600, 885, 915]})
                offset = (number - 1) % len(conditions)
                for condition, reuse, close in conditions[offset:] + conditions[:offset]:
                    if (scope, number, condition) in completed:
                        continue
                    run(scope, number, uuid, condition, reuse, close)
                    due_probes()
        while any(probe["ages"] for probe in probes):
            due_probes()
            time.sleep(1)
    print("Completed paired benchmark and requested age probes.", flush=True)


if __name__ == "__main__":
    main()
