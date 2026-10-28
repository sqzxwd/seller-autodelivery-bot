import logging

import httpx

from config import Settings

logger = logging.getLogger(__name__)


class NotifierError(Exception):
    """Базовое исключение для ошибок модуля уведомлений."""


def mask_key(raw_key: str) -> str:
    """Маскирует центральную часть ключа (например, STEAM-****-1111)."""
    cleaned = raw_key.strip()
    if not cleaned:
        return "—"
    if "-" in cleaned:
        parts = cleaned.split("-")
        if len(parts) >= 3:
            return f"{parts[0]}-****-{parts[-1]}"
    if len(cleaned) <= 6:
        return "****"
    visible = 3 if len(cleaned) <= 10 else 4
    return f"{cleaned[:visible]}****{cleaned[-visible:]}"


class TelegramNotifier:
    """Асинхронный клиент отправки push-уведомлений продавцу в Telegram."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        base_url: str = "https://api.telegram.org",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        settings = Settings.from_env()
        self.bot_token = (
            bot_token if bot_token is not None else settings.telegram_bot_token
        ).strip()
        self.chat_id = (chat_id if chat_id is not None else settings.telegram_chat_id).strip()
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout

    @property
    def is_enabled(self) -> bool:
        """Возвращает True, если заданы токен бота и ID чата получателя."""
        return bool(self.bot_token and self.chat_id)

    async def send_message(self, text: str) -> bool:
        """
        Отправляет текстовое сообщение в Telegram.
        Если токен не задан, безопасно возвращает False без генерации исключений.
        """
        if not self.is_enabled:
            logger.info("Telegram push пропущен: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")
            return False

        url = f"{self.base_url}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        }

        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.post(url, json=payload)
                if response.status_code == 200:
                    return True
                logger.warning(
                    "Telegram API вернул ошибку %d: %s", response.status_code, response.text
                )
                return False
        except httpx.HTTPError as exc:
            logger.error("Сетевая ошибка при отправке push в Telegram: %s", exc)
            return False

    async def notify_order_delivered(
        self,
        order_id: str,
        category: str,
        raw_key: str,
        delivered_at: str,
    ) -> bool:
        """Уведомление об успешной выдаче цифрового товара."""
        masked_key = mask_key(raw_key)
        text = (
            "✅ <b>Заказ успешно выдан</b>\n\n"
            f"📦 <b>ID заказа:</b> <code>{order_id}</code>\n"
            f"🏷 <b>Категория:</b> {category}\n"
            f"🔑 <b>Ключ:</b> <code>{masked_key}</code>\n"
            f"🕒 <b>Время:</b> {delivered_at}"
        )
        return await self.send_message(text)

    async def notify_low_stock(self, category: str, remaining_count: int) -> bool:
        """Алерт о критическом снижении остатков товара (меньше 3 шт.)."""
        text = (
            "⚠️ <b>Внимание: Заканчивается товар!</b>\n\n"
            f"🏷 <b>Категория:</b> {category}\n"
            f"📉 <b>Осталось свободных ключей:</b> <b>{remaining_count} шт.</b>\n"
            "Пожалуйста, пополните склад."
        )
        return await self.send_message(text)
