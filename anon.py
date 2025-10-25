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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_banned BOOLEAN DEFAULT 0,
                ban_reason TEXT DEFAULT NULL
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
                is_sponsor BOOLEAN DEFAULT 0,
                sponsor_owner_id INTEGER DEFAULT NULL,
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
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_messages (
                admin_message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_admin_id INTEGER,
                to_user_id INTEGER,
                message_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

def create_anon_link(user_id, title, description, is_sponsor=False, sponsor_owner_id=None):
    link_id = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
    expires_at = datetime.now() + timedelta(days=365)
    run_query('INSERT INTO links (link_id, user_id, title, description, expires_at, is_sponsor, sponsor_owner_id) VALUES (?, ?, ?, ?, ?, ?, ?)', 
              (link_id, user_id, title, description, expires_at, is_sponsor, sponsor_owner_id), commit=True)
    push_db_to_github(f"Create link for user {user_id}")
    return link_id

def save_message(link_id, from_user_id, to_user_id, message_text, message_type='text', file_id=None, file_size=None, file_name=None):
    message_id = run_query(
        'INSERT INTO messages (link_id, from_user_id, to_user_id, message_text, message_type, file_id, file_size, file_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', 
        (link_id, from_user_id, to_user_id, message_text, message_type, file_id, file_size, file_name), 
        commit=True
    )
    push_db_to_github(f"Save message from {from_user_id} to {to_user_id}")
    return message_id

def save_reply(message_id, from_user_id, reply_text):
    run_query('INSERT INTO replies (message_id, from_user_id, reply_text) VALUES (?, ?, ?)', 
              (message_id, from_user_id, reply_text), commit=True)
    push_db_to_github(f"Save reply to message {message_id}")

def save_admin_message(from_admin_id, to_user_id, message_text):
    run_query('INSERT INTO admin_messages (from_admin_id, to_user_id, message_text) VALUES (?, ?, ?)', 
              (from_admin_id, to_user_id, message_text), commit=True)
    push_db_to_github(f"Save admin message to user {to_user_id}")

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

def get_conversation_for_user(user_id):
    """Получает полную переписку пользователя"""
    return run_query('''
        SELECT 
            m.message_id,
            m.message_text,
            m.message_type,
            m.file_id,
            m.file_size,
            m.file_name,
            m.created_at,
            u_from.username as from_username,
            u_from.first_name as from_first_name,
            m.from_user_id,
            u_to.username as to_username,
            u_to.first_name as to_first_name,
            m.to_user_id,
            l.title as link_title,
            l.link_id,
            r.reply_text,
            r.reply_id,
            u_reply.username as reply_username,
            u_reply.first_name as reply_first_name,
            CASE 
                WHEN r.reply_id IS NOT NULL THEN 'reply'
                ELSE 'message'
            END as type
        FROM messages m
        LEFT JOIN users u_from ON m.from_user_id = u_from.user_id
        LEFT JOIN users u_to ON m.to_user_id = u_to.user_id
        LEFT JOIN links l ON m.link_id = l.link_id
        LEFT JOIN replies r ON m.message_id = r.message_id AND r.is_active = 1
        LEFT JOIN users u_reply ON r.from_user_id = u_reply.user_id
        WHERE (m.from_user_id = ? OR m.to_user_id = ? OR r.from_user_id = ?) 
          AND m.is_active = 1
        ORDER BY m.created_at ASC, r.created_at ASC
    ''', (user_id, user_id, user_id), fetch="all")

def get_all_users_for_admin():
    result = run_query("SELECT user_id, username, first_name, created_at, is_banned, ban_reason FROM users ORDER BY created_at DESC", fetch="all")
    return result or []

def get_user_links_for_admin(user_id):
    result = run_query('''
        SELECT l.link_id, l.title, l.description, l.created_at,
               (SELECT COUNT(*) FROM messages m WHERE m.link_id = l.link_id AND m.is_active = 1) as message_count
        FROM links l
        WHERE l.user_id = ? AND l.is_active = 1
        ORDER BY l.created_at DESC
    ''', (user_id,), fetch="all")
    return result or []

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
        
        stats['banned'] = run_query("SELECT COUNT(*) FROM users WHERE is_banned = 1", fetch="one")[0] or 0
        stats['sponsor_links'] = run_query("SELECT COUNT(*) FROM links WHERE is_sponsor = 1", fetch="one")[0] or 0
    except Exception as e:
        logging.error(f"Ошибка при получении статистики: {e}")
        stats = {'users': 0, 'links': 0, 'messages': 0, 'replies': 0, 'photos': 0, 'videos': 0, 'documents': 0, 'voice': 0, 'banned': 0, 'sponsor_links': 0}
    
    return stats

# --- НОВЫЕ ФУНКЦИИ ДЛЯ УПРАВЛЕНИЯ ПОЛЬЗОВАТЕЛЯМИ ---

def ban_user(user_id, reason=None):
    """Блокирует пользователя"""
    return run_query('UPDATE users SET is_banned = 1, ban_reason = ? WHERE user_id = ?', 
                    (reason, user_id), commit=True)

def unban_user(user_id):
    """Разблокирует пользователя"""
    return run_query('UPDATE users SET is_banned = 0, ban_reason = NULL WHERE user_id = ?', 
                    (user_id,), commit=True)

def delete_user(user_id):
    """Полностью удаляет пользователя и все его данные"""
    try:
        # Получаем все ссылки пользователя
        user_links = get_user_links_for_admin(user_id)
        
        # Удаляем все ссылки пользователя
        for link in user_links:
            delete_link_completely(link[0])
        
        # Удаляем сообщения пользователя
        run_query('DELETE FROM messages WHERE from_user_id = ? OR to_user_id = ?', (user_id, user_id), commit=True)
        
        # Удаляем ответы пользователя
        run_query('DELETE FROM replies WHERE from_user_id = ?', (user_id,), commit=True)
        
        # Удаляем пользователя
        run_query('DELETE FROM users WHERE user_id = ?', (user_id,), commit=True)
        
        push_db_to_github(f"Completely delete user {user_id}")
        return True
    except Exception as e:
        logging.error(f"Ошибка при удалении пользователя: {e}")
        return False

def is_user_banned(user_id):
    """Проверяет, забанен ли пользователь"""
    result = run_query('SELECT is_banned FROM users WHERE user_id = ?', (user_id,), fetch="one")
    return result and result[0] == 1

def create_sponsor_link(admin_id, title, description, target_user_id=None):
    """Создает спонсорскую ссылку"""
    return create_anon_link(target_user_id or admin_id, title, description, is_sponsor=True, sponsor_owner_id=admin_id)

def get_sponsor_links(admin_id):
    """Получает спонсорские ссылки админа"""
    return run_query('SELECT link_id, title, description, created_at, user_id FROM links WHERE is_sponsor = 1 AND sponsor_owner_id = ?', (admin_id,), fetch="all")

def transfer_sponsor_link(link_id, new_user_id):
    """Передает спонсорскую ссылку другому пользователю"""
    return run_query('UPDATE links SET user_id = ? WHERE link_id = ?', (new_user_id, link_id), commit=True)

# --- ФУНКЦИИ УДАЛЕНИЯ ---

def delete_link_completely(link_id):
    """Полностью удаляет ссылку и все связанные данные"""
    try:
        # Удаляем ответы
        run_query('''
            DELETE FROM replies 
            WHERE message_id IN (SELECT message_id FROM messages WHERE link_id = ?)
        ''', (link_id,), commit=True)
        
        # Удаляем сообщения
        run_query('DELETE FROM messages WHERE link_id = ?', (link_id,), commit=True)
        
        # Удаляем ссылку
        run_query('DELETE FROM links WHERE link_id = ?', (link_id,), commit=True)
        
        push_db_to_github(f"Completely delete link {link_id}")
        return True
    except Exception as e:
        logging.error(f"Ошибка при удалении ссылки: {e}")
        return False

def delete_message_completely(message_id):
    """Полностью удаляет сообщение и ответы"""
    try:
        # Удаляем ответы
        run_query('DELETE FROM replies WHERE message_id = ?', (message_id,), commit=True)
        
        # Удаляем сообщение
        run_query('DELETE FROM messages WHERE message_id = ?', (message_id,), commit=True)
        
        push_db_to_github(f"Completely delete message {message_id}")
        return True
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщения: {e}")
        return False

def get_message_info(message_id):
    """Получает информацию о сообщении"""
    return run_query('''
        SELECT m.message_text, m.message_type, m.file_name, m.created_at, 
               u_from.username as from_username, u_from.first_name as from_first_name,
               u_to.username as to_username, u_to.first_name as to_first_name,
               l.title as link_title, l.link_id
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
            SELECT u.user_id, u.username, u.first_name, u.created_at, u.is_banned, u.ban_reason,
                   (SELECT COUNT(*) FROM links l WHERE l.user_id = u.user_id AND l.is_active = 1) as link_count,
                   (SELECT COUNT(*) FROM messages m WHERE m.to_user_id = u.user_id AND m.is_active = 1) as received_messages,
                   (SELECT COUNT(*) FROM messages m WHERE m.from_user_id = u.user_id AND m.is_active = 1) as sent_messages
            FROM users u
            ORDER BY u.created_at DESC
        ''', fetch="all") or []
        
        data['links'] = run_query('''
            SELECT l.link_id, l.title, l.description, l.created_at, l.expires_at, l.is_sponsor,
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
        
        # Новые данные для детального отчета
        data['conversations'] = run_query('''
            SELECT l.link_id, l.title, l.description, l.created_at,
                   u.username, u.first_name, u.user_id,
                   COUNT(m.message_id) as message_count,
                   MAX(m.created_at) as last_activity
            FROM links l
            LEFT JOIN users u ON l.user_id = u.user_id
            LEFT JOIN messages m ON l.link_id = m.link_id AND m.is_active = 1
            WHERE l.is_active = 1
            GROUP BY l.link_id
            ORDER BY last_activity DESC
        ''', fetch="all") or []
        
        data['detailed_messages'] = run_query('''
            SELECT m.message_id, m.message_text, m.message_type, m.file_size, m.file_name, m.created_at,
                   u_from.username as from_username, u_from.first_name as from_first_name, u_from.user_id as from_user_id,
                   u_to.username as to_username, u_to.first_name as to_first_name, u_to.user_id as to_user_id,
                   l.title as link_title, l.link_id,
                   (SELECT COUNT(*) FROM replies r WHERE r.message_id = m.message_id AND r.is_active = 1) as reply_count
            FROM messages m
            LEFT JOIN users u_from ON m.from_user_id = u_from.user_id
            LEFT JOIN users u_to ON m.to_user_id = u_to.user_id
            LEFT JOIN links l ON m.link_id = l.link_id
            WHERE m.is_active = 1
            ORDER BY m.created_at DESC
            LIMIT 500
        ''', fetch="all") or []
        
    except Exception as e:
        logging.error(f"Ошибка при получении данных для HTML: {e}")
        data = {'stats': get_admin_stats(), 'users': [], 'links': [], 'recent_messages': [], 'conversations': [], 'detailed_messages': []}
    
    return data

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def safe_int(value, default=0):
    """Безопасное преобразование в int"""
    try:
        if value is None:
            return default
        return int(value)
    except (ValueError, TypeError):
        return default

def safe_str(value, default=""):
    """Безопасное преобразование в строку"""
    if value is None:
        return default
    return str(value)

def escape_markdown_v2(text: str) -> str:
    """Экранирует специальные символы для MarkdownV2"""
    if text is None:
        return ""
    
    # Преобразуем в строку на случай если пришел не строковый тип
    text = str(text)
    
    if not text.strip():
        return text
    
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def format_datetime(dt_string):
    """Форматирует дату-время с точностью до секунд (Красноярское время UTC+7)"""
    if isinstance(dt_string, str):
        try:
            # Пробуем разные форматы даты
            for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f']:
                try:
                    dt = datetime.strptime(dt_string, fmt)
                    break
                except ValueError:
                    continue
            else:
                return dt_string
        except:
            return dt_string
    elif isinstance(dt_string, datetime):
        dt = dt_string
    else:
        return str(dt_string)
    
    # Добавляем 7 часов для Красноярского времени
    krasnoyarsk_time = dt + timedelta(hours=7)
    return krasnoyarsk_time.strftime("%Y-%m-%d %H:%M:%S") + " (Krasnoyarsk)"

def parse_formatting(text):
    """Простое форматирование текста для Telegram"""
    if not text:
        return text
    
    # Простое форматирование без сложных преобразований
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.*?)__', r'<b>\1</b>', text)
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    text = re.sub(r'_(.*?)_', r'<i>\1</i>', text)
    text = re.sub(r'~~(.*?)~~', r'<s>\1</s>', text)
    text = re.sub(r'`(.*?)`', r'<code>\1</code>', text)
    text = re.sub(r'\|\|(.*?)\|\|', r'<spoiler>\1</spoiler>', text)
    
    # Обрабатываем цитаты безопасно
    text = re.sub(r'>>(.*?)(?=\n|$)', r'<blockquote>\1</blockquote>', text)
    text = re.sub(r'>>>(.*?)(?=\n|$)', r'<blockquote>\1</blockquote>', text)
    
    # Экранируем только опасные символы, но сохраняем теги
    text = text.replace('<', '&lt;').replace('>', '&gt;')
    
    # Восстанавливаем безопасные теги Telegram
    text = text.replace('&lt;b&gt;', '<b>').replace('&lt;/b&gt;', '</b>')
    text = text.replace('&lt;i&gt;', '<i>').replace('&lt;/i&gt;', '</i>')
    text = text.replace('&lt;s&gt;', '<s>').replace('&lt;/s&gt;', '</s>')
    text = text.replace('&lt;code&gt;', '<code>').replace('&lt;/code&gt;', '</code>')
    text = text.replace('&lt;spoiler&gt;', '<spoiler>').replace('&lt;/spoiler&gt;', '</spoiler>')
    text = text.replace('&lt;blockquote&gt;', '<blockquote>').replace('&lt;/blockquote&gt;', '</blockquote>')
    
    return text

def escape_html_safe(text):
    """Безопасное экранирование HTML"""
    if not text:
        return ""
    
    # Простое экранирование
    text = html.escape(text)
    
    # Восстанавливаем форматирование
    text = re.sub(r'&lt;b&gt;(.*?)&lt;/b&gt;', r'<b>\1</b>', text)
    text = re.sub(r'&lt;i&gt;(.*?)&lt;/i&gt;', r'<i>\1</i>', text)
    text = re.sub(r'&lt;s&gt;(.*?)&lt;/s&gt;', r'<s>\1</s>', text)
    text = re.sub(r'&lt;code&gt;(.*?)&lt;/code&gt;', r'<code>\1</code>', text)
    text = re.sub(r'&lt;spoiler&gt;(.*?)&lt;/spoiler&gt;', r'<spoiler>\1</spoiler>', text)
    text = re.sub(r'&lt;blockquote&gt;(.*?)&lt;/blockquote&gt;', r'<blockquote>\1</blockquote>', text)
    
    return text

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

def back_to_main_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]])

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Управление пользователями", callback_data="admin_users")],
        [InlineKeyboardButton("🔗 Спонсорские ссылки", callback_data="admin_sponsor_links")],
        [InlineKeyboardButton("🎨 HTML Отчет", callback_data="admin_html_report")],
        [InlineKeyboardButton("📢 Оповещение", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
    ])

def user_management_keyboard(user_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔗 Ссылки", callback_data=f"admin_user_links_{user_id}"),
            InlineKeyboardButton("📨 Переписка", callback_data=f"admin_view_conversation_{user_id}")
        ],
        [
            InlineKeyboardButton("🚫 Забанить", callback_data=f"admin_ban_user_{user_id}"),
            InlineKeyboardButton("✅ Разбанить", callback_data=f"admin_unban_user_{user_id}")
        ],
        [
            InlineKeyboardButton("🗑️ Удалить", callback_data=f"admin_delete_user_{user_id}"),
            InlineKeyboardButton("✉️ Написать", callback_data=f"admin_message_user_{user_id}")
        ],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_users")]
    ])

def message_actions_keyboard(message_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Ответить", callback_data=f"reply_{message_id}")],
        [InlineKeyboardButton("🗑️ Удалить", callback_data=f"confirm_delete_message_{message_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="my_messages")]
    ])

def delete_confirmation_keyboard(item_type, item_id):
    """Клавиатура подтверждения удаления"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, удалить", callback_data=f"delete_{item_type}_{item_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_delete")]
    ])

def broadcast_formatting_keyboard():
    """Клавиатура форматирования для рассылки"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Жирный **текст**", callback_data="format_bold"),
            InlineKeyboardButton("Курсив *текст*", callback_data="format_italic")
        ],
        [
            InlineKeyboardButton("Зачеркивание ~~текст~~", callback_data="format_strike"),
            InlineKeyboardButton("Скрытый ||текст||", callback_data="format_spoiler")
        ],
        [
            InlineKeyboardButton("Моноширинный `текст`", callback_data="format_code"),
            InlineKeyboardButton("Цитата >>текст", callback_data="format_quote")
        ],
        [
            InlineKeyboardButton("✅ Отправить", callback_data="broadcast_send"),
            InlineKeyboardButton("❌ Отмена", callback_data="admin_panel")
        ]
    ])

def sponsor_links_keyboard():
    """Клавиатура для управления спонсорскими ссылками"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Создать спонсорскую ссылку", callback_data="admin_create_sponsor_link")],
        [InlineKeyboardButton("📋 Мои спонсорские ссылки", callback_data="admin_my_sponsor_links")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")]
    ])

def sponsor_link_actions_keyboard(link_id):
    """Клавиатура действий для спонсорской ссылки"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Передать ссылку", callback_data=f"admin_transfer_sponsor_{link_id}")],
        [InlineKeyboardButton("🗑️ Удалить ссылку", callback_data=f"admin_delete_sponsor_{link_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_my_sponsor_links")]
    ])

# --- ОСНОВНЫЕ ОБРАБОТЧИКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        
        # Проверяем, не забанен ли пользователь
        if is_user_banned(user.id):
            await update.message.reply_text("❌ Вы заблокированы в этом боте и не можете использовать его функции.")
            return
        
        save_user(user.id, user.username, user.first_name)
        
        if context.args:
            link_id = context.args[0]
            link_info = get_link_info(link_id)
            if link_info:
                context.user_data['current_link'] = link_id
                text = f"🔗 *Анонимная ссылка*\n\n📝 *{escape_markdown_v2(link_info[2])}*\n📋 {escape_markdown_v2(link_info[3])}\n\n✍️ Напишите анонимное сообщение или отправьте медиафайл\\."
                await update.message.reply_text(text, parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
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
            # Проверяем пароль, если он еще не введен
            if not context.user_data.get('admin_authenticated'):
                if context.args and context.args[0] == ADMIN_PASSWORD:
                    context.user_data['admin_authenticated'] = True
                    # Удаляем сообщение с паролем
                    try:
                        await update.message.delete()
                    except:
                        pass
                    await update.message.reply_text(
                        "✅ *Добро пожаловать в панель администратора*",
                        reply_markup=admin_keyboard(),
                        parse_mode='MarkdownV2'
                    )
                else:
                    # Не показываем информацию о пароле, просто говорим что доступ запрещен
                    await update.message.reply_text("⛔️ *Доступ запрещен*", parse_mode='MarkdownV2')
                    return
            else:
                await update.message.reply_text(
                    "🛠️ *Панель администратора*",
                    reply_markup=admin_keyboard(),
                    parse_mode='MarkdownV2'
                )
        else:
            await update.message.reply_text("⛔️ *Доступ запрещен*", parse_mode='MarkdownV2')
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
            await query.edit_message_text(text, reply_markup=main_keyboard(), parse_mode='MarkdownV2')
            return
        
        elif data == "my_links":
            links = get_user_links(user.id)
            if links:
                text = "🔗 *Ваши анонимные ссылки:*\n\n"
                for link in links:
                    bot_username = context.bot.username
                    link_url = f"https://t.me/{bot_username}?start={link[0]}"
                    created = format_datetime(link[3])
                    text += f"📝 *{escape_markdown_v2(link[1])}*\n📋 {escape_markdown_v2(link[2])}\n🔗 `{escape_markdown_v2(link_url)}`\n🕒 `{created}`\n\n"
                
                # Добавляем кнопки удаления для каждой ссылки
                keyboard_buttons = []
                for link in links:
                    keyboard_buttons.append([InlineKeyboardButton(f"🗑️ Удалить {link[1]}", callback_data=f"confirm_delete_link_{link[0]}")])
                
                keyboard_buttons.append([InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")])
                keyboard = InlineKeyboardMarkup(keyboard_buttons)
                
                await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=keyboard)
            else:
                await query.edit_message_text("У вас пока нет созданных ссылок\\.", parse_mode='MarkdownV2', reply_markup=main_keyboard())
            return
        
        elif data == "my_messages":
            messages = get_user_messages_with_replies(user.id)
            if messages:
                text = "📨 *Ваши последние сообщения:*\n\n"
                for msg in messages:
                    msg_id, msg_text, msg_type, file_id, file_size, file_name, created, link_title, link_id, reply_count = msg
                    
                    type_icon = {"text": "📝", "photo": "🖼️", "video": "🎥", "document": "📄", "voice": "🎤"}.get(msg_type, "📄")
                    
                    preview = safe_str(msg_text) or f"*{msg_type}*"
                    if len(preview) > 50:
                        preview = preview[:50] + "\\.\\.\\."
                        
                    created_str = format_datetime(created)
                    text += f"{type_icon} *{escape_markdown_v2(link_title)}*\n`{preview}`\n🕒 `{created_str}` \\| 💬 Ответов\\: {reply_count}\n\n"
                
                # Добавляем кнопки действий для сообщений
                keyboard_buttons = []
                for msg in messages:
                    keyboard_buttons.append([
                        InlineKeyboardButton(f"💬 Ответить {msg[7]}", callback_data=f"reply_{msg[0]}"),
                        InlineKeyboardButton(f"🗑️ Удалить", callback_data=f"confirm_delete_message_{msg[0]}")
                    ])
                
                keyboard_buttons.append([InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")])
                keyboard = InlineKeyboardMarkup(keyboard_buttons)
                
                await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=keyboard)
            else:
                await query.edit_message_text("У вас пока нет сообщений\\.", parse_mode='MarkdownV2', reply_markup=main_keyboard())
            return
        
        elif data == "create_link":
            context.user_data['creating_link'] = True
            context.user_data['link_stage'] = 'title'
            await query.edit_message_text("📝 Введите *название* для вашей ссылки:", parse_mode='MarkdownV2', reply_markup=cancel_keyboard())
            return
        
        # Ответ на сообщение
        elif data.startswith("reply_"):
            try:
                message_id_str = data.replace("reply_", "")
                if message_id_str and message_id_str != "None":
                    message_id = safe_int(message_id_str)
                    context.user_data['replying_to'] = message_id
                    await query.edit_message_text(
                        "💬 *Режим ответа*\n\nВведите ваш ответ на это сообщение:",
                        parse_mode='MarkdownV2',
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="my_messages")]])
                    )
                else:
                    await query.answer("Ошибка: сообщение не найдено", show_alert=True)
            except (ValueError, TypeError) as e:
                logging.error(f"Ошибка преобразования message_id: {e}")
                await query.answer("Ошибка: неверный идентификатор сообщения", show_alert=True)
            return
        
        # Управление удалением
        elif data.startswith("confirm_delete_link_"):
            link_id = data.replace("confirm_delete_link_", "")
            if link_id and link_id != "None":
                link_info = get_link_info(link_id)
                
                if link_info:
                    text = f"🗑️ *Подтверждение удаления ссылки*\n\n"
                    text += f"📝 *Название:* {escape_markdown_v2(link_info[2])}\n"
                    text += f"📋 *Описание:* {escape_markdown_v2(link_info[3])}\n\n"
                    text += "❓ *Вы уверены, что хотите удалить эту ссылку?*\n"
                    text += "⚠️ *Все сообщения через эту ссылку также будут удалены\\!*"
                    
                    await query.edit_message_text(text, parse_mode='MarkdownV2', 
                                               reply_markup=delete_confirmation_keyboard("link", link_id))
            else:
                await query.answer("Ошибка: ссылка не найдена", show_alert=True)
            return
        
        elif data.startswith("confirm_delete_message_"):
            message_id_str = data.replace("confirm_delete_message_", "")
            if message_id_str and message_id_str != "None":
                message_id = safe_int(message_id_str)
                message_info = get_message_info(message_id)
                
                if message_info:
                    msg_text, msg_type, file_name, created, from_user, from_name, to_user, to_name, link_title, link_id = message_info
                    
                    text = f"🗑️ *Подтверждение удаления сообщения*\n\n"
                    text += f"📝 *Сообщение:*\n`{safe_str(msg_text) if msg_text else f'Медиафайл: {msg_type}'}`\n\n"
                    text += f"❓ *Вы уверены, что хотите удалить это сообщение?*"
                    
                    await query.edit_message_text(text, parse_mode='MarkdownV2', 
                                               reply_markup=delete_confirmation_keyboard("message", message_id))
            else:
                await query.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        elif data.startswith("delete_link_"):
            link_id = data.replace("delete_link_", "")
            if link_id and link_id != "None":
                success = delete_link_completely(link_id)
                
                if success:
                    await query.edit_message_text("✅ *Ссылка и все связанные сообщения успешно удалены\\!*", 
                                               parse_mode='MarkdownV2', 
                                               reply_markup=main_keyboard())
                else:
                    await query.edit_message_text("❌ *Ошибка при удалении ссылки*", 
                                               parse_mode='MarkdownV2', 
                                               reply_markup=main_keyboard())
            else:
                await query.answer("Ошибка: ссылка не найдена", show_alert=True)
            return
        
        elif data.startswith("delete_message_"):
            message_id_str = data.replace("delete_message_", "")
            if message_id_str and message_id_str != "None":
                message_id = safe_int(message_id_str)
                success = delete_message_completely(message_id)
                
                if success:
                    await query.edit_message_text("✅ *Сообщение успешно удалено\\!*", 
                                               parse_mode='MarkdownV2', 
                                               reply_markup=main_keyboard())
                else:
                    await query.edit_message_text("❌ *Ошибка при удалении сообщения*", 
                                               parse_mode='MarkdownV2', 
                                               reply_markup=main_keyboard())
            else:
                await query.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        elif data == "cancel_delete":
            await query.edit_message_text("❌ *Удаление отменено*", 
                                       parse_mode='MarkdownV2', 
                                       reply_markup=main_keyboard())
            return

        # АДМИН ПАНЕЛЬ
        if is_admin:
            # Проверка аутентификации админа
            if not context.user_data.get('admin_authenticated'):
                await query.edit_message_text(
                    "🔐 *Требуется аутентификация*\n\nИспользуйте команду /admin с паролем",
                    parse_mode='MarkdownV2'
                )
                return

            if data == "admin_panel":
                await query.edit_message_text(
                    "🛠️ *Панель администратора*",
                    reply_markup=admin_keyboard(),
                    parse_mode='MarkdownV2'
                )
                return

            elif data == "admin_stats":
                stats = get_admin_stats()
                text = f"""📊 *Статистика бота\\:*

👥 *Пользователи\\:*
• Всего пользователей\\: {stats['users']}
• Заблокированных\\: {stats['banned']}
• Активных ссылок\\: {stats['links']}
• Спонсорских ссылок\\: {stats['sponsor_links']}

💌 *Сообщения\\:*
• Всего сообщений\\: {stats['messages']}
• Ответов\\: {stats['replies']}

📁 *Файлы\\:*
• Фотографий\\: {stats['photos']}
• Видео\\: {stats['videos']}
• Документов\\: {stats['documents']}
• Голосовых\\: {stats['voice']}"""
                await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                return
            
            elif data == "admin_users":
                users = get_all_users_for_admin()
                if users:
                    text = "👥 *Управление пользователями*\n\n"
                    for u in users[:15]:
                        # Безопасное получение username
                        username = u[1] if u[1] else (u[2] or f"ID:{u[0]}")
                        username_display = f"@{username}" if u[1] else username
                        created = format_datetime(u[3])
                        ban_status = "🚫 ЗАБЛОКИРОВАН" if u[4] else "✅ АКТИВЕН"
                        text += f"👤 *{escape_markdown_v2(username_display)}*\n🆔 `{u[0]}` \\| 📅 `{created}` \\| {ban_status}\n\n"
                    
                    # Добавляем кнопки управления для каждого пользователя
                    keyboard_buttons = []
                    for u in users[:15]:
                        username_display = f"@{u[1]}" if u[1] else (u[2] or f"ID:{u[0]}")
                        keyboard_buttons.append([
                            InlineKeyboardButton(f"👤 {username_display}", callback_data=f"admin_user_manage_{u[0]}")
                        ])
                    
                    keyboard_buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")])
                    keyboard = InlineKeyboardMarkup(keyboard_buttons)
                    
                    await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=keyboard)
                else:
                    await query.edit_message_text("Пользователей не найдено\\.", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                return
            
            elif data.startswith("admin_user_manage_"):
                user_id_str = data.replace("admin_user_manage_", "")
                if user_id_str and user_id_str != "None":
                    user_id = safe_int(user_id_str)
                    user_info = run_query("SELECT username, first_name, is_banned FROM users WHERE user_id = ?", (user_id,), fetch="one")
                    
                    if user_info:
                        username, first_name, is_banned = user_info
                        user_display = f"@{username}" if username else (first_name or f"ID:{user_id}")
                        status = "🚫 ЗАБЛОКИРОВАН" if is_banned else "✅ АКТИВЕН"
                        
                        text = f"👤 *Управление пользователем*\n\n"
                        text += f"*Информация:*\n"
                        text += f"• ID\\: `{user_id}`\n"
                        text += f"• Имя\\: {escape_markdown_v2(user_display)}\n"
                        text += f"• Статус\\: {status}\n\n"
                        text += f"*Доступные действия:*"
                        
                        await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=user_management_keyboard(user_id))
                    else:
                        await query.answer("Пользователь не найден", show_alert=True)
                return
            
            elif data.startswith("admin_ban_user_"):
                user_id_str = data.replace("admin_ban_user_", "")
                if user_id_str and user_id_str != "None":
                    user_id = safe_int(user_id_str)
                    context.user_data['banning_user'] = user_id
                    await query.edit_message_text(
                        f"🚫 *Блокировка пользователя {user_id}*\n\nВведите причину блокировки \\(или нажмите отмена\\):",
                        parse_mode='MarkdownV2',
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data=f"admin_user_manage_{user_id}")]])
                    )
                return
            
            elif data.startswith("admin_unban_user_"):
                user_id_str = data.replace("admin_unban_user_", "")
                if user_id_str and user_id_str != "None":
                    user_id = safe_int(user_id_str)
                    success = unban_user(user_id)
                    
                    if success:
                        # Пытаемся уведомить пользователя о разблокировке
                        try:
                            await context.bot.send_message(
                                user_id, 
                                "✅ *Ваша блокировка в боте снята\\!*\n\nТеперь вы снова можете использовать все функции бота\\.",
                                parse_mode='MarkdownV2'
                            )
                        except:
                            pass
                        
                        await query.edit_message_text(
                            f"✅ *Пользователь {user_id} разблокирован\\!*",
                            parse_mode='MarkdownV2',
                            reply_markup=user_management_keyboard(user_id)
                        )
                    else:
                        await query.answer("Ошибка при разблокировке пользователя", show_alert=True)
                return
            
            elif data.startswith("admin_delete_user_"):
                user_id_str = data.replace("admin_delete_user_", "")
                if user_id_str and user_id_str != "None":
                    user_id = safe_int(user_id_str)
                    
                    text = f"🗑️ *УДАЛЕНИЕ ПОЛЬЗОВАТЕЛЯ*\n\n"
                    text += f"❓ *Вы уверены, что хотите полностью удалить пользователя {user_id}?*\n\n"
                    text += "⚠️ *ВНИМАНИЕ\\! Это действие нельзя отменить\\!*\n"
                    text += "• Все ссылки пользователя будут удалены\n"
                    text += "• Все сообщения пользователя будут удалены\n"
                    text += "• Все ответы пользователя будут удалены\n"
                    text += "• Пользователь будет полностью удален из системы"
                    
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ ДА, УДАЛИТЬ", callback_data=f"admin_confirm_delete_user_{user_id}")],
                        [InlineKeyboardButton("❌ ОТМЕНА", callback_data=f"admin_user_manage_{user_id}")]
                    ])
                    
                    await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=keyboard)
                return
            
            elif data.startswith("admin_confirm_delete_user_"):
                user_id_str = data.replace("admin_confirm_delete_user_", "")
                if user_id_str and user_id_str != "None":
                    user_id = safe_int(user_id_str)
                    success = delete_user(user_id)
                    
                    if success:
                        await query.edit_message_text(
                            f"✅ *Пользователь {user_id} и все его данные полностью удалены\\!*",
                            parse_mode='MarkdownV2',
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К списку пользователей", callback_data="admin_users")]])
                        )
                    else:
                        await query.answer("Ошибка при удалении пользователя", show_alert=True)
                return
            
            elif data.startswith("admin_message_user_"):
                user_id_str = data.replace("admin_message_user_", "")
                if user_id_str and user_id_str != "None":
                    user_id = safe_int(user_id_str)
                    context.user_data['admin_messaging_user'] = user_id
                    await query.edit_message_text(
                        f"✉️ *Отправка сообщения пользователю {user_id}*\n\nВведите сообщение, которое будет отправлено от имени бота:",
                        parse_mode='MarkdownV2',
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data=f"admin_user_manage_{user_id}")]])
                    )
                return
            
            elif data == "admin_sponsor_links":
                await query.edit_message_text(
                    "🔗 *Управление спонсорскими ссылками*\n\nСпонсорские ссылки могут быть созданы для любого пользователя и переданы им позже\\.",
                    parse_mode='MarkdownV2',
                    reply_markup=sponsor_links_keyboard()
                )
                return
            
            elif data == "admin_create_sponsor_link":
                context.user_data['creating_sponsor_link'] = True
                context.user_data['sponsor_stage'] = 'title'
                await query.edit_message_text(
                    "🔗 *Создание спонсорской ссылки*\n\nВведите *название* для спонсорской ссылки:",
                    parse_mode='MarkdownV2',
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_sponsor_links")]])
                )
                return
            
            elif data == "admin_my_sponsor_links":
                sponsor_links = get_sponsor_links(user.id)
                if sponsor_links:
                    text = "🔗 *Ваши спонсорские ссылки:*\n\n"
                    for link in sponsor_links:
                        link_id, title, description, created, target_user_id = link
                        bot_username = context.bot.username
                        link_url = f"https://t.me/{bot_username}?start={link_id}"
                        created_str = format_datetime(created)
                        text += f"📝 *{escape_markdown_v2(title)}*\n📋 {escape_markdown_v2(description)}\n👤 Владелец\\: `{target_user_id}`\n🔗 `{escape_markdown_v2(link_url)}`\n🕒 `{created_str}`\n\n"
                    
                    # Добавляем кнопки действий для каждой ссылки
                    keyboard_buttons = []
                    for link in sponsor_links:
                        keyboard_buttons.append([InlineKeyboardButton(f"🔄 {link[1]}", callback_data=f"admin_sponsor_actions_{link[0]}")])
                    
                    keyboard_buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="admin_sponsor_links")])
                    keyboard = InlineKeyboardMarkup(keyboard_buttons)
                    
                    await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=keyboard)
                else:
                    await query.edit_message_text(
                        "У вас пока нет спонсорских ссылок\\.",
                        parse_mode='MarkdownV2',
                        reply_markup=sponsor_links_keyboard()
                    )
                return
            
            elif data.startswith("admin_sponsor_actions_"):
                link_id = data.replace("admin_sponsor_actions_", "")
                link_info = get_link_info(link_id)
                
                if link_info:
                    text = f"🔗 *Управление спонсорской ссылкой*\n\n"
                    text += f"*Название:* {escape_markdown_v2(link_info[2])}\n"
                    text += f"*Описание:* {escape_markdown_v2(link_info[3])}\n"
                    text += f"*ID ссылки:* `{link_id}`\n\n"
                    text += f"*Доступные действия:*"
                    
                    await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=sponsor_link_actions_keyboard(link_id))
                else:
                    await query.answer("Ссылка не найдена", show_alert=True)
                return
            
            elif data.startswith("admin_transfer_sponsor_"):
                link_id = data.replace("admin_transfer_sponsor_", "")
                context.user_data['transferring_sponsor_link'] = link_id
                await query.edit_message_text(
                    f"🔄 *Передача спонсорской ссылки*\n\nВведите *ID пользователя*, которому хотите передать ссылку:",
                    parse_mode='MarkdownV2',
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data=f"admin_sponsor_actions_{link_id}")]])
                )
                return
            
            elif data.startswith("admin_delete_sponsor_"):
                link_id = data.replace("admin_delete_sponsor_", "")
                success = delete_link_completely(link_id)
                
                if success:
                    await query.edit_message_text(
                        "✅ *Спонсорская ссылка успешно удалена\\!*",
                        parse_mode='MarkdownV2',
                        reply_markup=sponsor_links_keyboard()
                    )
                else:
                    await query.answer("Ошибка при удалении ссылки", show_alert=True)
                return

            elif data == "admin_html_report":
                await query.edit_message_text("🔄 *Генерация HTML отчета\\.\\.\\.*", parse_mode='MarkdownV2')
                
                html_content = generate_beautiful_html_report()
                
                report_path = "/tmp/admin_report.html"
                with open(report_path, 'w', encoding='utf-8') as f:
                    f.write(html_content)
                
                with open(report_path, 'rb') as f:
                    await query.message.reply_document(
                        document=f,
                        filename=f"admin_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
                        caption="🎨 *Красивый HTML отчет администратора*",
                        parse_mode='MarkdownV2'
                    )
                
                await query.edit_message_text("✅ *HTML отчет сгенерирован и отправлен\\!*", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                return
            
            elif data == "admin_broadcast":
                context.user_data['broadcasting'] = True
                context.user_data['broadcast_message'] = ""
                await query.edit_message_text(
                    "📢 *Режим рассылки*\n\n"
                    "💡 *Доступные форматы:*\n"
                    "• **Жирный** текст\n" 
                    "• *Курсив* текст\n"
                    "• ~~Зачеркивание~~\n"
                    "• Скрытый текст\n"
                    "• `Моноширинный`\n"
                    "• Цитата\n\n"
                    "Введите сообщение для рассылки:",
                    parse_mode='MarkdownV2', 
                    reply_markup=broadcast_formatting_keyboard()
                )
                return
            
            # Обработка форматирования рассылки
            elif data.startswith("format_"):
                if context.user_data.get('broadcasting'):
                    format_type = data.replace("format_", "")
                    current_text = context.user_data.get('broadcast_message', '')
                    
                    format_examples = {
                        'bold': '**жирный текст**',
                        'italic': '*курсив*', 
                        'strike': '~~зачеркнутый~~',
                        'spoiler': '||скрытый текст||',
                        'code': '`моноширинный`',
                        'quote': '>>цитата'
                    }
                    
                    example = format_examples.get(format_type, '')
                    new_text = current_text + example
                    context.user_data['broadcast_message'] = new_text
                    
                    # Простой предпросмотр без сложного форматирования
                    preview_text = new_text
                    
                    await query.edit_message_text(
                        f"📢 *Сообщение для рассылки:*\n\n{preview_text}\n\n"
                        "Используйте кнопки для добавления форматирования или введите текст:",
                        parse_mode=None,  # Отключаем форматирование для предпросмотра
                        reply_markup=broadcast_formatting_keyboard()
                    )
                return
            
            elif data == "broadcast_send":
                if context.user_data.get('broadcasting'):
                    message_text = context.user_data.get('broadcast_message', '')
                    if not message_text or not message_text.strip():
                        await query.answer("Сообщение не может быть пустым!", show_alert=True)
                        return
                    
                    context.user_data.pop('broadcasting', None)
                    context.user_data.pop('broadcast_message', None)
                    
                    # Простое форматирование для рассылки
                    try:
                        formatted_text = parse_formatting(message_text.strip())
                    except Exception as e:
                        logging.error(f"Ошибка форматирования рассылки: {e}")
                        formatted_text = message_text.strip()
                    
                    users = get_all_users_for_admin()
                    success_count = 0
                    failed_count = 0
                    
                    await query.edit_message_text(f"🔄 *Отправка рассылки...*", parse_mode='MarkdownV2')
                    
                    for u in users:
                        try:
                            await context.bot.send_message(
                                u[0], 
                                f"📢 *Оповещение от администратора*\n\n{formatted_text}", 
                                parse_mode='HTML'
                            )
                            success_count += 1
                            await asyncio.sleep(0.1)
                        except Exception as e:
                            logging.error(f"Ошибка отправки пользователю {u[0]}: {e}")
                            failed_count += 1
                    
                    await query.edit_message_text(
                        f"✅ *Рассылка завершена\\!*\n\n"
                        f"• 📨 Успешно отправлено\\: {success_count}\n"
                        f"• ❌ Не удалось отправить\\: {failed_count}",
                        parse_mode='MarkdownV2', 
                        reply_markup=admin_keyboard()
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
        
        # Проверяем, не забанен ли пользователь
        if is_user_banned(user.id):
            await update.message.reply_text("❌ Вы заблокированы в этом боте и не можете использовать его функции.")
            return
        
        text = update.message.text
        save_user(user.id, user.username, user.first_name)
        is_admin = user.username == ADMIN_USERNAME or user.id == ADMIN_ID

        # Удаляем сообщение пользователя
        try:
            await update.message.delete()
        except:
            pass

        # Обработка блокировки пользователя
        if context.user_data.get('banning_user'):
            user_id = context.user_data.pop('banning_user')
            success = ban_user(user_id, text)
            
            if success:
                # Пытаемся уведомить пользователя о блокировке
                try:
                    ban_message = f"🚫 *Вы были заблокированы в боте*\n\n*Причина:* {text}\n\nЕсли вы считаете, что это ошибка, свяжитесь с администратором\\."
                    await context.bot.send_message(user_id, ban_message, parse_mode='MarkdownV2')
                except:
                    pass
                
                await update.message.reply_text(
                    f"✅ *Пользователь {user_id} заблокирован\\!*\n*Причина:* {text}",
                    parse_mode='MarkdownV2',
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"admin_user_manage_{user_id}")]])
                )
            else:
                await update.message.reply_text("❌ Ошибка при блокировке пользователя")
            return

        # Обработка отправки сообщения от имени бота
        if context.user_data.get('admin_messaging_user'):
            target_user_id = context.user_data.pop('admin_messaging_user')
            
            try:
                # Отправляем сообщение пользователю
                await context.bot.send_message(
                    target_user_id, 
                    f"📢 *Сообщение от администратора*\n\n{text}",
                    parse_mode='MarkdownV2'
                )
                
                # Сохраняем в историю
                save_admin_message(user.id, target_user_id, text)
                
                await update.message.reply_text(
                    f"✅ *Сообщение отправлено пользователю {target_user_id}\\!*",
                    parse_mode='MarkdownV2',
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"admin_user_manage_{target_user_id}")]])
                )
            except Exception as e:
                logging.error(f"Ошибка отправки сообщения пользователю {target_user_id}: {e}")
                await update.message.reply_text(
                    f"❌ *Не удалось отправить сообщение пользователю {target_user_id}*\n\nВозможно, пользователь заблокировал бота\\.",
                    parse_mode='MarkdownV2'
                )
            return

        # Обработка передачи спонсорской ссылки
        if context.user_data.get('transferring_sponsor_link'):
            link_id = context.user_data.pop('transferring_sponsor_link')
            try:
                new_user_id = int(text)
                success = transfer_sponsor_link(link_id, new_user_id)
                
                if success:
                    await update.message.reply_text(
                        f"✅ *Спонсорская ссылка передана пользователю {new_user_id}\\!*",
                        parse_mode='MarkdownV2',
                        reply_markup=sponsor_links_keyboard()
                    )
                else:
                    await update.message.reply_text("❌ Ошибка при передаче ссылки")
            except ValueError:
                await update.message.reply_text("❌ Неверный формат ID пользователя\\. Введите числовой ID\\.")
            return

        # Обработка создания спонсорской ссылки
        if context.user_data.get('creating_sponsor_link'):
            stage = context.user_data.get('sponsor_stage')
            
            if stage == 'title':
                context.user_data['sponsor_title'] = text
                context.user_data['sponsor_stage'] = 'description'
                await update.message.reply_text(
                    "📋 Введите *описание* для спонсорской ссылки:",
                    parse_mode='MarkdownV2',
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_sponsor_links")]])
                )
            elif stage == 'description':
                context.user_data['sponsor_description'] = text
                context.user_data['sponsor_stage'] = 'target_user'
                await update.message.reply_text(
                    "👤 Введите *ID пользователя* для спонсорской ссылки \\(или 0 для создания без привязки\\):",
                    parse_mode='MarkdownV2',
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_sponsor_links")]])
                )
            elif stage == 'target_user':
                title = context.user_data.pop('sponsor_title')
                description = context.user_data.pop('sponsor_description')
                context.user_data.pop('creating_sponsor_link')
                context.user_data.pop('sponsor_stage')
                
                try:
                    target_user_id = int(text) if text != '0' else None
                    link_id = create_sponsor_link(user.id, title, description, target_user_id)
                    
                    bot_username = context.bot.username
                    link_url = f"https://t.me/{bot_username}?start={link_id}"
                    
                    await update.message.reply_text(
                        f"✅ *Спонсорская ссылка создана\\!*\n\n"
                        f"📝 *{escape_markdown_v2(title)}*\n"
                        f"📋 {escape_markdown_v2(description)}\n"
                        f"👤 Владелец\\: `{target_user_id or 'Не назначен'}`\n\n"
                        f"🔗 `{escape_markdown_v2(link_url)}`",
                        parse_mode='MarkdownV2',
                        reply_markup=sponsor_links_keyboard()
                    )
                except ValueError:
                    await update.message.reply_text("❌ Неверный формат ID пользователя\\. Введите числовой ID или 0\\.")
            return

        # Ответ на сообщение
        if context.user_data.get('replying_to'):
            message_id = context.user_data.pop('replying_to')
            message_info = get_message_info(message_id)
            
            if message_info:
                # Сохраняем ответ
                save_reply(message_id, user.id, text)
                
                # Отправляем уведомление получателю (простой текст)
                msg_text, msg_type, file_name, created, from_user, from_name, to_user, to_name, link_title, link_id = message_info
                
                notification = f"💬 *Новый ответ на ваше сообщение*\n\n{text}"
                try:
                    await context.bot.send_message(to_user, notification, parse_mode='MarkdownV2')
                except Exception as e:
                    logging.error(f"Failed to send reply notification to {to_user}: {e}")
                
                await update.message.reply_text("✅ *Ответ отправлен\\!*", parse_mode='MarkdownV2', reply_markup=main_keyboard())
            return

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
                    await context.bot.send_message(link_info[1], notification, parse_mode='MarkdownV2', reply_markup=message_actions_keyboard(msg_id))
                except Exception as e:
                    logging.error(f"Failed to send message notification: {e}")
                    # Если не удалось отправить уведомление, все равно сообщаем пользователю
                
                await update.message.reply_text("✅ Ваше сообщение отправлено анонимно\\!", reply_markup=main_keyboard(), parse_mode='MarkdownV2')
            return

        # Рассылка от админа
        if context.user_data.get('broadcasting') and is_admin:
            context.user_data['broadcast_message'] = text
            formatted_text = parse_formatting(text)
            
            await update.message.reply_text(
                f"📢 *Предпросмотр рассылки:*\n\n{formatted_text}\n\n"
                "✅ *Сообщение готово к отправке*\n\n"
                "Используйте кнопку ниже для отправки:",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚀 Отправить рассылку", callback_data="broadcast_send")],
                    [InlineKeyboardButton("✏️ Редактировать", callback_data="admin_broadcast")]
                ])
            )
            return

        await update.message.reply_text("Используйте кнопки для навигации\\.", reply_markup=main_keyboard(), parse_mode='MarkdownV2')

    except Exception as e:
        logging.error(f"Ошибка в обработчике текста: {e}")
        await update.message.reply_text("❌ Произошла ошибка\\. Попробуйте позже\\.", parse_mode='MarkdownV2')

# Функции handle_media, generate_conversation_report остаются без изменений
# Добавляем функцию generate_beautiful_html_report

def generate_beautiful_html_report():
    """Генерирует красивый HTML отчет с твоим стилем"""
    data = get_all_data_for_html()
    
    html_content = f'''
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>🟣 Анонимный Бот - Панель Администратора</title>
        <link href="https://fonts.googleapis.com/css2?family=Rubik:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
                -webkit-tap-highlight-color: rgba(184, 77, 255, 0.3);
            }}
            
            /* Кастомное выделение */
            ::selection {{
                background: rgba(184, 77, 255, 0.3);
                color: #ffffff;
                border-radius: 4px;
            }}
            
            ::-moz-selection {{
                background: rgba(184, 77, 255, 0.3);
                color: #ffffff;
                border-radius: 4px;
            }}
            
            body {{
                font-family: 'Rubik', sans-serif;
                background: #0f0829;
                color: #e8e6f3;
                min-height: 100vh;
                padding: 40px 20px;
                overflow-x: auto;
            }}
            
            .container {{
                max-width: 1400px;
                margin: 0 auto;
                min-width: 1000px;
            }}
            
            .header {{
                text-align: center;
                margin-bottom: 40px;
            }}
            
            .title {{
                font-weight: 900;
                font-size: 3.2em;
                background: linear-gradient(135deg, #b84dff 0%, #6c43ff 50%, #ff47d6 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
                margin-bottom: 15px;
                text-transform: uppercase;
                letter-spacing: 2px;
            }}
            
            .subtitle {{
                font-weight: 600;
                font-size: 1.3em;
                color: #a78bfa;
                opacity: 0.9;
                margin-bottom: 25px;
            }}
            
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                gap: 25px;
                margin-bottom: 40px;
            }}
            
            .stat-card {{
                background: linear-gradient(135deg, rgba(255, 255, 255, 0.1) 0%, rgba(255, 255, 255, 0.05) 100%);
                backdrop-filter: blur(15px);
                padding: 30px 25px;
                border-radius: 20px;
                text-align: center;
                border: 1px solid rgba(255, 255, 255, 0.1);
                transition: all 0.3s ease;
                position: relative;
                overflow: hidden;
            }}
            
            .stat-card:hover {{
                transform: translateY(-8px) scale(1.02);
                box-shadow: 0 15px 35px rgba(0, 0, 0, 0.4);
                border-color: rgba(184, 77, 255, 0.3);
            }}
            
            .stat-card h3 {{
                font-family: 'Rubik', sans-serif;
                font-weight: 800;
                font-size: 3em;
                margin-bottom: 15px;
                background: linear-gradient(135deg, #ffd700 0%, #ff6b6b 50%, #b84dff 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
            }}
            
            .stat-card p {{
                color: #e0e0ff;
                font-size: 1em;
                font-weight: 500;
                text-transform: uppercase;
                letter-spacing: 1px;
            }}
            
            .section {{
                background: linear-gradient(135deg, rgba(255, 255, 255, 0.08) 0%, rgba(255, 255, 255, 0.03) 100%);
                backdrop-filter: blur(15px);
                padding: 30px;
                border-radius: 20px;
                margin-bottom: 35px;
                border: 1px solid rgba(255, 255, 255, 0.08);
                position: relative;
                overflow: hidden;
            }}
            
            .section::before {{
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 3px;
                background: linear-gradient(90deg, #b84dff, #6c43ff, #ff47d6);
            }}
            
            .section h2 {{
                font-family: 'Rubik', sans-serif;
                font-weight: 800;
                font-size: 1.8em;
                margin-bottom: 25px;
                color: #ffffff;
                display: flex;
                align-items: center;
                gap: 15px;
            }}
            
            table {{
                width: 100%;
                border-collapse: collapse;
                background: rgba(255, 255, 255, 0.02);
                border-radius: 15px;
                overflow: hidden;
                margin-top: 15px;
            }}
            
            th, td {{
                padding: 15px 20px;
                text-align: left;
                border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            }}
            
            th {{
                background: linear-gradient(135deg, rgba(184, 77, 255, 0.2) 0%, rgba(108, 67, 255, 0.2) 100%);
                color: #ffd700;
                font-weight: 700;
                font-family: 'Rubik', sans-serif;
                text-transform: uppercase;
                letter-spacing: 1px;
                font-size: 0.9em;
            }}
            
            td {{
                color: #e0e0ff;
                font-weight: 500;
            }}
            
            tr:hover {{
                background: rgba(255, 255, 255, 0.05);
            }}
            
            .user-banned {{
                color: #ff6b6b;
                font-weight: bold;
            }}
            
            .user-active {{
                color: #4ade80;
                font-weight: bold;
            }}
            
            .footer {{
                text-align: center;
                margin-top: 50px;
                padding: 30px;
                background: linear-gradient(135deg, rgba(184, 77, 255, 0.1) 0%, rgba(108, 67, 255, 0.1) 100%);
                border-radius: 20px;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }}
            
            .footer-text {{
                font-family: 'Rubik', sans-serif;
                font-weight: 800;
                font-size: 1.1em;
                color: #ffd700;
                letter-spacing: 2px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <!-- Заголовок -->
            <div class="header">
                <h1 class="title">🛠️ АДМИН ПАНЕЛЬ</h1>
                <div class="subtitle">Анонимный Бот - Полная статистика системы</div>
                <div style="color: #a78bfa; font-size: 1.1em;">
                    📅 Отчет сгенерирован: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} (Krasnoyarsk)
                </div>
            </div>
            
            <!-- Основная статистика -->
            <div class="stats-grid">
                <div class="stat-card">
                    <h3>{data['stats']['users']}</h3>
                    <p>👥 Всего пользователей</p>
                </div>
                <div class="stat-card">
                    <h3>{data['stats']['banned']}</h3>
                    <p>🚫 Заблокированных</p>
                </div>
                <div class="stat-card">
                    <h3>{data['stats']['links']}</h3>
                    <p>🔗 Активных ссылок</p>
                </div>
                <div class="stat-card">
                    <h3>{data['stats']['sponsor_links']}</h3>
                    <p>🎁 Спонсорских ссылок</p>
                </div>
            </div>
            
            <!-- Статистика сообщений -->
            <div class="stats-grid">
                <div class="stat-card">
                    <h3>{data['stats']['messages']}</h3>
                    <p>📨 Всего сообщений</p>
                </div>
                <div class="stat-card">
                    <h3>{data['stats']['replies']}</h3>
                    <p>💬 Ответов</p>
                </div>
                <div class="stat-card">
                    <h3>{data['stats']['photos']}</h3>
                    <p>🖼️ Фотографий</p>
                </div>
                <div class="stat-card">
                    <h3>{data['stats']['videos']}</h3>
                    <p>🎥 Видео</p>
                </div>
            </div>
            
            <!-- Пользователи -->
            <div class="section">
                <h2>👥 АКТИВНЫЕ ПОЛЬЗОВАТЕЛИ</h2>
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Информация</th>
                            <th>Регистрация</th>
                            <th>Статус</th>
                            <th>Статистика</th>
                        </tr>
                    </thead>
                    <tbody>
    '''
    
    for user in data['users'][:20]:
        username_display = f"@{user[1]}" if user[1] else (html.escape(user[2]) if user[2] else f"ID:{user[0]}")
        status = f'<span class="user-banned">🚫 ЗАБЛОКИРОВАН</span>' if user[4] else f'<span class="user-active">✅ АКТИВЕН</span>'
        html_content += f'''
                        <tr>
                            <td><strong>{user[0]}</strong></td>
                            <td>{username_display}</td>
                            <td>{user[3].split()[0] if isinstance(user[3], str) else user[3].strftime("%Y-%m-%d")}</td>
                            <td>{status}</td>
                            <td>🔗 {user[5]} | 📨 {user[6]} | 📤 {user[7]}</td>
                        </tr>
        '''
    
    html_content += '''
                    </tbody>
                </table>
            </div>
            
            <!-- Ссылки -->
            <div class="section">
                <h2>🔗 АКТИВНЫЕ ССЫЛКИ</h2>
                <table>
                    <thead>
                        <tr>
                            <th>ID Ссылки</th>
                            <th>Название</th>
                            <th>Владелец</th>
                            <th>Тип</th>
                            <th>Сообщения</th>
                            <th>Создана</th>
                        </tr>
                    </thead>
                    <tbody>
    '''
    
    for link in data['links'][:25]:
        owner = f"@{link[6]}" if link[6] else (html.escape(link[7]) if link[7] else f"ID:{link[8]}")
        link_type = "🎁 СПОНСОР" if link[5] else "🔗 ОБЫЧНАЯ"
        html_content += f'''
                        <tr>
                            <td><code>{link[0]}</code></td>
                            <td>{html.escape(link[1])}</td>
                            <td>{owner}</td>
                            <td>{link_type}</td>
                            <td>{link[9]} сообщ.</td>
                            <td>{link[3].split()[0] if isinstance(link[3], str) else link[3].strftime("%Y-%m-%d")}</td>
                        </tr>
        '''
    
    html_content += '''
                    </tbody>
                </table>
            </div>
            
            <!-- Последние сообщения -->
            <div class="section">
                <h2>📨 ПОСЛЕДНИЕ СООБЩЕНИЯ</h2>
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Тип</th>
                            <th>Отправитель</th>
                            <th>Получатель</th>
                            <th>Ссылка</th>
                            <th>Дата</th>
                        </tr>
                    </thead>
                    <tbody>
    '''
    
    for msg in data['recent_messages'][:15]:
        msg_type_icon = {
            "text": "📝",
            "photo": "🖼️", 
            "video": "🎥",
            "document": "📄",
            "voice": "🎤"
        }.get(msg[2], "📄")
        
        from_user = f"@{msg[6]}" if msg[6] else (html.escape(msg[7]) if msg[7] else f"ID:{msg[8]}")
        to_user = f"@{msg[9]}" if msg[9] else (html.escape(msg[10]) if msg[10] else f"ID:{msg[11]}")
        
        html_content += f'''
                        <tr>
                            <td>#{msg[0]}</td>
                            <td>{msg_type_icon}</td>
                            <td>{from_user}</td>
                            <td>{to_user}</td>
                            <td>{html.escape(msg[12])}</td>
                            <td>{msg[5].split()[0] if isinstance(msg[5], str) else msg[5].strftime("%Y-%m-%d")}</td>
                        </tr>
        '''
    
    html_content += '''
                    </tbody>
                </table>
            </div>
            
            <!-- Футер -->
            <div class="footer">
                <div class="footer-text">
                    🟣 АНОНИМНЫЙ Бот | СИСТЕМА УПРАВЛЕНИЯ
                </div>
            </div>
        </div>
    </body>
    </html>
    '''
    
    return html_content

# Остальные функции (handle_media, generate_conversation_report, main) остаются без изменений

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
