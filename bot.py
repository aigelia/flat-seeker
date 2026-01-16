import asyncio
import json
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from environs import Env
from parser import fetch_new_apartments

# ---------- Логирование ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

env = Env()
env.read_env()

TG_TOKEN = env.str("TG_TOKEN")
CHAT_ID = env.str("CHAT_ID")
ALLOWED_USER_IDS = env.list("ALLOWED_USER_IDS", subcast=int)

CONFIG_PATH = "config.json"
PUBLISHED_IDS_PATH = "published_ids.json"
POLL_INTERVAL = 3600

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()


# ---------- Utility для изменения конфига и админ проверки ----------
def admin_only(func):
    async def wrapper(message: types.Message):
        if message.from_user.id not in ALLOWED_USER_IDS:
            await message.answer("⛔️ У вас нет доступа к этой команде")
            logger.warning(f"Попытка доступа от {message.from_user.id} ({message.from_user.username})")
            return
        return await func(message)
    return wrapper


def update_config_param(param: str, value: int):
    config_file = Path(CONFIG_PATH)
    config = json.loads(config_file.read_text(encoding="utf-8"))
    if param in config["search_params"]:
        config["search_params"][param] = value
        config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Настройка {param} обновлена на {value}")
        return True
    logger.warning(f"Не удалось обновить {param}")
    return False


# ---------- Инлайн меню ----------
def settings_menu():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📏 Расстояние от центра", callback_data="edit_FRadius"),
        InlineKeyboardButton(text="🏠 Мин. площадь", callback_data="edit_FAreaOverAllMin"),
        InlineKeyboardButton(text="💰 Макс. цена", callback_data="edit_FPriceMax"),
    )
    return kb.as_markup()


# ---------- Хэндлеры ----------
@dp.message(Command(commands=["start", "settings"]))
@admin_only
async def cmd_start(message: types.Message):
    config_file = Path(CONFIG_PATH)
    if not config_file.exists():
        default_config = {"search_params": {"FRadius": 5, "FAreaOverAllMin": 60, "FPriceMax": 1200}}
        config_file.write_text(json.dumps(default_config, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Создан дефолтный config.json")

    config = json.loads(config_file.read_text(encoding="utf-8"))
    params = config.get("search_params", {})

    text = (
        f"Привет! Текущие параметры поиска:\n"
        f"📏 Расстояние от центра: {params.get('FRadius', '—')}\n"
        f"🏠 Мин. площадь: {params.get('FAreaOverAllMin', '—')} м²\n"
        f"💰 Макс. цена: {params.get('FPriceMax', '—')}\n\n"
        f"Выбери, что хочешь изменить:"
    )

    await message.answer(text, reply_markup=settings_menu())


@dp.callback_query()
async def callbacks(call: types.CallbackQuery):
    param_map = {
        "edit_FRadius": ("FRadius", "📏 Расстояние от центра"),
        "edit_FAreaOverAllMin": ("FAreaOverAllMin", "🏠 Мин. площадь"),
        "edit_FPriceMax": ("FPriceMax", "💰 Макс. цена")
    }

    if call.data in param_map:
        param_name, param_label = param_map[call.data]
        await call.message.answer(
            f"Чтобы изменить {param_label}, используй команду:\n"
            f"<code>/set {param_name} значение</code>\n\n"
            f"Например: <code>/set {param_name} 100</code>",
            parse_mode="HTML"
        )
        await call.answer()

@dp.message(Command(commands=["set"]))
async def cmd_set(message: types.Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("Использование: /set параметр значение")
        return

    _, param_name, value_str = parts
    if not value_str.isdigit():
        await message.answer("Значение должно быть числом!")
        return

    value = int(value_str)
    if update_config_param(param_name, value):
        await message.answer(f"✅ Параметр {param_name} установлен на {value}")
    else:
        await message.answer(f"❌ Не удалось обновить параметр {param_name}")


# ---------- Периодический парсинг ----------
async def send_apt(bot: Bot, chat_id: str, apt: dict, delay: float = 3.0, max_retries: int = 5):
    """
    Отправка одного объявления с обработкой флуд-контроля.
    delay — безопасная пауза между сообщениями в одном чате
    max_retries — максимум попыток при flood control
    """
    text = (
        f"📍 <b>{apt.get('address', '—')}</b>\n"
        f"💰 Цена: {apt.get('price', '—')}\n"
        f"🛏 Комнаты: {apt.get('rooms', '—')}, 🏡 Площадь: {apt.get('area', '—')} м²\n"
        f"🏢 Этаж: {apt.get('floor', '—')}\n"
        f"🔗 <a href='{apt.get('url', '#')}'>Ссылка на объявление</a>"
    )

    retries = 0
    while retries < max_retries:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                disable_notification=True
            )
            await asyncio.sleep(delay)
            return True
        except TelegramRetryAfter as e:
            retries += 1
            logger.warning(f"Flood control для {apt['id']}, ждём {e.retry_after} сек (попытка {retries}/{max_retries})")
            if retries >= max_retries:
                logger.error(f"Превышен лимит попыток для {apt['id']}")
                return False
            await asyncio.sleep(e.retry_after)
        except TelegramAPIError as e:
            logger.error(f"Ошибка Telegram при отправке {apt['id']}: {e}")
            return False
        except Exception as e:
            logger.error(f"Неизвестная ошибка при отправке {apt['id']}: {e}", exc_info=True)
            return False

    return False


async def periodic_parser():
    """
    Основной цикл: парсинг и отправка новых квартир с безопасной задержкой и записью после каждой отправки
    """
    published_ids_file = Path(PUBLISHED_IDS_PATH)

    while True:
        try:
            # Загружаем published_ids перед каждым циклом для синхронизации
            if published_ids_file.exists():
                published_ids = set(json.loads(published_ids_file.read_text(encoding="utf-8")))
            else:
                published_ids = set()

            new_apts = await asyncio.to_thread(
                fetch_new_apartments,
                config_path=CONFIG_PATH,
                published_ids_path=PUBLISHED_IDS_PATH
            )

            if new_apts is None:
                logger.error("Парсер вернул None, пропускаем цикл")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            if not new_apts:
                logger.info("Новых квартир не найдено")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            sent_count = 0
            for apt in new_apts:
                if apt["id"] in published_ids:
                    continue

                success = await send_apt(bot, CHAT_ID, apt)
                if success:
                    sent_count += 1
                    # Сохраняем прямо после успешной отправки
                    published_ids.add(apt["id"])
                    published_ids_file.write_text(
                        json.dumps(list(published_ids), ensure_ascii=False, indent=2),
                        encoding="utf-8"
                    )
                else:
                    logger.warning(f"Не удалось отправить {apt['id']}")

            if sent_count > 0:
                logger.info(f"✓ Отправлено {sent_count} новых объявлений")

        except Exception as e:
            logger.error(f"Ошибка парсинга или в цикле отправки: {e}", exc_info=True)

        await asyncio.sleep(POLL_INTERVAL)


# ---------- Запуск бота ----------
async def main():
    async with Bot(token=TG_TOKEN) as bot_instance:
        try:
            chat = await bot_instance.get_chat(CHAT_ID)
            logger.info("Чат найден: %s", chat.title if hasattr(chat, "title") else chat.id)
        except Exception as e:
            logger.error(f"Ошибка с чатом: {e}")
            return

        asyncio.create_task(periodic_parser())
        await dp.start_polling(bot_instance)


if __name__ == "__main__":
    asyncio.run(main())
