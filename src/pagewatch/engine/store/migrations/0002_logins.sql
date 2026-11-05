-- PageWatch schema v2: per-bookmark sign-in state (M6, logins).
-- Passwords never live here: a macro or a server login names a secret-store key, and the
-- database holds the key *name* only. This table holds what the engine must remember about a
-- bookmark's session across restarts.

CREATE TABLE login_state (
    bookmark_id INTEGER PRIMARY KEY REFERENCES bookmark(id) ON DELETE CASCADE,
    last_signin_at TEXT,                          -- the owner last signed in by hand
    last_auto_login_at TEXT,                      -- a macro last logged in on its own
    auto_login_blocked INTEGER NOT NULL DEFAULT 0,  -- a macro login failed: no more until reset
    needs_login_at TEXT,                          -- when the one needs-login alert was sent
    reason TEXT,                                  -- why the bookmark needs a login
    last_prompt_at TEXT                           -- the daily "sign in again?" prompt
);
