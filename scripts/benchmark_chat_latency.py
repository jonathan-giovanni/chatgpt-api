"""Sequential, opt-in live chat benchmark. Results stay under ignored outputs/."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


def bridge_key() -> str:
    if key := os.environ.get("CHATGPT_API_KEY"):
        return key
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            if line.strip().startswith("CHATGPT_API_KEY="):
                return line.strip().split("=", 1)[1].strip().strip("\"'")
    return "local-dev-key"


def measure(client: httpx.Client, body: dict) -> dict:
    started = time.perf_counter()
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "first_text_ms": None,
              "conversation_id": body.get("conversation_id"), "done": False}
    chunks = []
    try:
        with client.stream("POST", "/v1/chat/completions", json=body) as response:
            result.update(status=response.status_code, request_id=response.headers.get("x-request-id"),
                          headers_ms=round((time.perf_counter() - started) * 1000, 3))
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    result["done"] = True
                    result["done_ms"] = round((time.perf_counter() - started) * 1000, 3)
                    continue
                event = json.loads(data)
                result["conversation_id"] = event.get("conversation_id") or result["conversation_id"]
                for choice in event.get("choices", []):
                    text = choice.get("delta", {}).get("content")
                    if text:
                        if result["first_text_ms"] is None:
                            result["first_text_ms"] = round((time.perf_counter() - started) * 1000, 3)
                        result["last_text_ms"] = round((time.perf_counter() - started) * 1000, 3)
                        chunks.append(text)
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        result["error_type"] = type(exc).__name__
    result.update(total_ms=round((time.perf_counter() - started) * 1000, 3), text="".join(chunks))
    result["ok"] = bool(result["done"] and result["conversation_id"] and chunks
                        and not result.get("error_type")
                        and not result["text"].startswith("ChatGPT provider error"))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--model", default="auto")
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--conversations", type=int, default=20)
    parser.add_argument("--follow-ups", type=int, default=3)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("outputs/request-metrics/client.jsonl"))
    args = parser.parse_args()
    if not 1 <= args.conversations <= 100 or not 0 <= args.follow_ups <= 10:
        parser.error("use 1–100 conversations and 0–10 follow-ups")
    prompt = args.prompt_file.read_text(encoding="utf-8-sig").strip()
    if not prompt:
        parser.error("prompt file must not be empty")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    with httpx.Client(base_url=args.base_url, headers={"Authorization": f"Bearer {bridge_key()}"},
                      timeout=180, trust_env=False) as client:
        projects_response = client.get("/v1/chatgpt/admin/projects")
        projects_response.raise_for_status()
        mappings = projects_response.json().get("data", [])
        if not any(args.project.casefold() in {str(p.get("name", "")).casefold(), str(p.get("alias", "")).casefold()} for p in mappings):
            raise SystemExit("Project is not registered in this bridge; configure it in Console first.")
        with args.output.open("w", encoding="utf-8") as output:
            for conversation in range(1, args.conversations + 1):
                conversation_id = None
                for turn in range(1, args.follow_ups + 2):
                    body = {"model": args.model, "chatgpt_project": args.project, "stream": True,
                            "temporary_chat": False, "messages": [{"role": "user", "content": prompt}]}
                    if conversation_id:
                        body["conversation_id"] = conversation_id
                    result = measure(client, body)
                    result.update(conversation=conversation, turn=turn, model=args.model, input_chars=len(prompt))
                    output.write(json.dumps(result, ensure_ascii=False) + "\n")
                    output.flush()
                    results.append(result)
                    print(f"chat {conversation:02d} turn {turn}: HTTP {result.get('status')}, "
                          f"first={result.get('first_text_ms')} ms, total={result.get('total_ms')} ms, ok={result['ok']}", flush=True)
                    if not result["ok"] or (conversation_id and conversation_id != result["conversation_id"]):
                        raise SystemExit("Stopped on failed response or changed conversation UUID; see local results.")
                    conversation_id = result["conversation_id"]
    for turn in range(1, args.follow_ups + 2):
        group = [r for r in results if r["turn"] == turn]
        print(json.dumps({"turn": turn, "n": len(group),
                          "mean_first_text_ms": statistics.mean(r["first_text_ms"] for r in group),
                          "mean_total_ms": statistics.mean(r["total_ms"] for r in group)}, indent=2))


if __name__ == "__main__":
    main()
