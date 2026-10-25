# fusion-channel

Unified messaging channel access layer for the Fusion ecosystem ("一核九端"). One library gives any Fusion application the ability to **receive, send, join groups, be @-mentioned, and push approval buttons** across SMS and IM providers — with verification, dedup, outbox retry, rate limiting, archiving, and identity mapping built in.

> Status: v0.1.0 — M1–M3 scope landed (core abstractions + 9 first-batch adapters). The M4 bridge service is intentionally **not** built yet (library-first per the PRD); M5 ecosystem adoption happens inside consumer projects.

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

Full runnable example: `examples/approve_bot.py` with `examples/channels.yaml`.

## Architecture

```
ChannelRegistry ── ChannelAdapter(SPI) ── 9 provider adapters
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
4. **Outbox pattern** — send intents enqueue first; the flush loop retries with `[30, 120, 600]`s backoff, 3 attempts max, then fires `on_permanent_failure` (the ai-family alert pattern).
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
pytest tests/                          # 116 tests: crypto vectors, webhook E2E, outbox, matrix, hardening
pytest tests/test_e2e_approval.py -v   # cross-channel approval acceptance
ruff check . && ruff format --check .  # lint
```

Test coverage highlights:

- Real signature vectors for every channel (including Feishu/WeCom AES-CBC round-trips, Slack replay-window rejection, DingTalk ms-timestamp handling).
- FastAPI TestClient E2E: signed webhook → verify → parse → dedup → archive → handler; bad signature → 403 + reject archive; duplicate → 200, no redispatch.
- Cross-channel approval acceptance: the same `AP-123456` decision arrives via SMS text, Slack Block Kit button, DingTalk signed command, Telegram inline keyboard, WhatsApp text, WeCom encrypted callback, and Feishu group mention — one handler, one state machine.
- Outbound E2E via `httpx.MockTransport`: Twilio/Feishu/Slack provider payload shapes asserted; `test_connection()` smoke-tested per adapter.
- Live provider calls require sandbox credentials and are marked `integration` (excluded by default).

## License

Apache-2.0
