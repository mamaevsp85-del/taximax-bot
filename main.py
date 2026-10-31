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

# Вопрос пассажиру, когда долго нет водителя (минуты).
NO_DRIVER_FIRST_ASK_MIN = 10        # первый вопрос — после 10 мин ожидания
NO_DRIVER_ASK_INTERVAL_MIN = 10     # повторный вопрос — каждые 10 мин
NO_DRIVER_UNANSWERED_MAX_MIN = 15   # если пассажир не ответил — авто-отмена через 15 мин после вопроса


def _no_driver_sound_wav() -> bytes:
    """Генерирует короткий двухтональный WAV-сигнал «такси ждёт».

    MAX Bot API не даёт выбрать системный звук пуша — поэтому прикрепляем
    аудио к сообщению: на телефоне оно будет проиграно со своим звуком,
    а не «просто вибрацией».
    """
    import io
    import math
    import struct
    import wave

    sr = 44100
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()

        def tone(freq, dur, vol=0.55):
            n = int(sr * dur)
            out = bytearray()
            for i in range(n):
                # плавное начало/конец — без щелчков
                env = min(1.0, i / (sr * 0.01), (n - i) / (sr * 0.02))
                v = vol * 32767 * env * math.sin(2 * math.pi * freq * i / sr)
                out += struct.pack("<h", int(v))
            return out

        # два «бипа», характерных для такси: 880 → 1175 → пауза → 1568
        frames = (tone(880, 0.16) + tone(1175, 0.20)
                  + tone(0, 0.18) + tone(1568, 0.28))
        w.writeframes(bytes(frames))
    return buf.getvalue()


async def _send_no_driver_question(client, order) -> bool:
    """Отправить пассажиру вопрос «Водитель не найден: ждать или отменить?»

    Со звуком: окно уведомления на телефоне проиграет прикреплённый
    аудиофайл (наш «бип»), а не обычный звук пуша. Если аудио-вложение
    не удалось загрузить — отправляем обычное сообщение (не роняем заказ).
    """
    pid = order.get("passenger_id")
    if not pid:
        return False
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    oid = order.get("id")
    text = (
        "⏳ <b>Водитель пока не найден</b> по заказу #" + str(oid) + "\n\n"
        "Ищем уже 10 минут. Как поступим?\n\n"
        "• <b>Ждать ещё</b> — продолжим поиск водителя\n"
        "• <b>Отменить</b> — закроем заказ"
    )
    from max_api.keyboards import callback_button, keyboard_attachment
    kb = keyboard_attachment([
        [callback_button("🕐 Ждать ещё", f"passenger:no_driver_wait:{oid}"),
         callback_button("❌ Отменить", f"passenger:cancel:{oid}")],
    ])
    attachments: list = []
    try:
        up = await client.upload_file("no_driver_tone.wav", _no_driver_sound_wav(),
                                      "audio/wav")
        token = (up or {}).get("token")
        if token:
            attachments.append({"type": "audio", "payload": {"token": token}})
    except Exception as exc:
        logger.debug("Не удалось прикрепить звук «нет водителя» #%s: %s", oid, exc)
    attachments.extend(kb)
    try:
        await client.send_message(user_id=pid_int, text=text,
                                  attachments=attachments, notify=True)
        return True
    except Exception as exc:
        # Пробуем без звука (вдруг аудио-вложение не поддержано)
        try:
            await client.send_message(user_id=pid_int, text=text,
                                      attachments=kb, notify=True)
            return True
        except Exception as exc2:
            logger.warning("Не удалось уведомить пассажира #%s о поиске водителя: %s", oid, exc2)
            return False


async def _notify_order_cancelled_no_driver(client, order, reason: str) -> None:
    """Уведомить пассажира об авто-отмене заказа без найденного водителя."""
    pid = order.get("passenger_id")
    if not pid:
        return
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return
    oid = order.get("id")
    text = (
        f"❌ <b>Заказ #{oid} отменён автоматически.</b>\n\n"
        f"Водитель не нашёлся, и ваш вопрос остался без ответа ({reason}).\n"
        "Можно создать новый заказ в любое время."
    )
    try:
        await client.send_message(user_id=pid_int, text=text, notify=True)
    except Exception as exc:
        logger.warning("Не удалось уведомить пассажира об авто-отмене #%s: %s", oid, exc)


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
                # 1) Вопрос «ждать или отменить?» для заказов без водителя.
                no_driver_ids = storage.storage.get_pending_no_driver_orders(
                    first_ask_minutes=NO_DRIVER_FIRST_ASK_MIN,
                    ask_interval_minutes=NO_DRIVER_ASK_INTERVAL_MIN)
                for oid in no_driver_ids:
                    order = storage.storage.get_order(oid)
                    if not order or order.get("status") != "pending":
                        continue
                    if await _send_no_driver_question(notif_client, order):
                        storage.storage.mark_no_driver_asked(oid)
                        logger.info(
                            "Вопрос «ждать/отменить» отправлен по заказу #%s", oid)

                # 1b) Если вопрос без ответа дольше допустимого — авто-отмена.
                cancelled_ids = storage.storage.cancel_unanswered_no_driver(
                    minutes_after_ask=NO_DRIVER_UNANSWERED_MAX_MIN)
                for oid in cancelled_ids:
                    order = storage.storage.get_order(oid)
                    if not order:
                        continue
                    await _notify_order_cancelled_no_driver(
                        notif_client, order, "вопрос остался без ответа")
                    logger.info(
                        "Авто-отмена заказа #%s (нет ответа на вопрос)", oid)

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
