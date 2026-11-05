"""The Netscape cookies.txt importer: formats, expiry, summaries, and what reaches the jars."""

from __future__ import annotations

import httpx
import pytest

from pagewatch.engine.fetch import cookies as ck

NOW = 1_800_000_000.0
FILE = (
    "# Netscape HTTP Cookie File\n"
    "# comment line\n"
    "\n"
    ".example.com\tTRUE\t/\tTRUE\t1900000000\tsid\tabc\n"
    "www.example.com\tFALSE\t/app\tFALSE\t0\tpref\tdark\n"
    "#HttpOnly_.example.com\tTRUE\t/\tTRUE\t1900000000\ttoken\tsecret-value\n"
    ".example.com\tTRUE\t/\tFALSE\t1700000000\told\tgone\n"
)


def test_a_cookies_txt_is_read_field_by_field() -> None:
    parsed = ck.parse_netscape(FILE)
    names = [c.name for c in parsed.cookies]
    assert names == ["sid", "pref", "token", "old"] and parsed.skipped == 0
    sid, pref, token, old = parsed.cookies
    assert sid.include_subdomains and sid.secure and sid.expires == 1900000000
    assert not pref.include_subdomains and pref.expires is None and pref.path == "/app"
    assert token.http_only and token.domain == ".example.com"
    assert old.expired(NOW) and not sid.expired(NOW)


def test_lines_that_cannot_be_read_are_counted_not_fatal() -> None:
    parsed = ck.parse_netscape(
        "garbage line\n.a.com\tTRUE\t/\tMAYBE\t0\tn\tv\n.a.com\tTRUE\t/\tFALSE\tsoon\tn\tv\n"
        ".a.com\tTRUE\t/\tFALSE\t0\tgood\tv\n"
    )
    assert [c.name for c in parsed.cookies] == ["good"] and parsed.skipped == 3


def test_space_separated_files_are_accepted_and_a_bom_is_ignored() -> None:
    parsed = ck.parse_netscape("﻿.a.com TRUE / FALSE 0 name value\n")
    assert len(parsed.cookies) == 1 and parsed.cookies[0].value == "value"


def test_summary_counts_without_revealing_values() -> None:
    parsed = ck.parse_netscape(FILE)
    status = ck.summarize(parsed, NOW)
    assert status.count == 4 and status.expired == 1
    assert status.domains == ["example.com", "www.example.com"]
    assert status.earliest_expiry == "2030-03-17T17:46:40Z"
    assert "secret-value" not in status.model_dump_json()


def test_unreadable_files_raise_a_clear_error(tmp_path: pytest.TempPathFactory) -> None:
    with pytest.raises(ck.CookieFileError, match="cannot read"):
        ck.load_cookie_file(str(tmp_path) + "/missing.txt")  # type: ignore[operator]


def test_cookies_reach_an_httpx_jar_only_where_they_apply() -> None:
    jar = httpx.Cookies()
    added = ck.add_to_httpx(jar, ck.parse_netscape(FILE).cookies, NOW)
    assert added == 3  # the expired one is left out
    client = httpx.Client(cookies=jar)
    https = client.build_request("GET", "https://www.example.com/x")
    assert "sid=abc" in https.headers["cookie"] and "token=secret-value" in https.headers["cookie"]
    plain = client.build_request("GET", "http://www.example.com/x")
    assert "sid=abc" not in plain.headers.get("cookie", "")  # a Secure cookie stays on https
    other = client.build_request("GET", "https://other.org/")
    assert "cookie" not in other.headers


def test_a_one_label_host_such_as_localhost_still_matches() -> None:
    jar = httpx.Cookies()
    ck.add_to_httpx(jar, ck.parse_netscape("localhost\tFALSE\t/\tFALSE\t0\ts\tv\n").cookies, NOW)
    req = httpx.Client(cookies=jar).build_request("GET", "http://localhost:8080/")
    assert req.headers["cookie"] == "s=v"


def test_playwright_cookies_carry_domain_expiry_and_flags() -> None:
    out = ck.to_playwright(ck.parse_netscape(FILE).cookies, NOW)
    by_name = {c["name"]: c for c in out}
    assert set(by_name) == {"sid", "pref", "token"}
    assert by_name["sid"]["domain"] == ".example.com" and by_name["sid"]["secure"]
    assert by_name["pref"]["domain"] == "www.example.com" and by_name["pref"]["expires"] == -1.0
    assert by_name["token"]["httpOnly"] is True
