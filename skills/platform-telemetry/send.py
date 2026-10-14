"""One POST to the telemetry endpoint; never raises (IDE-151).

`config.json` beside this file holds the endpoint. `IDP_TELEMETRY_ENDPOINT`
overrides it on purpose (tests, a staging receiver) and
`IDP_TELEMETRY_ENDPOINT_FILE` names a file holding the URL, for a test receiver
on an ephemeral port. The same transport rules apply to all three: https only,
plain http for a loopback receiver, redirects refused, five seconds.

    send.py <payload.json> [--delete]   sends, prints nothing, exits 0
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def load_config(env=None):
    """The shipped config, or the file IDP_TELEMETRY_CONFIG names (tests, staging)."""
    env = os.environ if env is None else env
    try:
        with open(env.get("IDP_TELEMETRY_CONFIG") or CONFIG_FILE, encoding="utf-8") as handle:
            config = json.load(handle)
        endpoint = config.get("endpoint")
        return {"endpoint": endpoint.strip() if isinstance(endpoint, str) else "",
                "enabled": config.get("enabled") is not False}
    except Exception:
        return {"endpoint": "", "enabled": True}


def resolve_endpoint(env=None, config=None):
    env = os.environ if env is None else env
    config = load_config(env) if config is None else config
    direct = (env.get("IDP_TELEMETRY_ENDPOINT") or "").strip()
    if direct:
        return direct
    if env.get("IDP_TELEMETRY_ENDPOINT_FILE"):
        try:
            with open(env["IDP_TELEMETRY_ENDPOINT_FILE"], encoding="utf-8") as handle:
                url = handle.read().strip()
            if url:
                return url
        except OSError:
            pass  # not published yet
    return config.get("endpoint") or ""


def endpoint_problem(endpoint):
    """None when the endpoint may be used, else the reason."""
    try:
        parts = urllib.parse.urlsplit(endpoint)
    except ValueError:
        return "invalid endpoint"
    if not parts.scheme or not parts.hostname:
        return "invalid endpoint"
    if parts.scheme == "https":
        return None
    if parts.scheme == "http" and parts.hostname in LOOPBACK:
        return None
    return "endpoint must use https"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _ssl_context():
    # The python.org build ships without a CA bundle; fall back to the system one, never to no verification.
    context = ssl.create_default_context()
    if not context.get_ca_certs():
        for candidate in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem"):
            if os.path.exists(candidate):
                return ssl.create_default_context(cafile=candidate)
    return context


def send(payload, endpoint, timeout=5.0):
    """{"ok", "status", "error"}. Success is a 2xx status line; the body is never read."""
    if not endpoint:
        return {"ok": False, "status": None, "error": "no endpoint"}
    problem = endpoint_problem(endpoint)
    if problem:
        return {"ok": False, "status": None, "error": problem}
    try:
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        body = body.encode("utf-8") if isinstance(body, str) else body
        request = urllib.request.Request(endpoint, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        handlers = [_NoRedirect()]
        if endpoint.startswith("https:"):
            handlers.append(urllib.request.HTTPSHandler(context=_ssl_context()))
        opener = urllib.request.build_opener(*handlers)
        response = opener.open(request, timeout=timeout)
        status = response.status
        response.close()
        ok = 200 <= status < 300
        return {"ok": ok, "status": status, "error": None if ok else f"HTTP {status}"}
    except urllib.error.HTTPError as error:
        try:
            error.close()
        except Exception:
            pass
        if 300 <= error.code < 400:
            return {"ok": False, "status": error.code, "error": "redirect refused"}
        return {"ok": False, "status": error.code, "error": f"HTTP {error.code}"}
    except Exception as error:
        reason = getattr(error, "reason", error)
        timed_out = isinstance(reason, TimeoutError) or "timed out" in str(reason)
        return {"ok": False, "status": None, "error": "timeout" if timed_out else str(reason)[:200]}


def main(argv):
    try:
        files = [a for a in argv if not a.startswith("--")]
        if not files:
            return
        with open(files[0], encoding="utf-8") as handle:
            body = handle.read()
        if "--delete" in argv:
            try:
                os.unlink(files[0])
            except OSError:
                pass
        send(body, resolve_endpoint())
    except Exception:
        pass


if __name__ == "__main__":
    main(sys.argv[1:])  # exits 0 whatever happened
