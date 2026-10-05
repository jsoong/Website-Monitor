import json
import logging
from pathlib import Path

from pagewatch.engine.logs import REDACTED, configure_logging, get_logger, redact


def test_redact_by_key_and_value() -> None:
    out = redact(
        {
            "url": "https://example.com",
            "password": "hunter2",
            "headers": {"Authorization": "Bearer abcdefgh12345", "Accept": "*/*"},
            "note": "sent Authorization: Bearer abcdefghij1234 ok",
            "cookies": ["a=b"],
        }
    )
    assert out["url"] == "https://example.com"
    assert out["password"] == REDACTED
    assert out["headers"]["Authorization"] == REDACTED
    assert out["headers"]["Accept"] == "*/*"
    assert "abcdefghij1234" not in out["note"]
    assert out["cookies"] == REDACTED


def test_file_log_is_json_and_redacted_and_debug_toggles(tmp_path: Path) -> None:
    configure_logging(tmp_path, console=False, debug=False)
    log = get_logger("pagewatch.test")
    log.debug("hidden")
    log.info("check", bookmark_id=7, token="supersecret", duration_ms=12)
    logging.getLogger("uvicorn.error").warning("third party")
    from pagewatch.engine.logs import set_debug

    set_debug(True)
    log.debug("shown")
    for h in logging.getLogger().handlers:
        h.flush()
    lines = [json.loads(x) for x in (tmp_path / "engine.log").read_text().splitlines()]
    events = [x["event"] for x in lines]
    assert "hidden" not in events and "shown" in events and "third party" in events
    check = next(x for x in lines if x["event"] == "check")
    assert check["bookmark_id"] == 7 and check["token"] == REDACTED
    assert check["level"] == "info" and check["timestamp"].endswith("Z")
    configure_logging(None, console=False)  # restore: no file handler for other tests
