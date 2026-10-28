from pathlib import Path

from typer.testing import CliRunner

from main import app
from storage import KeyStorage

runner = CliRunner()


def test_cli_load_keys_success(tmp_path: Path) -> None:
    """Проверка команды load-keys: фильтрация пустых строк, указание категории и запись в БД."""
    db_file = tmp_path / "cli_test.db"
    goods_file = tmp_path / "goods.txt"
    goods_file.write_text(
        "\n  STEAM-KEY-101  \n\nSTEAM-KEY-102\n\n   \nSTEAM-KEY-101\nSTEAM-KEY-103\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["load-keys", str(goods_file), "--category", "games", "--db", str(db_file)],
    )

    assert result.exit_code == 0
    assert "Импорт ключей завершен" in result.output
    assert "games" in result.output

    # Проверка базы данных
    storage = KeyStorage(db_file)
    stats = storage.get_stats()
    assert stats["total"] == 3
    assert stats["available"] == 3

    cat_stats = storage.get_category_stats()
    assert len(cat_stats) == 1
    assert cat_stats[0]["category"] == "games"
    assert cat_stats[0]["available"] == 3


def test_cli_load_keys_file_not_found(tmp_path: Path) -> None:
    """Проверка ошибки при отсутствии файла."""
    missing_file = tmp_path / "non_existent.txt"
    db_file = tmp_path / "cli_test.db"

    result = runner.invoke(
        app,
        ["load-keys", str(missing_file), "--db", str(db_file)],
    )

    assert result.exit_code == 1
    assert "Файл не найден" in result.output


def test_cli_stats_command(tmp_path: Path) -> None:
    """Проверка команды stats: вывод сводной таблицы категорий."""
    db_file = tmp_path / "cli_test.db"
    goods_steam = tmp_path / "steam.txt"
    goods_steam.write_text("STEAM-1\nSTEAM-2\n", encoding="utf-8")

    goods_vpn = tmp_path / "vpn.txt"
    goods_vpn.write_text("VPN-1\n", encoding="utf-8")

    # Загружаем товары разных категорий
    runner.invoke(app, ["load-keys", str(goods_steam), "--category", "steam", "--db", str(db_file)])
    runner.invoke(app, ["load-keys", str(goods_vpn), "--category", "vpn", "--db", str(db_file)])

    # Вызываем stats
    result = runner.invoke(app, ["stats", "--db", str(db_file)])
    assert result.exit_code == 0
    assert "СВОДНАЯ СТАТИСТИКА ПО КАТЕГОРИЯМ ТОВАРОВ" in result.output
    assert "steam" in result.output
    assert "vpn" in result.output
