import asyncio
import logging

from config import Settings
from marketplace import MarketplaceClient
from storage import KeyStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def process_orders(storage: KeyStorage, client: MarketplaceClient) -> int:
    """Выполняет один цикл проверки и автовыдачи ключей по оплаченным заказам."""
    orders = await client.fetch_paid_orders()
    processed_count = 0

    for order in orders:
        logger.info("Обработка заказа: %s для покупателя %s", order.order_id, order.customer_id)
        key = storage.pop_unused_key(order_id=order.order_id)
        if not key:
            logger.error("Закончились ключи на складе для заказа %s!", order.order_id)
            continue

        sent = await client.send_chat_message(
            chat_id=order.chat_id,
            message=f"Спасибо за покупку! Ваш ключ активации: {key}",
        )
        if sent:
            await client.fulfill_order(order_id=order.order_id, delivered_key=key)
            processed_count += 1
            logger.info("Ключ успешно выдан для заказа %s", order.order_id)

    return processed_count


async def main() -> None:
    settings = Settings.from_env()
    storage = KeyStorage(settings.database_path)

    async with MarketplaceClient(base_url=settings.api_url, api_key=settings.api_key) as client:
        logger.info("Бот автовыдачи запущен. Опрос маркетплейса...")
        await process_orders(storage, client)


if __name__ == "__main__":
    asyncio.run(main())
