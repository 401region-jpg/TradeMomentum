"""
scripts/get_chat_id.py
Получить ваш Telegram chat_id.
Использование: python scripts\get_chat_id.py
Напишите боту любое сообщение в Telegram — увидите chat_id.
Остановить: Ctrl+C
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

if not TOKEN or "ВСТАВЬТЕ" in TOKEN:
    print("ОШИБКА: TELEGRAM_BOT_TOKEN не найден в .env")
    sys.exit(1)

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes


async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.effective_chat.id
    username = update.effective_user.username or "—"
    print(f"\n=== Получено сообщение! ===")
    print(f"   chat_id  : {chat_id}")
    print(f"   username : @{username}")
    print(f"\nДобавьте в .env:")
    print(f"TELEGRAM_CHAT_ID={chat_id}")
    print(f"\nТеперь нажмите Ctrl+C чтобы остановить скрипт.")
    await update.message.reply_text(
        f"Ваш chat_id: <b>{chat_id}</b>\n\n"
        f"Добавьте в .env:\n<code>TELEGRAM_CHAT_ID={chat_id}</code>",
        parse_mode="HTML"
    )


def main():
    print("Бот запущен. Напишите ему любое сообщение в Telegram...")
    print("Остановить: Ctrl+C\n")

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT, handler))
    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
