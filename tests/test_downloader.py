import ccxt

from src.download_data import call_with_retry


def test_retry_retries_rate_limit_with_exponential_backoff():
    attempts = []
    sleeps = []

    def operation():
        attempts.append(1)
        if len(attempts) < 3:
            raise ccxt.RateLimitExceeded("slow down")
        return "ok"

    result = call_with_retry(
        operation,
        max_retries=4,
        backoff_base_seconds=0.5,
        sleep=sleeps.append,
    )

    assert result == "ok"
    assert len(attempts) == 3
    assert sleeps == [0.5, 1.0]
