# 2fu — a tiny 2FA SMS code relay

**2fu** receives the text of a 2FA SMS (forwarded from a phone) and lets a known
consumer fetch the parsed code exactly once. The code lives in memory only and is
dropped the moment it's served.

It's one file, Python standard library only — no dependencies, no build step,
nothing written to disk.

## Why

You have an automation (a script, a browser extension, a headless job) that needs
the 6-digit SMS code to finish a login. Your phone can forward the text somewhere
(an iPhone Shortcut, Tasker, an SMS-forwarding app). 2fu is the small middleman:
the phone POSTs the message to it, your automation GETs the code.

## How it works

- **One slot.** 2fu holds the single most recent code. A newer text replaces an
  unfetched older one.
- **Single use.** The first allowlisted consumer to GET the code receives it, and
  the code is dropped. A second GET gets nothing.
- **In memory only.** Nothing is persisted; a restart loses any pending code.
- **Expiry.** An unfetched code is dropped after 5 minutes so a consumer never
  gets a stale code from an earlier login.

## Endpoints

| Request | Result |
|---|---|
| `POST /sms` — JSON `{"message": "<full SMS text>"}` | `204` code stored · `422` no code found · `400` bad body · `413` body over 16 KB |
| `GET /code?client=<name>` | `200 {"code":"123456"}` and the code is dropped · `403` unknown client · `404` nothing pending |

`POST /sms` is **not** authenticated — the phone sends only the text, and anyone
on your network could post a bogus code, though they can't *read* a real one.
`GET /code` is gated by the client allowlist, checked before anything else so an
unknown caller can't even tell whether a code is waiting. Keep 2fu on a trusted
network (LAN, VPN, Tailscale); it has no TLS of its own.

## Run it

```sh
python3 server.py
```

It listens on all interfaces on port 8787. Edit the constants at the top of
`server.py`:

```python
CLIENTS = {"my-client"}   # the consumer name(s) allowed to fetch a code
PORT = 8787
TTL_SECONDS = 300         # how long an unfetched code lives
```

### Try it

```sh
# pretend a phone forwarded a 2FA text:
curl -i -X POST http://localhost:8787/sms \
  -H 'Content-Type: application/json' \
  -d '{"message": "Your verification code is 123456"}'

# a consumer fetches it (and it's dropped):
curl -i 'http://localhost:8787/code?client=my-client'
```

Consumers poll `GET /code` until they get a `200`.

## Code parsing

2fu takes the 4–8 digit number following a keyword (`code`, `PIN`, `passcode`,
`OTP`, …); failing that, the first standalone 4–8 digit number. `123-456` and
`123 456` are joined. Messages with no such number return `422`.

## iPhone Shortcut (one way to forward texts)

1. Shortcuts app → **Automation** → **＋** → **Message**.
2. **Message Contains** a word your 2FA texts share, e.g. `code`. **Run Immediately**.
3. Add **Get Contents of URL**.
4. URL: `http://<your-2fu-host>:8787/sms`.
5. Method `POST`, Request Body `JSON`, one field `message` = the **Shortcut Input**
   variable (the incoming message).

Add one automation per keyword if your texts don't all share one. Any
SMS-forwarding method that can POST JSON works just as well.

## Run as a service (Linux, systemd user unit)

A template unit is in [`2fu.service`](2fu.service):

```sh
cp 2fu.service ~/.config/systemd/user/
# edit ExecStart to point at your checkout
systemctl --user daemon-reload
systemctl --user enable --now 2fu
loginctl enable-linger "$USER"   # so it runs without a login session
```

The log records events only (stored, served to which client, denied). It never
contains a code or the message text.

## Tests

```sh
python3 -m unittest
```

## Design

See [`docs/DESIGN.md`](docs/DESIGN.md).

## License

MIT — see [LICENSE](LICENSE). © 2026 DynamoFoundry LLC.

Part of the [Bricoleur.ai](https://bricoleur.ai) ecosystem.
