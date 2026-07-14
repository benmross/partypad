-- PartyPad public device authorization. Secret columns contain only keyed or
-- one-way hashes; raw codes, device tokens, and email addresses are never stored.
PRAGMA foreign_keys = ON;

CREATE TABLE identities (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    subject_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    revoked_at INTEGER,
    delete_after INTEGER,
    UNIQUE (provider, subject_hash)
);

CREATE TABLE device_authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_code_hash TEXT NOT NULL UNIQUE,
    user_code_hash TEXT NOT NULL UNIQUE,
    verifier_hash TEXT NOT NULL,
    device_name TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('windows', 'macos', 'linux')),
    client_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'consumed')),
    identity_id TEXT REFERENCES identities(id),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    approved_at INTEGER,
    consumed_at INTEGER,
    last_poll_at INTEGER,
    delete_after INTEGER NOT NULL
);
CREATE INDEX device_authorizations_expiry ON device_authorizations(delete_after);

CREATE TABLE devices (
    id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES identities(id),
    name TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('windows', 'macos', 'linux')),
    client_version TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER,
    delete_after INTEGER
);
CREATE INDEX devices_identity ON devices(identity_id, revoked_at);

CREATE TABLE device_tokens (
    token_hash TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    overlap_until INTEGER,
    revoked_at INTEGER
);
CREATE INDEX device_tokens_device ON device_tokens(device_id, revoked_at);

CREATE TABLE session_owners (
    session_id TEXT PRIMARY KEY,
    device_id TEXT REFERENCES devices(id),
    identity_id TEXT REFERENCES identities(id),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    ended_at INTEGER
);
CREATE INDEX session_owners_device_active ON session_owners(device_id, ended_at, expires_at);
CREATE INDEX session_owners_identity_active ON session_owners(identity_id, ended_at, expires_at);

CREATE TABLE rate_limits (
    bucket_hash TEXT NOT NULL,
    action TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (bucket_hash, action, window_start)
);
CREATE INDEX rate_limits_expiry ON rate_limits(expires_at);

CREATE TABLE service_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

INSERT INTO service_settings (key, value, updated_at)
VALUES ('new_sessions_enabled', 'true', unixepoch());
