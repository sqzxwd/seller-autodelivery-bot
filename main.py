import asyncio
import signal
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from config import Settings
from marketplace import MarketplaceClient, MarketplaceError
from notifier import TelegramNotifier
from storage import KeyStorage, StorageError

app = typer.Typer(
    name="bot-cli",
    help="CLI-интерфейс управления товарами и автовыдачей (Python 3.14).",
    no_args_is_help=True,
)


class MockDemoTransport(httpx.AsyncBaseTransport):
    """Имитатор API маркетплейса для работы в режиме --demo."""

    def __init__(self) -> None:
        self._order_counter = 1
        self._delivered_orders: set[str] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_path = request.url.path

        if url_path.endswith("/orders") and request.method == "GET":
            orders: list[dict[str, object]] = []
            if self._order_counter <= 6:
                order_id = f"DEMO-ORD-{self._order_counter:04d}"
                if order_id not in self._delivered_orders:
                    orders.append(
                        {
                            "order_id": order_id,
                            "customer_id": f"buyer_{100 + self._order_counter}",
                            "chat_id": f"chat_{800 + self._order_counter}",
                            "status": "paid",
                            "product_id": "game-sub-premium",
                            "amount": 29.99,
                        }
                    )
                    self._order_counter += 1
            return httpx.Response(200, json={"orders": orders})

        if "/chats/" in url_path and url_path.endswith("/messages") and request.method == "POST":
            return httpx.Response(
                200, json={"status": "sent", "timestamp": datetime.now().isoformat()}
            )

        if url_path.endswith("/fulfill") and request.method == "POST":
            parts = url_path.split("/")
            fulfill_idx = parts.index("fulfill")
            order_id = parts[fulfill_idx - 1]
            self._delivered_orders.add(order_id)
            return httpx.Response(200, json={"status": "fulfilled", "order_id": order_id})

        return httpx.Response(404, json={"detail": "Not found"})


class DeliveryRecord:
    __slots__ = ("chat_id", "customer_id", "delivered_at", "key", "order_id", "status")

    def __init__(
        self,
        order_id: str,
        customer_id: str,
        chat_id: str,
        key: str | None,
        status: str,
        delivered_at: str,
    ) -> None:
        self.order_id = order_id
        self.customer_id = customer_id
        self.chat_id = chat_id
        self.key = key
        self.status = status
        self.delivered_at = delivered_at


