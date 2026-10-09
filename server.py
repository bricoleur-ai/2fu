#!/usr/bin/env python3
"""2fu: relay 2FA codes from SMS text to consumers on the local network."""

import json
import logging
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# Defaults. Each can be overridden from the environment (see settings_from_env):
# TWOFU_CLIENTS  names your consumers present as ?client=<name>, comma-separated; only these may fetch a code
# TWOFU_PORT     the port to listen on, on all interfaces
# TWOFU_TTL_SECONDS  how long an unfetched code lives
CLIENTS = {"my-client"}
PORT = 8787
TTL_SECONDS = 300

MAX_BODY_BYTES = 16 * 1024

log = logging.getLogger("2fu")

# A standalone run of 4-8 digits, or two groups of three split by a hyphen or space.
_CANDIDATE = r"(?<!\d)(\d{3}[- ]\d{3}|\d{4,8})(?!\d)"
_AFTER_KEYWORD = re.compile(
    r"\b(?:code|pin|passcode|password|otp)\b\D{0,20}?" + _CANDIDATE, re.IGNORECASE
)
_FIRST_CANDIDATE = re.compile(_CANDIDATE)


def parse_code(message):
    """Return the 2FA code in an SMS as a string of digits, or None."""
    match = _AFTER_KEYWORD.search(message) or _FIRST_CANDIDATE.search(message)
    if match is None:
        return None
    return re.sub(r"\D", "", match.group(1))


class CodeSlot:
    """Holds at most one code, in memory only."""

    def __init__(self, ttl, clock):
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._code = None
        self._stored_at = 0.0

    def put(self, code):
        with self._lock:
            self._code = code
            self._stored_at = self._clock()

    def take(self):
        """Return the pending code and drop it, or None if absent or expired."""
        with self._lock:
            code, self._code = self._code, None
            if code is None or self._clock() - self._stored_at > self._ttl:
                return None
            return code


class Handler(BaseHTTPRequestHandler):
    # Stop a client that stalls mid-request from holding a thread forever.
    timeout = 10

    def do_POST(self):
        if urlsplit(self.path).path != "/sms":
            return self._reply(404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._reply(400)
        if length < 0:
            return self._reply(400)
        if length > MAX_BODY_BYTES:
            # Drain a modest overage so the client sees the 413 rather than a reset.
            self.rfile.read(min(length, 64 * MAX_BODY_BYTES))
            return self._reply(413)
        try:
            message = json.loads(self.rfile.read(length))["message"]
        except (ValueError, KeyError, TypeError, RecursionError):
            return self._reply(400)
        if not isinstance(message, str):
            return self._reply(400)
        code = parse_code(message)
        if code is None:
            log.info("message had no code")
            return self._reply(422)
        self.server.slot.put(code)
        log.info("code stored")
        self._reply(204)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != "/code":
            return self._reply(404)
        client = parse_qs(url.query).get("client", [""])[0]
        # Check the name first so an unknown caller learns nothing about the slot.
        if client not in self.server.clients:
            log.info("fetch denied for client %r", client)
            return self._reply(403)
        code = self.server.slot.take()
        if code is None:
            return self._reply(404)
        log.info("code served to %s", client)
        self._reply(200, {"code": code})

    def _reply(self, status, body=None):
        payload = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        if payload:
            self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_request(self, code="-", size="-"):
        # The default request log would echo full query strings.
        pass

    def log_message(self, format, *args):
        log.info("%s %s", self.address_string(), format % args)


class Server(ThreadingHTTPServer):
    # The default backlog of 5 makes a burst of connections wait on SYN retries.
    request_queue_size = 64


def settings_from_env(environ):
    """Return (clients, port, ttl) from TWOFU_* variables, with the constants above as defaults.

    A variable that is unset or blank means "use the default". A value that cannot be used raises
    ValueError, with a message that starts with the variable's name.
    """
    clients = CLIENTS
    raw = environ.get("TWOFU_CLIENTS", "")
    if raw.strip():
        clients = {name.strip() for name in raw.split(",") if name.strip()}
        if not clients:
            raise ValueError(f"TWOFU_CLIENTS must name at least one client, got {raw!r}")
    port = _whole_number(environ, "TWOFU_PORT", PORT, low=1, high=65535)
    ttl = _whole_number(environ, "TWOFU_TTL_SECONDS", TTL_SECONDS, low=1)
    return frozenset(clients), port, ttl


def _whole_number(environ, name, default, low, high=None):
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None
    if value < low or (high is not None and value > high):
        limits = f"from {low} to {high}" if high is not None else f"at least {low}"
        raise ValueError(f"{name} must be {limits}, got {value}")
    return value


def make_server(port=PORT, clients=CLIENTS, ttl=TTL_SECONDS, clock=time.monotonic):
    server = Server(("0.0.0.0", port), Handler)
    server.clients = frozenset(clients)
    server.slot = CodeSlot(ttl, clock)
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        clients, port, ttl = settings_from_env(os.environ)
    except ValueError as error:
        sys.exit(f"2fu: {error}")
    server = make_server(port, clients, ttl)
    log.info("listening on port %d, clients: %s, codes live %ds", port, ", ".join(sorted(clients)), ttl)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
