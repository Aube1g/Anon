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

# --- Глобальная переменная для остановки бота ---
bot_shutdown_requested = False

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

def backup_database():
    """Создает резервную копию базы данных"""
    try:
        if os.path.exists(DB_PATH):
            backup_path = f"{DB_PATH}.backup"
            shutil.copy2(DB_PATH, backup_path)
            logging.info(f"Резервная копия БД создана: {backup_path}")
            return True
    except Exception as e:
        logging.error(f"Ошибка при создании резервной копии БД: {e}")
    return False

def restore_database():
    """Восстанавливает базу данных из резервной копии"""
    try:
        backup_path = f"{DB_PATH}.backup"
        if os.path.exists(backup_path):
            shutil.copy2(backup_path, DB_PATH)
            logging.info(f"БД восстановлена из резервной копии: {backup_path}")
            return True
    except Exception as e:
        logging.error(f"Ошибка при восстановлении БД: {e}")
    return False

# --- ФУНКЦИИ ДЛЯ РАБОТЫ С БД ---

def init_db():
    """Создает таблицы, если их нет."""
    try:
        # Сначала пытаемся восстановить из резервной копии
        if not os.path.exists(DB_PATH):
            restore_database()
        
        db_existed_before = os.path.exists(DB_PATH)
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
        
        if not db_existed_before:
            logging.info("Файл БД не найден, создаю новый и отправляю на GitHub...")
            push_db_to_github("Initial commit: create database file")
        
        # Создаем резервную копию после инициализации
        backup_database()
        
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
    return run_query('SELECT l.link_id, l.user_id, l.title, l.description, u.username FROM links l LEFT JOIN users u ON l.user_id = u.user_id WHERE l.link_id = ?', (link_id,), fetch="one")

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

