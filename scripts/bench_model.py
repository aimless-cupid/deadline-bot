"""
bench_model.py — A/B bench for the extraction model (Rung 6.6).

Runs a fixed sample set through the real prompt and schema (imported from
extractor.py) against a given model id, printing per call: latency, HTTP status,
usageMetadata (including thoughtsTokenCount if the model reports thinking), and
the parsed dict. On a non-200 it prints the response body, which is how the
free-tier daily quota wall turned up.

No thinkingConfig is sent. Gemini 3.x controls thinking with thinkingLevel, not
2.5's thinkingBudget, and setting both is an API error. If latency is already
~1-2s, thinking is off by default and nothing is needed.

Usage:  python scripts/bench_model.py [model_id]     (default: gemini-3.1-flash-lite)
"""
import os
import sys
import json
import time

import requests
from dotenv import load_dotenv

# reuse the real prompt + schema so the bench measures what prod actually runs
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extractor import PROMPT, SCHEMA

load_dotenv()
KEY = os.environ["GEMINI_API_KEY"]

# (announcement, expectation) — spans college + general deadlines; the two
# negatives (social events / date-mentions with no action) MUST stay negative.
SAMPLES = [
    ("DBMS assignment 3 is due this Sunday on the portal.",
     {"has_deadline": True, "when_text": "this Sunday"}),
    ("Last date to pay the semester exam fee is the 25th.",
     {"has_deadline": True, "when_text": "the 25th"}),
    ("Come for biryani night this Friday at the hostel mess!",
     {"has_deadline": False}),
    ("Ram's birthday party is on Saturday, come by!",
     {"has_deadline": False}),
    ("Your electricity bill of Rs 2400 is due on the 20th.",
     {"has_deadline": True, "when_text": "the 20th"}),
    ("Car insurance renewal deadline is 5 August.",
     {"has_deadline": True, "when_text": "5 August"}),
    ("Project submissions open soon, dates to be announced.",
     {"has_deadline": True, "when_text": None}),
]

SPACING_S = 5   # ~respect 15 RPM


def call(model, text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": KEY, "Content-Type": "application/json"}
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(announcement=text)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "temperature": 0,
        },
        # no thinkingConfig on purpose; see the module docstring.
    }
    t0 = time.perf_counter()
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    return resp, time.perf_counter() - t0


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.1-flash-lite"
    print(f"model: {model}\n")
    latencies, all_ok = [], True
    for i, (text, expect) in enumerate(SAMPLES):
        resp, dt = call(model, text)
        print(f"[{i+1}] {dt:.2f}s  HTTP {resp.status_code}")
        if resp.status_code != 200:
            print("    BODY:", resp.text)              # non-200: show the body (quota details live here)
            all_ok = False
        else:
            latencies.append(dt)
            j = resp.json()
            usage = j.get("usageMetadata", {})
            keys = ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "totalTokenCount")
            print("    usage:", {k: usage[k] for k in keys if k in usage})
            parsed = json.loads(j["candidates"][0]["content"]["parts"][0]["text"])
            ok = parsed.get("has_deadline") == expect["has_deadline"]
            if "when_text" in expect:
                ok = ok and parsed.get("when_text") == expect["when_text"]
            all_ok = all_ok and ok
            print(f"    parsed: {parsed}")
            print(f"    expect: {expect}  -> {'OK' if ok else 'MISMATCH'}")
        if i < len(SAMPLES) - 1:
            time.sleep(SPACING_S)
    if latencies:
        print(f"\nlatency: min={min(latencies):.2f}s max={max(latencies):.2f}s "
              f"avg={sum(latencies)/len(latencies):.2f}s (n={len(latencies)})")
    print("GATE extraction+schema:", "PASS" if all_ok else "FAIL")


if __name__ == "__main__":
    main()
