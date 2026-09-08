-- Short numeric codes so a phone that already has PartyPad on its Home Screen
-- can join a new session without re-adding the icon. The icon opens the bare
-- origin, the player types the code, and the Worker exchanges it for the
-- session's join secret. Codes live and die with their session.
CREATE TABLE room_codes (
    code TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    join_secret TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX room_codes_expiry ON room_codes(expires_at);