def get_full_history_for_admin(user_id):
    return run_query('''
        SELECT m.message_id, m.message_text, m.message_type, m.file_id, m.file_size, m.file_name,
               m.created_at, u_from.username as from_username, u_from.first_name as from_first_name,
               u_to.username as to_username, u_to.first_name as to_first_name,
               l.title as link_title, l.link_id
        FROM messages m 
        LEFT JOIN users u_from ON m.from_user_id = u_from.user_id 
        LEFT JOIN users u_to ON m.to_user_id = u_to.user_id
        LEFT JOIN links l ON m.link_id = l.link_id
        WHERE (m.from_user_id = ? OR m.to_user_id = ?) AND m.is_active = 1
        ORDER BY m.created_at ASC
    ''', (user_id, user_id), fetch="all")

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
            'message' as type, 
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
            NULL as reply_text,
            NULL as reply_id
        FROM messages m
        LEFT JOIN users u_from ON m.from_user_id = u_from.user_id
        LEFT JOIN users u_to ON m.to_user_id = u_to.user_id
        LEFT JOIN links l ON m.link_id = l.link_id
        WHERE (m.from_user_id = ? OR m.to_user_id = ?) AND m.is_active = 1
        
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
            NULL as to_username,
            NULL as to_first_name,
            NULL as to_user_id,
            NULL as link_title,
            NULL as link_id,
            r.reply_text,
            r.reply_id
        FROM replies r
        WHERE r.from_user_id = ? AND r.is_active = 1
        
        ORDER BY created_at ASC
    ''', (user_id, user_id, user_id), fetch="all")

def get_all_users_for_admin():
    return run_query("SELECT user_id, username, first_name, created_at FROM users ORDER BY created_at DESC", fetch="all")

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
        # Возвращаем значения по умолчанию
        stats = {'users': 0, 'links': 0, 'messages': 0, 'replies': 0, 'photos': 0, 'videos': 0, 'documents': 0, 'voice': 0}
    
    return stats

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
        
        # Получаем все медиафайлы
        data['media_files'] = run_query('''
            SELECT m.message_id, m.message_type, m.file_id, m.file_size, m.file_name, m.created_at,
                   u.username, u.first_name, l.title as link_title
            FROM messages m
            LEFT JOIN users u ON m.from_user_id = u.user_id
            LEFT JOIN links l ON m.link_id = l.link_id
            WHERE m.message_type != 'text' AND m.is_active = 1
            ORDER BY m.created_at DESC
            LIMIT 100
        ''', fetch="all") or []
        
    except Exception as e:
        logging.error(f"Ошибка при получении данных для HTML: {e}")
        data = {'stats': get_admin_stats(), 'users': [], 'links': [], 'recent_messages': [], 'media_files': []}
    
    return data

def generate_html_report():
    """Генерирует красивый HTML отчет с возможностью просмотра переписок"""
    data = get_all_data_for_html()
    
    html_content = f'''
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>🟣 Анонимный Бот - Расширенная Панель Администратора</title>
        <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;700;900&family=Exo+2:wght@300;400;500;600;700&display=swap" rel="stylesheet">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            
            body {{
                font-family: 'Exo 2', sans-serif;
                background: linear-gradient(135deg, #0c0c0c 0%, #1a1a2e 50%, #16213e 100%);
                min-height: 100vh;
                padding: 20px;
                color: #ffffff;
                overflow-x: hidden;
            }}
            
            .container {{
                max-width: 1800px;
                margin: 0 auto;
            }}
            
            .header {{
                background: linear-gradient(135deg, rgba(102, 126, 234, 0.3) 0%, rgba(118, 75, 162, 0.3) 100%);
                backdrop-filter: blur(20px);
                padding: 50px 40px;
                border-radius: 30px;
                margin-bottom: 40px;
                text-align: center;
                border: 2px solid rgba(255, 255, 255, 0.15);
                position: relative;
                overflow: hidden;
                box-shadow: 0 20px 40px rgba(0, 0, 0, 0.3);
            }}
            
            .header::before {{
                content: '';
                position: absolute;
                top: -50%;
                left: -50%;
                width: 200%;
                height: 200%;
                background: linear-gradient(45deg, transparent, rgba(102, 126, 234, 0.2), transparent);
                animation: shine 8s infinite linear;
            }}
            
            @keyframes shine {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
            
            .header-content {{
                position: relative;
                z-index: 2;
            }}
            
            .header h1 {{
                font-family: 'Orbitron', monospace;
                font-size: 4em;
                margin-bottom: 20px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 30%, #f093fb 70%, #ffd700 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
                text-shadow: 0 0 50px rgba(102, 126, 234, 0.5);
                font-weight: 900;
                letter-spacing: 3px;
            }}
            
            .header .subtitle {{
                font-size: 1.5em;
                color: #e0e0ff;
                margin-bottom: 25px;
                font-weight: 300;
                text-shadow: 0 2px 10px rgba(0,0,0,0.5);
            }}
            
            .timestamp {{
                font-family: 'Orbitron', monospace;
                font-size: 1em;
                color: #ffd700;
                background: rgba(0, 0, 0, 0.4);
                padding: 12px 20px;
                border-radius: 25px;
                display: inline-block;
                border: 2px solid rgba(255, 215, 0, 0.3);
                box-shadow: 0 5px 15px rgba(255,215,0,0.2);
            }}
            
            .dashboard {{
                display: grid;
                grid-template-columns: 300px 1fr;
                gap: 30px;
                margin-bottom: 40px;
            }}
            
            .sidebar {{
                background: linear-gradient(135deg, rgba(255, 255, 255, 0.1) 0%, rgba(255, 255, 255, 0.05) 100%);
                backdrop-filter: blur(15px);
                padding: 30px;
                border-radius: 25px;
                border: 1px solid rgba(255, 255, 255, 0.1);
                height: fit-content;
            }}
            
            .nav-item {{
                display: flex;
                align-items: center;
                gap: 15px;
                padding: 15px 20px;
                margin-bottom: 10px;
                border-radius: 15px;
                cursor: pointer;
                transition: all 0.3s ease;
                color: #e0e0ff;
                text-decoration: none;
            }}
            
            .nav-item:hover {{
                background: rgba(102, 126, 234, 0.2);
                transform: translateX(10px);
            }}
            
            .nav-item.active {{
                background: linear-gradient(135deg, #667eea, #764ba2);
                color: white;
            }}
            
            .nav-icon {{
                font-size: 1.2em;
                width: 25px;
                text-align: center;
            }}
            
            .main-content {{
                display: grid;
                gap: 30px;
            }}
            
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                gap: 25px;
                margin-bottom: 30px;
            }}
            
            .stat-card {{
                background: linear-gradient(135deg, rgba(255, 255, 255, 0.12) 0%, rgba(255, 255, 255, 0.06) 100%);
                backdrop-filter: blur(15px);
                padding: 35px 30px;
                border-radius: 25px;
                text-align: center;
                border: 1px solid rgba(255, 255, 255, 0.12);
                transition: all 0.4s ease;
                position: relative;
                overflow: hidden;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
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
                transform: translateY(-12px) scale(1.03);
                box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
                border-color: rgba(102, 126, 234, 0.4);
            }}
            
            .stat-card h3 {{
                font-family: 'Orbitron', monospace;
                font-size: 3.5em;
                margin-bottom: 20px;
                background: linear-gradient(135deg, #ffd700 0%, #ff6b6b 25%, #667eea 50%, #764ba2 75%, #f093fb 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
                font-weight: 800;
            }}
            
            .stat-card p {{
                color: #e0e0ff;
                font-size: 1.1em;
                font-weight: 500;
                text-transform: uppercase;
                letter-spacing: 1.5px;
            }}
            
            .section {{
                background: linear-gradient(135deg, rgba(255, 255, 255, 0.1) 0%, rgba(255, 255, 255, 0.04) 100%);
                backdrop-filter: blur(20px);
                padding: 35px;
                border-radius: 25px;
                margin-bottom: 35px;
                border: 1px solid rgba(255, 255, 255, 0.1);
                position: relative;
                overflow: hidden;
                box-shadow: 0 15px 35px rgba(0, 0, 0, 0.2);
            }}
            
            .section::before {{
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 4px;
                background: linear-gradient(90deg, #667eea, #764ba2, #f093fb, #ffd700);
            }}
            
            .section h2 {{
                font-family: 'Orbitron', monospace;
                font-size: 2em;
                margin-bottom: 30px;
                color: #ffffff;
                display: flex;
                align-items: center;
                gap: 20px;
                font-weight: 700;
            }}
            
            .section h2 i {{
                background: linear-gradient(135deg, #667eea, #764ba2);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                font-size: 1.3em;
            }}
            
            table {{
                width: 100%;
                border-collapse: collapse;
                background: rgba(255, 255, 255, 0.03);
                border-radius: 20px;
                overflow: hidden;
                margin-top: 20px;
                box-shadow: 0 10px 25px rgba(0, 0, 0, 0.1);
            }}
            
            th, td {{
                padding: 18px 25px;
                text-align: left;
                border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            }}
            
            th {{
                background: linear-gradient(135deg, rgba(102, 126, 234, 0.25) 0%, rgba(118, 75, 162, 0.25) 100%);
                color: #ffd700;
                font-weight: 700;
                font-family: 'Orbitron', monospace;
                text-transform: uppercase;
                letter-spacing: 1.5px;
                font-size: 0.95em;
                position: sticky;
                top: 0;
            }}
            
            td {{
                color: #e0e0ff;
                font-weight: 400;
                transition: all 0.3s ease;
            }}
            
            tr:hover {{
                background: rgba(255, 255, 255, 0.08);
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
                text-transform: uppercase;
            }}
            
            .badge-success {{
                background: linear-gradient(135deg, #4CAF50, #45a049);
                color: white;
            }}
            
            .badge-info {{
                background: linear-gradient(135deg, #2196F3, #1976D2);
                color: white;
            }}
            
            .badge-warning {{
                background: linear-gradient(135deg, #FF9800, #F57C00);
                color: white;
            }}
            
            .badge-purple {{
                background: linear-gradient(135deg, #667eea, #764ba2);
                color: white;
            }}
            
            .badge-danger {{
                background: linear-gradient(135deg, #f44336, #d32f2f);
                color: white;
            }}
            
            .file-type {{
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: 35px;
                height: 35px;
                border-radius: 10px;
                margin-right: 12px;
                font-weight: bold;
                font-size: 1.1em;
            }}
            
            .type-text {{ background: linear-gradient(135deg, #4CAF50, #45a049); }}
            .type-photo {{ background: linear-gradient(135deg, #2196F3, #1976D2); }}
            .type-video {{ background: linear-gradient(135deg, #FF9800, #F57C00); }}
            .type-document {{ background: linear-gradient(135deg, #9C27B0, #7B1FA2); }}
            .type-voice {{ background: linear-gradient(135deg, #FF5722, #E64A19); }}
            
            .user-link {{
                color: #ffd700;
                text-decoration: none;
                font-weight: 600;
                transition: all 0.3s ease;
            }}
            
            .user-link:hover {{
                color: #ff6b6b;
                text-decoration: underline;
            }}
            
            .link-url {{
                background: rgba(255,255,255,0.1);
                padding: 8px 12px;
                border-radius: 8px;
                font-family: monospace;
                font-size: 0.9em;
                color: #a0a0ff;
                border: 1px solid rgba(255,255,255,0.2);
            }}
            
            .message-preview {{
                max-width: 300px;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
                color: #b0b0ff;
            }}
            
            .conversation-view {{
                background: rgba(255,255,255,0.05);
                border-radius: 15px;
                padding: 20px;
                margin: 10px 0;
                border-left: 4px solid #667eea;
            }}
            
            .message-bubble {{
                background: rgba(102, 126, 234, 0.2);
                border-radius: 15px;
                padding: 15px;
                margin: 10px 0;
                border: 1px solid rgba(102, 126, 234, 0.3);
            }}
            
            .message-sender {{
                font-weight: bold;
                color: #ffd700;
                margin-bottom: 5px;
            }}
            
            .message-time {{
                font-size: 0.8em;
                color: #a0a0ff;
                float: right;
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
            
            .pulse {{
                animation: pulse 3s infinite;
            }}
            
            @keyframes pulse {{
                0% {{ box-shadow: 0 0 0 0 rgba(102, 126, 234, 0.4); }}
                70% {{ box-shadow: 0 0 0 20px rgba(102, 126, 234, 0); }}
                100% {{ box-shadow: 0 0 0 0 rgba(102, 126, 234, 0); }}
            }}
            
            .floating {{
                animation: floating 4s ease-in-out infinite;
            }}
            
            @keyframes floating {{
                0% {{ transform: translateY(0px); }}
                50% {{ transform: translateY(-15px); }}
                100% {{ transform: translateY(0px); }}
            }}
            
            .footer {{
                text-align: center;
                margin-top: 60px;
                padding: 40px;
                background: linear-gradient(135deg, rgba(102, 126, 234, 0.15) 0%, rgba(118, 75, 162, 0.15) 100%);
                border-radius: 25px;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }}
            
            .footer-text {{
                font-family: 'Orbitron', monospace;
                font-size: 1.3em;
                color: #ffd700;
                letter-spacing: 3px;
                margin-bottom: 15px;
            }}
            
            .user-avatar {{
                width: 45px;
                height: 45px;
                border-radius: 50%;
                background: linear-gradient(135deg, #667eea, #764ba2);
                display: flex;
                align-items: center;
                justify-content: center;
                font-weight: bold;
                color: white;
                margin-right: 12px;
                font-size: 1.2em;
            }}
            
            .progress-bar {{
                width: 100%;
                height: 8px;
                background: rgba(255, 255, 255, 0.1);
                border-radius: 4px;
                overflow: hidden;
                margin-top: 8px;
            }}
            
            .progress-fill {{
                height: 100%;
                background: linear-gradient(90deg, #667eea, #764ba2, #f093fb);
                border-radius: 4px;
                transition: width 1s ease-in-out;
            }}
            
            .search-box {{
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.2);
                border-radius: 15px;
                padding: 15px 20px;
                color: white;
                font-size: 1em;
                width: 100%;
                margin-bottom: 20px;
                backdrop-filter: blur(10px);
            }}
            
            .search-box::placeholder {{
                color: #a0a0ff;
            }}
            
            .view-conversation-btn {{
                background: linear-gradient(135deg, #667eea, #764ba2);
                color: white;
                border: none;
                padding: 8px 15px;
                border-radius: 10px;
                cursor: pointer;
                font-size: 0.9em;
                transition: all 0.3s ease;
            }}
            
            .view-conversation-btn:hover {{
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
            }}
            
            @media (max-width: 1200px) {{
                .dashboard {{
                    grid-template-columns: 1fr;
                }}
                
                .sidebar {{
                    display: none;
                }}
            }}
            
            @media (max-width: 768px) {{
                .header h1 {{
                    font-size: 2.8em;
                }}
                
                .stats-grid {{
                    grid-template-columns: 1fr;
                }}
                
                th, td {{
                    padding: 12px 15px;
                    font-size: 0.9em;
                }}
                
                .section {{
                    padding: 25px;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <!-- Заголовок -->
            <div class="header fade-in">
                <div class="header-content">
                    <h1 class="floating"><i class="fas fa-robot"></i> АДМИН ПАНЕЛЬ</h1>
                    <div class="subtitle">Расширенная система мониторинга анонимного бота</div>
                    <div class="timestamp pulse">
                        <i class="fas fa-clock"></i> Отчет сгенерирован: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    </div>
                </div>
            </div>
            
            <div class="dashboard">
                <!-- Боковая панель -->
                <div class="sidebar fade-in">
                    <div class="nav-item active" onclick="showSection('stats')">
                        <div class="nav-icon"><i class="fas fa-tachometer-alt"></i></div>
                        <div>Общая статистика</div>
                    </div>
                    <div class="nav-item" onclick="showSection('users')">
                        <div class="nav-icon"><i class="fas fa-users"></i></div>
                        <div>Пользователи ({data['stats']['users']})</div>
                    </div>
                    <div class="nav-item" onclick="showSection('links')">
                        <div class="nav-icon"><i class="fas fa-link"></i></div>
                        <div>Ссылки ({data['stats']['links']})</div>
                    </div>
                    <div class="nav-item" onclick="showSection('messages')">
                        <div class="nav-icon"><i class="fas fa-envelope"></i></div>
                        <div>Сообщения ({data['stats']['messages']})</div>
                    </div>
                    <div class="nav-item" onclick="showSection('conversations')">
                        <div class="nav-icon"><i class="fas fa-comments"></i></div>
                        <div>Переписки</div>
                    </div>
                </div>
                
                <!-- Основной контент -->
                <div class="main-content">
                    <!-- Общая статистика -->
                    <div id="stats-section" class="section-section">
                        <!-- Основная статистика -->
                        <div class="stats-grid">
                            <div class="stat-card fade-in">
                                <h3>{data['stats']['users']}</h3>
                                <p><i class="fas fa-users"></i> Всего пользователей</p>
                                <div class="progress-bar">
                                    <div class="progress-fill" style="width: {min(data['stats']['users'] * 2, 100)}%"></div>
                                </div>
                            </div>
                            <div class="stat-card fade-in">
                                <h3>{data['stats']['links']}</h3>
                                <p><i class="fas fa-link"></i> Активных ссылок</p>
                                <div class="progress-bar">
                                    <div class="progress-fill" style="width: {min(data['stats']['links'] * 5, 100)}%"></div>
                                </div>
                            </div>
                            <div class="stat-card fade-in">
                                <h3>{data['stats']['messages']}</h3>
                                <p><i class="fas fa-envelope"></i> Всего сообщений</p>
                                <div class="progress-bar">
                                    <div class="progress-fill" style="width: {min(data['stats']['messages'] * 0.5, 100)}%"></div>
                                </div>
                            </div>
                            <div class="stat-card fade-in">
                                <h3>{data['stats']['replies']}</h3>
                                <p><i class="fas fa-reply"></i> Ответов</p>
                                <div class="progress-bar">
                                    <div class="progress-fill" style="width: {min(data['stats']['replies'] * 2, 100)}%"></div>
                                </div>
                            </div>
                        </div>
                        
                        <!-- Статистика файлов -->
                        <div class="stats-grid">
                            <div class="stat-card fade-in">
                                <h3>{data['stats']['photos']}</h3>
                                <p><i class="fas fa-image"></i> Фотографий</p>
                            </div>
                            <div class="stat-card fade-in">
                                <h3>{data['stats']['videos']}</h3>
                                <p><i class="fas fa-video"></i> Видео</p>
                            </div>
                            <div class="stat-card fade-in">
                                <h3>{data['stats']['documents']}</h3>
                                <p><i class="fas fa-file"></i> Документов</p>
                            </div>
                            <div class="stat-card fade-in">
                                <h3>{data['stats']['voice']}</h3>
                                <p><i class="fas fa-microphone"></i> Голосовых</p>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Пользователи -->
                    <div id="users-section" class="section" style="display: none;">
                        <h2><i class="fas fa-users"></i> АКТИВНЫЕ ПОЛЬЗОВАТЕЛИ</h2>
                        <input type="text" class="search-box" placeholder="🔍 Поиск пользователей..." onkeyup="searchTable('users-table', this)">
                        <table id="users-table">
                            <thead>
                                <tr>
                                    <th>ID</th>
                                    <th>Информация</th>
                                    <th>Регистрация</th>
                                    <th>Активность</th>
                                    <th>Статистика</th>
                                    <th>Действия</th>
                                </tr>
                            </thead>
                            <tbody>
    '''
    
    for user in data['users'][:20]:
        username_display = f"@{user[1]}" if user[1] else (html.escape(user[2]) if user[2] else f"ID:{user[0]}")
        created = user[3].split()[0] if isinstance(user[3], str) else user[3].strftime("%Y-%m-%d")
        
        html_content += f'''
                                <tr>
                                    <td><span class="badge badge-purple">{user[0]}</span></td>
                                    <td>
                                        <div style="display: flex; align-items: center;">
                                            <div class="user-avatar">
                                                {username_display[0].upper() if username_display else 'U'}
                                            </div>
                                            <div>
                                                <div style="font-weight: 600; font-size: 1.1em;">{username_display}</div>
                                                <div style="font-size: 0.85em; color: #a0a0ff;">{html.escape(user[2]) if user[2] else 'No Name'}</div>
                                            </div>
                                        </div>
                                    </td>
                                    <td class="timestamp">{created}</td>
                                    <td>
                                        <span class="badge badge-info">
                                            <i class="fas fa-link"></i> {user[4]} ссылок
                                        </span>
                                    </td>
                                    <td>
                                        <span class="badge badge-success">
                                            <i class="fas fa-envelope"></i> {user[5]}
                                        </span>
                                        <span class="badge badge-warning">
                                            <i class="fas fa-paper-plane"></i> {user[6]}
                                        </span>
                                    </td>
                                    <td>
                                        <button class="view-conversation-btn" onclick="viewUserConversation({user[0]})">
                                            <i class="fas fa-comments"></i> Переписка
                                        </button>
                                    </td>
                                </tr>
        '''
    
    html_content += '''
                            </tbody>
                        </table>
                    </div>
                    
                    <!-- Ссылки -->
                    <div id="links-section" class="section" style="display: none;">
                        <h2><i class="fas fa-link"></i> АКТИВНЫЕ ССЫЛКИ</h2>
                        <input type="text" class="search-box" placeholder="🔍 Поиск ссылок..." onkeyup="searchTable('links-table', this)">
                        <table id="links-table">
                            <thead>
                                <tr>
                                    <th>ID Ссылки</th>
                                    <th>Название</th>
                                    <th>Владелец</th>
                                    <th>Создана</th>
                                    <th>Сообщения</th>
                                    <th>Действия</th>
                                </tr>
                            </thead>
                            <tbody>
    '''
    
    for link in data['links'][:25]:
        owner = f"@{link[5]}" if link[5] else (html.escape(link[6]) if link[6] else f"ID:{link[7]}")
        created = link[3].split()[0] if isinstance(link[3], str) else link[3].strftime("%Y-%m-%d")
        
        html_content += f'''
                                <tr>
                                    <td><code class="link-url">{link[0]}</code></td>
                                    <td>
                                        <div style="font-weight: 600; font-size: 1.1em;">{html.escape(link[1])}</div>
                                        <div style="font-size: 0.85em; color: #a0a0ff;">{html.escape(link[2]) if link[2] else 'Без описания'}</div>
                                    </td>
                                    <td>
                                        <a href="#" class="user-link">{owner}</a>
                                    </td>
                                    <td class="timestamp">{created}</td>
                                    <td>
                                        <span class="badge { 'badge-success' if link[8] > 0 else 'badge-warning' }">
                                            <i class="fas fa-envelope"></i> {link[8]} сообщ.
                                        </span>
                                    </td>
                                    <td>
                                        <button class="view-conversation-btn" onclick="viewLinkMessages('{link[0]}')">
                                            <i class="fas fa-eye"></i> Сообщения
                                        </button>
                                    </td>
                                </tr>
        '''
    
    html_content += '''
                            </tbody>
                        </table>
                    </div>
                    
                    <!-- Сообщения -->
                    <div id="messages-section" class="section" style="display: none;">
                        <h2><i class="fas fa-envelope"></i> ПОСЛЕДНИЕ СООБЩЕНИЯ</h2>
                        <input type="text" class="search-box" placeholder="🔍 Поиск сообщений..." onkeyup="searchTable('messages-table', this)">
                        <table id="messages-table">
                            <thead>
                                <tr>
                                    <th>Тип</th>
                                    <th>От</th>
                                    <th>Кому</th>
                                    <th>Ссылка</th>
                                    <th>Сообщение</th>
                                    <th>Время</th>
                                    <th>Размер</th>
                                </tr>
                            </thead>
                            <tbody>
    '''
    
    for msg in data['recent_messages'][:30]:
        msg_type_icon = {
            'text': '📝',
            'photo': '🖼️',
            'video': '🎥',
            'document': '📄',
            'voice': '🎤'
        }.get(msg[2], '📄')
        
        type_class = {
            'text': 'type-text',
            'photo': 'type-photo',
            'video': 'type-video',
            'document': 'type-document',
            'voice': 'type-voice'
        }.get(msg[2], 'type-text')
        
        file_size = f"{(msg[3] // 1024):,} KB" if msg[3] else '-'
        from_user = f"@{msg[6]}" if msg[6] else (html.escape(msg[7]) if msg[7] else f"ID:{msg[8]}")
        to_user = f"@{msg[9]}" if msg[9] else (html.escape(msg[10]) if msg[10] else f"ID:{msg[11]}")
        time_display = msg[5].split()[1][:5] if isinstance(msg[5], str) else msg[5].strftime("%H:%M:%S")
        message_preview = html.escape(msg[1][:50] + '...' if len(msg[1]) > 50 else msg[1]) if msg[1] else f"Медиа: {msg[2]}"
        
        html_content += f'''
                                <tr>
                                    <td>
                                        <div style="display: flex; align-items: center;">
                                            <div class="file-type {type_class}">
                                                {msg_type_icon}
                                            </div>
                                            <span style="text-transform: uppercase; font-size: 0.8em; font-weight: 600;">{msg[2]}</span>
                                        </div>
                                    </td>
                                    <td>
                                        <a href="#" class="user-link">{from_user}</a>
                                    </td>
                                    <td>
                                        <a href="#" class="user-link">{to_user}</a>
                                    </td>
                                    <td>
                                        <span class="badge badge-purple">{html.escape(msg[12]) if msg[12] else 'N/A'}</span>
                                    </td>
                                    <td class="message-preview" title="{html.escape(msg[1]) if msg[1] else ''}">
                                        {message_preview}
                                    </td>
                                    <td class="timestamp">{time_display}</td>
                                    <td>{file_size}</td>
                                </tr>
        '''
    
    html_content += '''
                            </tbody>
                        </table>
                    </div>
                    
                    <!-- Переписки -->
                    <div id="conversations-section" class="section" style="display: none;">
                        <h2><i class="fas fa-comments"></i> ПРОСМОТР ПЕРЕПИСОК</h2>
                        <div class="conversation-view">
                            <h3><i class="fas fa-info-circle"></i> Выберите пользователя или ссылку для просмотра переписки</h3>
                            <p>Используйте кнопки "Переписка" в таблице пользователей или "Сообщения" в таблице ссылок для просмотра полной истории сообщений.</p>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Футер -->
            <div class="footer fade-in">
                <div class="footer-text">
                    <i class="fas fa-robot"></i> АНОНИМНЫЙ БОТ | РАСШИРЕННАЯ СИСТЕМА МОНИТОРИНГА
                </div>
                <div style="margin-top: 20px; color: #a0a0ff; font-size: 1em;">
                    <i class="fas fa-shield-alt"></i> Защищенная система | <i class="fas fa-bolt"></i> Реальное время | <i class="fas fa-chart-line"></i> Полная аналитика
                </div>
                <div style="margin-top: 15px; color: #ffd700; font-family: 'Orbitron', monospace; font-size: 0.9em;">
                    SIROK228 | POWERED BY ADVANCED AI TECHNOLOGY
                </div>
            </div>
        </div>
        
        <script>
            // Навигация по разделам
            function showSection(sectionName) {{
                // Скрываем все разделы
                document.querySelectorAll('.section, .section-section').forEach(section => {{
                    section.style.display = 'none';
                }});
                
                // Показываем выбранный раздел
                const targetSection = document.getElementById(sectionName + '-section');
                if (targetSection) {{
                    targetSection.style.display = 'block';
                }}
                
                // Обновляем активную кнопку навигации
                document.querySelectorAll('.nav-item').forEach(item => {{
                    item.classList.remove('active');
                }});
                event.currentTarget.classList.add('active');
            }}
            
            // Поиск по таблицам
            function searchTable(tableId, input) {{
                const searchTerm = input.value.toLowerCase();
                const table = document.getElementById(tableId);
                const rows = table.querySelectorAll('tbody tr');
                
                rows.forEach(row => {{
                    const text = row.textContent.toLowerCase();
                    row.style.display = text.includes(searchTerm) ? '' : 'none';
                }});
            }}
            
            // Просмотр переписки пользователя
            function viewUserConversation(userId) {{
                showSection('conversations');
                const section = document.getElementById('conversations-section');
                section.innerHTML = `
                    <h2><i class="fas fa-comments"></i> ПЕРЕПИСКА ПОЛЬЗОВАТЕЛЯ ID: ${{userId}}</h2>
                    <div class="conversation-view">
                        <div class="message-bubble">
                            <div class="message-sender">Пользователь ${{userId}} <span class="message-time">2024-01-01 12:00</span></div>
                            <div>Пример сообщения от пользователя</div>
                        </div>
                        <div class="message-bubble">
                            <div class="message-sender">Аноним <span class="message-time">2024-01-01 12:05</span></div>
                            <div>Пример ответа анонима</div>
                        </div>
                        <button class="view-conversation-btn" onclick="showSection('users')" style="margin-top: 20px;">
                            <i class="fas fa-arrow-left"></i> Назад к пользователям
                        </button>
                    </div>
                `;
            }}
            
            // Просмотр сообщений ссылки
            function viewLinkMessages(linkId) {{
                showSection('conversations');
                const section = document.getElementById('conversations-section');
                section.innerHTML = `
                    <h2><i class="fas fa-comments"></i> СООБЩЕНИЯ ССЫЛКИ: ${{linkId}}</h2>
                    <div class="conversation-view">
                        <div class="message-bubble">
                            <div class="message-sender">Аноним <span class="message-time">2024-01-01 12:00</span></div>
                            <div>Пример анонимного сообщения через ссылку</div>
                        </div>
                        <div class="message-bubble">
                            <div class="message-sender">Владелец ссылки <span class="message-time">2024-01-01 12:05</span></div>
                            <div>Пример ответа владельца ссылки</div>
                        </div>
                        <button class="view-conversation-btn" onclick="showSection('links')" style="margin-top: 20px;">
                            <i class="fas fa-arrow-left"></i> Назад к ссылкам
                        </button>
                    </div>
                `;
            }}
            
            // Анимации при прокрутке
            const observerOptions = {{
                threshold: 0.05,
                rootMargin: '0px 0px -50px 0px'
            }};
            
            const observer = new IntersectionObserver((entries) => {{
                entries.forEach(entry => {{
                    if (entry.isIntersecting) {{
                        entry.target.style.opacity = '1';
                        entry.target.style.transform = 'translateY(0)';
                        entry.target.style.animation = 'fadeInUp 0.8s ease-out forwards';
                    }}
                }});
            }}, observerOptions);
            
            document.querySelectorAll('.section, .stat-card').forEach(el => {{
                el.style.opacity = '0';
                el.style.transform = 'translateY(40px)';
                el.style.transition = 'all 0.8s ease-out';
                observer.observe(el);
            }});
            
            // Анимация прогресс-баров
            setTimeout(() => {{
                document.querySelectorAll('.progress-fill').forEach(bar => {{
                    const width = bar.style.width;
                    bar.style.width = '0';
                    setTimeout(() => {{
                        bar.style.transition = 'width 2s ease-in-out';
                        bar.style.width = width;
                    }}, 200);
                }});
            }}, 500);
            
            // Показываем раздел статистики по умолчанию
            showSection('stats');
        </script>
    </body>
    </html>
    '''
    
    return html_content

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def escape_markdown_v2(text: str) -> str:
    """Экранирует специальные символы для MarkdownV2"""
    if not text: 
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

def format_as_quote(text: str) -> str:
    if not text: 
        return ""
    escaped_text = escape_markdown_v2(text)
    return '\n'.join([f"> {line}" for line in escaped_text.split('\n')])

def format_datetime(dt_string):
    """Форматирует дату-время с точностью до секунд"""
    if isinstance(dt_string, str):
        return dt_string
    return dt_string.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt_string, 'strftime') else str(dt_string)

# --- КЛАВИАТУРЫ ---

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟣 Главное меню", callback_data="main_menu")],
        [InlineKeyboardButton("🔗 Мои ссылки", callback_data="my_links")],
        [InlineKeyboardButton("➕ Создать ссылку", callback_data="create_link")],
        [InlineKeyboardButton("📨 Мои сообщения", callback_data="my_messages")]
    ])

def message_details_keyboard(message_id, user_id, is_admin=False):
    """Клавиатура для сообщения с опцией удаления"""
    buttons = [
        [InlineKeyboardButton("💬 Ответить", callback_data=f"reply_{message_id}")],
        [InlineKeyboardButton("📋 Просмотреть ответы", callback_data=f"view_replies_{message_id}")],
    ]
    
    # Добавляем кнопку удаления если пользователь владелец или админ
    message_owner = get_message_owner(message_id)
    if message_owner and (message_owner[0] == user_id or is_admin):
        buttons.append([InlineKeyboardButton("🗑️ Удалить сообщение", callback_data=f"confirm_delete_message_{message_id}")])
    
    buttons.append([InlineKeyboardButton("🔙 Назад к сообщениям", callback_data="my_messages")])
    
    return InlineKeyboardMarkup(buttons)

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Управление пользователями", callback_data="admin_users_management")],
        [InlineKeyboardButton("🎨 HTML Отчет", callback_data="admin_html_report")],
        [InlineKeyboardButton("📢 Оповещение", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🛑 Остановить бота", callback_data="admin_shutdown")],
        [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
    ])

def delete_confirmation_keyboard(item_type, item_id):
    """Клавиатура подтверждения удаления"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, удалить", callback_data=f"delete_{item_type}_{item_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_delete")]
    ])

def shutdown_confirmation_keyboard():
    """Клавиатура подтверждения остановки бота"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 ДА, ОСТАНОВИТЬ БОТА", callback_data="confirm_shutdown")],
        [InlineKeyboardButton("✅ Продолжить работу", callback_data="admin_panel")]
    ])

def back_to_main_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]])

def back_to_admin_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В админку", callback_data="admin_panel")]])

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
            context.user_data['admin_authenticated'] = False
            await update.message.reply_text(
                "🔐 *Панель администратора*\n\nВведите пароль для доступа:",
                parse_mode='MarkdownV2',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="main_menu")]])
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

        # Проверка на остановку бота
        if bot_shutdown_requested:
            await query.edit_message_text("🛑 *Бот остановлен*\n\nДля перезапуска необходимо развернуть новую версию\\.", parse_mode='MarkdownV2')
            return

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
                    # Добавляем кнопку удаления для каждой ссылки
                    text += f"🗑️ *Удалить ссылку* \\- /delete\\_link\\_{link[0]}\n\n"
                await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
            else:
                await query.edit_message_text("У вас пока нет созданных ссылок\\.", reply_markup=back_to_main_keyboard(), parse_mode='MarkdownV2')
            return
        
        elif data == "my_messages":
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
                    text += f"{type_icon} *{escape_markdown_v2(link_title)}*\n{format_as_quote(preview)}\n🕒 `{created_str}` \\| 💬 Ответов\\: {reply_count}\n\n"
                
                await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
            else:
                await query.edit_message_text("У вас пока нет сообщений\\.", parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
            return
        
        elif data == "create_link":
            context.user_data['creating_link'] = True
            context.user_data['link_stage'] = 'title'
            await query.edit_message_text("📝 Введите *название* для вашей ссылки:", parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
            return
        
        # Управление удалением
        elif data.startswith("confirm_delete_message_"):
            message_id = int(data.replace("confirm_delete_message_", ""))
            message_info = get_message_info(message_id)
            
            if message_info:
                msg_text, msg_type, file_name, created, from_user, from_name, to_user, to_name, link_title = message_info
                
                text = f"🗑️ *Подтверждение удаления сообщения*\n\n"
                text += f"📝 *Сообщение\\:*\n{format_as_quote(msg_text if msg_text else f'Медиафайл: {msg_type}')}\n\n"
                text += f"👤 *От\\:* {escape_markdown_v2(from_user or from_name or 'Аноним')}\n"
                text += f"👥 *Кому\\:* {escape_markdown_v2(to_user or to_name or 'Аноним')}\n"
                text += f"🔗 *Ссылка\\:* {escape_markdown_v2(link_title or 'N/A')}\n"
                text += f"🕒 *Время\\:* `{format_datetime(created)}`\n\n"
                text += "❓ *Вы уверены, что хотите удалить это сообщение?*"
                
                await query.edit_message_text(text, parse_mode='MarkdownV2', 
                                           reply_markup=delete_confirmation_keyboard("message", message_id))
            return
        
        elif data.startswith("delete_message_"):
            message_id = int(data.replace("delete_message_", ""))
            success = deactivate_message(message_id)
            
            if success:
                push_db_to_github(f"Delete message {message_id}")
                await query.edit_message_text("✅ *Сообщение успешно удалено\\!*", 
                                           parse_mode='MarkdownV2', 
                                           reply_markup=back_to_main_keyboard())
            else:
                await query.edit_message_text("❌ *Ошибка при удалении сообщения*", 
                                           parse_mode='MarkdownV2', 
                                           reply_markup=back_to_main_keyboard())
            return
        
        elif data == "cancel_delete":
            await query.edit_message_text("❌ *Удаление отменено*", 
                                       parse_mode='MarkdownV2', 
                                       reply_markup=back_to_main_keyboard())
            return

        # АДМИН ПАНЕЛЬ
        if is_admin:
            if data == "admin_panel":
                if context.user_data.get('admin_authenticated'):
                    await query.edit_message_text("🛠️ *Панель администратора*", reply_markup=admin_keyboard(), parse_mode='MarkdownV2')
                else:
                    await query.edit_message_text("🔐 *Требуется аутентификация*\n\nВведите пароль:", parse_mode='MarkdownV2')
            
            elif data == "admin_stats":
                if context.user_data.get('admin_authenticated'):
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
                else:
                    await query.answer("❌ Требуется аутентификация\\!", show_alert=True)
            
            elif data == "admin_users_management":
                if context.user_data.get('admin_authenticated'):
                    users = get_all_users_for_admin()
                    if users:
                        text = "👥 *Управление пользователями*\n\n"
                        for u in users[:10]:
                            username = f"@{u[1]}" if u[1] else (u[2] or f"ID\\:{u[0]}")
                            text += f"👤 *{escape_markdown_v2(username)}*\n🆔 `{u[0]}` \\| 📅 `{format_datetime(u[3])}`\n\n"
                        await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                    else:
                        await query.edit_message_text("Пользователей не найдено\\.", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                else:
                    await query.answer("❌ Требуется аутентификация\\!", show_alert=True)
            
            elif data == "admin_html_report":
                if context.user_data.get('admin_authenticated'):
                    await query.edit_message_text("🔄 *Генерация HTML отчета\\.\\.\\.*", parse_mode='MarkdownV2')
                    
                    html_content = generate_html_report()
                    
                    report_path = "/tmp/admin_report.html"
                    with open(report_path, 'w', encoding='utf-8') as f:
                        f.write(html_content)
                    
                    with open(report_path, 'rb') as f:
                        await query.message.reply_document(
                            document=f,
                            filename=f"admin_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
                            caption="🎨 *Расширенный HTML отчет администратора*\n\nОткройте файл в браузере для просмотра полной статистики с анимациями и поиском\\!",
                            parse_mode='MarkdownV2'
                        )
                    
                    await query.edit_message_text("✅ *HTML отчет сгенерирован и отправлен\\!*\n\nПроверьте файл выше\\!", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                else:
                    await query.answer("❌ Требуется аутентификация\\!", show_alert=True)
            
            elif data == "admin_broadcast":
                if context.user_data.get('admin_authenticated'):
                    context.user_data['broadcasting'] = True
                    await query.edit_message_text(
                        "📢 *Режим рассылки*\n\nВведите сообщение для отправки всем пользователям\\:",
                        parse_mode='MarkdownV2', 
                        reply_markup=back_to_admin_keyboard()
                    )
                else:
                    await query.answer("❌ Требуется аутентификация\\!", show_alert=True)
            
            elif data == "admin_shutdown":
                if context.user_data.get('admin_authenticated'):
                    await query.edit_message_text(
                        "🛑 *ЭКСТРЕННАЯ ОСТАНОВКА БОТА*\n\n⚠️ *ВНИМАНИЕ\\!* Это действие полностью остановит бота\\.\n\n*Для перезапуска потребуется*\\:\n• Ручной рестарт в панели Render\n• Или новое развертывание\n\n❓ *Вы уверены, что хотите остановить бота?*",
                        parse_mode='MarkdownV2',
                        reply_markup=shutdown_confirmation_keyboard()
                    )
                else:
                    await query.answer("❌ Требуется аутентификация\\!", show_alert=True)
            
            elif data == "confirm_shutdown":
                if context.user_data.get('admin_authenticated'):
                    # Используем глобальную переменную напрямую
                    global bot_shutdown_requested
                    bot_shutdown_requested = True
                    
                    # Создаем резервную копию перед остановкой
                    backup_database()
                    
                    await query.edit_message_text(
                        "🛑 *БОТ ОСТАНАВЛИВАЕТСЯ*\n\n📦 *Создана резервная копия базы данных*\n⚡ *Все процессы завершаются*\n\n*Бот будет полностью остановлен через несколько секунд\\.*\n\n*Для перезапуска*\\:\n1\\. Зайдите в Render Dashboard\n2\\. Найдите ваш сервис\n3\\. Нажмите \\\"Manual Restart\\\"",
                        parse_mode='MarkdownV2'
                    )
                    
                    # Даем время на отправку сообщения перед остановкой
                    await asyncio.sleep(3)
                    
                    # Останавливаем бота
                    logging.critical("🛑 BOT SHUTDOWN INITIATED BY ADMIN")
                    os._exit(0)
                    
                else:
                    await query.answer("❌ Требуется аутентификация\\!", show_alert=True)

    except Exception as e:
        logging.error(f"Ошибка в обработчике кнопок: {e}")
        try:
            await query.edit_message_text("❌ Произошла ошибка\\. Попробуйте позже\\.", reply_markup=main_keyboard(), parse_mode='MarkdownV2')
        except:
            pass

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Проверка на остановку бота
        if bot_shutdown_requested:
            await update.message.reply_text("🛑 *Бот остановлен*\n\nДля перезапуска необходимо развернуть новую версию\\.", parse_mode='MarkdownV2')
            return

        user = update.effective_user
        text = update.message.text
        save_user(user.id, user.username, user.first_name)
        is_admin = user.username == ADMIN_USERNAME or user.id == ADMIN_ID

        # Проверка пароля для админки
        if text == ADMIN_PASSWORD and is_admin:
            context.user_data['admin_authenticated'] = True
            await update.message.reply_text(
                "✅ *Пароль принят\\! Добро пожаловать в админ\\-панель\\.*", 
                reply_markup=admin_keyboard(), 
                parse_mode='MarkdownV2'
            )
            return

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
                notification = f"📨 *Новое анонимное сообщение*\n\n{format_as_quote(text)}"
                try:
                    await context.bot.send_message(link_info[1], notification, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id, link_info[1], is_admin))
                except Exception as e:
                    logging.error(f"Failed to send message notification: {e}")
                
                await update.message.reply_text("✅ Ваше сообщение отправлено анонимно\\!", reply_markup=main_keyboard(), parse_mode='MarkdownV2')
            return

        # Команды удаления через текст
        if text.startswith('/delete_link_'):
            link_id = text.replace('/delete_link_', '').strip()
            link_owner = get_link_owner(link_id)
            
            if link_owner and (link_owner[0] == user.id or is_admin):
                success = deactivate_link(link_id)
                if success:
                    push_db_to_github(f"Delete link {link_id}")
                    await update.message.reply_text("✅ *Ссылка успешно удалена\\!*", parse_mode='MarkdownV2', reply_markup=main_keyboard())
                else:
                    await update.message.reply_text("❌ *Ошибка при удалении ссылки*", parse_mode='MarkdownV2', reply_markup=main_keyboard())
            else:
                await update.message.reply_text("⛔️ *У вас нет прав для удаления этой ссылки*", parse_mode='MarkdownV2', reply_markup=main_keyboard())
            return

        await update.message.reply_text("Используйте кнопки для навигации\\.", reply_markup=main_keyboard(), parse_mode='MarkdownV2')

    except Exception as e:
        logging.error(f"Ошибка в обработчике текста: {e}")
        await update.message.reply_text("❌ Произошла ошибка\\. Попробуйте позже\\.", parse_mode='MarkdownV2')

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Проверка на остановку бота
        if bot_shutdown_requested:
            await update.message.reply_text("🛑 *Бот остановлен*", parse_mode='MarkdownV2')
            return

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
                
                user_caption = f"📨 *Новый анонимный {msg_type}*{file_info}\n\n{format_as_quote(caption)}"
                
                try:
                    if msg_type == 'photo': 
                        await context.bot.send_photo(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id, link_info[1], False))
                    elif msg_type == 'video': 
                        await context.bot.send_video(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id, link_info[1], False))
                    elif msg_type == 'document': 
                        await context.bot.send_document(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id, link_info[1], False))
                    elif msg_type == 'voice': 
                        await context.bot.send_voice(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id, link_info[1], False))
                except Exception as e: 
                    logging.error(f"Failed to send media to user: {e}")
                
                await update.message.reply_text("✅ Ваше медиа отправлено анонимно\\!", reply_markup=main_keyboard(), parse_mode='MarkdownV2')

    except Exception as e:
        logging.error(f"Ошибка в обработчике медиа: {e}")
        await update.message.reply_text("❌ Произошла ошибка при отправке медиа\\.", parse_mode='MarkdownV2')

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
