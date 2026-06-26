# Gemini Web Service

A small FastAPI service that wraps [`gemini-webapi`](../README.md) and exposes
Google Gemini over HTTP. Send a prompt, get back text plus any generated
**images**, **video**, or **voice/audio**. Media can be returned three ways
(per request): Gemini's original `url`, inline `base64`, or a binary `stream`
the service downloads with the account's own session — see [Media delivery](#media-delivery).

It supports **multiple Gemini accounts with automatic failover** — requests try
accounts in priority order and fall through to a backup when one is rate-limited
or its cookies expire. Accounts are managed at runtime through a small **admin UI**
(no file edits), and everything is gated behind an API key.

## Admin UI

Open `http://<host>:8000/` in a browser, enter your `SERVICE_API_KEY`, then:

- **Add account** — paste a label and the two cookie values (`__Secure-1PSID`,
  `__Secure-1PSIDTS`). That is all "adding an account" means.
- **Test cookies** — validates a pair before (or after) saving; reports the live
  account status. Bad/expired cookies are reported as failures.
- **Enable/disable**, set **priority** (lower number = tried first), or **delete**.
- **Try a prompt** — end-to-end test that exercises failover.

Accounts are stored in `accounts.json` on the data volume.

## Endpoints

| Method | Path                          | Auth        | Purpose                              |
|--------|-------------------------------|-------------|--------------------------------------|
| GET    | `/`                           | none*       | Admin UI (calls are key-gated).      |
| GET    | `/health`                     | none        | Liveness check.                      |
| GET    | `/models`                     | none        | List available model names.          |
| GET    | `/api/accounts`               | `X-API-Key` | List accounts (cookies masked).      |
| POST   | `/api/accounts`               | `X-API-Key` | Add an account.                      |
| PATCH  | `/api/accounts/{id}`          | `X-API-Key` | Edit label/priority/enabled/cookies. |
| DELETE | `/api/accounts/{id}`          | `X-API-Key` | Remove an account.                   |
| POST   | `/api/accounts/{id}/test`     | `X-API-Key` | Validate a stored account.           |
| POST   | `/api/test`                   | `X-API-Key` | Validate arbitrary cookies.          |
| POST   | `/generate`                   | `X-API-Key` | Generate content (with failover).    |
| POST   | `/jobs`                       | `X-API-Key` | Submit an async generation job (202 + id). |
| GET    | `/jobs`                       | `X-API-Key` | List jobs (newest first).            |
| GET    | `/jobs/{id}`                  | `X-API-Key` | Poll a job's status / result.        |
| DELETE | `/jobs/{id}`                  | `X-API-Key` | Delete a job.                        |
| GET    | `/media/{id}`                 | `X-API-Key` | Stream a downloaded media file (`media=stream`). |
| GET    | `/docs`                       | none        | Interactive OpenAPI docs.            |

### `POST /generate`

```jsonc
// request
{ "prompt": "Generate an image of a red bicycle on a beach",
  "model": "gemini-3-pro",        // optional; omit for the default model
  "media": "url",                 // url | base64 | stream  (default: url)
  "res-type": "image",            // optional; expected modality (see below)
  "remove_watermark": false }     // strip the visible corner logo (needs base64/stream)
```

```jsonc
// response — unified media[] list
{ "status": "completed",           // completed | failed (see "Expecting a response type")
  "account": "primary",            // which account served the request
  "text": "...",
  "thoughts": null,
  "media": [
    { "kind": "image",             // image | video | audio
      "title": "[Image]",
      "source_url": "https://lh3.googleusercontent.com/...",  // always
      "mime_type": "image/png",    // set when downloaded (base64/stream)
      "size": 1922870,             // bytes, when downloaded
      "data_base64": "iVBORw0K...",// set when media=base64
      "stream_url": null }         // set when media=stream -> GET it with the API key
  ],
  "metadata": ["c_...", "r_..."] } // [chat_id, reply_id] for follow-ups later
```

### Async jobs — `POST /jobs` → poll `GET /jobs/{id}`

For slow generations the client need not hold a connection open. Submit a job,
get an id back immediately, then poll for the result (or receive a webhook). The
request body is the same as `POST /generate`, plus an optional `callback_url`.

```jsonc
// POST /jobs  -> 202 Accepted
{ "id": "9f2c…", "status": "queued", "result": null, "error": null,
  "created_at": "…", "updated_at": "…" }
```

`GET /jobs/{id}` returns the current job; `status` moves through
`queued → processing → completed | failed`:

- `completed` → `result` holds the `GenerateResponse` (same shape as `/generate`).
- `failed` → `error` holds the failure message **or**, when a `res-type` was set
  and the response lacked that modality, `result` still holds the `GenerateResponse`
  (with its own `"status": "failed"`) so you can inspect what came back.

