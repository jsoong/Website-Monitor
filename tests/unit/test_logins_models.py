"""M6 logins: model rules, the migration, the repo helpers and routing."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from pagewatch.engine.fetch.select import BROWSER, STATIC, route_for
from pagewatch.engine.paths import DataDir
from pagewatch.engine.store import repo
from pagewatch.engine.store.db import connect, latest_schema_version, migrate
from pagewatch.models import (
    AuthConfig,
    CookieImportIn,
    ExperimentIn,
    FetchConfig,
    LoginConfig,
    Settings,
    check_proxy_url,
)

# -- proxies ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "proxy",
    [
        "http://proxy:3128",
        "https://proxy.example",
        "socks5://127.0.0.1:1080",
        "socks5h://p:9",
        "HTTP://P:80/",
    ],
)
def test_proxy_addresses_in_the_three_families_are_accepted(proxy: str) -> None:
    assert check_proxy_url(proxy) == proxy.rstrip("/")
    assert FetchConfig(proxy=proxy).proxy == proxy.rstrip("/")
    assert Settings(global_proxy=proxy).global_proxy == proxy.rstrip("/")


@pytest.mark.parametrize(
    "proxy,why",
    [
        ("ftp://proxy", "starts with"),
        ("proxy:3128", "starts with"),
        ("http://", "no host"),
        ("http://bob:pw@proxy", "do not put a user name or password"),
        ("http://bob@proxy", "do not put a user name or password"),
        ("http://proxy:99999", "not a proxy address"),
        ("http://proxy/with/path", "nothing after it"),
        ("http://proxy?x=1", "nothing after it"),
    ],
)
def test_proxy_addresses_that_cannot_work_or_hold_a_password_are_refused(
    proxy: str, why: str
) -> None:
    with pytest.raises(ValueError, match=why):
        check_proxy_url(proxy)
    with pytest.raises(ValidationError):
        FetchConfig(proxy=proxy)
    with pytest.raises(ValidationError):
        Settings(global_proxy=proxy)


def test_a_blank_proxy_means_none_and_a_proxy_login_needs_both_halves() -> None:
    assert FetchConfig(proxy="  ").proxy is None
    assert Settings(global_proxy="").global_proxy is None
    ok = FetchConfig(proxy="http://p:1", proxy_username="bob", proxy_secret_key="proxy-pw")
    assert ok.proxy_username == "bob"
    for bad in (
        {"proxy": "http://p:1", "proxy_username": "bob"},
        {"proxy": "http://p:1", "proxy_secret_key": "k"},
        {"proxy_username": "bob", "proxy_secret_key": "k"},  # a login without a proxy
        {"proxy": "http://p:1", "proxy_username": "bob", "proxy_secret_key": "bad key!"},
    ):
        with pytest.raises(ValidationError):
            FetchConfig.model_validate(bad)


# -- logins ------------------------------------------------------------------------------------


def test_secret_names_are_restricted_wherever_they_appear() -> None:
    AuthConfig(username="a", secret_key="ok.name:1_x-y")
    for bad in ("", "has space", "semi;colon", "x" * 65):
        with pytest.raises(ValidationError):
            AuthConfig(username="a", secret_key=bad)
    with pytest.raises(ValidationError):
        AuthConfig(username="", secret_key="k")


def test_login_policy_defaults_to_the_specs_six_hours_and_will_not_go_below_one() -> None:
    assert LoginConfig().min_login_interval_s == 6 * 3600
    assert LoginConfig(min_login_interval_s=3600).min_login_interval_s == 3600
    with pytest.raises(ValidationError):
        LoginConfig(min_login_interval_s=3599)
    assert LoginConfig(signin_url=" https://x.test/login ").signin_url == "https://x.test/login"
    assert LoginConfig(signin_url="  ").signin_url is None
    with pytest.raises(ValidationError):
        LoginConfig(signin_url="ftp://x")


def test_the_new_settings_have_safe_defaults() -> None:
    s = Settings()
    assert s.stored_password_logins is False  # until the portal experiment is recorded
    assert s.stretch_hosts is True and s.signin_prompt_enabled is True
    assert s.signin_prompt_time == "09:00"
    with pytest.raises(ValidationError):
        Settings(signin_prompt_time="9am")


def test_cookie_import_takes_text_or_a_path_but_not_both_or_neither() -> None:
    CookieImportIn(text="x")
    CookieImportIn(path="/a/b")
    for bad in ({}, {"text": "x", "path": "/a"}):
        with pytest.raises(ValidationError):
            CookieImportIn.model_validate(bad)


def test_experiment_parameters_follow_the_spec() -> None:
    e = ExperimentIn(bookmark_id=1)
    assert e.probe_minutes == [10, 20, 30, 60, 120] and e.duration_h == 48 and e.logins_per_day == 3
    for bad in (
        {"logins_per_day": 1},  # the spec asks for two to four a day
        {"logins_per_day": 5},
        {"probe_minutes": [20, 10]},
        {"probe_minutes": [10, 10]},
        {"probe_minutes": [0, 5]},
        {"probe_minutes": []},
        {"duration_h": 0},
    ):
        with pytest.raises(ValidationError):
            ExperimentIn.model_validate({"bookmark_id": 1, **bad})


# -- the database -----------------------------------------------------------------------------


def make_db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "db.sqlite"
    assert migrate(path) == latest_schema_version() >= 2
    return connect(path)


def add_bookmark(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO bookmark(name,url,schedule_json,created_at,updated_at) "
        "VALUES('b','http://x/','{}','t','t')"
    )
    return int(conn.execute("SELECT MAX(id) FROM bookmark").fetchone()[0])


def test_login_state_is_created_on_demand_updated_in_part_and_removed_with_the_bookmark(
    tmp_path: Path,
) -> None:
    conn = make_db(tmp_path)
    bid = add_bookmark(conn)
    assert repo.login_state_get(conn, bid) is None
    repo.login_state_set(conn, bid, last_signin_at="2026-01-01T00:00:00Z")
    repo.login_state_set(conn, bid, reason="login_signal", auto_login_blocked=1)
    row = repo.login_state_get(conn, bid)
    assert row is not None
    assert (row["last_signin_at"], row["reason"], row["auto_login_blocked"]) == (
        "2026-01-01T00:00:00Z", "login_signal", 1,
    )  # fmt: skip
    repo.login_state_set(conn, bid, reason=None)
    row = repo.login_state_get(conn, bid)
    assert row is not None and row["reason"] is None and row["last_signin_at"]  # the rest stays
    assert set(repo.login_state_all(conn)) == {bid}
    with pytest.raises(ValueError, match="unknown login_state field"):
        repo.login_state_set(conn, bid, password="no")
    conn.execute("DELETE FROM bookmark WHERE id=?", (bid,))
    assert repo.login_state_get(conn, bid) is None  # ON DELETE CASCADE


def test_macros_are_stored_listed_counted_and_unlinked_when_deleted(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    mid = repo.macro_insert(conn, "b macro", "[]", None)
    repo.macro_insert(conn, "A macro", "[]", '{"text":"x"}')
    assert [r["name"] for r in repo.macro_list(conn)] == ["A macro", "b macro"]  # nocase
    bid = add_bookmark(conn)
    conn.execute("UPDATE bookmark SET macro_id=? WHERE id=?", (mid, bid))
    got = repo.macro_get(conn, mid)
    assert got is not None and got["bookmarks"] == 1
    assert repo.macro_update(conn, mid, "renamed", "[1]", None)
    assert not repo.macro_update(conn, 999, "x", "[]", None)
    assert repo.macro_delete(conn, mid) and not repo.macro_delete(conn, mid)
    row = conn.execute("SELECT macro_id FROM bookmark WHERE id=?", (bid,)).fetchone()
    assert row["macro_id"] is None  # the bookmark outlives its macro


def test_a_version_one_database_is_upgraded_in_place_with_a_backup(tmp_path: Path) -> None:
    from importlib import resources

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    first = (resources.files("pagewatch.engine.store") / "migrations" / "0001_init.sql").read_text(
        "utf-8"
    )
    conn.executescript(first + "DELETE FROM schema_version; INSERT INTO schema_version VALUES(1);")
    conn.execute(
        "INSERT INTO bookmark(name,url,schedule_json,created_at,updated_at) "
        "VALUES('keep','http://x/','{}','t','t')"
    )
    conn.commit()
    conn.close()
    backups = tmp_path / "bk"
    assert migrate(path, backups) == latest_schema_version()
    assert list(backups.glob("pre-migration-v1-*.db"))  # the upgrade is preceded by a backup
    after = connect(path)
    assert after.execute("SELECT name FROM bookmark").fetchone()["name"] == "keep"
    assert after.execute("SELECT COUNT(*) FROM login_state").fetchone()[0] == 0


# -- places and routing ----------------------------------------------------------------------


def test_the_data_folder_has_places_for_cookies_and_experiments(tmp_path: Path) -> None:
    d = DataDir(tmp_path / "data").ensure()
    assert d.cookies_dir.is_dir() and d.experiments_dir.is_dir() and d.profiles_dir.is_dir()
    assert d.cookies_dir.name == "cookies" and d.experiments_dir.name == "experiments"


def test_a_signed_in_bookmark_always_goes_through_the_browser() -> None:
    url = "https://portal.example/account"
    assert route_for(url, "auto", "auto") == STATIC
    assert route_for(url, "auto", "auto", signed_in=True) == BROWSER
    assert route_for(url, "auto", "static", signed_in=True) == BROWSER
    shot = route_for(url, "auto", "screenshot", signed_in=True)
    assert shot is not None and shot.name == "screenshot"  # an explicit screenshot stays one
    ftp = route_for("ftp://x/y", "auto", "auto", signed_in=True)
    assert ftp is not None and ftp.name == "ftp"  # only web pages have a browser session
