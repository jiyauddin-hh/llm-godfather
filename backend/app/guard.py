"""The proxy that stands in front of a protected tunnel route.

A route published to the internet can be given any of three protections, and
they answer different problems:

* **an injected header** — the application wants a credential the caller has
  no way to send. The guard holds it and adds it on the way through, so the
  application is reached correctly without being changed.
* **OAuth** — callers authenticate to the guard before anything reaches the
  application. This is the only method clients like claude.ai were built for:
  their connector form takes an address and OAuth credentials and nothing
  else, so there is nowhere to type a token. The flow lives in oauth.py; this
  file serves it.
* **a secret path prefix** — requests must arrive under a random path or get a
  404. Obscurity, not authentication: worth something in front of an open
  service, worth nothing against someone who has the URL.

cloudflared points at this proxy rather than at the application, so the
application keeps whatever protection it already had and needs none of this.

One process serves one route, started and supervised as an ordinary God Father
application so it gets logs, auto-restart and start-on-boot like everything
else. It reads its own configuration out of SQLite by route id, which keeps
credentials off the command line — otherwise every `ps` would print them.

Proxied bytes are forwarded rather than parsed once the headers are past, so a
streaming response (server-sent events, a long download) arrives as it is
produced. Connections are closed after each exchange rather than kept alive:
the path rewrite applies to a request line, and re-deriving where the next
request begins in a reused connection would mean tracking Content-Length and
chunked framing for no gain at this scale.
"""

import asyncio
import html
import json
import secrets
import sys
import time
import urllib.parse

from . import oauth
from .database import db

UPSTREAM_HOST = "127.0.0.1"

# A request whose headers do not end within this is not a request we serve.
MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 1024 * 1024
HEADER_TIMEOUT = 20.0
CONNECT_TIMEOUT = 10.0

# Hop-by-hop headers we replace rather than pass on, because this proxy — not
# the caller — decides how its own connections are managed.
DROPPED = (b"connection", b"keep-alive", b"proxy-connection", b"te", b"upgrade")

WELL_KNOWN_RESOURCE = "/.well-known/oauth-protected-resource"
WELL_KNOWN_SERVER = "/.well-known/oauth-authorization-server"
REGISTER_PATH = "/oauth/register"
AUTHORIZE_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"

OAUTH_PATHS = (WELL_KNOWN_RESOURCE, WELL_KNOWN_SERVER, REGISTER_PATH, AUTHORIZE_PATH, TOKEN_PATH)


class Config:
    """One route's guard settings, read once at startup.

    Re-reading per request would be wasted work: changing any of these takes
    the route down and brings it back up, which restarts this process anyway.
    """

    def __init__(self, route_id: int) -> None:
        with db() as conn:
            row = conn.execute(
                "SELECT port, guard_header_name, guard_header_value, guard_secret, "
                "guard_port, guard_oauth, guard_oauth_password "
                "FROM tunnel_routes WHERE id = ?",
                (route_id,),
            ).fetchone()

        if row is None:
            raise SystemExit(f"guard: route {route_id} does not exist")
        if not row["guard_port"]:
            raise SystemExit(f"guard: route {route_id} has no guard port assigned")

        self.route_id = route_id
        self.listen_port: int = row["guard_port"]
        self.upstream_port: int = row["port"]
        self.header_name: str = row["guard_header_name"] or ""
        self.header_value: str = row["guard_header_value"] or ""
        self.secret: str = row["guard_secret"] or ""
        self.oauth: bool = bool(row["guard_oauth"])
        self.password_hash: str = row["guard_oauth_password"] or ""


# ------------------------------------------------------------------- request


class Request:
    __slots__ = ("method", "target", "version", "headers", "raw_headers", "body", "path", "query")

    def __init__(self, method, target, version, headers, raw_headers):
        self.method = method
        self.target = target
        self.version = version
        self.headers = headers
        self.raw_headers = raw_headers
        self.body = b""
        split = target.split("?", 1)
        self.path = split[0]
        self.query = urllib.parse.parse_qs(split[1]) if len(split) > 1 else {}

    def one(self, key: str, default: str = "") -> str:
        values = self.query.get(key)
        return values[0] if values else default

    def form(self) -> dict[str, str]:
        parsed = urllib.parse.parse_qs(self.body.decode("utf-8", "replace"))
        return {k: v[0] for k, v in parsed.items() if v}

    def json(self) -> dict:
        try:
            value = json.loads(self.body.decode("utf-8", "replace"))
            return value if isinstance(value, dict) else {}
        except ValueError:
            return {}

    def origin(self) -> str:
        """The address the caller reached us on, as they saw it.

        cloudflared terminates TLS and forwards the original Host, so this —
        not the local port — is what every URL published in the OAuth metadata
        has to be built from.
        """
        host = self.headers.get("host", "localhost")
        scheme = self.headers.get("x-forwarded-proto", "https").split(",")[0].strip()
        return f"{scheme}://{host}"