`GET /jobs` lists jobs (newest first); `DELETE /jobs/{id}` removes one.

**Delivery.** Poll `GET /jobs/{id}`, or pass a `callback_url`: on completion the
service POSTs `{id, status, result, error, created_at, updated_at}` to that URL
(retried with backoff — see `JOB_CALLBACK_*`). Polling stays available either way.

**State.** Job state lives in **Redis** when `REDIS_URL` is set, else in a local
JSON file (`JOBS_PATH`). Both survive a restart; a job left mid-flight is
re-queued on startup. Concurrency is bounded by `JOB_WORKERS`, and finished jobs
are evicted after `JOB_TTL` seconds. A set-but-unreachable `REDIS_URL` is logged
and degrades to the file store.

```sh
# submit, then poll until done
ID=$(curl -s -X POST localhost:8000/jobs -H 'x-api-key: KEY' \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a red apple","media":"base64"}' | jq -r .id)
curl -s localhost:8000/jobs/$ID -H 'x-api-key: KEY' | jq '{status, error}'
```

## Expecting a response type (`res-type`)

Gemini doesn't always return what you asked for — an image prompt can come back
as a plain text refusal (e.g. *"I can create more images as soon as your limit
resets"*) with no media. `res-type` lets you declare the modality you expect so
the service can tell you whether the response actually delivered it.

Set `res-type` to one of `text` | `image` | `video` | `audio`:

- If the response **contains** that modality → `status: "completed"`.
- If it **does not** → `status: "failed"`. The result is **still returned** (text,
  media, metadata) — only the status flips, so you can log or inspect what came back.
- Omit `res-type` entirely → no enforcement; `status` is always `"completed"`
  (unchanged behaviour for existing callers).

| `res-type` | "completed" when the response has… |
|------------|-------------------------------------|
| `text`     | non-empty text                      |
| `image`    | at least one image                  |
| `video`    | at least one video                  |
| `audio`    | at least one audio item             |

### `/generate` (synchronous)

The HTTP status is **always 200** — branch on the `status` field in the body:

```sh
# Expect an image. If Gemini returns only text, status comes back "failed".
curl -s -X POST localhost:8000/generate -H 'x-api-key: KEY' \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Generate an image of a red fox","media":"base64","res-type":"image"}' \
  | jq '{status, account, text, n_media: (.media | length)}'
```

```jsonc
// when the image quota is exhausted and only text comes back:
{ "status": "failed", "account": "photo01",
  "text": "I can create more images as soon as your limit resets.",
  "n_media": 0 }
```

### `/jobs` (asynchronous)

The job's own `status` becomes `failed` when the requested `res-type` is missing,
and `result` still carries the `GenerateResponse`:

```sh
ID=$(curl -s -X POST localhost:8000/jobs -H 'x-api-key: KEY' \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Generate an image of a red fox","media":"base64","res-type":"image"}' \
  | jq -r .id)
curl -s localhost:8000/jobs/$ID -H 'x-api-key: KEY' | jq '{status, result_status: .result.status}'
# -> { "status": "failed", "result_status": "failed" }   (result still populated)
```

## Media delivery

`media` in the request selects how bytes come back:

| Mode | What you get | Use when |
|------|--------------|----------|
| `url` (default) | `source_url` only — Gemini's own URL | You'll fetch it yourself with a valid session. |
| `base64` | `data_base64` + `mime_type` + `size` | Small media; one round-trip; decode and store. |
| `stream` | `stream_url` (`GET /media/{id}`, API-key gated) | Large media; pipe the binary straight into object storage (e.g. download → re-upload to MinIO → serve a public URL). |

For `base64`/`stream` the service downloads each item **using the producing
account's authenticated session**, so cookie-gated URLs resolve correctly. In
`stream` mode the bytes live in a short-lived on-disk cache
(`MEDIA_CACHE_TTL`, default 1h) and are evicted automatically.

## Watermark removal

Set `"remove_watermark": true` to strip Gemini's **visible** logo from the
bottom-right corner of generated images. The service inverts Gemini's alpha
compositing (`original = (watermarked − α·255) / (1 − α)`) using pre-captured
alpha maps in `service/assets/`, so recovery is mathematically exact apart from
8-bit rounding. Logo geometry is detected from the image size (48×48 / 32px
margin up to 1024px, 96×96 / 64px above).

Caveats:
- Requires `media=base64` or `media=stream` — the bytes must be downloaded to be
  processed; it is ignored for `media=url`. Only `kind=image` items are touched.
- Removes only the **visible** watermark, **not** SynthID (the invisible
  watermark Gemini embeds during generation).
- Works best on the original PNG; heavy re-compression degrades the result.

```sh
curl -s -X POST localhost:8000/generate -H 'x-api-key: KEY' \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a red bicycle on a beach","media":"base64","remove_watermark":true}'
```

Example — stream a generated image into a file:

```sh
URL=$(curl -s -X POST localhost:8000/generate -H 'x-api-key: KEY' \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a red apple","media":"stream"}' | jq -r '.media[0].stream_url')
curl -H 'x-api-key: KEY' "$URL" -o apple.png      # then re-upload apple.png to MinIO
```

## Where cookies come from

The library authenticates to `gemini.google.com` with browser cookies. On a
**headless host there is no browser**, so cookies are supplied explicitly per
account — through the admin UI (see above), not the environment.

To get a pair: in a browser logged in to <https://gemini.google.com>, open
DevTools (F12) → **Network** → click any request → copy the values of
`__Secure-1PSID` and `__Secure-1PSIDTS`. Paste them into the **Add account** form.

The service auto-refreshes `__Secure-1PSIDTS` per account while running and
persists the refreshed cookies to `GEMINI_COOKIE_PATH` (a mounted volume), so it
keeps working across restarts without re-pasting. `__Secure-1PSID` is stable for
weeks; when it finally expires the account shows `error` in the UI and you paste
a fresh pair — the backup account keeps serving traffic in the meantime.

> Tip: grab each account's cookies from a private/incognito session and close it
> right after, so Google doesn't rotate them out from under the service.

## Run with Docker (recommended)

```sh
cp .env.example .env          # set SERVICE_API_KEY (cookies are added via the UI)
docker compose up --build -d
# then open http://<host>:8000/ and add your accounts
```

`accounts.json`, media, and refreshed cookies persist under `./data/` (git-ignored).

## Run locally without Docker

```sh
pip install -e .                       # the gemini-webapi library
pip install -r service/requirements.txt
export SERVICE_API_KEY=test-key
uvicorn service.main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000/ and add accounts
```

## Example call

```sh
curl -X POST localhost:8000/generate \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <your SERVICE_API_KEY>' \
  -d '{"prompt":"Write a haiku about the sea"}'
```

## Configuration (environment variables)

| Var                     | Required | Description                                            |
|-------------------------|----------|--------------------------------------------------------|
| `SERVICE_API_KEY`       | yes      | Secret callers send as the `X-API-Key` header.         |
| `DATA_DIR`              | no       | Base dir for `accounts.json`, media, cookies (`./data`).|
| `GEMINI_SECURE_1PSID`   | no       | Optional one-time seed for the first account.          |
| `GEMINI_SECURE_1PSIDTS` | no       | Optional companion seed cookie.                        |
| `GEMINI_COOKIE_PATH`    | no       | Dir for auto-refreshed cookies (set to a volume).      |
| `GEMINI_PROXY`          | no       | Outbound proxy URL.                                    |
| `REQUEST_TIMEOUT`       | no       | Per-request timeout in seconds (default 300).          |
| `MEDIA_CACHE_DIR`       | no       | Dir for streamed-media buffer (`DATA_DIR/cache`).      |
| `MEDIA_CACHE_TTL`       | no       | Seconds before streamed media is evicted (default 3600).|
| `REDIS_URL`             | no       | Store async-job state in Redis; unset = local JSON file.|
| `JOBS_PATH`             | no       | File-backed job store path (when no `REDIS_URL`).      |
| `JOB_WORKERS`           | no       | Background workers draining the job queue (default 3). |
| `JOB_TTL`               | no       | Seconds to keep finished jobs (default 86400).         |
| `JOB_CALLBACK_TIMEOUT`  | no       | Webhook POST timeout in seconds (default 15).          |
| `JOB_CALLBACK_RETRIES`  | no       | Webhook delivery attempts (default 3).                 |

## Notes & limits

- **Accounts are shared across callers.** The API key controls *who* may call the
  service; it is not per-user quota. All traffic flows through the configured
  accounts and their Google rate limits, in priority order with failover.
- **Video/voice** depend on your account having access to those Gemini features
  (e.g. Veo). They generate the same way and appear in `media[]` with the
  matching `kind`.
- **`stream` mode buffers bytes briefly** in `MEDIA_CACHE_DIR` and evicts them
  after `MEDIA_CACHE_TTL`. Fetch the `stream_url` promptly. `base64` mode keeps
  nothing on disk.
- Image/video/audio generation can take significant time; `REQUEST_TIMEOUT`
  bounds each request.
- Put TLS/rate-limiting in front (e.g. nginx, Caddy, or your platform's ingress)
  for any public deployment.
