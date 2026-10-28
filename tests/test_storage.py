from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from storage import KeyStorage


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_keys.db"


@pytest.fixture
def storage(temp_db: Path) -> KeyStorage:
    return KeyStorage(temp_db)


def test_init_db_is_idempotent(temp_db: Path) -> None:
    """Проверка безопасности повторной инициализации БД."""
    store = KeyStorage(temp_db)
    store.init_db()
    store.init_db()
    stats = store.get_stats()
    assert stats["total"] == 0


def test_add_keys_and_deduplication(storage: KeyStorage) -> None:
    """Проверка добавления ключей и игнорирования дубликатов."""
    added = storage.add_keys(["KEY-1", "KEY-2", "KEY-1", "  KEY-3  ", ""])
    assert added == 3
    stats = storage.get_stats()
    assert stats["total"] == 3
    assert stats["available"] == 3


def test_add_keys_from_file(storage: KeyStorage, tmp_path: Path) -> None:
    """Проверка загрузки пачки товаров из файла."""
    keys_file = tmp_path / "goods.txt"
    keys_file.write_text("KEY-A\n\nKEY-B\nKEY-C\nKEY-A\n", encoding="utf-8")

    added = storage.add_keys_from_file(keys_file)
    assert added == 3
    stats = storage.get_stats()
    assert stats["available"] == 3


def test_pop_key_never_issues_same_key_twice(storage: KeyStorage) -> None:
    """Основной инвариант: один и тот же ключ никогда не выдается дважды."""
    keys = [f"SERIAL-{i:03d}" for i in range(1, 6)]
    storage.add_keys(keys)

    issued_keys: list[str] = []
    for i in range(len(keys)):
        key = storage.pop_unused_key(order_id=f"order-{i}")
        assert key is not None
        assert key not in issued_keys, f"Ключ {key} выдан повторно!"
        issued_keys.append(key)

    # Когда все ключи выданы, метод возвращает None
    empty_key = storage.pop_unused_key(order_id="order-exhausted")
    assert empty_key is None

    stats = storage.get_stats()
    assert stats["available"] == 0
    assert stats["used"] == len(keys)
    assert len(set(issued_keys)) == len(keys)


def test_concurrent_key_issuance_guarantees_uniqueness(storage: KeyStorage) -> None:
    """Стресс-тест конкурентной выдачи: параллельные потоки не должны получить дубликаты."""
    total_keys = 100
    keys = [f"CONCURRENT-KEY-{i:04d}" for i in range(total_keys)]
    storage.add_keys(keys)

    def worker(worker_id: int) -> str | None:
        return storage.pop_unused_key(order_id=f"concurrent-order-{worker_id}")

    # Запускаем 20 параллельных воркеров на 100 ключей
    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(worker, range(total_keys)))

    issued = [k for k in results if k is not None]
    assert len(issued) == total_keys
    # Проверка строгого отсутствия дубликатов
    assert len(set(issued)) == total_keys, "Обнаружены дубликаты ключей при параллельной выдаче!"