async def _read_request(reader: asyncio.StreamReader) -> Request | None:
    try:
        block = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=HEADER_TIMEOUT)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, TimeoutError):
        return None

    if len(block) > MAX_HEADER_BYTES:
        return None

    lines = block.split(b"\r\n")
    if not lines or not lines[0]:
        return None

    parts = lines[0].split(b" ")
    if len(parts) != 3:
        return None

    raw = [line for line in lines[1:] if line]
    headers: dict[str, str] = {}
    for line in raw:
        name, _, value = line.partition(b":")
        headers[name.strip().lower().decode("latin-1")] = value.strip().decode("latin-1")

    return Request(
        parts[0].decode("latin-1"),
        parts[1].decode("latin-1"),
        parts[2].decode("latin-1"),
        headers,
        raw,
    )


async def _read_body(reader: asyncio.StreamReader, request: Request) -> bool:
    """Buffer the body of a request the guard answers itself. False if absurd."""
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        return False
    if length <= 0:
        return True
    if length > MAX_BODY_BYTES:
        return False
    try:
        request.body = await asyncio.wait_for(
            reader.readexactly(length), timeout=HEADER_TIMEOUT
        )
    except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
        return False
    return True


# ------------------------------------------------------------------ responses


def _respond(writer: asyncio.StreamWriter, status: str, body: bytes, content_type: str,
             extra: list[str] | None = None) -> None:
    head = [
        f"HTTP/1.1 {status}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Cache-Control: no-store",
        "Connection: close",
    ]
    head.extend(extra or [])
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)


def _json(writer, status: str, payload: dict, extra: list[str] | None = None) -> None:
    _respond(writer, status, json.dumps(payload).encode(), "application/json", extra)


def _text(writer, status: str, message: str, extra: list[str] | None = None) -> None:
    _respond(writer, status, message.encode(), "text/plain; charset=utf-8", extra)


def _html(writer, status: str, markup: str) -> None:
    _respond(writer, status, markup.encode(), "text/html; charset=utf-8")


def _redirect(writer: asyncio.StreamWriter, location: str) -> None:
    _respond(writer, "302 Found", b"", "text/plain", [f"Location: {location}"])


# ------------------------------------------------------------- consent screen


CONSENT_CSS = """
:root { color-scheme: dark }
body { margin:0; min-height:100vh; display:grid; place-items:center;
       background:#11151c; color:#e6e9ef;
       font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif }
.card { width:min(420px,92vw); background:#171c25; border:1px solid #232a35;
        border-radius:12px; padding:28px }
h1 { margin:0 0 6px; font-size:19px }
p { margin:0 0 18px; color:#93a0b4; font-size:13px }
.who { background:#11151c; border:1px solid #232a35; border-radius:8px;
       padding:10px 12px; margin-bottom:18px; font-size:13px }
.who strong { color:#e6e9ef }
label { display:block; font-size:13px; margin-bottom:6px }
input { width:100%; box-sizing:border-box; padding:10px 12px; border-radius:8px;
        border:1px solid #2f3846; background:#0e1219; color:#e6e9ef; font-size:14px }
button { width:100%; margin-top:16px; padding:11px; border:0; border-radius:8px;
         background:#4f8cff; color:#fff; font-size:14px; font-weight:600; cursor:pointer }
button:hover { background:#3f7bee }
.err { background:rgba(255,90,90,.12); border:1px solid rgba(255,90,90,.4);
       color:#ffb4b4; padding:9px 12px; border-radius:8px; margin-bottom:16px; font-size:13px }
.foot { margin:16px 0 0; font-size:12px; color:#6b7789 }
"""


def _consent_page(request: Request, client_name: str, error: str = "") -> str:
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(request.one(key))}">'
        for key in ("client_id", "redirect_uri", "state", "code_challenge",
                    "code_challenge_method", "scope", "resource")
        if request.one(key)
    )
    problem = f'<div class="err">{html.escape(error)}</div>' if error else ""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize access</title><style>{CONSENT_CSS}</style></head>
