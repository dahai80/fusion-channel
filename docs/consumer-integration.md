# Consumer Integration Guide

How a consumer application (FastAPI service, agent backend, ...) wires into fusion-channel. Grounded in the code — symbol references point at `src/fusion_channel/`.

Written while answering issue #4 (ai-booking-agent M3 outbound booking-confirmation SMS); the answers generalize.

## 1. Outbound: `registry.send()` + outbox worker

```python
from fusion_channel import ChannelRegistry

registry = ChannelRegistry.from_config("channels.yaml", store=my_store)

outbox_id = await registry.send(
    "booking_sms",                                # channel id from channels.yaml
    {"type": "direct", "id": "+15550000001->+15550000000"},  # to->from for SMS
    text="Your cleaning is confirmed for Tue 10:00. Reply RESCHEDULE to change.",
)
# outbox_id: int  — persist it in your own notifications row for correlation.
# None means a dedup hit: identical dedup_key already enqueued (idempotent).
```

- `send()` only **enqueues** (`OutboxSender.enqueue`, `outbox.py`). The flush loop retries with `[30, 120, 600]`s backoff, 3 attempts max.
- Long-running process: start the owned worker once, stop it on shutdown — `registry.start_outbox(interval=5.0)`, cancelled by `await registry.close()`. A manual `registry.outbox.flush_once()` loop is the same thing with more code.
- Permanent failure (3 attempts exhausted, or pending > 24h) fires `registry.outbox.on_permanent_failure` — assign it: `async def alert(message, error): ...` → your alerting. Signature: `Callable[[OutboundMessage, str], Awaitable[None]]`.
- On success the outbox record stores `provider_message_id` (Twilio `MessageSid`, etc.) — correlate it with your own row via the `outbox_id` you got from `send()`.
- Optional per-channel rate limit: `rate_limit: {per_minute: N}` in the channel config (token bucket).

## 2. Delivery status: receipts, not messages

Provider status callbacks (Twilio `MessageStatus=delivered/undelivered/failed`) are parsed as `Receipt` and **archived, never dispatched as messages** (`registry.py` handle_inbound receipt branch → `ArchiveHook.on_receipt` → `store.save_receipt`).

To write delivery status into your own `notifications` table, point Twilio's status callback URL at the webhook route (`POST /webhook/{channel_id}` via `build_channel_router`) and either:

- subclass `ChannelStore.save_receipt` to also `UPDATE notifications SET status = %s`, or
- poll your store's receipts table from your side.

Receipt requests pass the same signature verification as any inbound webhook — status callbacks are attacker-reachable surface.

## 3. Store choice

`ChannelStore` (`storage.py`) is the sanctioned seam: "swap in a consumer's own DB by subclassing". The interface is small — `save_inbound` (returns False on duplicate), `bump_duplicate`, `save_rejected`, `save_outbound` (returns None on dedup hit), `due_outbound`, `update_outbound`, `claim_outbound` (CAS pending→sending), `requeue_stale_sending`, `save_receipt`, `init`, `close`.

- `SqliteStore`: fine for single-process services, local-first deployments, getting started.
- **Recommended for a long-running FastAPI service with its own Postgres**: implement a Postgres-backed `ChannelStore`. Then outbox, receipts, and your business rows live in one DB with one transaction story. Preserve the contracts exactly: `save_inbound`/`save_outbound` return values drive dedup; `claim_outbound` must be a true CAS (single `UPDATE ... WHERE status='pending'`) — that is the double-send guard for multi-worker deployments.

## 4. Credentials

Each credential value in `channels.yaml` resolves in this priority order (`registry._credential_value`):

1. `env:VAR_NAME` — read from the environment
2. a `CredentialProvider` key equal to the written value
3. the channel-scoped default key `{channel_id}:{field_name}`
4. used as the literal value (only when nothing above matches)

Multi-consumer isolation: nothing in the library constrains key naming — isolation is the issuing side's job (fusion-identity). Recommended convention: one credential key per consumer+channel, e.g. `ai-booking-agent/twilio:account_sid`, referenced explicitly from that consumer's config. Two caveats:

- Never write secret-looking literals in config — an unmatched value is **used as the literal** (fallback 4), so typos in a provider key silently downgrade to plaintext.
- The channel `id` is the isolation unit inside one registry process. Two consumers sharing one process need distinct channel ids + distinct credential keys; two consumers in separate processes (the normal case) can each have their own `channels.yaml` with no coordination.

## 5. SMS specifics (US long codes / 10DLC)

`twilio_sms` sends via the Twilio Messages API (`POST /2010-04-01/Accounts/{sid}/Messages.json`, `To`/`From`/`Body` + basic auth). `from_number` is validated as E.164 (`^\+[1-9]\d...`), so a US long code works out of the box. 10DLC brand/campaign registration and toll-free verification are Twilio-account-side concerns — the adapter has nothing that blocks or requires them. Auth accepts either `account_sid`+`auth_token` or `api_key_sid`+`api_key_secret`.
