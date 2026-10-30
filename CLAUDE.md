# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`fusion-channel` is the Fusion ecosystem's unified messaging channel access layer: webhook verification, outbox retry, dedup, archiving, interactive cards, group ops, and identity mapping across SMS/IM/email providers (Twilio, Aliyun/Tencent SMS, WeCom, Feishu, DingTalk, Slack, WhatsApp Cloud, Telegram, IMAP/SMTP email, Microsoft 365 Graph mail, Gmail API mail). In-tree Python library, `pip install -e fusion-channel`, same distribution mode as fusion-core. Library-first by design — no resident service, no port. The M4 bridge service is intentionally not built (PRD §9). Authoritative spec: `/Users/dahai/fusion/architecture/fusion-channel-prd-1025.md`.

## Commands

```bash
cd /Users/dahai/fusion && source .venv/bin/activate   # REQUIRED before any Python work

cd fusion-channel
pip install -e ".[test]"          # build/install (edit mode)
pytest tests/                     # full suite (asyncio_mode=auto)
pytest tests/test_e2e_approval.py::test_approval_converges_across_channels -v   # single test
ruff check . && ruff format .     # lint + format
```

Tests default to `-m 'not integration'`; live-provider tests (needing sandbox credentials) carry the `integration` marker.

## Architecture

`src/fusion_channel/`:

- `spi.py` — `ChannelAdapter` ABC. **`verify_request` is abstract: an adapter without its own implementation cannot be registered** (registry re-checks at registration and raises `ConfigError`).
- `registry.py` — `ChannelRegistry` facade: `from_config` → `handle_inbound` (verify → reject-archive → parse → group/mention filters → dedup → archive → dispatch) and `send` (outbox enqueue with card rendering).
- `models.py` — pydantic core: `InboundEnvelope` (archive-first: carries full raw payload), `OutboundMessage`, `Card`/`ApprovalCard`, `Interaction`, `Conversation` (direct/group/broadcast).
- `capabilities.py` — `ChannelCapabilities` + the 12-adapter capability matrix (PRD §3.3). Consumers branch on capabilities, never adapter identity.
- `verify.py` — per-channel signature/encryption primitives (Twilio HMAC-SHA1, Slack `v0:` signing with replay window, DingTalk HMAC-SHA256 with ms timestamps, WhatsApp hub signature, Feishu AES-256-CBC, WeCom SHA1+AES, generic HMAC). Includes encrypt helpers used by tests.
- `cards.py` — `CardRenderer` (per-CAN_CARDS degradation: full cards → partial card+text → fallback text) and `CommandParser` (text commands like `同意 AP-123456` produce the same `Interaction` a card button produces — the cross-channel convergence point).
- `outbox.py` — `OutboxSender`: enqueue-first, `[30,120,600]`s backoff, 3 attempts, `on_permanent_failure` hook, per-channel `TokenBucket` rate limits.
- `storage.py` — `ChannelStore` interface with `MemoryStore` and `SqliteStore` (aiosqlite). Swap in a consumer's own DB by subclassing.
- `credentials.py` — `CredentialProvider` interface; AES-256-GCM file-backed default (0600 key file, atomic writes, masked describe), env/memory/chained providers.
- `identity.py` — `IdentityMapper`: external id ↔ fusion-identity account, `assert_bound_identity()` two-factor primitive.
- `hooks.py` — `ArchiveHook` (inbound raw / rejected / receipts) and `AuditHook` (sinks).
- `config.py` — YAML config (PRD §4.3): channels, credentials refs, groups allowlist, `mentions_required`, `capabilities_override`, rate limits, `inbound.verify` (required|disabled), `inbound.archive` (full|metadata-only|off).
- `fastapi.py` — `build_channel_router(registry)` → `POST /webhook/{channel_id}`; handles Feishu/Slack challenge echoes via adapter `webhook_response`.
- `adapters/` — one file per provider, registered into `ADAPTERS` via `@register`. `HttpAdapterMixin._client()` injects `config["http_transport"]` (httpx.MockTransport) so tests never touch the network. `email_imap.py` is pull-mode: overrides `ChannelAdapter.poll()` (IMAP UNSEEN via injected `config["imap_fetch"]`) and sends via `config["smtp_send"]` (SMTP+STARTTLS); tests inject fakes for both. `ChannelRegistry.poll()` / `start_polling()` feed polled requests through the same handle_inbound pipeline; email `verify_request` always passes (TLS + login is the trust boundary). `graph_mail.py` is the M365/Exchange Online pull-mode variant (basic auth disabled there): OAuth2 client credentials via Graph API — token cached with 401 refresh, poll = list unread → fetch `$value` MIME → mark `isRead` → same ChannelRequest shape; Graph attachment refs become `MediaRef`s and `download_attachment(message_id, attachment_id)` returns real bytes; send via `sendMail`; config `folder` (INBOX default) + `poll_batch_limit` (20 default); verify passes (OAuth2 + TLS is the boundary). `gmail_api.py` is the Gmail/Google Workspace pull-mode variant: OAuth2 refresh-token grant — token cached with 401 refresh, poll = historyId incremental sync (config `history_id` seeds it; 404 gap falls back to the label list) → per-message `format=full` fetch → `modifyLabels` strips UNREAD; 429 backs off to the next cycle; `threadId`/In-Reply-To surface on the envelope, `download_attachment(message_id, attachment_id)` returns base64url-decoded bytes; send via `messages/send` or `drafts/create` (config `draft_mode`); config `label_ids` (INBOX,UNREAD default), `query`, `poll_batch_limit` (25 default); verify passes (OAuth2 + TLS is the boundary).

## Hard rules (from PRD, enforced by tests)

- Verification mandatory; reject = 403 **and** `RejectedEnvelope` archived (attack surface auditable).
- Archive-first inbound; duplicates counted, never dropped; interaction events deduped by message id (idempotent button clicks).
- Degradation is rendering, not business: consumers always send cards; the library picks the channel shape.
- Credentials never persist in the library; reads return masked values.
- Compliance red line: no personal-WeChat reverse protocols — official APIs only.

## Conventions

- setuptools backend, `[tool.setuptools.packages.find] where = ["src"]`, pytest `asyncio_mode=auto`, ruff (E/F/W/I/UP/B/SIM, line-length 120, ignores E501/B008/SIM117).
- 4-space indentation, no docstrings (comments only), logging on every module.
- Behavior-equivalent ports from ai-family live in `twilio_sms.py` (webhook verify/parse), `outbox.py` (backoff schedule), `credentials.py` (AESGCM provider). ai-family switch-over is its own repo's work — do not modify ai-family here.
- Repo is independent git at github.com/dahai80/fusion-channel (private). README.md English only; README_CN.md Chinese.

## Ports

None — library only. If a bridge service (M4) is ever built, register its port in `/Users/dahai/fusion/architecture/port-registry.yaml` and run `python scripts/check-ports.py` from the repo root.
