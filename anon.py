import logging
import os
import secrets
import string
import sqlite3
import re
import shutil
import time
import asyncio
import html
import json
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from git import Repo

# --- НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_ID = int(os.environ.get("ADMIN_ID")) if os.environ.get("ADMIN_ID") else None
ADMIN_PASSWORD = "sirok228"

# --- НАСТРОЙКИ ДЛЯ ХРАНЕНИЯ БД НА GITHUB ---
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
DB_FILENAME = os.environ.get("DB_FILENAME", "data.db")
REPO_PATH = "/tmp/repo"
DB_PATH = os.path.join(REPO_PATH, DB_FILENAME)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)

repo = None

# --- ФУНКЦИИ ДЛЯ РАБОТЫ С GIT ---

def setup_repo():
    """Клонирует репозиторий из GitHub во временную папку."""
    global repo
    remote_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"
    
    if os.path.exists(REPO_PATH):
        try:
            shutil.rmtree(REPO_PATH)
        except Exception as e:
            logging.warning(f"Не удалось удалить REPO_PATH: {e}")
    
    max_retries = 5
    for attempt in range(max_retries):
        try:
            logging.info(f"Клонирование репозитория {GITHUB_REPO}... (попытка {attempt + 1})")
            repo = Repo.clone_from(remote_url, REPO_PATH)
            repo.config_writer().set_value("user", "name", "AnonBot").release()
            repo.config_writer().set_value("user", "email", "bot@render.com").release()
            logging.info("Репозиторий успешно склонирован и настроен.")
            return True
        except Exception as e:
            logging.error(f"Ошибка при клонировании репозитория (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(10)
            else:
                logging.critical("Не удалось склонировать репозиторий, создаю локальную БД")
                os.makedirs(REPO_PATH, exist_ok=True)
                return False

def push_db_to_github(commit_message):
    """Отправляет файл базы данных на GitHub."""
    if not repo:
        logging.error("Репозиторий не инициализирован, push невозможен.")
        return False
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            repo.index.add([DB_PATH])
            if repo.is_dirty(index=True, working_tree=False):
                repo.index.commit(commit_message)
                origin = repo.remote(name='origin')
                origin.push()
                logging.info(f"База данных успешно отправлена на GitHub. Коммит: {commit_message}")
                return True
            else:
                logging.info("Нет изменений в БД для отправки.")
                return True
        except Exception as e:
            logging.error(f"Ошибка при отправке БД на GitHub (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(10)
            else:
                logging.error(f"Не удалось отправить БД на GitHub после {max_retries} попыток")
                return False

# --- ФУНКЦИИ ДЛЯ РАБОТЫ С БД ---

def init_db():
    """Создает таблицы, если их нет."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Проверяем существование колонок
        cursor.execute("PRAGMA table_info(links)")
        link_columns = [column[1] for column in cursor.fetchall()]
        
        cursor.execute("PRAGMA table_info(messages)")
        message_columns = [column[1] for column in cursor.fetchall()]
        
        cursor.execute("PRAGMA table_info(replies)")
        reply_columns = [column[1] for column in cursor.fetchall()]
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, 
                username TEXT, 
                first_name TEXT, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS links (
                link_id TEXT PRIMARY KEY, 
                user_id INTEGER, 
                title TEXT, 
                description TEXT, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
                expires_at TIMESTAMP, 
                is_active BOOLEAN DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT, 
                link_id TEXT, 
                from_user_id INTEGER, 
                to_user_id INTEGER, 
                message_text TEXT, 
                message_type TEXT DEFAULT "text", 
                file_id TEXT,
                file_size INTEGER,
                file_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
                is_active BOOLEAN DEFAULT 1,
                FOREIGN KEY (link_id) REFERENCES links (link_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS replies (
                reply_id INTEGER PRIMARY KEY AUTOINCREMENT, 
                message_id INTEGER, 
                from_user_id INTEGER, 
                reply_text TEXT, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
                is_active BOOLEAN DEFAULT 1,
                FOREIGN KEY (message_id) REFERENCES messages (message_id)
            )
        ''')
        
        # Добавляем отсутствующие колонки
        if 'is_active' not in link_columns:
            cursor.execute('ALTER TABLE links ADD COLUMN is_active BOOLEAN DEFAULT 1')
        
        if 'is_active' not in message_columns:
            cursor.execute('ALTER TABLE messages ADD COLUMN is_active BOOLEAN DEFAULT 1')
            
        if 'is_active' not in reply_columns:
            cursor.execute('ALTER TABLE replies ADD COLUMN is_active BOOLEAN DEFAULT 1')
        
        conn.commit()
        conn.close()
        
        logging.info("База данных успешно инициализирована")
        
    except Exception as e:
        logging.error(f"Ошибка при инициализации БД: {e}")

def run_query(query, params=(), commit=False, fetch=None):
    """Универсальная функция для выполнения запросов к БД."""
    try:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            if commit:
                conn.commit()
            if fetch == "one":
                return cursor.fetchone()
            if fetch == "all":
                return cursor.fetchall()
            if commit:
                return cursor.lastrowid
    except sqlite3.Error as e:
        logging.error(f"Ошибка базы данных: {e}")
        return None

def save_user(user_id, username, first_name):
    run_query('INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)', 
              (user_id, username, first_name), commit=True)

def create_anon_link(user_id, title, description):
    link_id = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
    expires_at = datetime.now() + timedelta(days=365)
    run_query('INSERT INTO links (link_id, user_id, title, description, expires_at) VALUES (?, ?, ?, ?, ?)', 
              (link_id, user_id, title, description, expires_at), commit=True)
    push_db_to_github(f"Create link for user {user_id}")
    return link_id

def save_message(link_id, from_user_id, to_user_id, message_text, message_type='text', file_id=None, file_size=None, file_name=None):
    message_id = run_query('INSERT INTO messages (link_id, from_user_id, to_user_id, message_text, message_type, file_id, file_size, file_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', 
                          (link_id, from_user_id, to_user_id, message_text, message_type, file_id, file_size, file_name), commit=True)
    push_db_to_github(f"Save message from {from_user_id} to {to_user_id}")
    return message_id

def save_reply(message_id, from_user_id, reply_text):
    run_query('INSERT INTO replies (message_id, from_user_id, reply_text) VALUES (?, ?, ?)', 
              (message_id, from_user_id, reply_text), commit=True)
    push_db_to_github(f"Save reply to message {message_id}")

def get_link_info(link_id):
    return run_query('SELECT l.link_id, l.user_id, l.title, l.description, u.username FROM links l LEFT JOIN users u ON l.user_id = u.user_id WHERE l.link_id = ? AND l.is_active = 1', (link_id,), fetch="one")

def get_user_links(user_id):
    return run_query('SELECT link_id, title, description, created_at FROM links WHERE user_id = ? AND is_active = 1', (user_id,), fetch="all")

def get_user_messages_with_replies(user_id, limit=20):
    return run_query('''
        SELECT m.message_id, m.message_text, m.message_type, m.file_id, m.file_size, m.file_name, 
               m.created_at, l.title as link_title, l.link_id,
               (SELECT COUNT(*) FROM replies r WHERE r.message_id = m.message_id AND r.is_active = 1) as reply_count
        FROM messages m 
        JOIN links l ON m.link_id = l.link_id 
        WHERE m.to_user_id = ? AND m.is_active = 1
        ORDER BY m.created_at DESC LIMIT ?
    ''', (user_id, limit), fetch="all")

def get_message_replies(message_id):
    return run_query('''
        SELECT r.reply_id, r.reply_text, r.created_at, u.username, u.first_name
        FROM replies r
        LEFT JOIN users u ON r.from_user_id = u.user_id
        WHERE r.message_id = ? AND r.is_active = 1
        ORDER BY r.created_at ASC
    ''', (message_id,), fetch="all")

def get_admin_stats():
    stats = {}
    try:
        stats['users'] = run_query("SELECT COUNT(*) FROM users", fetch="one")[0] or 0
        stats['links'] = run_query("SELECT COUNT(*) FROM links WHERE is_active = 1", fetch="one")[0] or 0
        stats['messages'] = run_query("SELECT COUNT(*) FROM messages WHERE is_active = 1", fetch="one")[0] or 0
        stats['replies'] = run_query("SELECT COUNT(*) FROM replies WHERE is_active = 1", fetch="one")[0] or 0
        
        stats['photos'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'photo' AND is_active = 1", fetch="one")[0] or 0
        stats['videos'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'video' AND is_active = 1", fetch="one")[0] or 0
        stats['documents'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'document' AND is_active = 1", fetch="one")[0] or 0
        stats['voice'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'voice' AND is_active = 1", fetch="one")[0] or 0
        
        # Дополнительная статистика
        stats['active_today'] = run_query("SELECT COUNT(DISTINCT from_user_id) FROM messages WHERE DATE(created_at) = DATE('now') AND is_active = 1", fetch="one")[0] or 0
        stats['new_today'] = run_query("SELECT COUNT(*) FROM users WHERE DATE(created_at) = DATE('now')", fetch="one")[0] or 0
        
    except Exception as e:
        logging.error(f"Ошибка при получении статистики: {e}")
        stats = {'users': 0, 'links': 0, 'messages': 0, 'replies': 0, 'photos': 0, 'videos': 0, 'documents': 0, 'voice': 0, 'active_today': 0, 'new_today': 0}
    
    return stats

def get_all_users_for_admin():
    return run_query("SELECT user_id, username, first_name, created_at FROM users ORDER BY created_at DESC", fetch="all")

def get_all_links_for_admin():
    return run_query('''
        SELECT l.link_id, l.title, l.description, l.created_at, u.username, u.first_name,
               (SELECT COUNT(*) FROM messages m WHERE m.link_id = l.link_id AND m.is_active = 1) as message_count
        FROM links l
        LEFT JOIN users u ON l.user_id = u.user_id
        WHERE l.is_active = 1
        ORDER BY l.created_at DESC
    ''', fetch="all")

def get_recent_messages_for_admin(limit=50):
    return run_query('''
        SELECT m.message_id, m.message_text, m.message_type, m.file_size, m.file_name, m.created_at,
               u_from.username as from_username, u_from.first_name as from_first_name,
               u_to.username as to_username, u_to.first_name as to_first_name,
               l.title as link_title, l.link_id
        FROM messages m
        LEFT JOIN users u_from ON m.from_user_id = u_from.user_id
        LEFT JOIN users u_to ON m.to_user_id = u_to.user_id
        LEFT JOIN links l ON m.link_id = l.link_id
        WHERE m.is_active = 1
        ORDER BY m.created_at DESC
        LIMIT ?
    ''', (limit,), fetch="all")

# --- ФУНКЦИИ УДАЛЕНИЯ ---

def deactivate_link(link_id):
    """Деактивирует ссылку"""
    return run_query('UPDATE links SET is_active = 0 WHERE link_id = ?', (link_id,), commit=True)

def deactivate_message(message_id):
    """Деактивирует сообщение"""
    return run_query('UPDATE messages SET is_active = 0 WHERE message_id = ?', (message_id,), commit=True)

def deactivate_reply(reply_id):
    """Деактивирует ответ"""
    return run_query('UPDATE replies SET is_active = 0 WHERE reply_id = ?', (reply_id,), commit=True)

def get_message_info(message_id):
    """Получает информацию о сообщении"""
    return run_query('''
        SELECT m.message_text, m.message_type, m.file_name, m.created_at, 
               u_from.username as from_username, u_from.first_name as from_first_name,
               u_to.username as to_username, u_to.first_name as to_first_name,
               l.title as link_title
        FROM messages m
        LEFT JOIN users u_from ON m.from_user_id = u_from.user_id
        LEFT JOIN users u_to ON m.to_user_id = u_to.user_id
        LEFT JOIN links l ON m.link_id = l.link_id
        WHERE m.message_id = ?
    ''', (message_id,), fetch="one")

def get_link_owner(link_id):
    """Получает владельца ссылки"""
    return run_query('SELECT user_id FROM links WHERE link_id = ?', (link_id,), fetch="one")

def get_message_owner(message_id):
    """Получает отправителя сообщения"""
    return run_query('SELECT from_user_id FROM messages WHERE message_id = ?', (message_id,), fetch="one")

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def escape_markdown_v2(text: str) -> str:
    """Экранирует специальные символы для MarkdownV2"""
    if not text: 
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

def format_datetime(dt_string):
    """Форматирует дату-время с точностью до секунд (Красноярское время UTC+7)"""
    if isinstance(dt_string, str):
        try:
            dt = datetime.fromisoformat(dt_string.replace('Z', '+00:00'))
        except:
            return dt_string
    else:
        dt = dt_string
    
    # Добавляем 7 часов для Красноярского времени
    krasnoyarsk_time = dt + timedelta(hours=7)
    return krasnoyarsk_time.strftime("%Y-%m-%d %H:%M:%S") + " (Krasnoyarsk)"

# --- КЛАВИАТУРЫ ---

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟣 Главное меню", callback_data="main_menu")],
        [InlineKeyboardButton("🔗 Мои ссылки", callback_data="my_links")],
        [InlineKeyboardButton("➕ Создать ссылку", callback_data="create_link")],
        [InlineKeyboardButton("📨 Мои сообщения", callback_data="my_messages")]
    ])

def compact_main_keyboard():
    """Компактная версия клавиатуры"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟣 Главная", callback_data="main_menu"),
            InlineKeyboardButton("🔗 Ссылки", callback_data="my_links")
        ],
        [
            InlineKeyboardButton("➕ Создать", callback_data="create_link"),
            InlineKeyboardButton("📨 Сообщения", callback_data="my_messages")
        ]
    ])

