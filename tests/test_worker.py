from pathlib import Path

import pytest

from main import MockDemoTransport, Worker
from marketplace import MarketplaceClient
from storage import KeyStorage


@pytest.mark.asyncio
async def test_worker_processing_cycle(tmp_path: Path) -> None:
    db_file = tmp_path / "worker_test.db"
    storage = KeyStorage(db_file)
    storage.add_keys(["KEY-100", "KEY-200"])

    transport = MockDemoTransport()
    async with MarketplaceClient(base_url="https://demo.local", transport=transport) as client:
        worker = Worker(storage=storage, client=client, poll_interval=0.1)

        # Выполняем один рабочий цикл
        await worker._process_cycle()

        assert worker.total_processed >= 1
        assert len(worker.recent_logs) >= 1
        assert worker.recent_logs[0].status == "ВЫДАН"

        # Проверяем рендеринг дашборда Rich
        dashboard = worker.render_dashboard()
        assert dashboard is not None


@pytest.mark.asyncio
async def test_worker_graceful_stop(tmp_path: Path) -> None:
    db_file = tmp_path / "worker_stop_test.db"
    storage = KeyStorage(db_file)

    transport = MockDemoTransport()
    async with MarketplaceClient(base_url="https://demo.local", transport=transport) as client:
        worker = Worker(storage=storage, client=client, poll_interval=0.05)

        # Останавливаем воркер после 1 цикла
        await worker.run(max_cycles=1)
        assert worker._stop_event.is_set() or "Демо-режим" in worker.status
