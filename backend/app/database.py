"""SQLite storage for registered applications and their lifecycle events."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,
    description   TEXT    NOT NULL DEFAULT '',
    command       TEXT    NOT NULL,
    working_dir   TEXT    NOT NULL,
    port          INTEGER,
    env           TEXT    NOT NULL DEFAULT '{}',
    auto_restart  INTEGER NOT NULL DEFAULT 0,
    start_on_boot INTEGER NOT NULL DEFAULT 0,
    share_lan     INTEGER NOT NULL DEFAULT 0,
    live_reload   INTEGER NOT NULL DEFAULT 0,
    preview_url   TEXT    NOT NULL DEFAULT '',
    news_enabled  INTEGER NOT NULL DEFAULT 1,
    news_keywords TEXT    NOT NULL DEFAULT '',
    -- Focus Mode. needs is a JSON list of app ids this app cannot run without;
    -- protected apps are never stopped by Focus Mode; uses_models names the
    -- Ollama models it talks to, comma-separated, so the rest can be unloaded.
    needs         TEXT    NOT NULL DEFAULT '[]',
    protected     INTEGER NOT NULL DEFAULT 0,
    uses_models   TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    type       TEXT    NOT NULL,
    message    TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_app ON events(app_id, id DESC);

-- The news ticker. app_name is copied rather than joined so a headline about
-- an app outlives the app, and app_id is NULL for news about God Father itself.
CREATE TABLE IF NOT EXISTS news (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     INTEGER REFERENCES apps(id) ON DELETE SET NULL,
    app_name   TEXT    NOT NULL DEFAULT '',
    level      TEXT    NOT NULL DEFAULT 'info',
    source     TEXT    NOT NULL DEFAULT 'event',
    message    TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

-- Webcam stills taken when an application starts, stops or dies. app_id is
-- nulled rather than cascaded when an app is deleted: the point of the record
-- is what happened, and removing the app should not erase the evidence.
CREATE TABLE IF NOT EXISTS captures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     INTEGER REFERENCES apps(id) ON DELETE SET NULL,
    app_name   TEXT    NOT NULL DEFAULT '',
    kind       TEXT    NOT NULL,
    message    TEXT    NOT NULL DEFAULT '',
    file       TEXT    NOT NULL DEFAULT '',
    -- Folder of frames preserved for this capture, if any. Only a power cut
    -- gets one: every other event's surroundings are still in the ring.
    clip       TEXT    NOT NULL DEFAULT '',
    ok         INTEGER NOT NULL DEFAULT 0,
    note       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_captures_recent ON captures(id DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Ports published to the internet through Cloudflare Tunnel. These rows are
-- the source of truth: ~/.cloudflared/config.yml is generated from them, so a
-- route survives switching between quick and named tunnels. app_id points at
-- the application row that runs it, and is nulled rather than cascaded so that
-- deleting the row leaves the route to be switched back on later.
-- guard_* protect a route that would otherwise be naked on the internet.
-- guard_header_name/value are injected on the way to the application, so a
-- service that wants a credential still gets one from a caller that cannot
-- send it. guard_secret is a random path prefix the caller must know. Either
-- one puts a small proxy in front of the port, on guard_port, and the tunnel
-- then serves that instead — see guard.py.
CREATE TABLE IF NOT EXISTS tunnel_routes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    port               INTEGER NOT NULL UNIQUE,
    hostname           TEXT    NOT NULL DEFAULT '',
    enabled            INTEGER NOT NULL DEFAULT 0,
    app_id             INTEGER REFERENCES apps(id) ON DELETE SET NULL,
    metrics_port       INTEGER,
    public_url         TEXT    NOT NULL DEFAULT '',
    last_error         TEXT    NOT NULL DEFAULT '',
    guard_header_name  TEXT    NOT NULL DEFAULT '',
    guard_header_value TEXT    NOT NULL DEFAULT '',
    guard_secret       TEXT    NOT NULL DEFAULT '',
    guard_port         INTEGER,
    guard_app_id       INTEGER REFERENCES apps(id) ON DELETE SET NULL,
    guard_oauth        INTEGER NOT NULL DEFAULT 0,
    guard_oauth_password TEXT  NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

-- Readings taken from each live tunnel's own metrics endpoint. cloudflared
-- reports counters that only ever climb, so a rate exists only once two
-- readings can be subtracted — which is the whole reason these are stored
-- rather than read straight through to the dashboard. Swept on a age.
CREATE TABLE IF NOT EXISTS tunnel_samples (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id       INTEGER NOT NULL REFERENCES tunnel_routes(id) ON DELETE CASCADE,
    ts             TEXT    NOT NULL,
    requests       INTEGER NOT NULL DEFAULT 0,
    errors         INTEGER NOT NULL DEFAULT 0,
    concurrent     INTEGER NOT NULL DEFAULT 0,
    connections    INTEGER NOT NULL DEFAULT 0,
    bytes_sent     INTEGER NOT NULL DEFAULT 0,
    bytes_received INTEGER NOT NULL DEFAULT 0,
    rtt_ms         REAL    NOT NULL DEFAULT 0,
    edge_location  TEXT    NOT NULL DEFAULT '',
    codes          TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tunnel_samples ON tunnel_samples(route_id, id DESC);

-- The machine's own vital signs, on a slower timer than the gauges read. Kept
-- so that "when was VRAM in use and when was it RAM" has an answer at all — a
-- live reading can only ever describe this second.
CREATE TABLE IF NOT EXISTS system_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    cpu_percent     REAL    NOT NULL DEFAULT 0,
    cpu_freq_mhz    REAL    NOT NULL DEFAULT 0,
    cpu_temp        REAL    NOT NULL DEFAULT 0,
    load1           REAL    NOT NULL DEFAULT 0,
    mem_used        INTEGER NOT NULL DEFAULT 0,
    mem_total       INTEGER NOT NULL DEFAULT 0,
    swap_used       INTEGER NOT NULL DEFAULT 0,
    disk_used       INTEGER NOT NULL DEFAULT 0,
    disk_total      INTEGER NOT NULL DEFAULT 0,
    disk_read_rate  REAL    NOT NULL DEFAULT 0,
    disk_write_rate REAL    NOT NULL DEFAULT 0,
    net_sent_rate   REAL    NOT NULL DEFAULT 0,
    net_recv_rate   REAL    NOT NULL DEFAULT 0,
    gpu_percent     REAL    NOT NULL DEFAULT 0,
    gpu_temp        REAL    NOT NULL DEFAULT 0,
    gpu_power       REAL    NOT NULL DEFAULT 0,
    vram_used       INTEGER NOT NULL DEFAULT 0,
    vram_total      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_system_samples ON system_samples(id DESC);

-- A threshold crossed, with whatever was holding the resource at the time.
-- Kept a week rather than a day: they are rare and small, and the point of an
-- event is being able to look back further than the charts reach.
CREATE TABLE IF NOT EXISTS system_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    level     TEXT NOT NULL DEFAULT 'warning',
    message   TEXT NOT NULL DEFAULT '',
    value     REAL NOT NULL DEFAULT 0,
    threshold REAL NOT NULL DEFAULT 0,
    culprits  TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_system_events ON system_events(id DESC);

-- Which application is linked to which model, and on whose word. `source` is
-- the whole point of the row: 'declared' was typed in by hand, 'observed' was
-- actually seen in use, 'detected' was read out of the application's own
-- source. They are stored side by side rather than merged so that a guess is
-- never mistaken for a fact — and so a re-scan can replace its own findings
-- without touching either of the other two. model is '' for a link that knows
-- the provider but not yet which model, which is the normal state of a socket.
CREATE TABLE IF NOT EXISTS ai_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    provider   TEXT    NOT NULL DEFAULT '',
    model      TEXT    NOT NULL DEFAULT '',
    source     TEXT    NOT NULL DEFAULT 'detected',
    kind       TEXT    NOT NULL DEFAULT '',
    evidence   TEXT    NOT NULL DEFAULT '',
    first_seen TEXT    NOT NULL,
    last_seen  TEXT    NOT NULL,
    UNIQUE(app_id, provider, model, source)
);

CREATE INDEX IF NOT EXISTS idx_ai_links_app ON ai_links(app_id);

-- One model call, with its token count. app_id is nulled rather than cascaded
-- when an application is removed: what was spent was still spent, and a bill
-- that shrinks when you delete a row is not a bill. Rows arrive either from an
-- application posting its own usage or from its captured log being read.
CREATE TABLE IF NOT EXISTS ai_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    app_id        INTEGER REFERENCES apps(id) ON DELETE SET NULL,
    app_name      TEXT    NOT NULL DEFAULT '',
    provider      TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT 'ingest',
    endpoint      TEXT    NOT NULL DEFAULT '',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0,
    latency_ms    REAL    NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 1,
    error         TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ai_calls_ts ON ai_calls(ts);
CREATE INDEX IF NOT EXISTS idx_ai_calls_recent ON ai_calls(id DESC);

-- Open connections to a provider, sampled on a timer. This is the only history
-- an uninstrumented application leaves behind — it cannot say how many calls
-- it made, but the record that it was talking at all is worth keeping, and it
-- is what stops the chart being empty on a machine that counts no tokens.
CREATE TABLE IF NOT EXISTS ai_activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    app_id      INTEGER NOT NULL,
    app_name    TEXT    NOT NULL DEFAULT '',
    provider    TEXT    NOT NULL DEFAULT '',
    connections INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ai_activity ON ai_activity(ts);

-- OAuth clients that registered themselves against a guarded route. claude.ai
-- and most MCP clients do not have credentials issued to them by hand — they
-- call the registration endpoint on first connection and are given a pair, so
-- the rows here are created by strangers and are worth exactly as much as the
-- consent step that follows. The secret is stored hashed for the same reason
-- tokens are: a copy of this database must not be usable as a key.
CREATE TABLE IF NOT EXISTS oauth_clients (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id           INTEGER NOT NULL REFERENCES tunnel_routes(id) ON DELETE CASCADE,
    client_id          TEXT    NOT NULL UNIQUE,
    client_secret_hash TEXT    NOT NULL DEFAULT '',
    name               TEXT    NOT NULL DEFAULT '',
    redirect_uris      TEXT    NOT NULL DEFAULT '[]',
    created_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oauth_clients_route ON oauth_clients(route_id);

-- One authorization code, alive for a minute and usable once. Stored hashed
-- and deleted on use rather than flagged, so a replay finds nothing at all.
CREATE TABLE IF NOT EXISTS oauth_codes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id       INTEGER NOT NULL REFERENCES tunnel_routes(id) ON DELETE CASCADE,
    client_id      TEXT    NOT NULL,
    code_hash      TEXT    NOT NULL UNIQUE,
    redirect_uri   TEXT    NOT NULL DEFAULT '',
    code_challenge TEXT    NOT NULL DEFAULT '',
    scope          TEXT    NOT NULL DEFAULT '',
    expires_at     TEXT    NOT NULL,
    created_at     TEXT    NOT NULL
);

-- Access and refresh tokens. Only the hash is kept: the plaintext is shown to
-- its owner once, at the moment it is issued, and never reconstructed. label
-- is what the Cloudflare tab lists, so a key can be revoked by name without
-- anyone needing to see the key itself.
CREATE TABLE IF NOT EXISTS oauth_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id     INTEGER NOT NULL REFERENCES tunnel_routes(id) ON DELETE CASCADE,
    client_id    TEXT    NOT NULL DEFAULT '',
    kind         TEXT    NOT NULL DEFAULT 'access',
    token_hash   TEXT    NOT NULL UNIQUE,
    label        TEXT    NOT NULL DEFAULT '',
    expires_at   TEXT    NOT NULL DEFAULT '',
    revoked      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    last_used_at TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_oauth_tokens_route ON oauth_tokens(route_id, id DESC);

-- Requests per application per minute, counted from the access lines its own
-- captured log prints (see traffic.py). h0..h11 are a response-time histogram
-- so a p95 survives being summed across minutes; they stay zero for servers
-- that do not print a duration. Swept after a week.
CREATE TABLE IF NOT EXISTS traffic_minutes (
    app_id      INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    minute      TEXT    NOT NULL,
    requests    INTEGER NOT NULL DEFAULT 0,
    s2xx        INTEGER NOT NULL DEFAULT 0,
    s3xx        INTEGER NOT NULL DEFAULT 0,
    s4xx        INTEGER NOT NULL DEFAULT 0,
    s5xx        INTEGER NOT NULL DEFAULT 0,
    loopback    INTEGER NOT NULL DEFAULT 0,
    lan         INTEGER NOT NULL DEFAULT 0,
    external    INTEGER NOT NULL DEFAULT 0,
    bytes       INTEGER NOT NULL DEFAULT 0,
    timed       INTEGER NOT NULL DEFAULT 0,
    latency_sum REAL    NOT NULL DEFAULT 0,
    h0 INTEGER NOT NULL DEFAULT 0, h1 INTEGER NOT NULL DEFAULT 0,
    h2 INTEGER NOT NULL DEFAULT 0, h3 INTEGER NOT NULL DEFAULT 0,
    h4 INTEGER NOT NULL DEFAULT 0, h5 INTEGER NOT NULL DEFAULT 0,
    h6 INTEGER NOT NULL DEFAULT 0, h7 INTEGER NOT NULL DEFAULT 0,
    h8 INTEGER NOT NULL DEFAULT 0, h9 INTEGER NOT NULL DEFAULT 0,
    h10 INTEGER NOT NULL DEFAULT 0, h11 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (app_id, minute)
);

CREATE INDEX IF NOT EXISTS idx_traffic_minutes ON traffic_minutes(minute);

-- The same requests by endpoint, per hour. Identifier segments are collapsed
-- to :id before they get here, so one route is one row rather than thousands.
CREATE TABLE IF NOT EXISTS traffic_paths (
    app_id      INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    hour        TEXT    NOT NULL,
    method      TEXT    NOT NULL,
    path        TEXT    NOT NULL,
    requests    INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    latency_sum REAL    NOT NULL DEFAULT 0,
    timed       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (app_id, hour, method, path)
);

CREATE INDEX IF NOT EXISTS idx_traffic_paths ON traffic_paths(hour);

-- How far each app's log has been read, so a restart neither re-counts nor
-- skips. inode tells a rotated file from the one that replaced it.
CREATE TABLE IF NOT EXISTS traffic_cursor (
    app_id     INTEGER PRIMARY KEY REFERENCES apps(id) ON DELETE CASCADE,
    inode      INTEGER NOT NULL DEFAULT 0,
    offset     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Short-lived connection with foreign keys and dict-style rows."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive schema changes, applied to databases created by earlier versions."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(apps)")}
    if "start_on_boot" not in columns:
        conn.execute("ALTER TABLE apps ADD COLUMN start_on_boot INTEGER NOT NULL DEFAULT 0")
    if "share_lan" not in columns:
        conn.execute("ALTER TABLE apps ADD COLUMN share_lan INTEGER NOT NULL DEFAULT 0")
    if "preview_url" not in columns:
        conn.execute("ALTER TABLE apps ADD COLUMN preview_url TEXT NOT NULL DEFAULT ''")
    if "live_reload" not in columns:
        conn.execute("ALTER TABLE apps ADD COLUMN live_reload INTEGER NOT NULL DEFAULT 0")
    if "news_enabled" not in columns:
        conn.execute("ALTER TABLE apps ADD COLUMN news_enabled INTEGER NOT NULL DEFAULT 1")
    if "news_keywords" not in columns:
        conn.execute("ALTER TABLE apps ADD COLUMN news_keywords TEXT NOT NULL DEFAULT ''")
    if "needs" not in columns:
        conn.execute("ALTER TABLE apps ADD COLUMN needs TEXT NOT NULL DEFAULT '[]'")
    if "protected" not in columns:
        conn.execute("ALTER TABLE apps ADD COLUMN protected INTEGER NOT NULL DEFAULT 0")
    if "uses_models" not in columns:
        conn.execute("ALTER TABLE apps ADD COLUMN uses_models TEXT NOT NULL DEFAULT ''")

    capture_columns = {row["name"] for row in conn.execute("PRAGMA table_info(captures)")}
    if capture_columns and "clip" not in capture_columns:
        conn.execute("ALTER TABLE captures ADD COLUMN clip TEXT NOT NULL DEFAULT ''")

    route_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tunnel_routes)")}
    if route_columns:
        for column, definition in (
            ("guard_header_name", "TEXT NOT NULL DEFAULT ''"),
            ("guard_header_value", "TEXT NOT NULL DEFAULT ''"),
            ("guard_secret", "TEXT NOT NULL DEFAULT ''"),
            ("guard_port", "INTEGER"),
            ("guard_app_id", "INTEGER"),
            ("guard_oauth", "INTEGER NOT NULL DEFAULT 0"),
            ("guard_oauth_password", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in route_columns:
                conn.execute(f"ALTER TABLE tunnel_routes ADD COLUMN {column} {definition}")


def init_db() -> None:
    with db() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def get_setting(key: str, default: str = "") -> str:
    try:
        with db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    except sqlite3.Error:
        return default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def record_event(app_id: int, type_: str, message: str = "") -> None:
    """Append a lifecycle event. Never raises — logging must not break the manager."""
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO events (app_id, type, message, created_at) VALUES (?, ?, ?, ?)",
                (app_id, type_, message, utcnow()),
            )
    except sqlite3.Error:
        return
    # Imported here because the feed itself stores through this module.
    from .news import news

    try:
        news.from_event(app_id, type_, message)
    except Exception:
        pass