def message_actions_keyboard(message_id, user_id, is_admin=False):
    """Компактная клавиатура действий для сообщения"""
    buttons = []
    
    # Основные действия в одной строке
    row = [
        InlineKeyboardButton("💬 Ответить", callback_data=f"reply_{message_id}"),
        InlineKeyboardButton("📋 Ответы", callback_data=f"view_replies_{message_id}")
    ]
    buttons.append(row)
    
    # Кнопка удаления если пользователь владелец или админ
    message_owner = get_message_owner(message_id)
    if message_owner and (message_owner[0] == user_id or is_admin):
        buttons.append([InlineKeyboardButton("🗑️ Удалить", callback_data=f"confirm_delete_message_{message_id}")])
    
    buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="my_messages")])
    
    return InlineKeyboardMarkup(buttons)

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton("🎨 ПИЗДАТЫЙ ОТЧЕТ", callback_data="admin_epic_report")],
        [InlineKeyboardButton("🔙 Главная", callback_data="main_menu")]
    ])

def delete_confirmation_keyboard(item_type, item_id):
    """Клавиатура подтверждения удаления"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да", callback_data=f"delete_{item_type}_{item_id}"),
            InlineKeyboardButton("❌ Нет", callback_data="cancel_delete")
        ]
    ])

# --- ОСНОВНЫЕ ОБРАБОТЧИКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        save_user(user.id, user.username, user.first_name)
        
        if context.args:
            link_id = context.args[0]
            link_info = get_link_info(link_id)
            if link_info:
                context.user_data['current_link'] = link_id
                text = f"🔗 *Анонимная ссылка*\n\n📝 *{escape_markdown_v2(link_info[2])}*\n📋 {escape_markdown_v2(link_info[3])}\n\n✍️ Напишите анонимное сообщение или отправьте медиафайл\\."
                await update.message.reply_text(text, parse_mode='MarkdownV2', reply_markup=compact_main_keyboard())
                return
        
        text = "👋 *Добро пожаловать в Анонимный Бот\\!*\n\nСоздавайте ссылки для получения анонимных сообщений\\."
        await update.message.reply_text(text, reply_markup=compact_main_keyboard(), parse_mode='MarkdownV2')
    except Exception as e:
        logging.error(f"Ошибка в команде start: {e}")
        await update.message.reply_text("❌ Произошла ошибка\\. Попробуйте позже\\.", parse_mode='MarkdownV2')

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /admin"""
    try:
        user = update.effective_user
        if user.username == ADMIN_USERNAME or user.id == ADMIN_ID:
            context.user_data['admin_authenticated'] = True
            await update.message.reply_text(
                "🛠️ *Панель администратора*",
                reply_markup=admin_keyboard(),
                parse_mode='MarkdownV2'
            )
        else:
            await update.message.reply_text("⛔️ Доступ запрещен\\.", parse_mode='MarkdownV2')
    except Exception as e:
        logging.error(f"Ошибка в команде admin: {e}")
        await update.message.reply_text("❌ Произошла ошибка\\. Попробуйте позже\\.", parse_mode='MarkdownV2')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        user = query.from_user
        data = query.data
        is_admin = user.username == ADMIN_USERNAME or user.id == ADMIN_ID

        # Основные команды меню
        if data == "main_menu":
            text = "🎭 *Главное меню*"
            await query.edit_message_text(text, reply_markup=compact_main_keyboard(), parse_mode='MarkdownV2')
            return
        
        elif data == "my_links":
            links = get_user_links(user.id)
            if links:
                text = "🔗 *Ваши анонимные ссылки:*\n\n"
                for link in links:
                    bot_username = context.bot.username
                    link_url = f"https://t.me/{bot_username}?start={link[0]}"
                    created = format_datetime(link[3])
                    text += f"📝 *{escape_markdown_v2(link[1])}*\n🔗 `{escape_markdown_v2(link_url)}`\n🕒 `{created}`\n"
                    text += f"🗑️ Удалить: /delete\\_link\\_{link[0]}\n\n"
                await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=compact_main_keyboard())
            else:
                await query.edit_message_text("У вас пока нет созданных ссылок\\.", reply_markup=compact_main_keyboard(), parse_mode='MarkdownV2')
            return
        
        elif data == "my_messages":
            messages = get_user_messages_with_replies(user.id, limit=10)
            if messages:
                text = "📨 *Ваши сообщения:*\n\n"
                for msg in messages:
                    msg_id, msg_text, msg_type, file_id, file_size, file_name, created, link_title, link_id, reply_count = msg
                    
                    type_icon = {"text": "📝", "photo": "🖼️", "video": "🎥", "document": "📄", "voice": "🎤"}.get(msg_type, "📄")
                    
                    preview = msg_text or f"*{msg_type}*"
                    if len(preview) > 30:
                        preview = preview[:30] + "\\.\\.\\."
                        
                    created_str = format_datetime(created)
                    text += f"{type_icon} *{escape_markdown_v2(link_title)}*\n`{preview}`\n🕒 `{created_str}` \\| 💬 {reply_count}\n\n"
                
                await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=compact_main_keyboard())
            else:
                await query.edit_message_text("У вас пока нет сообщений\\.", parse_mode='MarkdownV2', reply_markup=compact_main_keyboard())
            return
        
        elif data == "create_link":
            context.user_data['creating_link'] = True
            context.user_data['link_stage'] = 'title'
            await query.edit_message_text("📝 Введите *название* для вашей ссылки:", parse_mode='MarkdownV2', reply_markup=compact_main_keyboard())
            return
        
        # Управление удалением
        elif data.startswith("confirm_delete_message_"):
            message_id = int(data.replace("confirm_delete_message_", ""))
            message_info = get_message_info(message_id)
            
            if message_info:
                msg_text, msg_type, file_name, created, from_user, from_name, to_user, to_name, link_title = message_info
                
                text = f"🗑️ *Подтверждение удаления*\n\n"
                text += f"📝 *Сообщение:*\n`{msg_text if msg_text else f'Медиафайл: {msg_type}'}`\n\n"
                text += f"❓ *Вы уверены, что хотите удалить это сообщение?*"
                
                await query.edit_message_text(text, parse_mode='MarkdownV2', 
                                           reply_markup=delete_confirmation_keyboard("message", message_id))
            return
        
        elif data.startswith("delete_message_"):
            message_id = int(data.replace("delete_message_", ""))
            success = deactivate_message(message_id)
            
            if success:
                push_db_to_github(f"Delete message {message_id}")
                await query.edit_message_text("✅ *Сообщение удалено\\!*", 
                                           parse_mode='MarkdownV2', 
                                           reply_markup=compact_main_keyboard())
            else:
                await query.edit_message_text("❌ *Ошибка при удалении*", 
                                           parse_mode='MarkdownV2', 
                                           reply_markup=compact_main_keyboard())
            return
        
        elif data == "cancel_delete":
            await query.edit_message_text("❌ *Удаление отменено*", 
                                       parse_mode='MarkdownV2', 
                                       reply_markup=compact_main_keyboard())
            return

        # АДМИН ПАНЕЛЬ
        if is_admin:
            if data == "admin_stats":
                stats = get_admin_stats()
                text = f"""📊 *Статистика бота:*

👥 *Пользователи:*
• Всего: {stats['users']}
• Активных ссылок: {stats['links']}

💌 *Сообщения:*
• Всего: {stats['messages']}
• Ответов: {stats['replies']}

📁 *Файлы:*
• Фото: {stats['photos']}
• Видео: {stats['videos']}
• Документы: {stats['documents']}
• Голосовые: {stats['voice']}"""
                await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=admin_keyboard())
            
            elif data == "admin_users":
                users_count = get_admin_stats()['users']
                await query.edit_message_text(f"👥 *Пользователи:* {users_count}", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
            
            elif data == "admin_epic_report":
                await query.edit_message_text("🎨 *Генерирую пиздатый отчет...*", parse_mode='MarkdownV2')
                
                # Генерируем эпичный отчет
                html_content = generate_epic_html_report()
                
                report_path = "/tmp/epic_report.html"
                with open(report_path, 'w', encoding='utf-8') as f:
                    f.write(html_content)
                
                with open(report_path, 'rb') as f:
                    await query.message.reply_document(
                        document=f,
                        filename=f"EPIC_REPORT_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
                        caption="💜 *ПИЗДАТЫЙ ОТЧЕТ АДМИНА* 💜\n\nОткрой в браузере для полного эффекта!",
                        parse_mode='MarkdownV2'
                    )
                
                await query.edit_message_text("💜 *ЭПИЧНЫЙ ОТЧЕТ ОТПРАВЛЕН!*", parse_mode='MarkdownV2', reply_markup=admin_keyboard())

    except Exception as e:
        logging.error(f"Ошибка в обработчике кнопок: {e}")
        try:
            await query.edit_message_text("❌ Произошла ошибка\\. Попробуйте позже\\.", reply_markup=compact_main_keyboard(), parse_mode='MarkdownV2')
        except:
            pass

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        text = update.message.text
        save_user(user.id, user.username, user.first_name)

        # Создание ссылки
        if context.user_data.get('creating_link'):
            stage = context.user_data.get('link_stage')
            if stage == 'title':
                context.user_data['link_title'] = text
                context.user_data['link_stage'] = 'description'
                await update.message.reply_text("📋 Теперь введите *описание* для ссылки:", parse_mode='MarkdownV2')
            elif stage == 'description':
                title = context.user_data.pop('link_title')
                context.user_data.pop('creating_link')
                context.user_data.pop('link_stage')
                link_id = create_anon_link(user.id, title, text)
                bot_username = context.bot.username
                link_url = f"https://t.me/{bot_username}?start={link_id}"
                await update.message.reply_text(
                    f"✅ *Ссылка создана\\!*\n\n📝 *{escape_markdown_v2(title)}*\n🔗 `{escape_markdown_v2(link_url)}`\n\nПоделитесь ей, чтобы получать сообщения\\!",
                    parse_mode='MarkdownV2', 
                    reply_markup=compact_main_keyboard()
                )
            return

        # Отправка анонимного сообщения
        if context.user_data.get('current_link'):
            link_id = context.user_data.pop('current_link')
            link_info = get_link_info(link_id)
            if link_info:
                # Сохраняем сообщение с поддержкой форматирования
                msg_id = save_message(link_id, user.id, link_info[1], text)
                
                # Отправляем уведомление владельцу с сохранением форматирования
                notification = f"📨 *Новое анонимное сообщение*\n\n{text}"
                try:
                    await context.bot.send_message(
                        link_info[1], 
                        notification, 
                        parse_mode='MarkdownV2', 
                        reply_markup=message_actions_keyboard(msg_id, link_info[1], False)
                    )
                except Exception as e:
                    logging.error(f"Failed to send message notification: {e}")
                
                await update.message.reply_text("✅ Ваше сообщение отправлено анонимно\\!", reply_markup=compact_main_keyboard(), parse_mode='MarkdownV2')
            return

        # Команды удаления через текст
        if text.startswith('/delete_link_'):
            link_id = text.replace('/delete_link_', '').strip()
            link_owner = get_link_owner(link_id)
            
            if link_owner and link_owner[0] == user.id:
                success = deactivate_link(link_id)
                if success:
                    push_db_to_github(f"Delete link {link_id}")
                    await update.message.reply_text("✅ *Ссылка удалена\\!*", parse_mode='MarkdownV2', reply_markup=compact_main_keyboard())
                else:
                    await update.message.reply_text("❌ *Ошибка при удалении*", parse_mode='MarkdownV2', reply_markup=compact_main_keyboard())
            else:
                await update.message.reply_text("⛔️ *Нет прав для удаления*", parse_mode='MarkdownV2', reply_markup=compact_main_keyboard())
            return

        await update.message.reply_text("Используйте кнопки для навигации\\.", reply_markup=compact_main_keyboard(), parse_mode='MarkdownV2')

    except Exception as e:
        logging.error(f"Ошибка в обработчике текста: {e}")
        await update.message.reply_text("❌ Произошла ошибка\\. Попробуйте позже\\.", parse_mode='MarkdownV2')

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        save_user(user.id, user.username, user.first_name)
        msg = update.message
        caption = msg.caption or ""
        file_id, msg_type, file_size, file_name = None, "unknown", None, None

        if msg.photo: 
            file_id, msg_type = msg.photo[-1].file_id, "photo"
            file_size = msg.photo[-1].file_size
        elif msg.video: 
            file_id, msg_type = msg.video.file_id, "video"
            file_size = msg.video.file_size
            file_name = msg.video.file_name
        elif msg.voice: 
            file_id, msg_type = msg.voice.file_id, "voice"
            file_size = msg.voice.file_size
        elif msg.document: 
            file_id, msg_type = msg.document.file_id, "document"
            file_size = msg.document.file_size
            file_name = msg.document.file_name

        if context.user_data.get('current_link') and file_id:
            link_id = context.user_data.pop('current_link')
            link_info = get_link_info(link_id)
            if link_info:
                msg_id = save_message(link_id, user.id, link_info[1], caption, msg_type, file_id, file_size, file_name)
                
                file_info = ""
                if file_size:
                    file_info = f" \\({(file_size or 0) // 1024} KB\\)"
                if file_name:
                    file_info += f"\n📄 `{escape_markdown_v2(file_name)}`"
                
                user_caption = f"📨 *Новый анонимный {msg_type}*{file_info}\n\n{caption}"
                
                try:
                    if msg_type == 'photo': 
                        await context.bot.send_photo(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_actions_keyboard(msg_id, link_info[1], False))
                    elif msg_type == 'video': 
                        await context.bot.send_video(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_actions_keyboard(msg_id, link_info[1], False))
                    elif msg_type == 'document': 
                        await context.bot.send_document(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_actions_keyboard(msg_id, link_info[1], False))
                    elif msg_type == 'voice': 
                        await context.bot.send_voice(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_actions_keyboard(msg_id, link_info[1], False))
                except Exception as e: 
                    logging.error(f"Failed to send media to user: {e}")
                
                await update.message.reply_text("✅ Ваше медиа отправлено анонимно\\!", reply_markup=compact_main_keyboard(), parse_mode='MarkdownV2')

    except Exception as e:
        logging.error(f"Ошибка в обработчике медиа: {e}")
        await update.message.reply_text("❌ Произошла ошибка при отправке медиа\\.", parse_mode='MarkdownV2')

def generate_epic_html_report():
    """Генерирует пиздатый красивый отчет"""
    stats = get_admin_stats()
    users = get_all_users_for_admin()[:15]  # Топ 15 пользователей
    links = get_all_links_for_admin()[:20]  # Топ 20 ссылок
    messages = get_recent_messages_for_admin(30)  # 30 последних сообщений
    
    html_content = f'''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>💜 EPIC ADMIN REPORT - Анонимный Бот</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;700;900&family=Exo+2:wght@300;400;500;600;700&family=Rajdhani:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        :root {{
            --primary: #8A2BE2;
            --primary-dark: #6A0DAD;
            --primary-light: #9B30FF;
            --accent: #FF00FF;
            --accent-glow: #FF00FF;
            --text: #FFFFFF;
            --text-secondary: #E0E0FF;
            --bg-dark: #0A0A1A;
            --bg-card: rgba(255, 255, 255, 0.08);
            --bg-card-hover: rgba(255, 255, 255, 0.12);
        }}
        
        body {{
            font-family: 'Exo 2', sans-serif;
            background: linear-gradient(135deg, var(--bg-dark) 0%, #1A1A2E 50%, #16213E 100%);
            min-height: 100vh;
            color: var(--text);
            overflow-x: hidden;
            position: relative;
        }}
        
        body::before {{
            content: '';
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: 
                radial-gradient(circle at 20% 80%, rgba(138, 43, 226, 0.1) 0%, transparent 50%),
                radial-gradient(circle at 80% 20%, rgba(255, 0, 255, 0.1) 0%, transparent 50%),
                radial-gradient(circle at 40% 40%, rgba(106, 13, 173, 0.05) 0%, transparent 50%);
            pointer-events: none;
            z-index: -1;
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }}
        
        /* Эпичный хедер */
        .epic-header {{
            text-align: center;
            padding: 60px 20px;
            position: relative;
            margin-bottom: 50px;
        }}
        
        .epic-header::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: linear-gradient(45deg, transparent, rgba(138, 43, 226, 0.1), transparent);
            animation: headerShine 8s infinite linear;
            z-index: -1;
        }}
        
        @keyframes headerShine {{
            0% {{ transform: translateX(-100%); }}
            100% {{ transform: translateX(100%); }}
        }}
        
        .main-title {{
            font-family: 'Orbitron', monospace;
            font-size: 4.5em;
            font-weight: 900;
            background: linear-gradient(135deg, var(--primary) 0%, var(--accent) 50%, var(--primary-light) 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin-bottom: 20px;
            text-shadow: 0 0 50px rgba(138, 43, 226, 0.5);
            letter-spacing: 3px;
            position: relative;
            display: inline-block;
        }}
        
        .main-title::after {{
            content: '💜';
            position: absolute;
            right: -60px;
            top: 50%;
            transform: translateY(-50%);
            font-size: 0.8em;
            animation: pulse 2s infinite;
        }}
        
        .subtitle {{
            font-size: 1.4em;
            color: var(--text-secondary);
            margin-bottom: 30px;
            font-weight: 300;
        }}
        
        .timestamp {{
            font-family: 'Orbitron', monospace;
            font-size: 1.1em;
            color: var(--accent);
            background: rgba(255, 255, 255, 0.1);
            padding: 15px 30px;
            border-radius: 25px;
            display: inline-block;
            border: 1px solid rgba(255, 0, 255, 0.3);
            backdrop-filter: blur(10px);
            box-shadow: 0 0 20px rgba(255, 0, 255, 0.2);
        }}
        
        /* Статистика */
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 25px;
            margin-bottom: 50px;
        }}
        
        .stat-card {{
            background: var(--bg-card);
            backdrop-filter: blur(20px);
            padding: 35px 25px;
            border-radius: 20px;
            text-align: center;
            border: 1px solid rgba(255, 255, 255, 0.1);
            transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            position: relative;
            overflow: hidden;
        }}
        
        .stat-card::before {{
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.1), transparent);
            transition: left 0.6s ease;
        }}
        
        .stat-card:hover::before {{
            left: 100%;
        }}
        
        .stat-card:hover {{
            transform: translateY(-10px) scale(1.02);
            border-color: var(--primary-light);
            box-shadow: 0 15px 35px rgba(138, 43, 226, 0.3);
        }}
        
        .stat-icon {{
            font-size: 3em;
            margin-bottom: 20px;
            background: linear-gradient(135deg, var(--primary), var(--accent));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        
        .stat-number {{
            font-family: 'Orbitron', monospace;
            font-size: 3.5em;
            font-weight: 800;
            margin-bottom: 10px;
            background: linear-gradient(135deg, var(--text), var(--text-secondary));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        
        .stat-label {{
            color: var(--text-secondary);
            font-size: 1.1em;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 1.5px;
        }}
        
        /* Секции */
        .section {{
            background: var(--bg-card);
            backdrop-filter: blur(20px);
            padding: 40px;
            border-radius: 25px;
            margin-bottom: 40px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            position: relative;
            overflow: hidden;
        }}
        
        .section::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
            background: linear-gradient(90deg, var(--primary), var(--accent), var(--primary-light));
        }}
        
        .section-title {{
            font-family: 'Orbitron', monospace;
            font-size: 2.2em;
            margin-bottom: 30px;
            color: var(--text);
            display: flex;
            align-items: center;
            gap: 15px;
        }}
        
        .section-title i {{
            background: linear-gradient(135deg, var(--primary), var(--accent));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-size: 1.2em;
        }}
        
        /* Таблицы */
        .table-container {{
            overflow-x: auto;
            border-radius: 15px;
            background: rgba(255, 255, 255, 0.05);
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            background: transparent;
        }}
        
        th {{
            background: linear-gradient(135deg, rgba(138, 43, 226, 0.3), rgba(255, 0, 255, 0.2));
            color: var(--text);
            padding: 20px 15px;
            text-align: left;
            font-weight: 600;
            font-family: 'Orbitron', monospace;
            text-transform: uppercase;
            letter-spacing: 1px;
            font-size: 0.9em;
            position: sticky;
            top: 0;
        }}
        
        td {{
            padding: 18px 15px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            color: var(--text-secondary);
            transition: all 0.3s ease;
        }}
        
        tr:hover td {{
            background: rgba(255, 255, 255, 0.08);
            color: var(--text);
            transform: scale(1.01);
        }}
        
        .badge {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 600;
            font-family: 'Orbitron', monospace;
            letter-spacing: 1px;
        }}
        
        .badge-primary {{
            background: linear-gradient(135deg, var(--primary), var(--primary-dark));
            color: white;
        }}
        
        .badge-accent {{
            background: linear-gradient(135deg, var(--accent), #CC00CC);
            color: white;
        }}
        
        .badge-success {{
            background: linear-gradient(135deg, #00D4AA, #00B894);
            color: white;
        }}
        
        .user-avatar {{
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: linear-gradient(135deg, var(--primary), var(--accent));
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            color: white;
            margin-right: 12px;
            font-size: 1.1em;
        }}
        
        .user-info {{
            display: flex;
            align-items: center;
        }}
        
        .message-preview {{
            max-width: 200px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            color: var(--text-secondary);
        }}
        
        /* Анимации */
        @keyframes pulse {{
            0%, 100% {{ transform: translateY(-50%) scale(1); }}
            50% {{ transform: translateY(-50%) scale(1.1); }}
        }}
        
        @keyframes float {{
            0%, 100% {{ transform: translateY(0px); }}
            50% {{ transform: translateY(-20px); }}
        }}
        
        .floating {{
            animation: float 6s ease-in-out infinite;
        }}
        
        @keyframes fadeInUp {{
            from {{
                opacity: 0;
                transform: translateY(40px);
            }}
            to {{
                opacity: 1;
                transform: translateY(0);
            }}
        }}
        
        .fade-in {{
            animation: fadeInUp 0.8s ease-out forwards;
        }}
        
        /* Футер */
        .epic-footer {{
            text-align: center;
            padding: 60px 20px;
            margin-top: 50px;
            background: linear-gradient(135deg, rgba(138, 43, 226, 0.1), rgba(255, 0, 255, 0.05));
            border-radius: 25px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            position: relative;
            overflow: hidden;
        }}
        
        .footer-title {{
            font-family: 'Orbitron', monospace;
            font-size: 2em;
            margin-bottom: 20px;
            background: linear-gradient(135deg, var(--primary), var(--accent));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        
        .footer-subtitle {{
            color: var(--text-secondary);
            font-size: 1.1em;
            margin-bottom: 30px;
        }}
        
        .tech-stack {{
            display: flex;
            justify-content: center;
            gap: 20px;
            flex-wrap: wrap;
            margin-top: 30px;
        }}
        
        .tech-item {{
            background: rgba(255, 255, 255, 0.1);
            padding: 10px 20px;
            border-radius: 15px;
            font-family: 'Orbitron', monospace;
            font-size: 0.9em;
            color: var(--text-secondary);
            border: 1px solid rgba(255, 255, 255, 0.1);
        }}
        
        /* Адаптивность */
        @media (max-width: 768px) {{
            .main-title {{
                font-size: 2.8em;
            }}
            
            .stats-grid {{
                grid-template-columns: 1fr;
            }}
            
            .section {{
                padding: 25px;
            }}
            
            th, td {{
                padding: 12px 8px;
                font-size: 0.9em;
            }}
        }}
        
        /* Специальные эффекты */
        .glow {{
            text-shadow: 0 0 20px var(--accent-glow), 0 0 40px var(--accent-glow);
        }}
        
        .neon-border {{
            box-shadow: 0 0 10px var(--primary), 0 0 20px var(--primary), 0 0 40px var(--accent);
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Эпичный хедер -->
        <div class="epic-header fade-in">
            <h1 class="main-title floating">EPIC ADMIN REPORT</h1>
            <div class="subtitle">Расширенная аналитика анонимного бота в реальном времени</div>
            <div class="timestamp glow">
                <i class="fas fa-clock"></i> Отчет сгенерирован: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} (Krasnoyarsk)
            </div>
        </div>
        
        <!-- Основная статистика -->
        <div class="stats-grid">
            <div class="stat-card fade-in">
                <div class="stat-icon">
                    <i class="fas fa-users"></i>
                </div>
                <div class="stat-number">{stats['users']}</div>
                <div class="stat-label">Пользователи</div>
            </div>
            
            <div class="stat-card fade-in">
                <div class="stat-icon">
                    <i class="fas fa-link"></i>
                </div>
                <div class="stat-number">{stats['links']}</div>
                <div class="stat-label">Активные ссылки</div>
            </div>
            
            <div class="stat-card fade-in">
                <div class="stat-icon">
                    <i class="fas fa-envelope"></i>
                </div>
                <div class="stat-number">{stats['messages']}</div>
                <div class="stat-label">Сообщения</div>
            </div>
            
            <div class="stat-card fade-in">
                <div class="stat-icon">
                    <i class="fas fa-reply"></i>
                </div>
                <div class="stat-number">{stats['replies']}</div>
                <div class="stat-label">Ответы</div>
            </div>
        </div>
        
        <!-- Дополнительная статистика -->
        <div class="stats-grid">
            <div class="stat-card fade-in">
                <div class="stat-icon">
                    <i class="fas fa-image"></i>
                </div>
                <div class="stat-number">{stats['photos']}</div>
                <div class="stat-label">Фотографии</div>
            </div>
            
            <div class="stat-card fade-in">
                <div class="stat-icon">
                    <i class="fas fa-video"></i>
                </div>
                <div class="stat-number">{stats['videos']}</div>
                <div class="stat-label">Видео</div>
            </div>
            
            <div class="stat-card fade-in">
                <div class="stat-icon">
                    <i class="fas fa-file"></i>
                </div>
                <div class="stat-number">{stats['documents']}</div>
                <div class="stat-label">Документы</div>
            </div>
            
            <div class="stat-card fade-in">
                <div class="stat-icon">
                    <i class="fas fa-microphone"></i>
                </div>
                <div class="stat-number">{stats['voice']}</div>
                <div class="stat-label">Голосовые</div>
            </div>
        </div>
        
        <!-- Топ пользователей -->
        <div class="section fade-in">
            <h2 class="section-title">
                <i class="fas fa-crown"></i> ТОП ПОЛЬЗОВАТЕЛИ
            </h2>
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Пользователь</th>
                            <th>Username</th>
                            <th>Регистрация</th>
                        </tr>
                    </thead>
                    <tbody>
    '''
    
    for user in users:
        username = f"@{user[1]}" if user[1] else "—"
        first_name = html.escape(user[2]) if user[2] else "No Name"
        created = user[3].split()[0] if isinstance(user[3], str) else user[3].strftime("%Y-%m-%d")
        
        html_content += f'''
                        <tr>
                            <td><span class="badge badge-primary">{user[0]}</span></td>
                            <td>
                                <div class="user-info">
                                    <div class="user-avatar">
                                        {first_name[0].upper() if first_name else 'U'}
                                    </div>
                                    <div>{first_name}</div>
                                </div>
                            </td>
                            <td>{username}</td>
                            <td>{created}</td>
                        </tr>
        '''
    
    html_content += '''
                    </tbody>
                </table>
            </div>
        </div>
        
        <!-- Активные ссылки -->
        <div class="section fade-in">
            <h2 class="section-title">
                <i class="fas fa-fire"></i> АКТИВНЫЕ ССЫЛКИ
            </h2>
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>ID Ссылки</th>
                            <th>Название</th>
                            <th>Владелец</th>
                            <th>Сообщения</th>
                            <th>Создана</th>
                        </tr>
                    </thead>
                    <tbody>
    '''
    
    for link in links:
        owner = f"@{link[4]}" if link[4] else (html.escape(link[5]) if link[5] else f"ID:?")
        created = link[3].split()[0] if isinstance(link[3], str) else link[3].strftime("%Y-%m-%d")
        
        html_content += f'''
                        <tr>
                            <td><code>{link[0]}</code></td>
                            <td>{html.escape(link[1])}</td>
                            <td>{owner}</td>
                            <td><span class="badge badge-accent">{link[6]}</span></td>
                            <td>{created}</td>
                        </tr>
        '''
    
    html_content += '''
                    </tbody>
                </table>
            </div>
        </div>
        
        <!-- Последние сообщения -->
        <div class="section fade-in">
            <h2 class="section-title">
                <i class="fas fa-bolt"></i> ПОСЛЕДНИЕ СООБЩЕНИЯ
            </h2>
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Тип</th>
                            <th>От</th>
                            <th>Кому</th>
                            <th>Сообщение</th>
                            <th>Время</th>
                        </tr>
                    </thead>
                    <tbody>
    '''
    
    for msg in messages:
        msg_type_icon = {
            'text': '📝',
            'photo': '🖼️',
            'video': '🎥',
            'document': '📄',
            'voice': '🎤'
        }.get(msg[2], '📄')
        
        from_user = f"@{msg[6]}" if msg[6] else (html.escape(msg[7]) if msg[7] else "Аноним")
        to_user = f"@{msg[8]}" if msg[8] else (html.escape(msg[9]) if msg[9] else "Аноним")
        message_preview = html.escape(msg[1][:30] + '...' if msg[1] and len(msg[1]) > 30 else msg[1]) if msg[1] else f"Медиа: {msg[2]}"
        time_display = format_datetime(msg[5])
        
        html_content += f'''
                        <tr>
                            <td>{msg_type_icon}</td>
                            <td>{from_user}</td>
                            <td>{to_user}</td>
                            <td class="message-preview">{message_preview}</td>
                            <td>{time_display}</td>
                        </tr>
        '''
    
    html_content += '''
                    </tbody>
                </table>
            </div>
        </div>
        
        <!-- Эпичный футер -->
        <div class="epic-footer fade-in">
            <div class="footer-title">SIROK228 SYSTEMS</div>
            <div class="footer-subtitle">Мощная аналитика для анонимного бота</div>
            
            <div class="tech-stack">
                <div class="tech-item">Python 3.11</div>
                <div class="tech-item">SQLite3</div>
                <div class="tech-item">Telegram API</div>
                <div class="tech-item">GitHub Sync</div>
                <div class="tech-item">HTML5 + CSS3</div>
            </div>
            
            <div style="margin-top: 30px; color: var(--text-secondary);">
                <i class="fas fa-shield-alt"></i> Защищенная система | 
                <i class="fas fa-bolt"></i> Реальное время | 
                <i class="fas fa-chart-line"></i> Продвинутая аналитика
            </div>
        </div>
    </div>

    <script>
        // Анимации при прокрутке
        const observerOptions = {
            threshold: 0.1,
            rootMargin: '0px 0px -50px 0px'
        };
        
        const observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.style.opacity = '1';
                    entry.target.style.transform = 'translateY(0)';
                }
            });
        }, observerOptions);
        
        // Применяем анимации ко всем элементам
        document.querySelectorAll('.fade-in').forEach(el => {
            el.style.opacity = '0';
            el.style.transform = 'translateY(30px)';
            el.style.transition = 'all 0.8s ease-out';
            observer.observe(el);
        });
        
        // Случайные свечения для карточек
        setInterval(() => {
            const cards = document.querySelectorAll('.stat-card');
            const randomCard = cards[Math.floor(Math.random() * cards.length)];
            randomCard.classList.add('neon-border');
            setTimeout(() => {
                randomCard.classList.remove('neon-border');
            }, 2000);
        }, 3000);
        
        // Параллакс эффект для фона
        document.addEventListener('mousemove', (e) => {
            const moveX = (e.clientX - window.innerWidth / 2) * 0.01;
            const moveY = (e.clientY - window.innerHeight / 2) * 0.01;
            document.body.style.backgroundPosition = `calc(50% + ${moveX}px) calc(50% + ${moveY}px)`;
        });
    </script>
</body>
</html>
    '''
    
    return html_content

def main():
    if not all([BOT_TOKEN, ADMIN_ID]):
        logging.critical("КРИТИЧЕСКАЯ ОШИБКА: Не установлены обязательные переменные окружения BOT_TOKEN и ADMIN_ID")
        return
    
    # Инициализация репозитория и БД
    try:
        setup_repo()
        init_db()
    except Exception as e:
        logging.error(f"Ошибка при инициализации: {e}")
    
    # Создание приложения
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Добавление обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    media_filters = filters.PHOTO | filters.VIDEO | filters.VOICE | filters.Document.ALL
    application.add_handler(MessageHandler(media_filters & ~filters.COMMAND, handle_media))
    
    # Добавление обработчика ошибок
    application.add_error_handler(lambda update, context: logging.error(f"Exception: {context.error}"))
    
    logging.info("Бот запускается...")
    
    try:
        # Запуск бота
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            pool_timeout=20,
            read_timeout=20,
            connect_timeout=20
        )
    except Exception as e:
        logging.critical(f"Критическая ошибка при запуске бота: {e}")

if __name__ == "__main__":
    main()
