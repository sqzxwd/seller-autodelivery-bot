import json
from pathlib import Path

import httpx
import pytest

from main import MockDemoTransport, Worker
from marketplace import MarketplaceClient
from notifier import TelegramNotifier, mask_key
from storage import KeyStorage


def test_mask_key() -> None:
    """Проверка маскирования ключей активации."""
    assert mask_key("STEAM-PROMO-9999") == "STEAM-****-9999"
    assert mask_key("ABCD-EFGH-IJKL") == "ABCD-****-IJKL"
    assert mask_key("12345678901234") == "1234****1234"
    assert mask_key("1234") == "****"
    assert mask_key("") == "—"


@pytest.mark.asyncio
async def test_telegram_notifier_empty_token_is_noop() -> None:
    """При пустом токене нотификатор не выполняет запросы и возвращает False без ошибок."""
    notifier = TelegramNotifier(bot_token="", chat_id="")
    assert notifier.is_enabled is False

    res_msg = await notifier.send_message("Привет!")
    assert res_msg is False

    res_delivery = await notifier.notify_order_delivered(
        order_id="ORD-1",
        category="games",
        raw_key="STEAM-KEY-1111",
        delivered_at="12:00:00",
    )
    assert res_delivery is False

    res_alert = await notifier.notify_low_stock(category="games", remaining_count=1)
    assert res_alert is False


@pytest.mark.asyncio
async def test_telegram_notifier_successful_delivery() -> None:
    """Проверка успешной отправки push-уведомления о выдаче заказа через MockTransport."""
    captured_requests: list[dict[str, object]] = []

    def mock_tg_handler(request: httpx.Request) -> httpx.Response:
        assert "/botTEST_TOKEN/sendMessage" in str(request.url)
        data = json.loads(request.content)
        captured_requests.append(data)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    transport = httpx.MockTransport(mock_tg_handler)
    notifier = TelegramNotifier(
        bot_token="TEST_TOKEN",
        chat_id="123456789",
        transport=transport,
    )
    assert notifier.is_enabled is True

    success = await notifier.notify_order_delivered(
        order_id="ORD-777",
        category="steam",
        raw_key="STEAM-KEY-9999",
        delivered_at="15:30:00",
    )
    assert success is True
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req["chat_id"] == "123456789"
    assert "ORD-777" in str(req["text"])
    assert "STEAM-****-9999" in str(req["text"])
    assert "steam" in str(req["text"])


@pytest.mark.asyncio
async def test_telegram_notifier_low_stock_alert() -> None:
    """Проверка отправки алерта о критически низком остатке товаров."""
    captured_requests: list[dict[str, object]] = []

    def mock_tg_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        captured_requests.append(data)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(mock_tg_handler)
    notifier = TelegramNotifier(
        bot_token="TEST_TOKEN",
        chat_id="123456789",
        transport=transport,
    )

    success = await notifier.notify_low_stock(category="vpn", remaining_count=2)
    assert success is True
    assert len(captured_requests) == 1
    assert "Заканчивается товар" in str(captured_requests[0]["text"])
    assert "vpn" in str(captured_requests[0]["text"])
    assert "2 шт." in str(captured_requests[0]["text"])


@pytest.mark.asyncio
async def test_telegram_notifier_handles_http_errors_safely() -> None:
    """При ошибке сервера Telegram нотификатор возвращает False без падения приложения."""

    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False, "description": "Internal Server Error"})

    transport = httpx.MockTransport(fail_handler)
    notifier = TelegramNotifier(
        bot_token="TEST_TOKEN",
        chat_id="123456789",
        transport=transport,
    )

    success = await notifier.send_message("Тест ошибки")
    assert success is False


@pytest.mark.asyncio
async def test_worker_integration_with_notifier(tmp_path: Path) -> None:
    """Интеграционный тест: воркер выдает заказ, отправляет push и триггерит алерт низкого остатка."""
    db_file = tmp_path / "notifier_worker.db"
    storage = KeyStorage(db_file)
    # Загружаем всего 2 ключа (меньше 3 -> должен сработать алерт остатка)
    storage.add_keys(["KEY-101", "KEY-102"], category="steam")

    tg_messages: list[str] = []

    def tg_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        tg_messages.append(str(data["text"]))
        return httpx.Response(200, json={"ok": True})

    tg_transport = httpx.MockTransport(tg_handler)
    notifier = TelegramNotifier(
        bot_token="MY_BOT_TOKEN",
        chat_id="999888777",
        transport=tg_transport,
    )

    marketplace_transport = MockDemoTransport()
    async with MarketplaceClient(
        base_url="https://demo.local", transport=marketplace_transport
    ) as client:
        worker = Worker(
            storage=storage,
            client=client,
            poll_interval=0.1,
            notifier=notifier,
        )

        # Выполняем один цикл воркера
        await worker._process_cycle()

        assert worker.total_processed >= 1
        # Должны быть отправлены: 1) уведомление о выдаче, 2) алерт остатка (< 3)
        assert len(tg_messages) == 2
        assert any("Заказ успешно выдан" in msg for msg in tg_messages)
        assert any("Заканчивается товар" in msg for msg in tg_messages)
