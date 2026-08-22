import json

from src.report import write_quality_report


def test_quality_report_writes_markdown_and_json(tmp_path):
    results = [
        {
            "symbol": "BTC/USDT",
            "raw_rows": 10,
            "clean_rows": 9,
            "start_utc": "2024-01-01T00:00:00+00:00",
            "end_utc": "2024-01-10T00:00:00+00:00",
            "quality": {
                "missing_dates": 1,
                "duplicate_rows": 0,
                "invalid_ohlc_rows": 0,
                "non_positive_price_rows": 0,
            },
        }
    ]

    markdown_path, json_path = write_quality_report(results, tmp_path, "run-1")

    assert "BTC/USDT" in markdown_path.read_text(encoding="utf-8")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-1"
    assert payload["datasets"][0]["quality"]["missing_dates"] == 1
