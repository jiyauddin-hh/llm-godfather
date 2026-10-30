"""Probe applications to decide what can be shown on the live wall.

A tile is only worth rendering if the app answers HTTP with a page the browser
will agree to embed. Everything else — a database port, an API returning JSON,
a page sending X-Frame-Options — is reported honestly rather than rendered as a
blank frame the user has to puzzle over.
"""

import http.client
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from .traffic import note_probe

PROBE_TIMEOUT = 2.5
MAX_PROBE_WORKERS = 8


def preview_url_for(port: int | None, override: str) -> str | None:
    """Full URL for an app's tile: an explicit override, a path, or just the port."""
    override = (override or "").strip()

    if override.startswith(("http://", "https://")):
        return override
    if override.startswith("/"):
        return f"http://127.0.0.1:{port}{override}" if port else None
    if port:
        return f"http://127.0.0.1:{port}/"
    return None


def _blocks_framing(headers: dict[str, str]) -> str | None:
    """Why the browser would refuse to embed this page, if it would."""
    xfo = (headers.get("x-frame-options") or "").strip().lower()
    if xfo in {"deny", "sameorigin"} or xfo.startswith("allow-from"):
        # The dashboard is a different origin, so SAMEORIGIN blocks it too.
        return f"X-Frame-Options: {xfo}"

    csp = (headers.get("content-security-policy") or "").lower()
    if "frame-ancestors" in csp:
        directive = csp.split("frame-ancestors", 1)[1].split(";", 1)[0].strip()
        if "'none'" in directive:
            return "CSP frame-ancestors 'none'"
        if "*" not in directive and "127.0.0.1" not in directive and "localhost" not in directive:
            return f"CSP frame-ancestors {directive[:40]}"
    return None


def probe(url: str) -> dict:
    """One cheap request: status, content type, and whether framing is allowed."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return {"kind": "down", "note": "invalid preview URL"}

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    # The app will log this request like any other; say so first, so the
    # Analytics tab does not count the wall watching an app as traffic to it.
    note_probe(path)

    conn = None
    try:
        conn = connection_class(parsed.hostname, port, timeout=PROBE_TIMEOUT)
        conn.request("GET", path, headers={"User-Agent": "GodFather-wall-probe"})
        response = conn.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}
        status = response.status
    except Exception as exc:
        return {"kind": "down", "note": type(exc).__name__.replace("Error", " error").strip()}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    content_type = (headers.get("content-type") or "").split(";")[0].strip()
    result = {"http_status": status, "content_type": content_type or None}

    blocked = _blocks_framing(headers)
    if blocked:
        return {**result, "kind": "blocked", "note": blocked}

    # A redirect is fine — the browser will follow it inside the frame.
    if status in {301, 302, 303, 307, 308}:
        return {**result, "kind": "web", "note": f"redirects to {headers.get('location', '')[:48]}"}

    if status >= 400:
        return {**result, "kind": "error", "note": f"HTTP {status}"}

    if content_type == "text/html":
        return {**result, "kind": "web", "note": None}

    return {**result, "kind": "text", "note": f"serves {content_type or 'unknown'}, not a web page"}


def probe_many(targets: list[tuple[int, str]]) -> dict[int, dict]:
    """Probe several apps at once; one slow app must not hold up the wall."""
    if not targets:
        return {}

    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=min(MAX_PROBE_WORKERS, len(targets))) as pool:
        futures = {pool.submit(probe, url): app_id for app_id, url in targets}
        for future, app_id in futures.items():
            try:
                results[app_id] = future.result(timeout=PROBE_TIMEOUT + 1.5)
            except Exception:
                results[app_id] = {"kind": "down", "note": "probe timed out"}
    return results
