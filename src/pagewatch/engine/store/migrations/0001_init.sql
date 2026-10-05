-- PageWatch schema v1. Applied by store/db.py inside one transaction.
-- Every *_hash column holds the SHA-256 of the uncompressed content, which is also
-- the blob's file name. Content never goes into SQLite.

CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE folder (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES folder(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_virtual INTEGER NOT NULL DEFAULT 0,
    query_json TEXT,                              -- saved search for virtual folders
    defaults_json TEXT NOT NULL DEFAULT '{}'      -- bookmark defaults inherited by children
);

CREATE TABLE macro (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    login_signal_json TEXT
);

CREATE TABLE bookmark (
    id INTEGER PRIMARY KEY,
    folder_id INTEGER REFERENCES folder(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,                            -- http(s)://, ftp://, file:///
    source_type TEXT NOT NULL DEFAULT 'auto',     -- auto|html|feed|pdf|docx|xlsx|ftp|file|folder|image|binary|records
    check_method TEXT NOT NULL DEFAULT 'auto',    -- auto|static|browser|screenshot
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 0,          -- 1 = hotsite
    schedule_json TEXT NOT NULL,
    fetch_json TEXT NOT NULL DEFAULT '{}',        -- headers, POST body, UA, proxy, timeouts, browser options
    filter_json TEXT NOT NULL DEFAULT '{}',       -- cosmetic, watch, ignore, special
    gate_json TEXT NOT NULL DEFAULT '{}',         -- keywords, thresholds, black/whitelist
    highlight_mode TEXT NOT NULL DEFAULT 'standard',  -- standard|exact|table
    actions_json TEXT NOT NULL DEFAULT '[]',      -- actions plus alert_privacy
    macro_id INTEGER REFERENCES macro(id) ON DELETE SET NULL,
    plugin TEXT,
    info1 TEXT, info2 TEXT, info3 TEXT, note TEXT,
    status TEXT NOT NULL DEFAULT 'new',           -- new|ok|changed|error|needs_login|disabled
    unread INTEGER NOT NULL DEFAULT 0,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    current_interval_s INTEGER,                   -- adaptive state
    next_due_at TEXT,                             -- ISO-8601 UTC
    last_checked_at TEXT,
    last_changed_at TEXT,
    latest_version_id INTEGER,                    -- newest good fetch; every check compares against this
    baseline_version_id INTEGER,                  -- version at last mark-read; the viewer's diff starts here
    gate_anchor_version_id INTEGER,               -- version at last alert or read; cumulative thresholds compare here
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_bookmark_due ON bookmark(enabled, next_due_at);
CREATE INDEX ix_bookmark_folder ON bookmark(folder_id);
CREATE INDEX ix_bookmark_url ON bookmark(url);

CREATE TABLE version (
    id INTEGER PRIMARY KEY,
    bookmark_id INTEGER NOT NULL REFERENCES bookmark(id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    raw_hash TEXT,                                -- raw content (HTML, PDF, feed XML) in the blob store
    blocks_hash TEXT NOT NULL,                    -- normalized block list (JSON) in the blob store
    filtered_hash TEXT NOT NULL,                  -- hash of the comparison text; no blob
    screenshot_hash TEXT,                         -- PNG in the blob store
    http_status INTEGER, content_type TEXT,
    etag TEXT, last_modified TEXT,                -- sent back for conditional GET
    byte_size INTEGER, word_count INTEGER,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_version_bm ON version(bookmark_id, fetched_at DESC);

CREATE TABLE change (
    id INTEGER PRIMARY KEY,
    bookmark_id INTEGER NOT NULL REFERENCES bookmark(id) ON DELETE CASCADE,
    old_version_id INTEGER REFERENCES version(id),  -- what the gate compared against: previous latest, or the anchor in cumulative mode
    new_version_id INTEGER NOT NULL REFERENCES version(id),
    detected_at TEXT NOT NULL,
    added_words INTEGER, removed_words INTEGER, changed_blocks INTEGER,
    checks_accumulated INTEGER NOT NULL DEFAULT 1,  -- checks spanned by a cumulative alert
    keyword_hits_json TEXT,
    diff_hash TEXT,                               -- the gate diff (old -> new) in the blob store; what the alert reports
    summary TEXT,                                 -- first 200 chars of added text
    feedback TEXT,                                -- NULL | 'false_positive'
    read_at TEXT
);
CREATE INDEX ix_change_bm ON change(bookmark_id, detected_at DESC);
CREATE INDEX ix_change_new_version ON change(new_version_id);
CREATE INDEX ix_change_old_version ON change(old_version_id);

CREATE TABLE view_diff_cache (                    -- one row per bookmark: the viewer's baseline -> latest diff
    bookmark_id INTEGER PRIMARY KEY REFERENCES bookmark(id) ON DELETE CASCADE,
    baseline_version_id INTEGER NOT NULL,
    latest_version_id INTEGER NOT NULL,
    diff_hash TEXT NOT NULL,                      -- diff ops (JSON) in the blob store
    created_at TEXT NOT NULL
);

CREATE TABLE check_run (                          -- retained 30 days
    id INTEGER PRIMARY KEY,
    bookmark_id INTEGER NOT NULL REFERENCES bookmark(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL, finished_at TEXT,
    trigger TEXT NOT NULL,                        -- schedule|manual|catchup|retry|follow
    method TEXT NOT NULL,                         -- static|browser|screenshot|document|feed|ftp|file|records
    outcome TEXT NOT NULL,                        -- first|unchanged|changed|suppressed|error|skipped
    reason TEXT,                                  -- e.g. keyword_miss, blacklist, below_threshold, http_503
    duration_ms INTEGER, bytes INTEGER
);
CREATE INDEX ix_check_run_bm ON check_run(bookmark_id, started_at DESC);
CREATE INDEX ix_check_run_started ON check_run(started_at);

CREATE TABLE action_job (
    id INTEGER PRIMARY KEY,
    change_id INTEGER NOT NULL REFERENCES change(id) ON DELETE CASCADE,
    action_index INTEGER NOT NULL, action_type TEXT NOT NULL,
    status TEXT NOT NULL,                         -- queued|done|failed
    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, last_error TEXT,
    UNIQUE(change_id, action_index)
);
CREATE INDEX ix_action_job_status ON action_job(status, next_attempt_at);

CREATE TABLE metric (ts TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL);
CREATE INDEX ix_metric_name_ts ON metric(name, ts);

CREATE TABLE setting (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