<body><div class="card">
<h1>Authorize access</h1>
<p>An application is asking to connect through God Father.</p>
{problem}
<div class="who"><strong>{html.escape(client_name or 'An unnamed client')}</strong>
 wants to reach this service on your behalf.</div>
<form method="POST" action="{html.escape(AUTHORIZE_PATH)}">
{hidden}
<label for="pw">Approval password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" autofocus>
<button type="submit">Approve</button>
</form>
<p class="foot">Only approve this if you started it. Approving grants access
until you revoke it in God Father.</p>
</div></body></html>"""


# -------------------------------------------------------------- oauth handling


def _metadata_resource(origin: str) -> dict:
    return {
        "resource": origin,
        "authorization_servers": [origin],
        "bearer_methods_supported": ["header"],
    }


def _metadata_server(origin: str) -> dict:
    return {
        "issuer": origin,
        "authorization_endpoint": f"{origin}{AUTHORIZE_PATH}",
        "token_endpoint": f"{origin}{TOKEN_PATH}",
        "registration_endpoint": f"{origin}{REGISTER_PATH}",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        # S256 only. `plain` is permitted by the spec and gives away the whole
        # point of PKCE, so it is not offered.
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
    }


def _handle_register(config: Config, request: Request, writer) -> None:
    payload = request.json()
    uris = payload.get("redirect_uris") or []
    if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris) or not uris:
        _json(writer, "400 Bad Request",
              {"error": "invalid_redirect_uri",
               "error_description": "redirect_uris must be a non-empty list of strings"})
        return

    issued = oauth.register_client(
        config.route_id, str(payload.get("client_name") or "")[:200], uris
    )
    _json(writer, "201 Created", {
        **issued,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": uris,
        "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    })


def _authorize_error(writer, request: Request, code: str, description: str) -> None:
    """Report a failure to the client when we can, to the person when we cannot.

    An error is only safe to send onward once the redirect URI is known to be
    one the client registered — otherwise this endpoint would forward
    attacker-chosen text to an attacker-chosen address.
    """
    redirect = request.one("redirect_uri")
    state = request.one("state")
    if redirect:
        joiner = "&" if "?" in redirect else "?"
        params = urllib.parse.urlencode(
            {"error": code, "error_description": description, **({"state": state} if state else {})}
        )
        _redirect(writer, f"{redirect}{joiner}{params}")
    else:
        _text(writer, "400 Bad Request", f"{code}: {description}\n")


def _validated_client(config: Config, request: Request) -> tuple[dict | None, str]:
    """The registered client this request names, or why it cannot be trusted."""
    client_id = request.one("client_id")
    if not client_id:
        return None, "client_id is missing"

    item = oauth.client(config.route_id, client_id)
    if item is None:
        return None, "unknown client_id"

    redirect = request.one("redirect_uri")
    # Exact match only. A prefix or suffix rule here is the classic way an
    # authorization code ends up delivered to somebody else's server.
    if redirect not in item["redirect_uris"]:
        return None, "redirect_uri was not registered for this client"

    return item, ""


def _handle_authorize_get(config: Config, request: Request, writer) -> None:
    item, problem = _validated_client(config, request)
    if item is None:
        # Nothing here may be echoed to an unverified redirect_uri.
        _text(writer, "400 Bad Request", f"invalid_request: {problem}\n")
        return

    if request.one("response_type") != "code":
        _authorize_error(writer, request, "unsupported_response_type", "only 'code' is supported")
        return
    if request.one("code_challenge_method") != "S256":
        _authorize_error(writer, request, "invalid_request", "PKCE with S256 is required")
        return
    if not request.one("code_challenge"):
        _authorize_error(writer, request, "invalid_request", "code_challenge is required")
        return
    if not config.password_hash:
        _text(writer, "500 Internal Server Error",
              "This route has OAuth switched on but no approval password set.\n")
        return

    _html(writer, "200 OK", _consent_page(request, item["name"]))


def _handle_authorize_post(config: Config, request: Request, writer) -> None:
    fields = request.form()
    # The form posts back the same parameters, so reuse the query-string reader.
    request.query = {k: [v] for k, v in fields.items()}

    item, problem = _validated_client(config, request)
    if item is None:
        _text(writer, "400 Bad Request", f"invalid_request: {problem}\n")
        return

    if not oauth.check_password(fields.get("password", ""), config.password_hash):
        _html(writer, "401 Unauthorized",
              _consent_page(request, item["name"], "That password was not right."))
        return

    code = oauth.issue_code(
        config.route_id,
        item["client_id"],
        request.one("redirect_uri"),
        request.one("code_challenge"),
        request.one("scope"),
    )

    redirect = request.one("redirect_uri")
    state = request.one("state")
    joiner = "&" if "?" in redirect else "?"
    params = urllib.parse.urlencode({"code": code, **({"state": state} if state else {})})
    _redirect(writer, f"{redirect}{joiner}{params}")


def _handle_token(config: Config, request: Request, writer) -> None:
    fields = request.form()
    grant = fields.get("grant_type", "")
    client_id = fields.get("client_id", "")

    item = oauth.client(config.route_id, client_id) if client_id else None
    if item is None:
        _json(writer, "401 Unauthorized", {"error": "invalid_client"})
        return
    if not oauth.check_client_secret(item, fields.get("client_secret", "")):
        _json(writer, "401 Unauthorized", {"error": "invalid_client"})
        return

    if grant == "refresh_token":
        issued = oauth.redeem_refresh(config.route_id, client_id, fields.get("refresh_token", ""))
        if issued is None:
            _json(writer, "400 Bad Request", {"error": "invalid_grant"})
            return
        _json(writer, "200 OK", issued)
        return

    if grant != "authorization_code":
        _json(writer, "400 Bad Request", {"error": "unsupported_grant_type"})
        return

    record = oauth.take_code(config.route_id, fields.get("code", ""))
    if record is None or record["client_id"] != client_id:
        _json(writer, "400 Bad Request", {"error": "invalid_grant"})
        return
    if record["redirect_uri"] != fields.get("redirect_uri", ""):
        _json(writer, "400 Bad Request",
              {"error": "invalid_grant", "error_description": "redirect_uri does not match"})
        return
    if not oauth.verify_challenge(record["code_challenge"], fields.get("code_verifier", "")):
        _json(writer, "400 Bad Request",
              {"error": "invalid_grant", "error_description": "PKCE verification failed"})
        return

    _json(writer, "200 OK", oauth.issue_tokens(config.route_id, client_id, item["name"] or client_id))


def _handle_oauth(config: Config, request: Request, writer) -> bool:
    """Answer an OAuth endpoint. False when the path is not one of ours."""
    path = request.path

    if path == WELL_KNOWN_RESOURCE:
        _json(writer, "200 OK", _metadata_resource(request.origin()))
    elif path == WELL_KNOWN_SERVER:
        _json(writer, "200 OK", _metadata_server(request.origin()))
    elif path == REGISTER_PATH and request.method == "POST":
        _handle_register(config, request, writer)
    elif path == AUTHORIZE_PATH and request.method == "GET":
        _handle_authorize_get(config, request, writer)
    elif path == AUTHORIZE_PATH and request.method == "POST":
        _handle_authorize_post(config, request, writer)
    elif path == TOKEN_PATH and request.method == "POST":
        _handle_token(config, request, writer)
    elif path in OAUTH_PATHS:
        _text(writer, "405 Method Not Allowed", "method not allowed\n")
    else:
        return False
    return True


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    return value[7:].strip() if value[:7].lower() == "bearer " else ""


# ---------------------------------------------------------------- proxy path


def _rewrite_path(target: str, secret: str) -> str | None:
    """Strip the secret prefix, or None when it is absent or wrong.

    Compared in constant time so that a caller cannot learn the secret one
    character at a time from how long the refusal takes.
    """
    if not secret:
        return target

    wanted = "/" + secret
    offered = target[: len(wanted)]
    if not secrets.compare_digest(offered, wanted):
        return None

    rest = target[len(wanted):]
    if rest and not rest.startswith(("/", "?")):
        return None
    # A bare query string is not a request target — /<secret>?x=1 has to come
    # out as /?x=1, not ?x=1, or the upstream rejects the request line.
    if rest.startswith("?"):
        rest = "/" + rest
    return rest or "/"


def _rebuild(request: Request, config: Config, target: str) -> bytes:
    """Render the request for the upstream, with our own header in place."""
    injected = config.header_name.lower().encode("latin-1") if config.header_name else b""
    kept: list[bytes] = []

    for line in request.raw_headers:
        name = line.split(b":", 1)[0].strip().lower()
        if name in DROPPED:
            continue
        # Whatever the caller sent under the injected name is discarded: the
        # point of the guard is that this value comes from here, not from them.
        if injected and name == injected:
            continue
        # An OAuth bearer belongs to the guard, not to the application behind
        # it. Passing it on would leak a credential the application has no use
        # for — and if the application does its own bearer check, ours would
        # fail it. Dropped whatever else is being injected: when the injected
        # header *is* Authorization it gets added back below, and when it is
        # some other header this is the only thing stopping the leak.
        if config.oauth and name == b"authorization":
            continue
        kept.append(line)

    if config.header_name:
        kept.append(
            config.header_name.encode("latin-1") + b": " + config.header_value.encode("latin-1")
        )
    kept.append(b"Connection: close")

    line = f"{request.method} {target} {request.version}".encode("latin-1")
    return line + b"\r\n" + b"\r\n".join(kept) + b"\r\n\r\n"


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Forward bytes until the source ends. Never raises into the caller."""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    except Exception:
        pass


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass


