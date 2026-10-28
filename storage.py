import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class StorageError(Exception):
    """Базовое исключение для ошибок хранилища ключей."""


class KeyStorage:
    """Управление хранилищем цифровых ключей в SQLite с гарантией атомарности выдачи."""

    def __init__(self, db_path: Path | str = "keys.db") -> None:
        self.db_path = Path(db_path)
        self.init_db()

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            autocommit=True,
        )
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        try:
            yield conn
        finally:
            conn.close()

    def init_db(self) -> None:
        """Идемпотентная инициализация схемы БД и миграция."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_key TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL DEFAULT 'default',
                    is_used INTEGER NOT NULL DEFAULT 0,
                    order_id TEXT DEFAULT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    used_at TEXT DEFAULT NULL
                );
            """)

            # Идемпотентная проверка наличия колонки category для существующих БД
            cols = [row[1] for row in conn.execute("PRAGMA table_info(keys);").fetchall()]
            if "category" not in cols:
                conn.execute(
                    "ALTER TABLE keys ADD COLUMN category TEXT NOT NULL DEFAULT 'default';"
                )

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_keys_unused
                ON keys(category, is_used, id)
                WHERE is_used = 0;
            """)

    def add_keys(self, keys: list[str], category: str = "default") -> int:
        """Добавляет список ключей в базу данных, игнорируя существующие дубликаты."""
        cleaned_category = category.strip() or "default"
        cleaned_keys = [k.strip() for k in keys if k and k.strip()]
        if not cleaned_keys:
            return 0

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                initial_changes = conn.total_changes
                conn.executemany(
                    "INSERT OR IGNORE INTO keys (item_key, category) VALUES (?, ?);",
                    [(k, cleaned_category) for k in cleaned_keys],
                )
                added = conn.total_changes - initial_changes
                conn.execute("COMMIT;")
                return added
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def add_keys_from_file(self, file_path: Path | str, category: str = "default") -> int:
        """Загружает пачку ключей построчно из текстового файла с указанием категории."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Файл с ключами не найден: {path}")

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"Ошибка чтения файла {path}: {exc}") from exc

        raw_keys = content.splitlines()
        return self.add_keys(raw_keys, category=category)

    def pop_unused_key(
        self, order_id: str | None = None, category: str | None = None
    ) -> str | None:
        """
        Атомарно резервирует и возвращает один неиспользованный ключ (опционально по категории).
        Исключает двойную выдачу ключа при параллельных запросах.
        Возвращает None, если доступных ключей нет.
        """
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                if category is not None:
                    query = """
                        UPDATE keys
                        SET is_used = 1,
                            order_id = ?,
                            used_at = datetime('now')
                        WHERE id = (
                            SELECT id FROM keys
                            WHERE is_used = 0 AND category = ?
                            ORDER BY id ASC
                            LIMIT 1
                        )
                        RETURNING item_key;
                    """
                    params: tuple[object, ...] = (order_id, category)
                else:
                    query = """
                        UPDATE keys
                        SET is_used = 1,
                            order_id = ?,
                            used_at = datetime('now')
                        WHERE id = (
                            SELECT id FROM keys
                            WHERE is_used = 0
                            ORDER BY id ASC
                            LIMIT 1
                        )
                        RETURNING item_key;
                    """
                    params = (order_id,)

                cursor = conn.execute(query, params)
                row = cursor.fetchone()
                conn.execute("COMMIT;")
                return row[0] if row else None
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def get_key_category(self, item_key: str) -> str:
        """Возвращает категорию товара по значению ключа."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT category FROM keys WHERE item_key = ?;",
                (item_key,),
            )
            row = cursor.fetchone()
            return str(row[0]) if row else "default"

    def count_available(self, category: str | None = None) -> int:
        """Возвращает количество оставшихся свободных ключей (опционально по категории)."""
        with self._get_connection() as conn:
            if category is not None:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM keys WHERE is_used = 0 AND category = ?;",
                    (category,),
                )
            else:
                cursor = conn.execute("SELECT COUNT(*) FROM keys WHERE is_used = 0;")
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def get_stats(self) -> dict[str, int]:
        """Возвращает общую статистику по ключам."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN is_used = 0 THEN 1 ELSE 0 END) AS available,
                    SUM(CASE WHEN is_used = 1 THEN 1 ELSE 0 END) AS used
                FROM keys;
            """)
            total, available, used = cursor.fetchone()
            return {
                "total": total or 0,
                "available": available or 0,
                "used": used or 0,
            }

    def get_category_stats(self) -> list[dict[str, object]]:
        """Возвращает сводную статистику с группировкой по категориям товаров."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT
                    category,
                    SUM(CASE WHEN is_used = 0 THEN 1 ELSE 0 END) AS available,
                    SUM(CASE WHEN is_used = 1 THEN 1 ELSE 0 END) AS used,
                    COALESCE(MAX(used_at), '—') AS last_used_at
                FROM keys
                GROUP BY category
                ORDER BY category ASC;
            """)
            rows = cursor.fetchall()
            return [
                {
                    "category": row[0],
                    "available": row[1] or 0,
                    "used": row[2] or 0,
                    "last_used_at": str(row[3]),
                }
                for row in rows
            ]
