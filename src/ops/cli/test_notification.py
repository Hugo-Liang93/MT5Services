"""Smoke-test the Telegram transport without starting the full app.

Uses the real ``TelegramTransport`` against the real Bot API but skips
dispatcher / outbox / scheduler / DI. The point is to answer **just** the
question: "can this machine reach Telegram with my configured token + chat?"

Usage::

    python -m src.ops.cli.test_notification
    python -m src.ops.cli.test_notification --message "hello from MT5Services"
    python -m src.ops.cli.test_notification --chat-id 987654321
    python -m src.ops.cli.test_notification --event-preview execution_failed

Exit codes:
- 0: Telegram returned ok=True. Check your chat.
- 1: Transport returned an error (see printed details).
- 2: Local config problem (no token / no chat_id / etc).
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from src.config import get_notification_config
from src.notifications.transport.telegram import TelegramTransport

logger = logging.getLogger("test_notification")

_DEFAULT_MESSAGE = (
    "🤖 *Telegram 推送自检成功*\n\n"
    "配置加载、Bot API 可达、Markdown 渲染正常。\n\n"
    "来自 MT5Services 通知模块。"
)


_EVENT_SAMPLES: dict[str, str] = {
    "execution_failed": (
        "🚨 *执行失败*\n\n"
        "• 策略: `trend_h1` (long)\n"
        "• 品种: `XAUUSD`\n"
        "• 原因: broker_reject\n"
        "• 实例: live-main\n"
        "• Trace: `abc-123`"
    ),
    "circuit_open": (
        "🔒 *熔断器打开，自动交易停止*\n\n"
        "• 实例: live-main\n"
        "• 连续失败: 3 次\n"
        "• 最近原因: timeout\n"
        "• 自动恢复: 30 分钟后"
    ),
    "startup_ready": (
        "✅ *系统就绪*\n\n"
        "• 实例: live-main\n"
        "• 环境: live\n"
        "• 模式: full\n"
        "• 版本: unified"
    ),
}


def _mask(secret: str) -> str:
    if len(secret) <= 14:
        return "***"
    return f"{secret[:10]}...{secret[-4:]}"


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.ops.cli.test_notification",
        description="发送一条测试消息到 Telegram，验证 bot_token + chat_id + 网络通路。",
    )
    parser.add_argument(
        "--message",
        help="自定义测试消息文本（Markdown 语法）。与 --event-preview 互斥。",
    )
    parser.add_argument(
        "--chat-id",
        help="覆盖默认 chat_id（默认读 notifications.local.ini [chats] default_chat_id）",
    )
    parser.add_argument(
        "--event-preview",
        choices=sorted(_EVENT_SAMPLES.keys()),
        help="发送一条预置事件样本消息（模拟 execution_failed / circuit_open / startup_ready）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="打印 DEBUG 日志",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    try:
        config = get_notification_config()
    except Exception as exc:  # noqa: BLE001 — surface the reason to operator
        print(f"✗ 配置加载失败：{exc}", file=sys.stderr)
        return 2

    bot_token_value = config.bot_token.get_secret_value().strip()
    if not bot_token_value:
        print(
            "✗ bot_token 未配置\n"
            "  请编辑 config/notifications.local.ini\n"
            "  在 [runtime] 或 [secrets] 段加入:\n"
            "    bot_token = 1234567890:AAE_xxx",
            file=sys.stderr,
        )
        return 2

    chat_id = (args.chat_id or config.chats.default_chat_id or "").strip()
    if not chat_id:
        print(
            "✗ chat_id 未配置\n"
            "  传 --chat-id 123456789 或在 notifications.local.ini [chats] 配置 default_chat_id",
            file=sys.stderr,
        )
        return 2

    if args.event_preview:
        message = _EVENT_SAMPLES[args.event_preview]
        message = f"[TEST] {message}"
    elif args.message:
        message = args.message
    else:
        message = _DEFAULT_MESSAGE

    # Summary before send (operator-friendly, secrets masked).
    print("── 配置摘要 ──────────────────────────")
    print(f"  bot_token  : {_mask(bot_token_value)}")
    print(f"  chat_id    : {chat_id}")
    print(f"  proxy      : {config.runtime.http_proxy_url or '(直连)'}")
    print(f"  timeout    : {config.runtime.http_timeout_seconds}s")
    print(f"  msg bytes  : {len(message)}")
    print("────────────────────────────────────────")
    print()
    print("→ 正在调用 Telegram Bot API sendMessage...")

    transport = TelegramTransport(
        bot_token=config.bot_token,
        timeout_seconds=config.runtime.http_timeout_seconds,
        proxy_url=config.runtime.http_proxy_url,
    )

    result = transport.send(chat_id=chat_id, text=message)

    print()
    if result.ok:
        print("✅ 发送成功！请打开 Telegram 查看消息。")
        print()
        print("下一步：")
        print("  - 若消息收到但格式异常 → 检查 Markdown 转义字符")
        print("  - 若要推送真实事件 → 启动应用 (python -m src.entrypoint.web)")
        print("    并在 config/notifications.ini [runtime] enabled=true")
        return 0

    print("❌ 发送失败")
    print(f"  retryable    : {result.retryable}")
    print(f"  error        : {result.error}")
    if result.retry_after_seconds is not None:
        print(f"  retry_after  : {result.retry_after_seconds}s")
    print()
    print("常见排查：")
    print("  - 401/Unauthorized → bot_token 错误或已吊销，重新找 @BotFather")
    print("  - 400/chat_id      → chat_id 错误，或没先给 bot 发过消息（bot 无法主动对陌生用户说话）")
    print("  - timeout/conn err → 网络不通，国内机器考虑 http_proxy_url")
    print("  - 429              → 被 Telegram 限流，等 retry_after 秒后重试")
    return 1


if __name__ == "__main__":
    sys.exit(main())
