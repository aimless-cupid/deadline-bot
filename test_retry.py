"""
test_retry.py — unit test for quota-aware 429 handling (Rung 6.6).

No live calls: extract() is monkeypatched to raise crafted HTTPErrors carrying
fixture 429 bodies, and time.sleep is stubbed. Verifies:
  - a per-DAY quota 429 is NOT retried (raises DailyQuotaExceeded, one call, no sleep),
  - a per-minute 429 with retryDelay sleeps the SERVER-specified delay, then retries
    and succeeds.

Standalone, no pytest: `python test_retry.py`.
"""
import requests
import handler

# real-shaped Gemini 429 bodies (google.rpc.QuotaFailure + RetryInfo)
PERDAY_BODY = {
    "error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                         "quotaValue": "20"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"},
    ]}
}
PERMIN_BODY = {
    "error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"},
    ]}
}


class _FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def _http_error(body):
    return requests.exceptions.HTTPError(response=_FakeResp(429, body))


def run():
    sleeps = []
    orig_extract, orig_sleep = handler.extract, handler.time.sleep
    handler.time.sleep = lambda s: sleeps.append(s)     # never actually sleep
    try:
        # --- per-DAY: must NOT retry, must raise DailyQuotaExceeded ---
        calls = {"n": 0}

        def perday(_text):
            calls["n"] += 1
            raise _http_error(PERDAY_BODY)

        handler.extract = perday
        raised = False
        try:
            handler.extract_with_retry("x")
        except handler.DailyQuotaExceeded:
            raised = True
        assert raised, "per-day 429 should raise DailyQuotaExceeded"
        assert calls["n"] == 1, f"per-day should NOT retry, made {calls['n']} calls"
        assert sleeps == [], f"per-day should not sleep, slept {sleeps}"
        print("PASS: per-day 429 -> DailyQuotaExceeded, no retry, no sleep")

        # --- per-minute: sleep the SERVER retryDelay, then retry + succeed ---
        sleeps.clear()
        state = {"n": 0}

        def permin(_text):
            state["n"] += 1
            if state["n"] == 1:
                raise _http_error(PERMIN_BODY)
            return {"has_deadline": True}

        handler.extract = permin
        out = handler.extract_with_retry("x")
        assert out == {"has_deadline": True}, out
        assert state["n"] == 2, f"expected one retry, made {state['n']} calls"
        assert sleeps == [7.0], f"expected sleep 7.0s (server retryDelay), got {sleeps}"
        print("PASS: per-minute 429 -> sleeps server retryDelay 7.0s, retries, succeeds")

        print("\nALL RETRY TESTS PASSED")
    finally:
        handler.extract = orig_extract
        handler.time.sleep = orig_sleep


if __name__ == "__main__":
    run()
