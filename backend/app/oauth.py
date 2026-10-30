"""OAuth 2.1 authorization server, terminated at the guard.

A guarded route can require callers to authenticate before anything reaches
the application behind it. The point of doing this here rather than in each
application is that the applications do not have to change — a plain HTTP
service published through God Father becomes an OAuth-protected one, and the
next one after it does too, with no work in either.

This exists because of a narrow but common problem: clients that can only be
given a URL. claude.ai's custom-connector form takes an address and OAuth
credentials and nothing else, so a service that wants `Authorization: Bearer …`
is simply unreachable from it — there is nowhere to type the token. OAuth is
the one authentication method that form was built for, and this is the smallest
place to put it that covers every route at once.

Shape of the flow, all of it served on the route's own public address:

    /.well-known/oauth-protected-resource   what protects this thing
    /.well-known/oauth-authorization-server what to talk to about it
    POST /oauth/register                    the client gives itself credentials
    GET  /oauth/authorize                   a person approves, with a password
    POST /oauth/token                       code + verifier are traded for a token

Registration is open, which looks alarming and is not: a registered client can
do nothing until a human has typed the route's approval password into the
consent screen. Registration hands out a name to be asked under, not access.

Secrets are stored hashed and never reconstructed. Someone holding a copy of
godfather.db has a list of who was issued what and when, and no way to use any
of it — which is the reason the plaintext is shown exactly once, at issue.
"""

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from .database import db, utcnow

# Short, because a code is exchanged within seconds of being issued and a long
# window is only ever useful to someone who has stolen one.
CODE_TTL = timedelta(minutes=2)
ACCESS_TTL = timedelta(hours=12)
REFRESH_TTL = timedelta(days=30)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _expired(stamp: str) -> bool:
    if not stamp:
        return False
    try:
        return datetime.fromisoformat(stamp) < _now()
    except ValueError:
        return True


# ------------------------------------------------------------------ passwords


def hash_password(password: str) -> str:
    """Salted PBKDF2. Slow on purpose: the consent screen is reachable by
    anyone who has the URL, so this is what a guesser has to grind through."""
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240_000)
    return f"pbkdf2${salt.hex()}${derived.hex()}"


def check_password(password: str, stored: str) -> bool:
    if not stored or not password:
        return False
    try:
        scheme, salt_hex, expected = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "pbkdf2":
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 240_000
    )
    return hmac.compare_digest(derived.hex(), expected)


# -------------------------------------------------------------------- clients


def register_client(route_id: int, name: str, redirect_uris: list[str]) -> dict:
    """Dynamic Client Registration (RFC 7591).

    Returns the plaintext secret — the only time it exists outside the caller.
    """
    client_id = f"gf-{secrets.token_urlsafe(18)}"
    client_secret = secrets.token_urlsafe(32)

    with db() as conn:
        conn.execute(
            """INSERT INTO oauth_clients
                   (route_id, client_id, client_secret_hash, name, redirect_uris, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                route_id,
                client_id,
                _hash(client_secret),
                name[:200],
                json.dumps(redirect_uris),
                utcnow(),
            ),
        )

    return {"client_id": client_id, "client_secret": client_secret}


def client(route_id: int, client_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_clients WHERE route_id = ? AND client_id = ?",
            (route_id, client_id),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["redirect_uris"] = json.loads(item["redirect_uris"])
    except ValueError:
        item["redirect_uris"] = []
    return item


def check_client_secret(item: dict, offered: str) -> bool:
    """A public client registers with no secret and proves itself with PKCE
    instead; only a client that was issued one has to present it."""
    if not item["client_secret_hash"]:
        return True
    return hmac.compare_digest(_hash(offered or ""), item["client_secret_hash"])


# ---------------------------------------------------------------------- codes


def issue_code(route_id: int, client_id: str, redirect_uri: str, challenge: str, scope: str) -> str:
    code = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            """INSERT INTO oauth_codes
                   (route_id, client_id, code_hash, redirect_uri, code_challenge,
                    scope, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                route_id,
                client_id,
                _hash(code),
                redirect_uri,
                challenge,
                scope,
                _stamp(_now() + CODE_TTL),
                utcnow(),
            ),
        )
    return code


