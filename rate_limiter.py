import time
import logging
import threading

logger = logging.getLogger("pim_ratelimit")

# ─── Token Bucket Rate Limiter ─────────────────────────────────
# Gemini 2.5 Flash Lite: 1,500 RPM (requests per minute)
# We set a conservative 30 RPM to stay well within limits
_RPM_LIMIT = 30
_INTERVAL = 60.0 / _RPM_LIMIT  # 2 seconds between requests

_lock = threading.Lock()
_last_request = 0.0


def wait_for_capacity():
    """Block until the rate limit allows a request. Thread-safe."""
    global _last_request
    with _lock:
        elapsed = time.time() - _last_request
        if elapsed < _INTERVAL:
            sleep = _INTERVAL - elapsed
            logger.debug(f"rate_limit | waiting {sleep:.1f}s")
            time.sleep(sleep)
        _last_request = time.time()


# ─── Cost Tracking ─────────────────────────────────────────────
# Gemini 2.5 Flash Lite pricing ($/1M tokens):
_PRICES = {
    "input": 0.10,
    "input_audio": 0.30,
    "output": 0.40,
}

_session_cost = 0.0
_session_requests = 0
_session_input_tokens = 0
_session_output_tokens = 0
_session_lock = threading.Lock()


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return max(1, len(text) // 4)


def track_cost(prompt: str, response_text: str = ""):
    """Track estimated cost for an LLM call."""
    global _session_cost, _session_requests, _session_input_tokens, _session_output_tokens
    in_tokens = estimate_tokens(prompt)
    out_tokens = estimate_tokens(response_text) if response_text else 0
    cost = (in_tokens / 1_000_000) * _PRICES["input"] + (out_tokens / 1_000_000) * _PRICES["output"]

    with _session_lock:
        _session_requests += 1
        _session_input_tokens += in_tokens
        _session_output_tokens += out_tokens
        _session_cost += cost

    logger.info(
        f"cost | ~{in_tokens} in + ~{out_tokens} out = ${cost:.6f} "
        f"(session: ${_session_cost:.4f}, {_session_requests} calls)"
    )
    return cost


def reset_session_cost():
    """Reset cost counters for a new session."""
    global _session_cost, _session_requests, _session_input_tokens, _session_output_tokens
    with _session_lock:
        c = _session_cost
        r = _session_requests
        inp = _session_input_tokens
        out = _session_output_tokens
        _session_cost = 0.0
        _session_requests = 0
        _session_input_tokens = 0
        _session_output_tokens = 0
    return {"cost": round(c, 4), "requests": r, "input_tokens": inp, "output_tokens": out}


def get_session_cost() -> dict:
    """Get current session cost without resetting."""
    with _session_lock:
        return {
            "cost": round(_session_cost, 4),
            "requests": _session_requests,
            "input_tokens": _session_input_tokens,
            "output_tokens": _session_output_tokens,
        }
