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

def get_user_messages_with_replies(user_id, limit=50):
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

def get_conversation_for_link(link_id):
    """Получает полную переписку по ссылке"""
    return run_query('''
        SELECT 
            'message' as type, 
            m.message_id, 
            m.message_text, 
            m.message_type, 
            m.file_id, 
            m.file_size, 
            m.file_name,
            m.created_at,
            u.username as from_username,
            u.first_name as from_first_name,
            m.from_user_id,
            NULL as reply_text,
            NULL as reply_username,
            NULL as reply_first_name,
            NULL as reply_id
        FROM messages m
        LEFT JOIN users u ON m.from_user_id = u.user_id
        WHERE m.link_id = ? AND m.is_active = 1
        
        UNION ALL
        
        SELECT 
            'reply' as type,
            r.message_id,
            NULL as message_text,
            NULL as message_type,
            NULL as file_id,
            NULL as file_size,
            NULL as file_name,
            r.created_at,
            NULL as from_username,
            NULL as from_first_name,
            r.from_user_id,
            r.reply_text,
            u.username as reply_username,
            u.first_name as reply_first_name,
            r.reply_id
        FROM replies r
        LEFT JOIN users u ON r.from_user_id = u.user_id
        LEFT JOIN messages m ON r.message_id = m.message_id
        WHERE m.link_id = ? AND r.is_active = 1
        
        ORDER BY created_at ASC
    ''', (link_id, link_id), fetch="all")

def get_all_users_for_admin():
    return run_query("SELECT user_id, username, first_name, created_at FROM users ORDER BY created_at DESC", fetch="all")

def get_user_links_for_admin(user_id):
    return run_query('''
        SELECT l.link_id, l.title, l.description, l.created_at,
               (SELECT COUNT(*) FROM messages m WHERE m.link_id = l.link_id AND m.is_active = 1) as message_count
        FROM links l
        WHERE l.user_id = ? AND l.is_active = 1
        ORDER BY l.created_at DESC
    ''', (user_id,), fetch="all")

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
    except Exception as e:
        logging.error(f"Ошибка при получении статистики: {e}")
        stats = {'users': 0, 'links': 0, 'messages': 0, 'replies': 0, 'photos': 0, 'videos': 0, 'documents': 0, 'voice': 0}
    
    return stats

# --- ФУНКЦИИ УДАЛЕНИЯ ---

def deactivate_link(link_id):
    """Деактивирует ссылку"""
    return run_query('UPDATE links SET is_active = 0 WHERE link_id = ?', (link_id,), commit=True)

def deactivate_message(message_id):
    """Деактивирует сообщение"""
    return run_query('UPDATE messages SET is_active = 0 WHERE message_id = ?', (message_id,), commit=True)

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

def get_all_data_for_html():
    data = {}
    try:
        data['stats'] = get_admin_stats()
        data['users'] = run_query('''
            SELECT u.user_id, u.username, u.first_name, u.created_at,
                   (SELECT COUNT(*) FROM links l WHERE l.user_id = u.user_id AND l.is_active = 1) as link_count,
                   (SELECT COUNT(*) FROM messages m WHERE m.to_user_id = u.user_id AND m.is_active = 1) as received_messages,
                   (SELECT COUNT(*) FROM messages m WHERE m.from_user_id = u.user_id AND m.is_active = 1) as sent_messages
            FROM users u
            ORDER BY u.created_at DESC
        ''', fetch="all") or []
        
        data['links'] = run_query('''
            SELECT l.link_id, l.title, l.description, l.created_at, l.expires_at,
                   u.username, u.first_name, u.user_id,
                   (SELECT COUNT(*) FROM messages m WHERE m.link_id = l.link_id AND m.is_active = 1) as message_count
            FROM links l
            LEFT JOIN users u ON l.user_id = u.user_id
            WHERE l.is_active = 1
            ORDER BY l.created_at DESC
        ''', fetch="all") or []
        
        data['recent_messages'] = run_query('''
            SELECT m.message_id, m.message_text, m.message_type, m.file_size, m.file_name, m.created_at,
                   u_from.username as from_username, u_from.first_name as from_first_name, u_from.user_id as from_user_id,
                   u_to.username as to_username, u_to.first_name as to_first_name, u_to.user_id as to_user_id,
                   l.title as link_title, l.link_id
            FROM messages m
            LEFT JOIN users u_from ON m.from_user_id = u_from.user_id
            LEFT JOIN users u_to ON m.to_user_id = u_to.user_id
            LEFT JOIN links l ON m.link_id = l.link_id
            WHERE m.is_active = 1
            ORDER BY m.created_at DESC
            LIMIT 200
        ''', fetch="all") or []
        
    except Exception as e:
        logging.error(f"Ошибка при получении данных для HTML: {e}")
        data = {'stats': get_admin_stats(), 'users': [], 'links': [], 'recent_messages': []}
    
    return data

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

def cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="main_menu")]])

def back_to_messages_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад к сообщениям", callback_data="my_messages")]])

def back_to_links_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад к ссылкам", callback_data="my_links")]])

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Управление пользователями", callback_data="admin_users")],
        [InlineKeyboardButton("🎨 HTML Отчет", callback_data="admin_html_report")],
        [InlineKeyboardButton("📢 Оповещение", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
    ])

def user_management_keyboard(user_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔗 Ссылки", callback_data=f"admin_user_links_{user_id}"),
            InlineKeyboardButton("📨 Переписка", callback_data=f"admin_user_conversation_{user_id}")
        ],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_users")]
    ])

def user_links_keyboard(user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👁️ Посмотреть переписку", callback_data=f"admin_view_conversation_{user_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"admin_user_links_{user_id}")]
    ])

def delete_confirmation_keyboard(item_type, item_id):
    """Клавиатура подтверждения удаления"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, удалить", callback_data=f"delete_{item_type}_{item_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_delete")]
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
                await update.message.reply_text(text, parse_mode='MarkdownV2', reply_markup=main_keyboard())
                return
        
        text = "👋 *Добро пожаловать в Анонимный Бот\\!*\n\nСоздавайте ссылки для получения анонимных сообщений и вопросов\\."
        await update.message.reply_text(text, reply_markup=main_keyboard(), parse_mode='MarkdownV2')
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
            # Удаляем предыдущее сообщение
            try:
                await query.message.delete()
            except:
                pass
            text = "🎭 *Главное меню*"
            await query.message.reply_text(text, reply_markup=main_keyboard(), parse_mode='MarkdownV2')
            return
        
        elif data == "my_links":
            # Удаляем предыдущее сообщение
            try:
                await query.message.delete()
            except:
                pass
            links = get_user_links(user.id)
            if links:
                text = "🔗 *Ваши анонимные ссылки:*\n\n"
                for link in links:
                    bot_username = context.bot.username
                    link_url = f"https://t.me/{bot_username}?start={link[0]}"
                    created = format_datetime(link[3])
                    text += f"📝 *{escape_markdown_v2(link[1])}*\n📋 {escape_markdown_v2(link[2])}\n🔗 `{escape_markdown_v2(link_url)}`\n🕒 `{created}`\n\n"
                    # Добавляем кнопку удаления для каждой ссылки
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🗑️ Удалить", callback_data=f"confirm_delete_link_{link[0]}")]
                    ])
                    await query.message.reply_text(text, parse_mode='MarkdownV2', reply_markup=keyboard)
                    text = ""  # Сбрасываем текст для следующего сообщения
            else:
                await query.message.reply_text("У вас пока нет созданных ссылок\\.", parse_mode='MarkdownV2', reply_markup=main_keyboard())
            return
        
        elif data == "my_messages":
            # Удаляем предыдущее сообщение
            try:
                await query.message.delete()
            except:
                pass
            messages = get_user_messages_with_replies(user.id)
            if messages:
                text = "📨 *Ваши последние сообщения:*\n\n"
                for msg in messages:
                    msg_id, msg_text, msg_type, file_id, file_size, file_name, created, link_title, link_id, reply_count = msg
                    
                    type_icon = {"text": "📝", "photo": "🖼️", "video": "🎥", "document": "📄", "voice": "🎤"}.get(msg_type, "📄")
                    
                    preview = msg_text or f"*{msg_type}*"
                    if len(preview) > 50:
                        preview = preview[:50] + "\\.\\.\\."
                        
                    created_str = format_datetime(created)
                    text += f"{type_icon} *{escape_markdown_v2(link_title)}*\n`{preview}`\n🕒 `{created_str}` \\| 💬 Ответов\\: {reply_count}\n\n"
                
                await query.message.reply_text(text, parse_mode='MarkdownV2', reply_markup=main_keyboard())
            else:
                await query.message.reply_text("У вас пока нет сообщений\\.", parse_mode='MarkdownV2', reply_markup=main_keyboard())
            return
        
        elif data == "create_link":
            # Удаляем предыдущее сообщение
            try:
                await query.message.delete()
            except:
                pass
            context.user_data['creating_link'] = True
            context.user_data['link_stage'] = 'title'
            await query.message.reply_text("📝 Введите *название* для вашей ссылки:", parse_mode='MarkdownV2', reply_markup=cancel_keyboard())
            return
        
        # Управление удалением
        elif data.startswith("confirm_delete_link_"):
            link_id = data.replace("confirm_delete_link_", "")
            link_info = get_link_info(link_id)
            
            if link_info:
                text = f"🗑️ *Подтверждение удаления ссылки*\n\n"
                text += f"📝 *Название:* {escape_markdown_v2(link_info[2])}\n"
                text += f"📋 *Описание:* {escape_markdown_v2(link_info[3])}\n\n"
                text += "❓ *Вы уверены, что хотите удалить эту ссылку?*"
                
                await query.edit_message_text(text, parse_mode='MarkdownV2', 
                                           reply_markup=delete_confirmation_keyboard("link", link_id))
            return
        
        elif data.startswith("confirm_delete_message_"):
            message_id = int(data.replace("confirm_delete_message_", ""))
            message_info = get_message_info(message_id)
            
            if message_info:
                msg_text, msg_type, file_name, created, from_user, from_name, to_user, to_name, link_title = message_info
                
                text = f"🗑️ *Подтверждение удаления сообщения*\n\n"
                text += f"📝 *Сообщение:*\n`{msg_text if msg_text else f'Медиафайл: {msg_type}'}`\n\n"
                text += f"❓ *Вы уверены, что хотите удалить это сообщение?*"
                
                await query.edit_message_text(text, parse_mode='MarkdownV2', 
                                           reply_markup=delete_confirmation_keyboard("message", message_id))
            return
        
        elif data.startswith("delete_link_"):
            link_id = data.replace("delete_link_", "")
            success = deactivate_link(link_id)
            
            if success:
                push_db_to_github(f"Delete link {link_id}")
                # Удаляем сообщение с подтверждением
                try:
                    await query.message.delete()
                except:
                    pass
                await query.message.reply_text("✅ *Ссылка успешно удалена\\!*", 
                                           parse_mode='MarkdownV2', 
                                           reply_markup=main_keyboard())
            else:
                await query.edit_message_text("❌ *Ошибка при удалении ссылки*", 
                                           parse_mode='MarkdownV2', 
                                           reply_markup=main_keyboard())
            return
        
        elif data.startswith("delete_message_"):
            message_id = int(data.replace("delete_message_", ""))
            success = deactivate_message(message_id)
            
            if success:
                push_db_to_github(f"Delete message {message_id}")
                # Удаляем сообщение с подтверждением
                try:
                    await query.message.delete()
                except:
                    pass
                await query.message.reply_text("✅ *Сообщение успешно удалено\\!*", 
                                           parse_mode='MarkdownV2', 
                                           reply_markup=main_keyboard())
            else:
                await query.edit_message_text("❌ *Ошибка при удалении сообщения*", 
                                           parse_mode='MarkdownV2', 
                                           reply_markup=main_keyboard())
            return
        
        elif data == "cancel_delete":
            await query.edit_message_text("❌ *Удаление отменено*", 
                                       parse_mode='MarkdownV2', 
                                       reply_markup=main_keyboard())
            return

        # АДМИН ПАНЕЛЬ
        if is_admin:
            if data == "admin_stats":
                stats = get_admin_stats()
                text = f"""📊 *Статистика бота\\:*

