#!/usr/bin/env python3
"""
cors-proxy.py - a tiny LOCAL relay for the Golden Bowl game.

Some LLM providers (DeepSeek is reported to be one) refuse requests that come straight from a
web page (no CORS headers). This relay runs on your own machine, forwards the game's requests to
the provider, and adds the CORS headers the browser needs.

    python3 cors-proxy.py                 # listens on http://localhost:8787
    python3 cors-proxy.py --port 9000
    python3 cors-proxy.py --allow my.llm.host --allow other.host:8443

In the game, put the provider's address AFTER the relay's address in the Base URL:

    http://localhost:8787/https://api.deepseek.com

(The "DeepSeek - via local relay" preset already does this.) The API key still goes in the game's
API key box; the relay just passes it along to the provider in the Authorization / x-api-key header.

Safety notes
  * Binds to 127.0.0.1 only: other computers cannot reach it.
  * Only forwards to an allow-list of hosts (below, plus whatever you add with --allow).
  * Never logs headers or bodies, so your API keys are not written anywhere.
  * Needs only the Python 3 standard library.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DEFAULT_ALLOWED = {
    "api.deepseek.com",
    "api.openai.com",
    "api.anthropic.com",
    "openrouter.ai",
}
# Request headers that must not be forwarded as-is. Origin/Referer/Sec-* are dropped on purpose so
# the provider sees an ordinary server-to-server call rather than a browser one.
DROP_REQUEST_HEADERS = {
    "host", "origin", "referer", "connection", "content-length", "accept-encoding",
    "keep-alive", "proxy-authorization", "proxy-connection", "te", "trailer",
    "transfer-encoding", "upgrade", "expect",
}
UPSTREAM_TIMEOUT_S = 180


class Relay(BaseHTTPRequestHandler):
    server_version = "golden-bowl-relay/1.0"
    protocol_version = "HTTP/1.1"
    allowed = set(DEFAULT_ALLOWED)

    # --- helpers -----------------------------------------------------------------------------
    def log_message(self, fmt, *args):  # silence the default access log (it could include URLs)
        pass

    def _cors_headers(self):
        origin = self.headers.get("Origin")
        h = {
            # Echo the caller's origin ("null" for a page opened from a file). No cookies are ever
            # involved, so this does not widen what a web page can do beyond what its own key allows.
            "Access-Control-Allow-Origin": origin if origin else "*",
            "Vary": "Origin",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": self.headers.get("Access-Control-Request-Headers") or "*",
            "Access-Control-Max-Age": "86400",
            "Access-Control-Allow-Private-Network": "true",
        }
        return h

    def _send(self, status, body=b"", content_type="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in self._cors_headers().items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status, message):
        self._send(status, json.dumps({"error": {"message": message, "type": "relay_error"}}).encode())

    def _target(self):
        """Path looks like /https://host/rest?query -> (scheme, host, 'https://host/rest?query')."""
        raw = self.path[1:] if self.path.startswith("/") else self.path
        if not (raw.startswith("http://") or raw.startswith("https://")):
            return None
        parts = urlsplit(raw)
        if not parts.hostname:
            return None
        return parts, raw

    # --- verbs -------------------------------------------------------------------------------
    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_GET(self):
        if self.path in ("/", "/health"):
            msg = ("Golden Bowl relay is running.\nUse Base URL: http://localhost:%d/https://<provider host>\n"
                   "Allowed hosts: %s\n" % (self.server.server_address[1], ", ".join(sorted(self.allowed))))
            return self._send(200, msg.encode(), "text/plain; charset=utf-8")
        self._forward()

    def do_POST(self):
        self._forward()

    def _forward(self):
        t0 = time.time()
        target = self._target()
        if not target:
            return self._error(400, "Relay URLs look like http://localhost:%d/https://api.example.com/v1/..." % self.server.server_address[1])
        parts, url = target
        host = parts.netloc  # includes :port if present
        if host not in self.allowed and parts.hostname not in self.allowed:
            return self._error(403, "Host '%s' is not on the relay's allow-list. Start the relay with --allow %s to permit it." % (host, host))

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in DROP_REQUEST_HEADERS and not k.lower().startswith("sec-")
                   and not k.lower().startswith("access-control-request-")}
        headers["Accept-Encoding"] = "identity"
        headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; golden-bowl-relay/1.0)")

        req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        status, data, ctype = 502, b"", "application/json"
        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_S) as resp:
                status, data, ctype = resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:           # provider answered with an error: pass it through untouched
            status, data, ctype = e.code, e.read(), e.headers.get("Content-Type", "application/json")
        except Exception as e:                        # provider unreachable / timed out
            print("  %s %s -> relay could not reach upstream (%s)" % (self.command, parts.hostname, type(e).__name__), flush=True)
            return self._error(502, "Relay could not reach %s: %s" % (parts.hostname, e))
        print("  %s %s%s -> %d (%d ms)" % (self.command, parts.netloc, parts.path, status, (time.time() - t0) * 1000), flush=True)
        self._send(status, data, ctype)


def main():
    ap = argparse.ArgumentParser(description="Local CORS relay for the Golden Bowl game")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--allow", action="append", default=[], metavar="HOST[:PORT]", help="extra upstream host to allow (repeatable)")
    args = ap.parse_args()
    Relay.allowed |= set(args.allow)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Relay)
    except OSError as e:
        sys.exit("Could not start on port %d: %s (try --port 8788)" % (args.port, e))
    print("Golden Bowl relay listening on http://localhost:%d" % args.port)
    print("Allowed upstream hosts: " + ", ".join(sorted(Relay.allowed)))
    print('In the game use Base URL like:  http://localhost:%d/https://api.deepseek.com' % args.port)
    print("Press Ctrl+C to stop.\n", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
