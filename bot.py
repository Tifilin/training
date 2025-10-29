# bot.py
import os, logging, sqlite3, csv
from datetime import datetime, date, timedelta
import pytz
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters, ConversationHandler

# ======== Конфигурация ========
load_dotenv()
TOKEN = os.getenv("TG_BOT_TOKEN")
TIMEZONE = os.getenv("TZ", "Europe/Moscow")
DB_PATH = os.getenv("DB_PATH", "data/ia1_reports.db")
CSV_BACKUP = os.getenv("CSV_BACKUP", "data/ia1_reports_backup.csv")
DEFAULT_REMINDER = os.getenv("DEFAULT_REMINDER", "21:00")

# ======== Логирование ========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ======== Инициализация базы ========
def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        ts TEXT,
        local_date TEXT,
        day_index INTEGER,
        text TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        user_id INTEGER PRIMARY KEY,
        reminder_time TEXT
    )""")
    conn.commit()
    conn.close()

def save_report(user_id, username, ts_iso, local_date_str, day_index, text):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reports (user_id, username, ts, local_date, day_index, text) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, username, ts_iso, local_date_str, day_index, text)
    )
    conn.commit()
    conn.close()
    write_header = not os.path.exists(CSV_BACKUP)
    with open(CSV_BACKUP, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["id","user_id","username","ts","local_date","day_index","text"])
        writer.writerow(["", user_id, username, ts_iso, local_date_str, day_index, text])

def set_reminder(user_id, time_str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("REPLACE INTO reminders (user_id, reminder_time) VALUES (?, ?)", (user_id, time_str))
    conn.commit()
    conn.close()

def get_reminders():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id, reminder_time FROM reminders")
    rows = cur.fetchall()
    conn.close()
    return rows

# ======== Вспомогательные функции ========
def get_local_now():
    tz = pytz.timezone(TIMEZONE)
    return datetime.now(tz)

def date_to_day_index(d: date) -> int:
    return ((d.day - 1) % 30) + 1

# ======== Миссии 30 дней ========
MISSIONS = []
for i in range(1, 31):
    if i <= 7:
        m = "Наблюдение: выбери место, 5 мин — запомни 10 деталей, потом восстанови."
    elif i <= 14:
        m = "Память: 10 минут головоломок или тренировка loci на 10 элементов."
    elif i <= 21:
        m = "Коммуникация: отзеркаль 1–2 собеседников и проанализируй реакцию."
    else:
        m = "Стратегия: сформируй план дня в формате OODA и выполни миссию вне зоны комфорта."
    MISSIONS.append(f"День {i}: {m}")

# ======== Handlers ========
REPORT_TEXT = 1

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/start от {update.effective_user.username}")
    await update.message.reply_text(
        "IA-1 Bot — приёмник отчётов.\nКоманды: /report, /mission, /progress, /setreminder"
    )

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Пришли текст отчёта (мин. 10 символов):")
    return REPORT_TEXT

async def receive_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) < 10:
        await update.message.reply_text("Слишком коротко. Опиши подробнее.")
        return REPORT_TEXT
    user = update.effective_user
    now = get_local_now()
    save_report(user.id, user.username or "", now.isoformat(), now.date().isoformat(), date_to_day_index(now.date()), text)
    await update.message.reply_text("Отчёт сохранён.")
    return ConversationHandler.END

async def cancel_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отправка отчёта отменена.")
    return ConversationHandler.END

async def cmd_mission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = get_local_now()
    day_idx = date_to_day_index(now.date())
    mission = MISSIONS[(day_idx - 1) % len(MISSIONS)]
    await update.message.reply_text(f"Миссия на {now.date().isoformat()}:\n{mission}")

async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    since = get_local_now().date() - timedelta(days=29)
    cur.execute("SELECT DISTINCT local_date FROM reports WHERE user_id=? AND local_date>=?", (user.id, since.isoformat()))
    rows = cur.fetchall()
    conn.close()
    days_with_reports = [r[0] for r in rows]
    await update.message.reply_text(f"Дней с отчётом за 30 дней: {len(days_with_reports)}\nДаты: {', '.join(days_with_reports) if days_with_reports else 'нет'}")

async def cmd_setreminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) != 1 or ":" not in args[0]:
            await update.message.reply_text("Использование: /setreminder HH:MM")
            return
        time_str = args[0]
        hour, minute = map(int, time_str.split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
        set_reminder(update.effective_user.id, time_str)
        await update.message.reply_text(f"Напоминание установлено на {time_str} каждый день.")
    except Exception:
        await update.message.reply_text("Неверный формат времени. Пример: /setreminder 21:00")

# ======== Напоминания через JobQueue ========
async def send_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    now = get_local_now()
    reminders = get_reminders()
    for user_id, time_str in reminders:
        h, m = map(int, time_str.split(":"))
        if now.hour == h and now.minute == m:
            try:
                await context.bot.send_message(chat_id=user_id, text="Напоминание: пришлите отчёт и выполните миссию сегодня!")
            except Exception as e:
                logger.error(f"Ошибка отправки напоминания {user_id}: {e}")

# ======== Main ========
def main():
    init_db()
    application = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('report', cmd_report)],
        states={REPORT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_report)]},
        fallbacks=[CommandHandler('cancel', cancel_report)],
        allow_reentry=True
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("mission", cmd_mission))
    application.add_handler(CommandHandler("progress", cmd_progress))
    application.add_handler(CommandHandler("setreminder", cmd_setreminder))

    # Добавляем JobQueue для напоминаний
    application.job_queue.run_repeating(send_reminder_job, interval=60, first=0)

    application.run_polling()

if __name__ == "__main__":
    main()