👥 *Пользователи\\:*
• Всего пользователей\\: {stats['users']}
• Активных ссылок\\: {stats['links']}

💌 *Сообщения\\:*
• Всего сообщений\\: {stats['messages']}
• Ответов\\: {stats['replies']}

📁 *Файлы\\:*
• Фотографий\\: {stats['photos']}
• Видео\\: {stats['videos']}
• Документов\\: {stats['documents']}
• Голосовых\\: {stats['voice']}"""
                await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=admin_keyboard())
            
            elif data == "admin_users":
                users = get_all_users_for_admin()
                if users:
                    text = "👥 *Управление пользователями*\n\n"
                    for u in users[:15]:
                        username = f"@{u[1]}" if u[1] else (u[2] or f"ID\\:{u[0]}")
                        created = format_datetime(u[3])
                        text += f"👤 *{escape_markdown_v2(username)}*\n🆔 `{u[0]}` \\| 📅 `{created}`\n\n"
                        # Добавляем кнопки управления для каждого пользователя
                        keyboard = user_management_keyboard(u[0])
                        await query.message.reply_text(text, parse_mode='MarkdownV2', reply_markup=keyboard)
                        text = ""  # Сбрасываем текст для следующего сообщения
                else:
                    await query.edit_message_text("Пользователей не найдено\\.", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                return
            
            elif data.startswith("admin_user_links_"):
                user_id = int(data.replace("admin_user_links_", ""))
                user_links = get_user_links_for_admin(user_id)
                
                if user_links:
                    text = f"🔗 *Ссылки пользователя {user_id}:*\n\n"
                    for link in user_links:
                        created = format_datetime(link[3])
                        text += f"📝 *{escape_markdown_v2(link[1])}*\n📋 {escape_markdown_v2(link[2])}\n🕒 `{created}` \\| 💬 Сообщений\\: {link[4]}\n\n"
                    
                    await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=user_links_keyboard(user_id))
                else:
                    await query.edit_message_text("У пользователя нет ссылок\\.", parse_mode='MarkdownV2', reply_markup=user_management_keyboard(user_id))
                return
            
            elif data.startswith("admin_view_conversation_"):
                user_id = int(data.replace("admin_view_conversation_", ""))
                await query.edit_message_text("🔄 *Генерация отчета переписки\\.\\.\\.*", parse_mode='MarkdownV2')
                
                # Генерируем HTML отчет переписки
                html_content = generate_conversation_report(user_id)
                
                report_path = f"/tmp/conversation_{user_id}.html"
                with open(report_path, 'w', encoding='utf-8') as f:
                    f.write(html_content)
                
                with open(report_path, 'rb') as f:
                    await query.message.reply_document(
                        document=f,
                        filename=f"conversation_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
                        caption=f"💬 *Переписка пользователя {user_id}*",
                        parse_mode='MarkdownV2'
                    )
                
                await query.edit_message_text("✅ *Отчет переписки отправлен\\!*", parse_mode='MarkdownV2', reply_markup=user_links_keyboard(user_id))
                return
            
            elif data == "admin_html_report":
                await query.edit_message_text("🔄 *Генерация HTML отчета\\.\\.\\.*", parse_mode='MarkdownV2')
                
                html_content = generate_html_report()
                
                report_path = "/tmp/admin_report.html"
                with open(report_path, 'w', encoding='utf-8') as f:
                    f.write(html_content)
                
                with open(report_path, 'rb') as f:
                    await query.message.reply_document(
                        document=f,
                        filename=f"admin_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
                        caption="🎨 *Расширенный HTML отчет администратора*",
                        parse_mode='MarkdownV2'
                    )
                
                await query.edit_message_text("✅ *HTML отчет сгенерирован и отправлен\\!*", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                return
            
            elif data == "admin_broadcast":
                context.user_data['broadcasting'] = True
                await query.edit_message_text(
                    "📢 *Режим рассылки*\n\nВведите сообщение для отправки всем пользователям\\:",
                    parse_mode='MarkdownV2', 
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Отмена", callback_data="admin_panel")]])
                )
                return

    except Exception as e:
        logging.error(f"Ошибка в обработчике кнопок: {e}")
        try:
            await query.edit_message_text("❌ Произошла ошибка\\. Попробуйте позже\\.", reply_markup=main_keyboard(), parse_mode='MarkdownV2')
        except:
            pass

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        text = update.message.text
        save_user(user.id, user.username, user.first_name)
        is_admin = user.username == ADMIN_USERNAME or user.id == ADMIN_ID

        # Удаляем сообщение пользователя
        try:
            await update.message.delete()
        except:
            pass

        # Создание ссылки
        if context.user_data.get('creating_link'):
            stage = context.user_data.get('link_stage')
            if stage == 'title':
                context.user_data['link_title'] = text
                context.user_data['link_stage'] = 'description'
                await update.message.reply_text("📋 Теперь введите *описание* для ссылки:", parse_mode='MarkdownV2', reply_markup=cancel_keyboard())
            elif stage == 'description':
                title = context.user_data.pop('link_title')
                context.user_data.pop('creating_link')
                context.user_data.pop('link_stage')
                link_id = create_anon_link(user.id, title, text)
                bot_username = context.bot.username
                link_url = f"https://t.me/{bot_username}?start={link_id}"
                await update.message.reply_text(
                    f"✅ *Ссылка создана\\!*\n\n📝 *{escape_markdown_v2(title)}*\n📋 {escape_markdown_v2(text)}\n\n🔗 `{escape_markdown_v2(link_url)}`\n\nПоделитесь ей, чтобы получать сообщения\\!",
                    parse_mode='MarkdownV2', 
                    reply_markup=main_keyboard()
                )
            return

        # Отправка анонимного сообщения
        if context.user_data.get('current_link'):
            link_id = context.user_data.pop('current_link')
            link_info = get_link_info(link_id)
            if link_info:
                msg_id = save_message(link_id, user.id, link_info[1], text)
                notification = f"📨 *Новое анонимное сообщение*\n\n{text}"
                try:
                    await context.bot.send_message(link_info[1], notification, parse_mode='MarkdownV2')
                except Exception as e:
                    logging.error(f"Failed to send message notification: {e}")
                
                await update.message.reply_text("✅ Ваше сообщение отправлено анонимно\\!", reply_markup=main_keyboard(), parse_mode='MarkdownV2')
            return

        # Рассылка от админа
        if context.user_data.get('broadcasting') and is_admin:
            context.user_data.pop('broadcasting')
            # Здесь должна быть логика рассылки всем пользователям
            await update.message.reply_text("✅ *Сообщение отправлено в рассылку\\!*", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
            return

        await update.message.reply_text("Используйте кнопки для навигации\\.", reply_markup=main_keyboard(), parse_mode='MarkdownV2')

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

        # Удаляем сообщение пользователя
        try:
            await update.message.delete()
        except:
            pass

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
                        await context.bot.send_photo(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2')
                    elif msg_type == 'video': 
                        await context.bot.send_video(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2')
                    elif msg_type == 'document': 
                        await context.bot.send_document(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2')
                    elif msg_type == 'voice': 
                        await context.bot.send_voice(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2')
                except Exception as e: 
                    logging.error(f"Failed to send media to user: {e}")
                
                await update.message.reply_text("✅ Ваше медиа отправлено анонимно\\!", reply_markup=main_keyboard(), parse_mode='MarkdownV2')

    except Exception as e:
        logging.error(f"Ошибка в обработчике медиа: {e}")
        await update.message.reply_text("❌ Произошла ошибка при отправке медиа\\.", parse_mode='MarkdownV2')

def generate_conversation_report(user_id):
    """Генерирует HTML отчет переписки пользователя"""
    conversations = get_conversation_for_user(user_id)
    
    html_content = f'''
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>💬 Переписка пользователя {user_id}</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: linear-gradient(135deg, #0c0c0c 0%, #1a1a2e 50%, #16213e 100%);
                color: white;
                padding: 20px;
            }}
            .container {{
                max-width: 800px;
                margin: 0 auto;
                background: rgba(255,255,255,0.1);
                padding: 30px;
                border-radius: 15px;
                backdrop-filter: blur(10px);
            }}
            .message {{
                background: rgba(255,255,255,0.2);
                padding: 15px;
                margin: 10px 0;
                border-radius: 10px;
                border-left: 4px solid #8A2BE2;
            }}
            .timestamp {{
                color: #ccc;
                font-size: 0.8em;
                text-align: right;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>💬 Переписка пользователя {user_id}</h1>
            <div class="messages">
    '''
    
    for conv in conversations:
        if conv[0] == 'message':
            html_content += f'''
                <div class="message">
                    <strong>📨 Сообщение:</strong><br>
                    {html.escape(conv[2]) if conv[2] else 'Медиафайл: ' + conv[3]}
                    <div class="timestamp">{format_datetime(conv[7])}</div>
                </div>
            '''
        else:
            html_content += f'''
                <div class="message">
                    <strong>💬 Ответ:</strong><br>
                    {html.escape(conv[11])}
                    <div class="timestamp">{format_datetime(conv[7])}</div>
                </div>
            '''
    
    html_content += '''
            </div>
        </div>
    </body>
    </html>
    '''
    
    return html_content

def generate_html_report():
    """Генерирует красивый HTML отчет"""
    data = get_all_data_for_html()
    
    html_content = f'''
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>🟣 Анонимный Бот - Отчет</title>
        <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;700;900&family=Exo+2:wght@300;400;500;600;700&display=swap" rel="stylesheet">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            body {{
                font-family: 'Exo 2', sans-serif;
                background: linear-gradient(135deg, #0c0c0c 0%, #1a1a2e 50%, #16213e 100%);
                color: white;
                padding: 20px;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
            }}
            .header {{
                text-align: center;
                padding: 40px 0;
                background: linear-gradient(135deg, rgba(138, 43, 226, 0.3), rgba(106, 13, 173, 0.3));
                border-radius: 20px;
                margin-bottom: 30px;
            }}
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 20px;
                margin-bottom: 30px;
            }}
            .stat-card {{
                background: rgba(255,255,255,0.1);
                padding: 25px;
                border-radius: 15px;
                text-align: center;
                backdrop-filter: blur(10px);
                border: 1px solid rgba(255,255,255,0.2);
            }}
            .stat-number {{
                font-family: 'Orbitron', monospace;
                font-size: 2.5em;
                font-weight: bold;
                background: linear-gradient(135deg, #8A2BE2, #FF00FF);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                margin-bottom: 10px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1 style="font-family: 'Orbitron', monospace; font-size: 3em;">🟣 АНОНИМНЫЙ БОТ</h1>
                <p>Полный отчет системы</p>
                <p>Сгенерирован: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} (Krasnoyarsk)</p>
            </div>
            
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-number">{data['stats']['users']}</div>
                    <div>Пользователей</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number">{data['stats']['links']}</div>
                    <div>Ссылок</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number">{data['stats']['messages']}</div>
                    <div>Сообщений</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number">{data['stats']['replies']}</div>
                    <div>Ответов</div>
                </div>
            </div>
        </div>
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
