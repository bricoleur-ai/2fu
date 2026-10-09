# 2fu — design

## Purpose

A phone forwards the text of each incoming 2FA SMS to this service. The service
extracts the code and holds it in memory. A consumer fetches the code by naming
itself; if the name is on an allowlist (`TWOFU_CLIENTS`) it gets the code, and the code is
then dropped. Success means a consumer that has just triggered a login can poll
and receive the code within a second or two of the text arriving, with no code
ever touching disk and no code served twice.

## Approach

A single Python file using only the standard library (`http.server` with
`ThreadingHTTPServer`). No dependencies, no build step.

## Configuration

The three `TWOFU_*` variables (`TWOFU_CLIENTS`, `TWOFU_PORT`, `TWOFU_TTL_SECONDS`)
override the defaults in `server.py`. A blank value means the default. A value 2fu
cannot use stops it at start, with a message naming the variable.

## API

### `POST /sms`

Body is JSON: `{"message": "<full text of the SMS>"}`.

| Case | Response |
|---|---|
| A code is found | `204`, code stored, replacing any unfetched code |
| Body is not a JSON object with a string `message` | `400` |
| Body larger than 16 KB | `413` |
| No code found in the message | `422`, the stored code (if any) is untouched |

Not gated by client name — the phone sends only the text.

### `GET /code?client=<name>`

The client name is checked first, so an unknown caller can't learn whether a code
is pending.

| Case | Response |
|---|---|
| `client` missing or not in the allowlist | `403`, the code is untouched |
| Known client, a live code is pending | `200 {"code":"123456"}`, then the code is dropped |
| Known client, nothing pending or expired | `404` |

Match on client name is exact and case-sensitive. Responses carry
`Cache-Control: no-store`.

## Parsing

A candidate is a standalone run of 4–8 digits, or two groups of three digits
separated by one hyphen or space (joined).

1. If a keyword (`code`, `pin`, `passcode`, `password`, `otp`, whole word,
   case-insensitive) is followed by a candidate with no other digits in between
   and within ~20 characters, use that candidate.
2. Otherwise use the first candidate in the message.
3. Otherwise there is no code.

## State and expiry

One slot holding `(code, stored_at)` guarded by a lock. Reading and clearing
happen under the same lock, so two simultaneous fetches can't both receive the
code. `stored_at` uses a monotonic clock; a code older than `TWOFU_TTL_SECONDS` (default 300) is
treated as absent. Nothing is persisted.

## Logging

One line per event: code stored, code served (with the client name), fetch
denied (with the name offered), message with no code. A fetch that finds nothing
pending is not logged, so a polling consumer doesn't flood the journal. The code
and the message text are never logged.

## Out of scope

- Authentication on `POST /sms`. Anyone on the network can inject a bogus code;
  they can't read a real one.
- Long-polling (consumers poll), per-client slots, a queue of codes, and TLS.
  Keep the service on a trusted network.
- Secret client names. A client name is a label, not a credential; anyone on the
  network who knows one can fetch a waiting code.
