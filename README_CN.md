# fusion-channel（融合通道接入层）

fusion 生态的统一 IM/短信通道接入库。任何 fusion 应用借助它即可获得**收消息、发消息、进群、被 @、弹审批按钮**的能力——验签、去重、outbox 重试、频控、归档、身份映射全部内置。

> 状态：v0.1.0 —— M1–M3 范围已落地（核心抽象 + 第一批 9 个适配器）。M4 桥服务按 PRD 决策**暂不建设**（库优先）；M5 生态推广由各消费应用在自己的仓库内完成。

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

**合规红线**：个人微信逆向/外挂协议一概不做，只走官方 API。

## 快速上手

```python
from fusion_channel import ChannelRegistry, ApprovalCard, CardAction
from fusion_channel.credentials import AESGCMCredentialProvider
from fusion_channel.storage import SqliteStore

store = SqliteStore("channel.db")
await store.init()
registry = ChannelRegistry.from_config(
    "channels.yaml",                 # PRD §4.3 数据化配置
    credentials=AESGCMCredentialProvider("data/secrets"),
    store=store,
)

# --- 入站：FastAPI webhook 一行接入 ---
from fusion_channel.fastapi import build_channel_router
app.include_router(build_channel_router(registry))   # POST /webhook/{channel_id}

# --- 卡片按钮与文本指令汇合到同一个 handler ---
async def on_interaction(interaction):
    code = interaction.payload.get("code") or interaction.payload.get("value") or ""
    print("审批", code, interaction.action)          # 一个状态机，全通道共用

registry.on_interaction(on_interaction)
registry.register_command("approve", ["同意", "approve"], code_required=True)
registry.register_command("reject", ["拒绝", "reject"], code_required=True)
registry.on_message(lambda env: print(env.channel_id, env.text))

# --- 出站：outbox 语义，卡片降级自动 ---
await registry.send(
    "family_twilio",
    {"type": "direct", "id": "+15550000001->+15550000000"},
    card=ApprovalCard(
        title="审批 AP-123456", code="AP-123456", body="新增雇员张阿姨",
        actions=[CardAction(action="approve", label="同意"),
                 CardAction(action="reject", label="拒绝")],
    ),
)
sent = await registry.outbox.flush_once()   # 或后台 run_loop()

# --- 身份：外部 id -> fusion-identity 账号 ---
user = registry.identify("family_twilio", "+15550000001")
```

完整可运行示例：`examples/approve_bot.py` + `examples/channels.yaml`。

## 架构

```
ChannelRegistry ── ChannelAdapter(SPI) ── 9 个服务商适配器
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
4. **出站走 outbox**——发送意图先入队；flush 循环按 `[30, 120, 600]` 秒退避重试，最多 3 次，之后触发 `on_permanent_failure`（ai-family 告警模式）。
5. **降级是渲染问题，不是业务问题**——消费应用永远说"发卡片"；`CAN_CARDS=none` 的通道自动渲染兜底文本（"回复 同意 AP-123456"），回复再解析回同一个 `Interaction`。
6. **凭据不进库**——`CredentialProvider` 接口 + AES-256-GCM 文件默认实现（0600 密钥、原子写入）；describe 只回掩码。

## 群组与机器人

- 会话类型：`direct` / `group` / `broadcast`。
- 群白名单（配置 `groups:`）——未声明的群归档后忽略。
- `mentions_required: true` 时群内消息必须 @机器人 才触发。
- CAN_GROUP 适配器提供 `group_ops()`：`list()` / `info()` / `broadcast()`；不支持的通道抛 `CapabilityNotSupported`，消费应用按能力降级为逐个私聊发送。

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
pytest tests/                          # 48 项测试：密码学向量、webhook E2E、outbox、矩阵
pytest tests/test_e2e_approval.py -v   # 跨通道审批验收
ruff check . && ruff format --check .  # lint
```

测试覆盖要点：

- 每通道真实签名向量（含飞书/企微 AES-CBC 往返、Slack 重放窗口拒绝、钉钉毫秒时间戳处理）。
- FastAPI TestClient E2E：签名 webhook → 验签 → 解析 → 去重 → 归档 → handler；坏签名 → 403 + 拒收留档；重复投递 → 200 且不重复派发。
- 跨通道审批验收：同一个 `AP-123456` 决定分别经 SMS 文本、Slack Block Kit 按钮、钉钉加签指令、Telegram inline keyboard、WhatsApp 文本、企微加密回调、飞书群 @ 到达——一个 handler、一个状态机。
- 出站 E2E（`httpx.MockTransport`）：断言 Twilio/飞书/Slack 真实请求体形态；每适配器 `test_connection()` 冒烟。
- 真实服务商调用需要沙箱凭据，标记为 `integration`（默认排除）。

## 许可证

Apache-2.0
