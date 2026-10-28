from dataclasses import dataclass
from types import TracebackType
from typing import Self

import httpx


class MarketplaceError(Exception):
    """Базовое исключение для ошибок маркетплейса."""


class APIConnectionError(MarketplaceError):
    """Ошибка сетевого взаимодействия с маркетплейсом."""


class OrderFulfillmentError(MarketplaceError):
    """Ошибка при обновлении статуса заказа."""


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    customer_id: str
    chat_id: str
    status: str
    product_id: str
    amount: float


class MarketplaceClient:
    """Асинхронный клиент для взаимодействия с API маркетплейса."""

    def __init__(
        self,
        base_url: str = "https://api.marketplace.local/v1",
        api_key: str = "",
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "AutoDeliveryBot/1.0",
        }
        self._client: httpx.AsyncClient | None = None
        self._transport = transport
        self._timeout = timeout

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=self._timeout,
            transport=self._transport,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "Клиент должен использоваться внутри асинхронного контекстного менеджера (async with)."
            )
        return self._client

    async def fetch_paid_orders(self) -> list[Order]:
        """Получает список оплаченных заказов, ожидающих выдачи товара."""
        try:
            response = await self.client.get("/orders", params={"status": "paid"})
        except httpx.RequestError as exc:
            raise APIConnectionError(f"Сетевой сбой при получении заказов: {exc}") from exc

        match response.status_code:
            case 200:
                data = response.json()
                raw_orders: list[dict[str, object]] = data.get("orders", [])
                return [
                    Order(
                        order_id=str(item["order_id"]),
                        customer_id=str(item["customer_id"]),
                        chat_id=str(item["chat_id"]),
                        status=str(item.get("status", "paid")),
                        product_id=str(item["product_id"]),
                        amount=float(item.get("amount", 0.0)),  # type: ignore[arg-type]
                    )
                    for item in raw_orders
                ]
            case 401 | 403:
                raise MarketplaceError("Ошибка авторизации: неверный или истекший API токен.")
            case 500 | 502 | 503:
                raise APIConnectionError(
                    f"Сервер маркетплейса временно недоступен ({response.status_code})."
                )
            case _:
                raise MarketplaceError(
                    f"Непредвиденный ответ API при запросе заказов: {response.status_code}"
                )

    async def send_chat_message(self, chat_id: str, message: str) -> bool:
        """Отправляет покупателю сообщение с выданным ключом."""
        payload = {"chat_id": chat_id, "text": message}
        try:
            response = await self.client.post(f"/chats/{chat_id}/messages", json=payload)
        except httpx.RequestError as exc:
            raise APIConnectionError(
                f"Не удалось отправить сообщение в чат {chat_id}: {exc}"
            ) from exc

        match response.status_code:
            case 200 | 201:
                return True
            case 404:
                return False
            case _:
                raise MarketplaceError(f"Ошибка отправки сообщения: {response.status_code}")

    async def fulfill_order(self, order_id: str, delivered_key: str) -> bool:
        """Помечает заказ как выполненный и сохраняет выданный ключ."""
        payload = {"order_id": order_id, "delivered_key": delivered_key, "status": "completed"}
        try:
            response = await self.client.post(f"/orders/{order_id}/fulfill", json=payload)
        except httpx.RequestError as exc:
            raise APIConnectionError(
                f"Сбой подтверждения выполнения заказа {order_id}: {exc}"
            ) from exc

        match response.status_code:
            case 200 | 204:
                return True
            case _:
                raise OrderFulfillmentError(
                    f"Не удалось завершить заказ {order_id}: {response.status_code}"
                )
