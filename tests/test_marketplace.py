import json
from pathlib import Path

import httpx
import pytest

from bot import process_orders
from marketplace import (
    MarketplaceClient,
    MarketplaceError,
)
from storage import KeyStorage


def mock_marketplace_transport(request: httpx.Request) -> httpx.Response:
    url_path = request.url.path

    if url_path == "/orders" and request.method == "GET":
        status_param = request.url.params.get("status")
        if status_param == "paid":
            payload = {
                "orders": [
                    {
                        "order_id": "ord-101",
                        "customer_id": "cust-501",
                        "chat_id": "chat-901",
                        "status": "paid",
                        "product_id": "prod-vpn",
                        "amount": 19.99,
                    },
                    {
                        "order_id": "ord-102",
                        "customer_id": "cust-502",
                        "chat_id": "chat-902",
                        "status": "paid",
                        "product_id": "prod-vpn",
                        "amount": 19.99,
                    },
                ]
            }
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"orders": []})

    if (
        url_path.startswith("/chats/")
        and url_path.endswith("/messages")
        and request.method == "POST"
    ):
        data = json.loads(request.content)
        if "text" in data:
            return httpx.Response(200, json={"sent": True, "message_id": "msg-1"})
        return httpx.Response(400, json={"error": "Invalid body"})

    if (
        url_path.startswith("/orders/")
        and url_path.endswith("/fulfill")
        and request.method == "POST"
    ):
        return httpx.Response(200, json={"success": True})

    return httpx.Response(404, json={"error": "Not found"})


@pytest.mark.asyncio
async def test_fetch_paid_orders() -> None:
    transport = httpx.MockTransport(mock_marketplace_transport)
    async with MarketplaceClient(base_url="https://test.api", transport=transport) as client:
        orders = await client.fetch_paid_orders()
        assert len(orders) == 2
        assert orders[0].order_id == "ord-101"
        assert orders[0].chat_id == "chat-901"
        assert orders[0].amount == 19.99


@pytest.mark.asyncio
async def test_send_chat_message() -> None:
    transport = httpx.MockTransport(mock_marketplace_transport)
    async with MarketplaceClient(base_url="https://test.api", transport=transport) as client:
        sent = await client.send_chat_message("chat-901", "Ваш ключ: ABCD-1234")
        assert sent is True


@pytest.mark.asyncio
async def test_auth_error_handling() -> None:
    def auth_fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Unauthorized"})

    transport = httpx.MockTransport(auth_fail_handler)
    async with MarketplaceClient(base_url="https://test.api", transport=transport) as client:
        with pytest.raises(MarketplaceError, match="Ошибка авторизации"):
            await client.fetch_paid_orders()


@pytest.mark.asyncio
async def test_end_to_end_order_processing(tmp_path: Path) -> None:
    """Интеграционный тест: списание ключей из БД и выдача клиентам через маркетплейс."""
    db_file = tmp_path / "orders_test.db"
    storage = KeyStorage(db_file)
    storage.add_keys(["KEY-ALPHA", "KEY-BETA"])

    transport = httpx.MockTransport(mock_marketplace_transport)
    async with MarketplaceClient(base_url="https://test.api", transport=transport) as client:
        processed = await process_orders(storage, client)
        assert processed == 2

        stats = storage.get_stats()
        assert stats["total"] == 2
        assert stats["available"] == 0
        assert stats["used"] == 2
