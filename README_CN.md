# fusion-channel（融合通道接入层）

[![CI](https://github.com/dahai80/fusion-channel/actions/workflows/ci.yml/badge.svg)](https://github.com/dahai80/fusion-channel/actions/workflows/ci.yml)

fusion 生态的统一 IM/短信通道接入库。任何 fusion 应用借助它即可获得**收消息、发消息、进群、被 @、弹审批按钮**的能力——验签、去重、outbox 重试、频控、归档、身份映射全部内置。

> 状态：v0.1.5 —— M1–M3 范围已落地（核心抽象 + 第一批 12 个适配器，含拉取式邮件）。M4 桥服务按 PRD 决策**暂不建设**（库优先）；M5 生态推广由各消费应用在自己的仓库内完成。

## 为什么需要它

fusion 有 48 个子项目，任何面向人的应用最终都需要 IM 通道。每家服务商细节完全不同——Twilio HMAC-SHA1 表单验签、飞书 AES-CBC 加密事件 + challenge 握手、钉钉 HMAC-SHA256 加签、Slack 请求签名、WhatsApp `X-Hub-Signature-256`、企业微信 AES + SHA1 回调。任何一处写错，代价都是"任意人可以伪造主人指令"。fusion-channel 把这些收敛到一处。

设计来源：`architecture/fusion-channel-prd-1025.md`，以及 ai-family 已在生产验证的短信链路（验签/去重/拒收留档/outbox 退避重试），行为等价上提。

## 安装

```bash
cd /Users/dahai/fusion && source .venv/bin/activate
pip install -e ".[test]"        # webhook 路由再加 ,fastapi
```

## 支持的通道（第一批）

| 适配器 | 通道 | 验签机制 | 卡片 | 群组 |
|--------|------|---------|------|------|
| `twilio_sms` | 短信（Twilio） | HMAC-SHA1 `X-Twilio-Signature` | 文本兜底 | – |
| `aliyun_sms` | 短信（阿里云） | 回调 HMAC（可选） | – | – |
| `tencent_sms` | 短信（腾讯云） | 回调 HMAC（可选） | – | – |
| `wecom_app` | 企业微信自建应用 | SHA1 签名 + AES-256-CBC | 部分（textcard） | ✅ |
| `feishu_app` | 飞书/Lark 应用 | Encrypt Key AES + Verification Token | 完整 | ✅ |
| `dingtalk_robot` | 钉钉群机器人 | HMAC-SHA256 加签（强制） | 完整（ActionCard） | ✅ |
| `slack_app` | Slack Events API | `X-Slack-Signature` HMAC-SHA256 | 完整（Block Kit） | ✅ |
| `whatsapp_cloud` | WhatsApp Cloud API | `X-Hub-Signature-256` | 文本兜底 | – |
| `telegram_bot` | Telegram Bot API | secret-token 头 | 完整（inline keyboard） | ✅ |
| `email_imap` | 邮件（IMAP/SMTP） | 传输层认证（TLS + 登录） | 文本兜底 | – |
| `graph_mail` | Microsoft 365 / Exchange Online（Graph API，OAuth2 客户端凭据） | 传输层认证（OAuth2 + TLS） | 文本兜底 | – |
| `gmail_api` | Gmail / Google Workspace（Gmail API，OAuth2 刷新令牌） | 传输层认证（OAuth2 + TLS） | 文本兜底 | – |

**合规红线**：个人微信逆向/外挂协议一概不做，只走官方 API。

## 快速上手

```python
from fusion_channel import ChannelRegistry, ApprovalCard, CardAction
from fusion_channel.credentials import AESGCMCredentialProvider
from fusion_channel.storage import SqliteStore

store = SqliteStore("channel.db")
await store.init()
registry = ChannelRegistry.from_config(
    "channels.yaml",  # PRD §4.3 数据化配置
    credentials=AESGCMCredentialProvider("data/secrets"),
    store=store,
)

# --- 入站：FastAPI webhook 一行接入 ---
from fusion_channel.fastapi import build_channel_router

app.include_router(build_channel_router(registry))  # POST /webhook/{channel_id}


# --- 卡片按钮与文本指令汇合到同一个 handler ---
async def on_interaction(interaction):
    code = interaction.payload.get("code") or interaction.payload.get("value") or ""
    print("审批", code, interaction.action)  # 一个状态机，全通道共用


registry.on_interaction(on_interaction)
registry.register_command("approve", ["同意", "approve"], code_required=True)
registry.register_command("reject", ["拒绝", "reject"], code_required=True)
registry.on_message(lambda env: print(env.channel_id, env.text))

# --- 出站：outbox 语义，卡片降级自动 ---
await registry.send(
    "family_twilio",
    {"type": "direct", "id": "+15550000001->+15550000000"},
    card=ApprovalCard(
        title="审批 AP-123456",
        code="AP-123456",
        body="新增雇员张阿姨",
        actions=[CardAction(action="approve", label="同意"), CardAction(action="reject", label="拒绝")],
    ),
)
sent = await registry.outbox.flush_once()  # 或后台 run_loop()

# --- 身份：外部 id -> fusion-identity 账号 ---
user = registry.identify("family_twilio", "+15550000001")
```

完整可运行示例：`examples/approve_bot.py` + `examples/channels.yaml`。消费应用接线（outbox worker 生命周期、送达状态回执、Postgres store、凭据隔离）：`docs/consumer-integration.md`。

## 架构

```
ChannelRegistry ── ChannelAdapter(SPI) ── 12 个服务商适配器
      │                  │
IdentityMapper    SignatureVerifier（每通道策略）
      │                  │
CredentialProvider  DedupGuard / ReplayGuard
      │                  │
ArchiveHook        OutboxSender（退避重试 / 令牌桶 / 降级）
      │
AuditHook          CardRenderer（按 CAN_CARDS 降级）
```

写死在库里的不可妥协规则（PRD §4.2 / §7.1）：

1. **验签是 SPI 级强制项**——不实现 `verify_request` 的适配器类无法注册（`ConfigError`）。
2. **先落库再处理**——入站消息带原始载荷先持久化，重复投递计数，绝不丢弃。
3. **拒收请求同样留档**（`RejectedEnvelope`），攻击面与丢消息可审计。
4. **出站走 outbox**——发送意图先入队；flush 循环按 `[30, 120, 600]` 秒退避重试，最多 3 次，之后触发 `on_permanent_failure`（ai-family 告警模式）。记录发送前先 CAS 认领（`pending` → `sending` 租约），双 worker 绝不重复发送；崩溃 worker 的租约过期后自动回队重发；pending 超过 24 小时的记录按永久失败处理。
5. **降级是渲染问题，不是业务问题**——消费应用永远说"发卡片"；`CAN_CARDS=none` 的通道自动渲染兜底文本（"回复 同意 AP-123456"），回复再解析回同一个 `Interaction`。
6. **凭据不进库**——`CredentialProvider` 接口 + AES-256-GCM 文件默认实现（0600 密钥、原子写入）；describe 只回掩码。
7. **失败即关闭**——攻击者输入导致验签/解析抛异常时拒收留档并回 4xx（绝不 500）；webhook 请求体超 1 MiB 返回 413；适配器错误字符串先脱敏再进日志或 `SendResult`。
8. **身份门禁**——通道配置 `require_bound_identity: true` 后，交互（卡片按钮 + 文本指令）只对 `identity_bindings` 中已绑定的发送者派发；未绑定返回 403 `identity_not_bound` 并记审计事件。

配置细节：

- **凭据引用**：每个凭据值可写 `env:变量名`（从环境变量读取）、`CredentialProvider` 的 key，或直接写明文（provider 无匹配时按字面值使用）。
- **归档模式**：`inbound.archive: full`（默认，保留原始载荷）/ `metadata-only`（归档时剥离内容与原始载荷；handler 仍拿到完整 envelope）/ `off`（只留去重记录）。
- **重放窗口**：Slack / 钉钉 / 企微签名超过 300 秒即拒绝。

## 群组与机器人

- 会话类型：`direct` / `group` / `broadcast`。
- 群白名单（配置 `groups:`）——未声明的群归档后忽略。
- `mentions_required: true` 时群内消息必须 @机器人 才触发。
- CAN_GROUP 适配器提供 `group_ops()`：`list()` / `info()` / `broadcast()`；不支持的通道抛 `CapabilityNotSupported`，消费应用按能力降级为逐个私聊发送。

## 拉取式通道（邮件）

Webhook 通道是服务端推；`email_imap` 是拉。重写 `ChannelAdapter.poll()` 的适配器自己取信（IMAP `UNSEEN`，批量上限可配），返回原始请求——registry 把每个请求喂进与 webhook 完全相同的 `handle_inbound` 流水线（去重、归档、指令、身份门禁）。邮件没有 webhook 验签：TLS + 账号登录即信任边界，`verify_request` 恒通过（有日志）。MIME 处理保持薄：`text/plain` 提取 + 附件转 `MediaRef`，`In-Reply-To` 映射为 `conversation.thread_id`。出站同样走 outbox（SMTP + STARTTLS）；主题用 `meta={"subject": ...}` 指定，缺省取正文第一行。

```python
await registry.poll()  # 单次：拉取 + 派发
registry.start_polling(interval=60)  # 后台 worker；close() 会取消
```

`graph_mail` 是 Microsoft 365 / Exchange Online 变体：M365 已禁用基本认证，IMAP 密码登录连不上。它同样走拉取，但用 Microsoft Graph + OAuth2 客户端凭据（`tenant_id` / `client_id` / `client_secret` / `mailbox`；在 Entra ID 注册应用并对共享邮箱授予 `Mail.ReadWrite` 应用权限）。配置项：`folder`（默认 `INBOX`）、`poll_batch_limit`（默认 20）。未读邮件拉取完整 MIME 后标记已读，二次拉取去重语义与 `email_imap` 一致；Graph 附件引用进入 `MediaRef`，消费应用可用 `adapter.download_attachment(graph_message_id, attachment_id)` 拉取真实字节——邮件 PDF 单据进入文档流水线的通路。出站走 `POST /users/{mailbox}/sendMail`，主题规则同 `email_imap`。

`gmail_api` 是 Gmail / Google Workspace 变体，同样拉取式 + OAuth2（`client_id` / `client_secret` / `refresh_token` / `user_id`；Google Cloud OAuth 客户端授予 `gmail.readonly` + `gmail.send` scope——服务账号 + 全域委托同样走刷新令牌契约）。配置项：`label_ids`（默认 `INBOX, UNREAD`）、`query`（可选 Gmail 搜索过滤）、`poll_batch_limit`（默认 25）、`history_id`（增量同步种子）、`draft_mode`（发送改为建草稿——人工审批通路）。轮询优先用 `historyId` 增量同步（上次轮询记录过则启用），历史窗口过期自动回退全量列表；邮件按 `format=full` 拉取（附件 id 在 payload parts 里），随后 `modifyLabels` 剥掉 `UNREAD`，二次拉取去重语义与 `email_imap` 一致；429 配额响应退避到下个轮询周期，绝不炸轮询循环。`threadId` 与 `In-Reply-To` 都上 envelope（`conversation.thread_id` + `raw.gmail_thread_id`）；附件字节走 `adapter.download_attachment(gmail_message_id, attachment_id)`。出站走 `POST .../messages/send`（draft 模式走 `drafts/create`），主题规则同 `email_imap`。

## 能力矩阵

每个适配器声明 `ChannelCapabilities`（CAN_RECEIVE / CAN_SEND / CAN_GROUP / CAN_BOT / CAN_MENTION / CAN_CARDS / CAN_MEDIA / CAN_RECEIPT / CAN_THREADS），见 `src/fusion_channel/capabilities.py`。消费应用按能力分支，绝不按适配器身份硬编码。配置 `capabilities_override` 可按通道实例覆盖。

## 与其他 fusion 组件的关系

- **fusion-core**：同为 in-tree 库、`pip install -e` 分发；不反向依赖。
- **fusion-identity**：`IdentityMapper` 负责外部身份到 identity 账号的映射；RBAC 归 fusion-identity（`assert_bound_identity()` 提供敏感指令双因子原语）。
- **fusion-gateway**：明确不走其 connector 框架（无 IM 概念、无验签、无后台使用模型）。
- **ai-family**：第一个消费者 + 代码捐献方（验签/去重/outbox/secret-store 语义上提）。其切换工作在 ai-family 自己的仓库内进行（PRD §11）。

## 开发

```bash
source /Users/dahai/fusion/.venv/bin/activate
pytest tests/                          # 196 项测试：密码学向量、webhook E2E、outbox、矩阵、加固、邮件
pytest tests/test_e2e_approval.py -v   # 跨通道审批验收
ruff check . && ruff format --check .  # lint
```

测试覆盖要点：

- 每通道真实签名向量（含飞书/企微 AES-CBC 往返、Slack 重放窗口拒绝、钉钉毫秒时间戳、阿里云 RFC3986 percent-encode）。
- FastAPI TestClient E2E：签名 webhook → 验签 → 解析 → 去重 → 归档 → handler；坏签名 → 403 + 拒收留档；重复投递 → 200 且不重复派发；内部异常 → 拒收留档 + 400，绝不 500。
- GET 握手：企微 `echostr` URL 验证、WhatsApp `hub.challenge` 回显（校验 verify_token），走路由的 GET 端点。
- 送达回执（Twilio `MessageStatus`、WhatsApp `statuses`）按 Receipt 归档，绝不当作消息派发；challenge 重试永远重新回显（不去重）。
- 跨通道审批验收：同一个 `AP-123456` 决定分别经 SMS 文本、Slack Block Kit 按钮、钉钉加签指令、Telegram inline keyboard、WhatsApp 文本、企微加密回调、飞书群 @、飞书卡片按钮回调到达——一个 handler、一个状态机。
- 出站 E2E（`httpx.MockTransport`）：断言 Twilio/飞书/Slack 真实请求体形态；每适配器 `test_connection()` 冒烟（钉钉为静态检查——机器人发送必进真实群）。
- 邮件（`email_imap`）：MIME 解析（正文提取、附件、In-Reply-To 线程）、SMTP 发送（Subject/In-Reply-To 头）、假传输 E2E 走 `registry.poll()`（含二次拉取去重）、拉取异常隔离、卡片兜底 outbox 发送、轮询 worker 生命周期。
- Microsoft 365（`graph_mail`）：`httpx.MockTransport` 模拟 Graph HTTP——OAuth2 客户端凭据 token 获取 + 缓存 + 401 刷新、未读列表 → MIME 拉取 → 标记已读验证、Graph 附件引用进 `MediaRef`、`download_attachment()` 真实字节、registry poll 派发 + 二次拉取去重、标记已读失败时的去重兜底、`sendMail` 请求体形态、凭据缺失与服务商报错路径。
- Gmail（`gmail_api`）：`httpx.MockTransport` 模拟 Gmail HTTP——OAuth2 刷新令牌 token 获取 + 缓存 + 401 刷新、标签列表 → `format=full` 拉取 → `modifyLabels` 剥 UNREAD 验证、historyId 增量同步 + 404 缺口回退全量列表、列表与单封拉取的 429 退避、`threadId`/`In-Reply-To` 线程、`download_attachment()` 真实字节、registry poll 派发 + 二次拉取去重、剥标签失败时的去重兜底、`messages.send` raw MIME 形态、`drafts.create` 草稿模式、凭据缺失与服务商报错路径。
- 真实服务商调用需要沙箱凭据，标记为 `integration`（默认排除）：飞书 tenant-token 握手；QQ 邮箱真实 IMAP/SMTP E2E（登录 + 拉取 + 二次拉取去重、SMTP 发给自己并回环收回，凭据走环境变量 `FUSION_CHANNEL_QQ_EMAIL` / `FUSION_CHANNEL_QQ_AUTH_CODE`）。

## 许可证

Apache-2.0
