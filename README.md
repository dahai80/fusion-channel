# fusion-channel

[![CI](https://github.com/dahai80/fusion-channel/actions/workflows/ci.yml/badge.svg)](https://github.com/dahai80/fusion-channel/actions/workflows/ci.yml)

Unified messaging channel access layer for the Fusion ecosystem ("一核九端"). One library gives any Fusion application the ability to **receive, send, join groups, be @-mentioned, and push approval buttons** across SMS and IM providers — with verification, dedup, outbox retry, rate limiting, archiving, and identity mapping built in.

> Status: v0.1.5 — M1–M3 scope landed (core abstractions + 12 first-batch adapters, including pull-mode email). The M4 bridge service is intentionally **not** built yet (library-first per the PRD); M5 ecosystem adoption happens inside consumer projects.

## Why

Fusion has 48 sub-projects; every human-facing one eventually needs an IM channel. Each provider's details differ wildly — Twilio HMAC-SHA1 form signing, Feishu AES-CBC encrypted events + challenge handshake, DingTalk HMAC-SHA256 signing, Slack request signatures, WhatsApp `X-Hub-Signature-256`, WeCom AES + SHA1 callbacks. Getting any of these wrong means "anyone can forge the owner's commands". fusion-channel centralizes them once.

Design source: the PRD at `architecture/fusion-channel-prd-1025.md` and the production-verified ai-family SMS pipeline (verification / dedup / reject-archive / outbox backoff), ported behavior-equivalent.

## Install

```bash
cd /Users/dahai/fusion && source .venv/bin/activate
pip install -e ".[test]"        # add ,fastapi for the webhook router
```

## Supported channels (first batch)

| Adapter | Channel | Verify scheme | Cards | Groups |
|---------|---------|---------------|-------|--------|
| `twilio_sms` | SMS (Twilio) | HMAC-SHA1 `X-Twilio-Signature` | fallback text | – |
| `aliyun_sms` | SMS (Aliyun) | callback HMAC (optional) | – | – |
| `tencent_sms` | SMS (Tencent Cloud) | callback HMAC (optional) | – | – |
| `wecom_app` | WeCom self-built app | SHA1 signature + AES-256-CBC | partial (textcard) | ✅ |
| `feishu_app` | Feishu/Lark app | Encrypt Key AES + verification token | full | ✅ |
| `dingtalk_robot` | DingTalk group robot | HMAC-SHA256 signing (required) | full (ActionCard) | ✅ |
| `slack_app` | Slack Events API | `X-Slack-Signature` HMAC-SHA256 | full (Block Kit) | ✅ |
| `whatsapp_cloud` | WhatsApp Cloud API | `X-Hub-Signature-256` | fallback text | – |
| `telegram_bot` | Telegram Bot API | secret-token header | full (inline keyboard) | ✅ |
| `email_imap` | Email (IMAP/SMTP) | transport auth (TLS + login) | fallback text | – |
| `graph_mail` | Microsoft 365 / Exchange Online (Graph API, OAuth2 client credentials) | transport auth (OAuth2 + TLS) | fallback text | – |
| `gmail_api` | Gmail / Google Workspace (Gmail API, OAuth2 refresh token) | transport auth (OAuth2 + TLS) | fallback text | – |

**Compliance red line:** no personal-WeChat reverse-engineered protocols. Official APIs only.

## Quick start

```python
from fusion_channel import ChannelRegistry, ApprovalCard, CardAction
from fusion_channel.credentials import AESGCMCredentialProvider
from fusion_channel.storage import SqliteStore

store = SqliteStore("channel.db")
await store.init()
registry = ChannelRegistry.from_config(
    "channels.yaml",  # PRD §4.3 data-driven config
    credentials=AESGCMCredentialProvider("data/secrets"),
    store=store,
)

# --- inbound: FastAPI webhook in one line ---
from fusion_channel.fastapi import build_channel_router

app.include_router(build_channel_router(registry))  # POST /webhook/{channel_id}


# --- handlers: card buttons and text commands converge here ---
async def on_interaction(interaction):
    code = interaction.payload.get("code") or interaction.payload.get("value") or ""
    print("approval", code, interaction.action)  # one state machine, all channels


registry.on_interaction(on_interaction)
registry.register_command("approve", ["同意", "approve"], code_required=True)
registry.register_command("reject", ["拒绝", "reject"], code_required=True)
registry.on_message(lambda env: print(env.channel_id, env.text))

# --- outbound: outbox semantics, card degradation automatic ---
await registry.send(
    "family_twilio",
    {"type": "direct", "id": "+15550000001->+15550000000"},
    card=ApprovalCard(
        title="审批 AP-123456",
        code="AP-123456",
        body="New caregiver profile",
        actions=[CardAction(action="approve", label="同意"), CardAction(action="reject", label="拒绝")],
    ),
)
sent = await registry.outbox.flush_once()  # or run_loop() in background

# --- identity: external id -> fusion-identity account ---
user = registry.identify("family_twilio", "+15550000001")
```

Full runnable example: `examples/approve_bot.py` with `examples/channels.yaml`. Wiring a consumer service (outbox worker lifecycle, delivery-status receipts, Postgres-backed store, credential isolation): `docs/consumer-integration.md`.

## Architecture

```
ChannelRegistry ── ChannelAdapter(SPI) ── 12 provider adapters
      │                  │
IdentityMapper    SignatureVerifier (per-channel policy)
      │                  │
CredentialProvider  DedupGuard / ReplayGuard
      │                  │
ArchiveHook        OutboxSender (backoff retry / token bucket / degradation)
      │
AuditHook          CardRenderer (per CAN_CARDS degradation)
```

Non-negotiable rules baked into the library (PRD §4.2 / §7.1):

1. **Verification is mandatory at the SPI level** — an adapter class that does not implement `verify_request` cannot be registered (`ConfigError`).
2. **Archive-first** — inbound messages are persisted with the raw payload before business processing; duplicates are counted, never dropped.
3. **Rejected requests are archived too** (`RejectedEnvelope`) so attacks and lost messages are auditable.
4. **Outbox pattern** — send intents enqueue first; the flush loop retries with `[30, 120, 600]`s backoff, 3 attempts max, then fires `on_permanent_failure` (the ai-family alert pattern). Records are CAS-claimed (`pending` → `sending` lease) so two workers never double-send; a crashed worker's lease expires and the record requeues; pending records older than 24h fail permanently.
5. **Degradation is rendering, not business logic** — consumers always ask for a card; `CAN_CARDS=none` channels get the fallback text ("回复 同意 AP-123456"), and the reply parses back into the same `Interaction`.
6. **Credentials never live in the library** — `CredentialProvider` interface with an AES-256-GCM file-backed default (0600 key, atomic writes); reads through it return masked values.
7. **Fail closed** — verification/parse exceptions on attacker input reject-archive and return 4xx (never 500); webhook bodies over 1 MiB get 413; adapter error strings are secret-redacted before they reach logs or `SendResult`.
8. **Identity gate** — set `require_bound_identity: true` on a channel to dispatch interactions (card buttons + text commands) only for senders present in `identity_bindings`; unbound senders get 403 `identity_not_bound` plus an audit event.

Config details:

- **Credential refs**: each credential value may be `env:VAR_NAME` (read from the environment), a `CredentialProvider` key, or a plain literal (used as-is when no provider entry matches).
- **Archive modes**: `inbound.archive: full` (default, raw payload kept) / `metadata-only` (content and raw payload stripped at archive time; handlers still see the full envelope) / `off` (dedup record only).
- **Replay windows**: Slack / DingTalk / WeCom signatures older than 300s are rejected.

## Group & bot capabilities

- Conversation types: `direct` / `group` / `broadcast`.
- Watched-group allowlist (`groups:` in config) — unlisted groups are archived and ignored.
- `mentions_required: true` gates group messages on @bot.
- `group_ops()` on CAN_GROUP adapters: `list()` / `info()` / `broadcast()`; raises `CapabilityNotSupported` elsewhere so consumers can fall back to per-direct sends.

## Pull-mode channels (email)

Webhook channels get requests pushed; `email_imap` pulls instead. Adapters that override `ChannelAdapter.poll()` fetch their own messages (IMAP `UNSEEN`, batch-limited) and return them as raw requests — the registry runs each through the exact same `handle_inbound` pipeline (dedup, archive, commands, identity gate). There is no webhook signature to verify; TLS + account login are the trust boundary, and email `verify_request` always passes (logged). MIME handling stays thin: `text/plain` extraction plus attachment `MediaRef`s, `In-Reply-To` mapped to `conversation.thread_id`. Outbound goes through the same outbox (SMTP + STARTTLS); set the subject via `meta={"subject": ...}` or it defaults to the first line of the text.

```python
await registry.poll()  # one-shot: fetch + dispatch
registry.start_polling(interval=60)  # background worker; close() cancels it
```

`graph_mail` is the Microsoft 365 / Exchange Online variant: M365 has basic auth disabled, so IMAP password login cannot connect. It polls the same way via Microsoft Graph with OAuth2 client credentials (`tenant_id` / `client_id` / `client_secret` / `mailbox`; register an Entra ID app with the `Mail.ReadWrite` application permission on the shared mailbox). Config keys: `folder` (default `INBOX`), `poll_batch_limit` (default 20). Unread messages are fetched as full MIME, then marked read so re-poll dedup matches `email_imap` semantics; Graph attachment refs land in `MediaRef` and consumers can pull real bytes with `adapter.download_attachment(graph_message_id, attachment_id)` — the document-pipeline path for emailed PDFs. Outbound uses `POST /users/{mailbox}/sendMail`; subject rules match `email_imap`.

`gmail_api` is the Gmail / Google Workspace variant, also pull-mode with OAuth2 (`client_id` / `client_secret` / `refresh_token` / `user_id`; a Google Cloud OAuth client with the `gmail.readonly` + `gmail.send` scopes — service accounts with domain-wide delegation work through the same refresh-token contract). Config keys: `label_ids` (default `INBOX, UNREAD`), `query` (optional Gmail search filter), `poll_batch_limit` (default 25), `history_id` (seed for incremental sync), `draft_mode` (send creates a draft instead of sending — the human-approval path). Polling uses `historyId` incremental sync when a previous poll recorded one, falling back to the full label list when the history window has expired; messages are fetched `format=full` (attachment ids live in the payload parts), then have `UNREAD` stripped via `modifyLabels` so re-poll dedup matches `email_imap` semantics. 429 quota responses back off to the next poll cycle instead of crashing the loop. `threadId` and `In-Reply-To` both surface on the envelope (`conversation.thread_id` + `raw.gmail_thread_id`); attachment bytes come via `adapter.download_attachment(gmail_message_id, attachment_id)`. Outbound uses `POST .../messages/send` (or `drafts/create` in draft mode); subject rules match `email_imap`.

## Capability matrix

Every adapter declares `ChannelCapabilities` (CAN_RECEIVE / CAN_SEND / CAN_GROUP / CAN_BOT / CAN_MENTION / CAN_CARDS / CAN_MEDIA / CAN_RECEIPT / CAN_THREADS) — see `src/fusion_channel/capabilities.py`. Consumers branch on capabilities, never on adapter identity. Config `capabilities_override` tweaks per channel instance.

## Relationship to other Fusion components

- **fusion-core**: same in-tree distribution mode (`pip install -e`); no reverse dependency.
- **fusion-identity**: `IdentityMapper` maps external ids to identity accounts; RBAC stays in fusion-identity (`assert_bound_identity()` provides the two-factor primitive for sensitive commands).
- **fusion-gateway**: deliberately does **not** use its connector framework (no IM concepts, no verification, no background model).
- **ai-family**: first consumer + code donor (verification/dedup/outbox/secret-store semantics ported). Its switch-over is tracked in its own repo (PRD §11).

## Development

```bash
source /Users/dahai/fusion/.venv/bin/activate
pytest tests/                          # 196 tests: crypto vectors, webhook E2E, outbox, matrix, hardening, email
pytest tests/test_e2e_approval.py -v   # cross-channel approval acceptance
ruff check . && ruff format --check .  # lint
```

Test coverage highlights:

- Real signature vectors for every channel (including Feishu/WeCom AES-CBC round-trips, Slack replay-window rejection, DingTalk ms-timestamp handling, Aliyun RFC3986 percent-encoding).
- FastAPI TestClient E2E: signed webhook → verify → parse → dedup → archive → handler; bad signature → 403 + reject archive; duplicate → 200, no redispatch; internal errors → reject-archive + 400, never 500.
- GET handshakes: WeCom `echostr` URL verification and WhatsApp `hub.challenge` echo (token-checked), via the router's GET route.
- Delivery receipts (Twilio `MessageStatus`, WhatsApp `statuses`) are archived as receipts, never dispatched as messages; challenge retries always re-echo (never deduped).
- Cross-channel approval acceptance: the same `AP-123456` decision arrives via SMS text, Slack Block Kit button, DingTalk signed command, Telegram inline keyboard, WhatsApp text, WeCom encrypted callback, Feishu group mention, and Feishu card button callback — one handler, one state machine.
- Outbound E2E via `httpx.MockTransport`: Twilio/Feishu/Slack provider payload shapes asserted; `test_connection()` smoke-tested per adapter (DingTalk's is static — a robot send always lands in a real group).
- Email (`email_imap`): MIME parse (text extraction, attachments, In-Reply-To threading), SMTP send with subject/In-Reply-To headers, fake-transport E2E through `registry.poll()` including dedup on re-poll, fetch-error isolation, card-fallback outbox send, polling worker lifecycle.
- Microsoft 365 (`graph_mail`): mocked Graph HTTP via `httpx.MockTransport` — OAuth2 client-credentials token fetch + caching + 401 refresh, unread list → MIME fetch → mark-read verified, Graph attachment refs into `MediaRef`, `download_attachment()` bytes, registry poll dispatch + dedup on re-poll, mark-read-failure dedup fallback, `sendMail` payload shape, credentials-missing and provider-error paths.
- Gmail (`gmail_api`): mocked Gmail HTTP via `httpx.MockTransport` — OAuth2 refresh-token token fetch + caching + 401 refresh, label list → `format=full` fetch → `modifyLabels` UNREAD-strip verified, historyId incremental sync + 404 gap fallback to full list, 429 backoff on list and per-message fetch, `threadId`/`In-Reply-To` threading, `download_attachment()` bytes, registry poll dispatch + dedup on re-poll, label-strip-failure dedup fallback, `messages.send` raw MIME shape, `drafts.create` draft mode, credentials-missing and provider-error paths.
- Live provider calls require sandbox credentials and are marked `integration` (excluded by default): Feishu tenant-token handshake; QQ Mail real IMAP/SMTP E2E (login + poll + dedup on the live INBOX, SMTP send-to-self with loopback receive, env-based credentials `FUSION_CHANNEL_QQ_EMAIL` / `FUSION_CHANNEL_QQ_AUTH_CODE`).

## License

Apache-2.0