class Worker:
    """Асинхронный воркер автоматической обработки и выдачи товаров."""

    def __init__(
        self,
        storage: KeyStorage,
        client: MarketplaceClient,
        poll_interval: float = 3.0,
        console: Console | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.storage = storage
        self.client = client
        self.poll_interval = poll_interval
        self.console = console or Console()
        self.notifier = notifier or TelegramNotifier()
        self.recent_logs: deque[DeliveryRecord] = deque(maxlen=8)
        self.total_processed: int = 0
        self.status: str = "Инициализация"
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        """Сигнал мягкой остановки воркера."""
        self.status = "Завершение работы (Graceful shutdown)..."
        self._stop_event.set()

    async def run(self, max_cycles: int | None = None) -> None:
        """Основной рабочий цикл с интервалом опроса."""
        self.status = "Активен (опрос заказов)"
        cycle_count = 0

        while not self._stop_event.is_set():
            cycle_count += 1
            await self._process_cycle()

            if max_cycles is not None and cycle_count >= max_cycles:
                self.status = f"Демо-режим завершен ({max_cycles} циклов)"
                break

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)
                break
            except TimeoutError:
                continue

    async def _process_cycle(self) -> None:
        """Один шаг: получение заказов, списание ключей и отправка клиенту."""
        try:
            orders = await self.client.fetch_paid_orders()
        except (MarketplaceError, httpx.HTTPError) as exc:
            self.status = f"Ошибка связи с API: {exc}"
            return

        for order in orders:
            if self._stop_event.is_set():
                break

            now_str = datetime.now().strftime("%H:%M:%S")
            key = self.storage.pop_unused_key(order_id=order.order_id)

            match key:
                case str() as product_key:
                    try:
                        await self.client.send_chat_message(
                            chat_id=order.chat_id,
                            message=f"Здравствуйте! Спасибо за покупку. Ваш ключ: {product_key}",
                        )
                        await self.client.fulfill_order(
                            order_id=order.order_id,
                            delivered_key=product_key,
                        )
                        self.total_processed += 1
                        category = self.storage.get_key_category(product_key)

                        # Отправка мгновенного push-уведомления продавцу
                        if self.notifier.is_enabled:
                            await self.notifier.notify_order_delivered(
                                order_id=order.order_id,
                                category=category,
                                raw_key=product_key,
                                delivered_at=now_str,
                            )
                        else:
                            self.console.log(
                                f"[dim]Telegram push пропущен (токен не задан): выдан заказ {order.order_id}[/dim]"
                            )

                        # Проверка критического остатка товаров (< 3 шт.)
                        remaining_stock = self.storage.count_available(category=category)
                        if remaining_stock < 3:
                            if self.notifier.is_enabled:
                                await self.notifier.notify_low_stock(
                                    category=category,
                                    remaining_count=remaining_stock,
                                )
                            else:
                                self.console.log(
                                    f"[yellow]Внимание: низкий остаток ключей в категории '{category}': {remaining_stock} шт.[/yellow]"
                                )

                        self.recent_logs.appendleft(
                            DeliveryRecord(
                                order_id=order.order_id,
                                customer_id=order.customer_id,
                                chat_id=order.chat_id,
                                key=product_key,
                                status="ВЫДАН",
                                delivered_at=now_str,
                            )
                        )
                    except (MarketplaceError, httpx.HTTPError, StorageError) as exc:
                        self.recent_logs.appendleft(
                            DeliveryRecord(
                                order_id=order.order_id,
                                customer_id=order.customer_id,
                                chat_id=order.chat_id,
                                key=product_key,
                                status=f"СБОЙ: {exc}",
                                delivered_at=now_str,
                            )
                        )
                case None:
                    self.recent_logs.appendleft(
                        DeliveryRecord(
                            order_id=order.order_id,
                            customer_id=order.customer_id,
                            chat_id=order.chat_id,
                            key="—",
                            status="НЕТ В НАЛИЧИИ",
                            delivered_at=now_str,
                        )
                    )

    def render_dashboard(self) -> Group:
        """Формирует цветной визуальный интерфейс через Rich."""
        stats = self.storage.get_stats()

        summary_table = Table(title="[bold cyan]📦 СТАТУС БОТА АВТОВЫДАЧИ[/bold cyan]", expand=True)
        summary_table.add_column("Статус процесса", justify="center", style="bold green")
        summary_table.add_column("Интервал", justify="center")
        summary_table.add_column("Обработано заказов", justify="center", style="bold yellow")
        summary_table.add_column("Всего товаров", justify="center")
        summary_table.add_column("В наличии", justify="center", style="bold green")
        summary_table.add_column("Выдано", justify="center", style="bold blue")

        status_style = "bold red" if "Завершение" in self.status else "bold green"
        summary_table.add_row(
            f"[{status_style}]{self.status}[/{status_style}]",
            f"{self.poll_interval:.1f} сек.",
            str(self.total_processed),
            str(stats["total"]),
            str(stats["available"]),
            str(stats["used"]),
        )

        logs_table = Table(
            title="[bold magenta]📝 ПОСЛЕДНИЕ ВЫДАННЫЕ ЗАКАЗЫ[/bold magenta]", expand=True
        )
        logs_table.add_column("Время", justify="center", style="dim")
        logs_table.add_column("ID Заказа", style="cyan")
        logs_table.add_column("Покупатель", style="blue")
        logs_table.add_column("Чат", style="dim")
        logs_table.add_column("Выданный ключ", style="bold white")
        logs_table.add_column("Статус", justify="center")

        if not self.recent_logs:
            logs_table.add_row(
                "—", "—", "—", "—", "—", "[dim]Ожидание новых оплаченных заказов...[/dim]"
            )
        else:
            for record in self.recent_logs:
                match record.status:
                    case "ВЫДАН":
                        status_badge = "[bold green]✔ ВЫДАН[/bold green]"
                    case "НЕТ В НАЛИЧИИ":
                        status_badge = "[bold red]✖ НЕТ В НАЛИЧИИ[/bold red]"
                    case _:
                        status_badge = f"[bold yellow]{record.status}[/bold yellow]"

                logs_table.add_row(
                    record.delivered_at,
                    record.order_id,
                    record.customer_id,
                    record.chat_id,
                    record.key or "—",
                    status_badge,
                )

        return Group(
            summary_table,
            Panel(logs_table, border_style="dim"),
        )


