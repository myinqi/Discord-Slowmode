#!/usr/bin/env python3
"""One-shot OAuth helper for the Twitch chat-bot.

Run this on **your local machine** (not the server) — it spins up a tiny
HTTP listener on http://localhost:17563/callback, opens your browser to
Twitch's consent page, exchanges the returned code for an access + refresh
token pair and prints the refresh token for you to paste into the Admin UI.

Required Twitch app redirect URL (must match exactly):
    http://localhost:17563/callback

Required scopes (the script asks for these automatically):
    user:write:chat   — post chat messages
    user:bot          — declares the user as a bot

After running:
    1. Log in to Twitch as your *bot* account (not the streamer).
    2. Confirm the app permissions.
    3. The browser tab will say "Done — refresh token printed in terminal".
    4. Copy the refresh token, open Admin UI → Twitch Radio → paste into
       "Refresh Token", save.
"""

from __future__ import annotations

import http.server
import json
import secrets
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

REDIRECT_URI = "http://localhost:17563/callback"
SCOPES = ["user:write:chat", "user:bot"]
AUTH_URL = "https://id.twitch.tv/oauth2/authorize"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"

_state = secrets.token_urlsafe(24)
_received: dict = {}
_done = threading.Event()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("state", [""])[0] != _state:
            self._reply(400, "<h1>State mismatch</h1>")
            _received["error"] = "state mismatch"
        elif "error" in params:
            err = params.get("error_description", params["error"])[0]
            self._reply(400, f"<h1>Twitch returned an error</h1><pre>{err}</pre>")
            _received["error"] = err
        elif "code" not in params:
            self._reply(400, "<h1>No code returned</h1>")
            _received["error"] = "no code"
        else:
            _received["code"] = params["code"][0]
            self._reply(200,
                "<h1>Done — refresh token printed in terminal.</h1>"
                "<p>You can close this tab.</p>")
        _done.set()

    def _reply(self, status: int, html: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(("<!doctype html><meta charset=utf-8>"
                          "<style>body{font:16px system-ui;padding:32px;"
                          "background:#0f0f12;color:#ddd}h1{color:#cfc8ff}"
                          "pre{background:#1a1a1f;padding:12px;border-radius:6px}"
                          "</style>" + html).encode("utf-8"))

    def log_message(self, *a, **kw):  # silence default access log
        pass


def _exchange_code(code: str, client_id: str, client_secret: str) -> dict:
    body = urllib.parse.urlencode({
        "client_id":     client_id,
        "client_secret": client_secret,
        "code":          code,
        "grant_type":    "authorization_code",
        "redirect_uri":  REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def main() -> int:
    print("=" * 70)
    print("Twitch chat-bot OAuth helper")
    print("=" * 70)
    print()
    client_id = input("Client ID (from dev.twitch.tv/console):     ").strip()
    if not client_id:
        print("Client ID is required.", file=sys.stderr)
        return 1
    client_secret = input("Client Secret:                              ").strip()
    if not client_secret:
        print("Client Secret is required.", file=sys.stderr)
        return 1

    auth_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id":     client_id,
        "redirect_uri":  REDIRECT_URI,
        "scope":         " ".join(SCOPES),
        "state":         _state,
        # `force_verify` makes Twitch always re-show the consent screen,
        # so the user can pick the *bot* account explicitly even if they're
        # already logged in as the streamer.
        "force_verify":  "true",
    })

    print()
    print("Starting local listener on http://localhost:17563 …")
    print("Opening browser. Log in as your **bot** account, not the streamer.")
    print()
    print("If the browser doesn't open, paste this URL manually:")
    print()
    print("   " + auth_url)
    print()

    server = socketserver.TCPServer(("localhost", 17563), _Handler)
    server.timeout = 1.0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # Wait up to 5 minutes for the user to complete the flow.
    if not _done.wait(timeout=300):
        print("Timed out waiting for callback.", file=sys.stderr)
        return 1
    server.shutdown()

    if "error" in _received:
        print(f"OAuth failed: {_received['error']}", file=sys.stderr)
        return 1

    print("Code received. Exchanging for tokens …")
    try:
        tok = _exchange_code(_received["code"], client_id, client_secret)
    except Exception as e:
        print(f"Token exchange failed: {e}", file=sys.stderr)
        return 1

    if "refresh_token" not in tok:
        print(f"No refresh_token in response: {tok}", file=sys.stderr)
        return 1

    print()
    print("=" * 70)
    print("SUCCESS")
    print("=" * 70)
    print()
    print("Paste these into your Admin UI → Twitch Radio:")
    print()
    print(f"   Client ID:      {client_id}")
    print(f"   Client Secret:  {client_secret}")
    print(f"   Refresh Token:  {tok['refresh_token']}")
    print()
    print("Scopes granted: " + " ".join(tok.get("scope", []) or SCOPES))
    print()
    print("Don't forget to also set 'Broadcaster Login' to the channel where the")
    print("bot should post (= the streamer's Twitch login name) and to /mod the")
    print("bot in that channel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
