"""
ТаксиМакс — независимый бот такси в мессенджере MAX.

Точка входа: проверка токена -> регистрация хендлеров -> long polling.

Запуск:  python main.py
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys


def _force_utf8() -> None:
    """Принудительно включаем UTF-8 для stdout/stderr (Windows часто cp1251).

    Без этого логи и консоль показывают кириллицу кракозябрами
    (Панель вместо кракозябр). Переконфигурация потоков работает
    ин-место, поэтому корректна и для logging, и для print.
    """
    # Python 3.7+: включаем UTF-8 режим (эквивалент PYTHONUTF8=1)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


_force_utf8()

from config import settings
from core import storage
from core.router import Router
from max_api.client import MaxClient, MaxApiError
from handlers.common import register_common
from handlers.passenger_new import register_passenger
from handlers.driver import register_driver
from handlers.admin import register_admin

# Типы обновлений, которые слушаем (важно: без types long polling
# отдаёт не все типы апдейтов).
UPDATE_TYPES = [
    "message_created",
    "message_edited",
    "message_callback",
    "bot_started",
    "bot_added",
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "run.log")

logger = logging.getLogger("main")


def setup_logging() -> None:
    level = getattr(logging, (settings.log_level or "INFO").upper(), logging.INFO)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        # Ротация логов: 5 файлов по 5 МБ каждый.
        handlers.append(logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5,
            encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    # httpx логирует каждый запрос — приглушаем до WARNING
    logging.getLogger("httpx").setLevel(logging.WARNING)


# === Фоновая задача: авто-повышение цен ожидания pending-заказов ===
_auto_increase_task = None


async def _send_price_notification(client, order_id: int, old_price: float,
                                   new_price: float) -> None:
    """Уведомить онлайн-водителей о повышении цены заказа."""
    order = storage.storage.get_order(order_id)
    if not order:
        return
    drivers = storage.storage.get_online_drivers()
    already_notified = set(order.get("notified_driver_ids", []) or [])
    pickup = order.get("pickup", "")
    destination = order.get("destination", "")
    from max_api.keyboards import callback_button
    from ui.reply import Reply
    text = (
        f"⚠️ Цена заказа #{order_id} повышена\n\n"
        f"📍 {pickup}\n"
        f"🎯 {destination}\n\n"
        f"Было: {old_price:.0f} ₽ → Стало: {new_price:.0f} ₽"
    )
    kb = [[callback_button("👀 Посмотреть заказ", f"order:accept:{order_id}")]]
    for d in drivers:
        did = d.get("user_id")
        if not did:
            continue
        if did in already_notified:
            continue
        storage.storage.add_notified_driver(order_id, did)
        try:
            await Reply(client, did).send(text, keyboard=kb)
        except Exception as exc:
            logger.warning("Не удалось уведомить водителя %s: %s", did, exc)


async def check_pending_order_auto_increase() -> None:
    """Фоновый цикл: каждые 30 с проверяет pending-заказы, ожидание >= 5 мин.
    Повышает цену согласно политике admin_data['pending_autoprice']
    и уведомляет водителей об изменении.
    """
    notif_client = MaxClient()
    try:
        while True:
            try:
                # 1) Отменяем застарелые pending-заказы (таймаут 5 мин).
                cancelled_ids = storage.storage.cancel_timeout_orders(timeout_minutes=5)
                for oid in cancelled_ids:
                    order = storage.storage.get_order(oid)
                    if not order:
                        continue
                    pid = order.get("passenger_id")
                    if pid:
                        try:
                            await notif_client.send_message(
                                user_id=int(pid),
                                text=(f"❌ Заказ #{oid} отменён автоматически: "
                                      f"водитель не нашелся в течение 5 мин."),
                            )
                        except Exception as exc:
                            logger.warning(
                                "Не удалось уведомить пассажира об отмене #%s: %s",
                                oid, exc)
                    logger.info("Авто-отмена заказа #%s по таймауту", oid)

                # 2) Авто-повышение цены для оставшихся pending-заказов.
                timeout_orders = storage.storage.check_order_timeout()
                for oid in timeout_orders:
                    order = storage.storage.get_order(oid)
                    if not order:
                        continue
                    # Пропускаем уже отменённые по таймауту.
                    if order.get("status") != "pending":
                        continue
                    old_price = order.get("price", 0)
                    new_price = storage.storage.apply_pending_order_auto_increase(oid)
                    if new_price is not None:
                        try:
                            await _send_price_notification(
                                notif_client, oid, old_price, new_price)
                        except Exception as exc:
                            logger.warning(
                                "Ошибка уведомления водителей о повышении цены #%s: %s",
                                oid, exc)
                        logger.info("Автоповышение цены заказа #%s: %s → %s",
                                    oid, old_price, new_price)
                    else:
                        logger.debug("Заказ #%s: автоповышение не требуется", oid)
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Ошибка цикла авто-повышения цен: %s", exc)
                await asyncio.sleep(30)
    finally:
        await notif_client.close()


async def run_polling() -> None:
    global _auto_increase_task
    logger = logging.getLogger("main")
    client = MaxClient()

    # 1) Проверяем токен
    try:
        me = await client.get_me()
    except MaxApiError as exc:
        logger.error("Не удалось подключиться к MAX API: %s", exc)
        await client.close()
        sys.exit(1)
    bot_name = me.get("name") or me.get("username") or "?"
    bot_id = me.get("user_id")
    logger.info("Бот подключён: %s (@%s, id=%s)",
                bot_name, me.get("username"), bot_id)

    # 2) Регистрируем хендлеры
    router = Router(client)
    register_common(router)
    register_passenger(router)
    register_driver(router)
    register_admin(router)
    logger.info("Хендлеры зарегистрированы: callbacks=%d, states=%d, roles=%d",
                len(router.callback_handlers), len(router.state_handlers),
                len(router.role_menu))

    # Запуск фоновой задачи авто-повышения цен ожидания
    global _auto_increase_task
    _auto_increase_task = asyncio.create_task(check_pending_order_auto_increase())
    logger.info("Запущена фоновая задача авто-повышения цен")

    # 3) Сообщаем MAX о командах
    try:
        await client.set_my_commands([
            {"command": "/start", "description": "Начать работу с ботом"},
            {"command": "/menu", "description": "Главное меню"},
        ])
    except MaxApiError as exc:
        logger.warning("Не удалось установить команды: %s", exc)

    # 4) Long polling
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows: SIGTERM через signal.signal
            try:
                signal.signal(sig, lambda *_: stop.set())
            except Exception:
                pass

    logger.info("Запуск long polling (типы: %s)...", ", ".join(UPDATE_TYPES))
    errors = 0
    seen_ids: dict[str, None] = {}
    while not stop.is_set():
        try:
            updates = await client.get_updates(
                timeout=30, types=UPDATE_TYPES, marker=client.last_marker,
            )
            errors = 0
            marker = updates.get("marker")
            if isinstance(marker, int):
                client._last_marker = marker
            # хранилище обработанных id обновлений (дубликаты MAX API приходят 2-3 раза)

            for raw in updates.get("updates", []):
                try:
                    from max_api.models import Update
                    upd = Update.from_dict(raw)
                    actor = upd.actor_id
                    raw_d = upd.raw if isinstance(upd.raw, dict) else {}
                    cb = raw_d.get("callback")
                    msg = raw_d.get("message")
                    body = (msg or {}).get("body") if isinstance(msg, dict) else None
                    mid = body.get("mid") if isinstance(body, dict) else (msg or {}).get("mid")
                    cb_id = (cb or {}).get("callback_id") if isinstance(cb, dict) else None
                    key = f"{upd.update_type}:{mid or cb_id or actor}:{upd.timestamp}"
                    if key in seen_ids:
                        logger.debug("пропущен дубликат апдейта: %s", key)
                        continue
                    seen_ids[key] = None
                    if len(seen_ids) > 30000:
                        seen_ids.pop(next(iter(seen_ids)))
                    detail = ""
                    if upd.is_message_callback and upd.callback:
                        detail = f" payload={upd.callback.payload!r}"
                    elif upd.message is not None:
                        detail = f" text={(upd.message.text or '')[:60]!r}"
                    logger.info("АПДЕЙТ %s от %s%s", upd.update_type, actor, detail)
                    await router.dispatch(upd)
                except Exception as exc:
                    logger.exception("Ошибка обработки апдейта: %s", exc)
        except asyncio.CancelledError:
            break
        except MaxApiError as exc:
            errors += 1
            wait = min(2 ** min(errors, 5), 60)
            logger.warning("Ошибка поллинга (%d): %s; повтор через %d с",
                           errors, exc, wait)
            await asyncio.sleep(wait)
        except Exception as exc:
            errors += 1
            logger.exception("Неожиданная ошибка поллинга: %s", exc)
            await asyncio.sleep(5)

    # 5) Graceful shutdown
    logger.info("Остановка... Сохраняю данные.")
    storage.storage.flush()
    if _auto_increase_task:
        _auto_increase_task.cancel()
        try:
            await _auto_increase_task
        except asyncio.CancelledError:
            pass
        _auto_increase_task = None
    await client.close()


def main() -> None:
    setup_logging()
    logger.info("=" * 50)
    logger.info("ТаксиМакс стартует (env=%s)", settings.environment)
    if not settings.admin_ids:
        logger.warning("ADMIN_USER_IDS пуст — роль админа никому не выдана!")
    try:
        asyncio.run(run_polling())
    except KeyboardInterrupt:
        pass
    logger.info("Работа завершена.")


if __name__ == "__main__":
    main()