def _serve(config: Config):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await _read_request(reader)
            if request is None:
                _text(writer, "400 Bad Request", "bad request\n")
                await writer.drain()
                return

            # OAuth endpoints live at the root of the public address, because
            # that is where a client looks for them. They are served before the
            # secret path is considered — a caller cannot be asked to know a
            # secret in order to find out how to authenticate.
            if config.oauth and request.path in OAUTH_PATHS:
                if not await _read_body(reader, request):
                    _text(writer, "413 Payload Too Large", "request body too large\n")
                    await writer.drain()
                    return
                if _handle_oauth(config, request, writer):
                    await writer.drain()
                    return

            if config.oauth and not oauth.verify_access(config.route_id, _bearer(request)):
                # The pointer to the metadata is what starts the OAuth dance:
                # without it a client has no way to discover where to go.
                origin = request.origin()
                _json(
                    writer, "401 Unauthorized",
                    {"error": "invalid_token"},
                    [f'WWW-Authenticate: Bearer resource_metadata="{origin}{WELL_KNOWN_RESOURCE}"'],
                )
                await writer.drain()
                return

            target = _rewrite_path(request.target, config.secret)
            if target is None:
                # Deliberately the same answer as a path that does not exist:
                # a 401 here would confirm that something is behind this URL.
                _text(writer, "404 Not Found", "not found\n")
                await writer.drain()
                return

            try:
                up_reader, up_writer = await asyncio.wait_for(
                    asyncio.open_connection(UPSTREAM_HOST, config.upstream_port),
                    timeout=CONNECT_TIMEOUT,
                )
            except (ConnectionError, OSError, TimeoutError):
                print(
                    f"[guard] upstream 127.0.0.1:{config.upstream_port} refused the connection",
                    file=sys.stderr, flush=True,
                )
                _text(writer, "502 Bad Gateway", "upstream unavailable\n")
                await writer.drain()
                return

            try:
                up_writer.write(_rebuild(request, config, target))
                # A body already buffered while answering an OAuth path is not
                # possible here, but a proxied request's body has not been read
                # yet — it is streamed below along with the rest.
                await up_writer.drain()

                # The request body and the response travel at the same time —
                # a streaming response can begin before the body has finished
                # arriving. The response ending is what ends the exchange.
                body = asyncio.create_task(_pump(reader, up_writer))
                await _pump(up_reader, writer)
                body.cancel()
            finally:
                await _close(up_writer)
        except Exception as exc:
            print(f"[guard] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        finally:
            await _close(writer)

    return handler


async def _main(route_id: int) -> None:
    config = Config(route_id)
    oauth.sweep(route_id)

    server = await asyncio.start_server(
        _serve(config), "127.0.0.1", config.listen_port, limit=MAX_HEADER_BYTES
    )

    protections = []
    if config.oauth:
        protections.append("OAuth required")
    if config.header_name:
        protections.append(f"injecting {config.header_name}")
    if config.secret:
        protections.append("secret path required")

    print(
        f"[guard] route {route_id}: 127.0.0.1:{config.listen_port} "
        f"-> 127.0.0.1:{config.upstream_port} ({', '.join(protections) or 'no protection'})",
        file=sys.stderr, flush=True,
    )

    async with server:
        await server.serve_forever()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m app.guard <route_id>")
    try:
        route_id = int(sys.argv[1])
    except ValueError:
        raise SystemExit("usage: python -m app.guard <route_id>")

    try:
        asyncio.run(_main(route_id))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