def take_code(route_id: int, code: str) -> dict | None:
    """Consume a code. Deleted as it is read, so it cannot be used twice."""
    digest = _hash(code)
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_codes WHERE route_id = ? AND code_hash = ?",
            (route_id, digest),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM oauth_codes WHERE id = ?", (row["id"],))

    if not row:
        return None
    item = dict(row)
    return None if _expired(item["expires_at"]) else item


def verify_challenge(challenge: str, verifier: str) -> bool:
    """PKCE S256 only. The plain method is allowed by the spec and defeats the
    point of it, so a client offering that is treated as offering nothing."""
    if not challenge:
        return False
    digest = hashlib.sha256((verifier or "").encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, challenge)


# --------------------------------------------------------------------- tokens


def issue_tokens(route_id: int, client_id: str, label: str) -> dict:
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    now = utcnow()

    with db() as conn:
        conn.execute(
            """INSERT INTO oauth_tokens
                   (route_id, client_id, kind, token_hash, label, expires_at, created_at)
               VALUES (?, ?, 'access', ?, ?, ?, ?)""",
            (route_id, client_id, _hash(access), label, _stamp(_now() + ACCESS_TTL), now),
        )
        conn.execute(
            """INSERT INTO oauth_tokens
                   (route_id, client_id, kind, token_hash, label, expires_at, created_at)
               VALUES (?, ?, 'refresh', ?, ?, ?, ?)""",
            (route_id, client_id, _hash(refresh), label, _stamp(_now() + REFRESH_TTL), now),
        )

    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TTL.total_seconds()),
    }


def verify_access(route_id: int, token: str) -> bool:
    """Whether one bearer token may pass. Records the use, so the tab can show
    which keys are actually in service and which are forgotten."""
    if not token:
        return False

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_tokens WHERE route_id = ? AND token_hash = ? AND kind = 'access'",
            (route_id, _hash(token)),
        ).fetchone()
        if row is None or row["revoked"] or _expired(row["expires_at"]):
            return False
        conn.execute(
            "UPDATE oauth_tokens SET last_used_at = ? WHERE id = ?", (utcnow(), row["id"])
        )
    return True


def redeem_refresh(route_id: int, client_id: str, token: str) -> dict | None:
    """Trade a refresh token for a new pair, retiring the one presented.

    Rotating on every use is what makes a stolen refresh token detectable:
    the thief and the owner cannot both keep using it, and the loser comes
    back for a new authorization.
    """
    with db() as conn:
        row = conn.execute(
            """SELECT * FROM oauth_tokens
               WHERE route_id = ? AND token_hash = ? AND kind = 'refresh'""",
            (route_id, _hash(token)),
        ).fetchone()
        if row is None or row["revoked"] or _expired(row["expires_at"]):
            return None
        if row["client_id"] and row["client_id"] != client_id:
            return None
        conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE id = ?", (row["id"],))
        label = row["label"]

    return issue_tokens(route_id, client_id, label)


def list_tokens(route_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT id, kind, label, client_id, expires_at, revoked, created_at, last_used_at
               FROM oauth_tokens WHERE route_id = ? AND kind = 'access' ORDER BY id DESC""",
            (route_id,),
        ).fetchall()
    return [
        {**dict(row), "revoked": bool(row["revoked"]), "expired": _expired(row["expires_at"])}
        for row in rows
    ]


def revoke(route_id: int, token_id: int) -> bool:
    """Revoke one issued key, and the refresh token that came with it."""
    with db() as conn:
        row = conn.execute(
            "SELECT client_id, label FROM oauth_tokens WHERE id = ? AND route_id = ?",
            (token_id, route_id),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            """UPDATE oauth_tokens SET revoked = 1
               WHERE route_id = ? AND client_id = ? AND label = ?""",
            (route_id, row["client_id"], row["label"]),
        )
    return True


def revoke_all(route_id: int) -> None:
    with db() as conn:
        conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE route_id = ?", (route_id,))


def sweep(route_id: int | None = None) -> None:
    """Drop codes that were never redeemed and tokens long past use."""
    cutoff = _stamp(_now() - timedelta(days=1))
    with db() as conn:
        if route_id is None:
            conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (cutoff,))
        else:
            conn.execute(
                "DELETE FROM oauth_codes WHERE route_id = ? AND expires_at < ?",
                (route_id, cutoff),
            )