async def _run_async_worker(
    demo: bool,
    demo_cycles: int | None,
    interval: float | None,
    db: Path | None,
) -> None:
    settings = Settings.from_env()
    db_path = db if db else settings.database_path
    storage = KeyStorage(db_path)

    transport: httpx.AsyncBaseTransport | None = None
    if demo:
        transport = MockDemoTransport()
        stats = storage.get_stats()
        if stats["available"] == 0:
            demo_keys = [f"DEMO-STEAM-KEY-{i:04d}" for i in range(1, 6)]
            storage.add_keys(demo_keys, category="steam")

    console = Console()
    poll_interval = interval if interval is not None else settings.poll_interval

    async with MarketplaceClient(
        base_url=settings.api_url,
        api_key=settings.api_key,
        transport=transport,
    ) as client:
        notifier = TelegramNotifier()
        worker = Worker(
            storage=storage,
            client=client,
            poll_interval=poll_interval,
            console=console,
            notifier=notifier,
        )

        loop = asyncio.get_running_loop()

        def handle_signal():
            worker.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, handle_signal)
            except NotImplementedError:
                pass

        with Live(
            worker.render_dashboard(), console=console, refresh_per_second=4, screen=False
        ) as live:

            async def refresh_ui_loop():
                while not worker._stop_event.is_set():
                    live.update(worker.render_dashboard())
                    await asyncio.sleep(0.25)
                live.update(worker.render_dashboard())

            ui_task = asyncio.create_task(refresh_ui_loop())
            try:
                await worker.run(max_cycles=demo_cycles)
            except asyncio.CancelledError:
                worker.stop()
            finally:
                worker.stop()
                await ui_task
                live.update(worker.render_dashboard())


@app.command(name="load-keys", help="Загрузить товары/ключи из файла в базу данных.")
def load_keys(
    file: Annotated[
        Path,
        typer.Argument(help="Путь к текстовому файлу с ключами (например, goods.txt)."),
    ],
    category: Annotated[
        str,
        typer.Option(
            "--category",
            "-c",
            help="Категория товара (например, steam, vpn, software).",
        ),
    ] = "default",
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help="Путь к файлу базы данных SQLite.",
        ),
    ] = None,
) -> None:
    """Загружает ключи из текстового файла в SQLite с указанием категории и фильтрацией пустых строк."""
    console = Console()
    if not file.is_file():
        console.print(f"[bold red]Ошибка:[/] Файл не найден: {file}")
        raise typer.Exit(code=1)

    settings = Settings.from_env()
    db_path = db or settings.database_path
    storage = KeyStorage(db_path)

    try:
        added_count = storage.add_keys_from_file(file, category=category)
    except StorageError as exc:
        console.print(f"[bold red]Ошибка чтения файла:[/] {exc}")
        raise typer.Exit(code=1) from exc

    stats = storage.get_stats()
    console.print(
        Panel(
            f"[bold green]✔ Загружено новых ключей:[/] [bold white]{added_count}[/]\n"
            f"[bold cyan]Категория:[/] [yellow]{category}[/]\n"
            f"[bold blue]Всего в наличии в БД:[/] [green]{stats['available']}[/]\n"
            f"[dim]Источник:[/] [dim]{file}[/]",
            title="[bold green]📦 Импорт ключей завершен[/bold green]",
            expand=False,
        )
    )


@app.command(name="stats", help="Отобразить сводную статистику по категориям товаров.")
def stats(
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help="Путь к файлу базы данных SQLite.",
        ),
    ] = None,
) -> None:
    """Выводит красивую Rich-таблицу со сводной статистикой по всем категориям товаров."""
    console = Console()
    settings = Settings.from_env()
    db_path = db or settings.database_path
    storage = KeyStorage(db_path)

    category_stats = storage.get_category_stats()

    table = Table(
        title="[bold cyan]📊 СВОДНАЯ СТАТИСТИКА ПО КАТЕГОРИЯМ ТОВАРОВ[/bold cyan]",
        expand=True,
    )
    table.add_column("Категория", style="bold cyan")
    table.add_column("Свободно ключей", justify="center", style="bold green")
    table.add_column("Выдано всего", justify="center", style="bold blue")
    table.add_column("Дата последней выдачи", justify="center", style="dim")

    if not category_stats:
        table.add_row("—", "0", "0", "—")
    else:
        for row in category_stats:
            table.add_row(
                str(row["category"]),
                str(row["available"]),
                str(row["used"]),
                str(row["last_used_at"]),
            )

    console.print(table)


@app.command(name="run", help="Запустить асинхронный воркер автовыдачи товаров.")
def run_worker(
    demo: Annotated[
        bool,
        typer.Option(
            "--demo",
            help="Запуск в режиме демонстрации с симуляцией заказов.",
        ),
    ] = False,
    demo_cycles: Annotated[
        int | None,
        typer.Option(
            "--demo-cycles",
            help="Количество демо-циклов до автозавершения.",
        ),
    ] = None,
    interval: Annotated[
        float | None,
        typer.Option(
            "--interval",
            help="Интервал проверки заказов в секундах (по умолчанию из .env).",
        ),
    ] = None,
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help="Путь к файлу базы данных SQLite.",
        ),
    ] = None,
) -> None:
    """Запускает непрерывный процесс автовыдачи с live-дашбордом и graceful shutdown."""
    try:
        asyncio.run(
            _run_async_worker(
                demo=demo,
                demo_cycles=demo_cycles,
                interval=interval,
                db=db,
            )
        )
    except KeyboardInterrupt:
        pass


def main() -> None:
    app()


if __name__ == "__main__":
    main()
