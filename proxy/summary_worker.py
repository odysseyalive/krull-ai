#!/usr/bin/env python3
"""
Background summary worker for Krull SSE proxy.

- Polls JOB_DIR for {fingerprint}.job.json files
- For each job, calls LiteLLM (local: http://krull-litellm:4000/v1/chat/completions)
  to produce an abstractive summary, writes STORE_DIR/{fingerprint}.summary.json
  and deletes the job file on success.

Environment variables:
- JOB_DIR (default /app/context_jobs)
- STORE_DIR (default /app/context_store)
- LITELLM_URL (default http://krull-litellm:4000/v1/chat/completions)
- SUMMARY_MODEL (optional; default empty -> let LiteLLM mapping choose)
- CONTEXT_SUMMARY_MAX_TOKENS (optional)
"""
import os
import time
import json
import requests
from pathlib import Path

JOB_DIR = Path(os.environ.get("KRULL_CONTEXT_JOB_DIR", "/app/context_jobs"))
STORE_DIR = Path(os.environ.get("KRULL_CONTEXT_STORE_DIR", "/app/context_store"))
LITELLM_URL = os.environ.get("KRULL_LITELLM_URL", "http://krull-litellm:4000/v1/chat/completions")
SUMMARY_MODEL = os.environ.get("CONTEXT_SUMMARY_MODEL", "")  # if empty, let server choose

# Small per-job max tokens for summary output (adjustable)
SUMMARY_MAX_TOKENS = int(os.environ.get("CONTEXT_SUMMARY_MAX_TOKENS", "800"))


def summarize_job(job_path: Path):
    try:
        with job_path.open("r", encoding="utf-8") as fh:
            job = json.load(fh)
    except Exception as e:
        print(f"[worker] failed to read job {job_path}: {e}")
        job_path.unlink(missing_ok=True)
        return

    fingerprint = job.get("fingerprint")
    old_messages = job.get("messages", [])

    # Build a compact prompt for the summarizer model
    # We want concise, factual summary of the older conversation
    user_prompt = "Summarize the following conversation history into a concise, factual summary (no opinions). Keep it focused and preserve key facts, actions, decisions, and outstanding tasks.\n\n"
    for m in old_messages:
        role = m.get("role", "unknown")
        content = m.get("content", "")
        if isinstance(content, list):
            # flatten
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        user_prompt += f"[{role}] {content}\n"

    payload = {
        "model": SUMMARY_MODEL or "claude-haiku-4-5",
        "messages": [{"role": "user", "content": user_prompt}],
        "max_tokens": SUMMARY_MAX_TOKENS,
        "temperature": 0.3,
    }

    try:
        resp = requests.post(LITELLM_URL, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        # Try to extract content robustly
        summary = ""
        if "choices" in data and len(data["choices"]) > 0:
            choice = data["choices"][0]
            if "message" in choice and isinstance(choice["message"], dict):
                summary = choice["message"].get("content", "")
            else:
                summary = choice.get("text", "")
        # fallback to data.get('output') etc if needed
        if not summary:
            # as a last resort, join any text fields
            summary = json.dumps(data)[:2000]
    except Exception as e:
        print(f"[worker] summarization failed for {fingerprint}: {e}")
        # keep the job file for retry later
        return

    # write the summary file (atomic write)
    summary_path = STORE_DIR / f"{fingerprint}.summary.json"
    try:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = summary_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump({"fingerprint": fingerprint, "ts": int(time.time()), "summary": summary}, fh)
        tmp.replace(summary_path)
        # remove job file
        job_path.unlink(missing_ok=True)
        print(f"[worker] wrote summary for {fingerprint}")
    except Exception as e:
        print(f"[worker] failed to write summary {summary_path}: {e}")


def main_loop():
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    print("[worker] starting, JOB_DIR=", JOB_DIR, "STORE_DIR=", STORE_DIR)
    while True:
        jobs = list(JOB_DIR.glob("*.job.json"))
        if not jobs:
            time.sleep(1)
            continue
        for job in jobs:
            try:
                summarize_job(job)
            except Exception as e:
                print("[worker] job exception:", e)
        # small pause to avoid busy-looping
        time.sleep(0.2)

if __name__ == "__main__":
    main_loop()
