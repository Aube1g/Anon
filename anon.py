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
               (SELECT COUNT(*) FROM replies r WHERE r.message_id = m.message_id) as reply_count
        FROM messages m 
        JOIN links l ON m.link_id = l.link_id 
        WHERE m.to_user_id = ? 
        ORDER BY m.created_at DESC LIMIT ?
    ''', (user_id, limit), fetch="all")

def get_message_replies(message_id):
    return run_query('''
        SELECT r.reply_id, r.reply_text, r.created_at, u.username, u.first_name
        FROM replies r
        LEFT JOIN users u ON r.from_user_id = u.user_id
        WHERE r.message_id = ?
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
        WHERE m.from_user_id = ? OR m.to_user_id = ? 
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
        WHERE m.link_id = ?
        
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
        WHERE m.link_id = ?
        
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
        WHERE m.from_user_id = ? OR m.to_user_id = ?
        
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
        WHERE r.from_user_id = ?
        
        ORDER BY created_at ASC
    ''', (user_id, user_id, user_id), fetch="all")

def get_all_users_for_admin():
    return run_query("SELECT user_id, username, first_name, created_at FROM users ORDER BY created_at DESC", fetch="all")

def get_admin_stats():
    stats = {}
    try:
        stats['users'] = run_query("SELECT COUNT(*) FROM users", fetch="one")[0] or 0
        stats['links'] = run_query("SELECT COUNT(*) FROM links WHERE is_active = 1", fetch="one")[0] or 0
        stats['messages'] = run_query("SELECT COUNT(*) FROM messages", fetch="one")[0] or 0
        stats['replies'] = run_query("SELECT COUNT(*) FROM replies", fetch="one")[0] or 0
        
        stats['photos'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'photo'", fetch="one")[0] or 0
        stats['videos'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'video'", fetch="one")[0] or 0
        stats['documents'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'document'", fetch="one")[0] or 0
        stats['voice'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'voice'", fetch="one")[0] or 0
    except Exception as e:
        logging.error(f"Ошибка при получении статистики: {e}")
        # Возвращаем значения по умолчанию
        stats = {'users': 0, 'links': 0, 'messages': 0, 'replies': 0, 'photos': 0, 'videos': 0, 'documents': 0, 'voice': 0}
    
    return stats

def get_all_data_for_html():
    data = {}
    try:
        data['stats'] = get_admin_stats()
        data['users'] = run_query('''
            SELECT u.user_id, u.username, u.first_name, u.created_at,
                   (SELECT COUNT(*) FROM links l WHERE l.user_id = u.user_id) as link_count,
                   (SELECT COUNT(*) FROM messages m WHERE m.to_user_id = u.user_id) as received_messages,
                   (SELECT COUNT(*) FROM messages m WHERE m.from_user_id = u.user_id) as sent_messages
            FROM users u
            ORDER BY u.created_at DESC
        ''', fetch="all") or []
        
        data['links'] = run_query('''
            SELECT l.link_id, l.title, l.description, l.created_at, l.expires_at,
                   u.username, u.first_name, u.user_id,
                   (SELECT COUNT(*) FROM messages m WHERE m.link_id = l.link_id) as message_count
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
            WHERE m.message_type != 'text'
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
            
            :root {{
                --primary: #8B5CF6;
                --primary-dark: #7C3AED;
                --primary-light: #A78BFA;
                --secondary: #EC4899;
                --accent: #06B6D4;
                --background: #0F0F23;
                --surface: #1A1A2E;
                --surface-light: #252547;
                --text: #FFFFFF;
                --text-secondary: #A5B4FC;
                --success: #10B981;
                --warning: #F59E0B;
                --danger: #EF4444;
            }}
            
            body {{
                font-family: 'Exo 2', sans-serif;
                background: linear-gradient(135deg, var(--background) 0%, #1E1B4B 50%, var(--surface) 100%);
                min-height: 100vh;
                padding: 20px;
                color: var(--text);
                overflow-x: hidden;
            }}
            
            .container {{
                max-width: 1800px;
                margin: 0 auto;
            }}
            
            /* Preloader */
            .preloader {{
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: var(--background);
                display: flex;
                justify-content: center;
                align-items: center;
                z-index: 9999;
                transition: opacity 0.5s ease;
            }}
            
            .preloader-content {{
                text-align: center;
            }}
            
            .loader {{
                width: 80px;
                height: 80px;
                border: 4px solid var(--surface-light);
                border-top: 4px solid var(--primary);
                border-radius: 50%;
                animation: spin 1s linear infinite;
                margin: 0 auto 20px;
            }}
            
            @keyframes spin {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
            
            .header {{
                background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
                padding: 60px 40px;
                border-radius: 30px;
                margin-bottom: 40px;
                text-align: center;
                position: relative;
                overflow: hidden;
                box-shadow: 0 25px 50px rgba(139, 92, 246, 0.3);
                animation: headerSlide 1s ease-out;
            }}
            
            @keyframes headerSlide {{
                from {{ transform: translateY(-50px); opacity: 0; }}
                to {{ transform: translateY(0); opacity: 1; }}
            }}
            
            .header::before {{
                content: '';
                position: absolute;
                top: -50%;
                left: -50%;
                width: 200%;
                height: 200%;
                background: linear-gradient(45deg, transparent, rgba(255, 255, 255, 0.1), transparent);
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
                font-size: 4.5em;
                margin-bottom: 20px;
                background: linear-gradient(135deg, #FFFFFF 0%, #A5B4FC 50%, #8B5CF6 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
                text-shadow: 0 0 60px rgba(139, 92, 246, 0.5);
                font-weight: 900;
                letter-spacing: 4px;
                animation: textGlow 3s ease-in-out infinite alternate;
            }}
            
            @keyframes textGlow {{
                from {{ text-shadow: 0 0 60px rgba(139, 92, 246, 0.5); }}
                to {{ text-shadow: 0 0 80px rgba(236, 72, 153, 0.6), 0 0 100px rgba(139, 92, 246, 0.5); }}
            }}
            
            .header .subtitle {{
                font-size: 1.6em;
                color: #E0E7FF;
                margin-bottom: 25px;
                font-weight: 300;
                animation: fadeIn 2s ease-in;
            }}
            
            .timestamp {{
                font-family: 'Orbitron', monospace;
                font-size: 1.1em;
                color: var(--warning);
                background: rgba(0, 0, 0, 0.3);
                padding: 15px 25px;
                border-radius: 30px;
                display: inline-block;
                border: 2px solid rgba(245, 158, 11, 0.3);
                box-shadow: 0 8px 25px rgba(245, 158, 11, 0.2);
                animation: pulse 2s infinite;
            }}
            
            @keyframes pulse {{
                0% {{ transform: scale(1); box-shadow: 0 8px 25px rgba(245, 158, 11, 0.2); }}
                50% {{ transform: scale(1.05); box-shadow: 0 12px 35px rgba(245, 158, 11, 0.4); }}
                100% {{ transform: scale(1); box-shadow: 0 8px 25px rgba(245, 158, 11, 0.2); }}
            }}
            
            .dashboard {{
                display: grid;
                grid-template-columns: 320px 1fr;
                gap: 35px;
                margin-bottom: 40px;
                animation: contentSlide 0.8s ease-out 0.3s both;
            }}
            
            @keyframes contentSlide {{
                from {{ transform: translateY(30px); opacity: 0; }}
                to {{ transform: translateY(0); opacity: 1; }}
            }}
            
            .sidebar {{
                background: rgba(255, 255, 255, 0.1);
                backdrop-filter: blur(20px);
                padding: 35px;
                border-radius: 25px;
                border: 1px solid rgba(255, 255, 255, 0.15);
                height: fit-content;
                box-shadow: 0 15px 35px rgba(0, 0, 0, 0.2);
            }}
            
            .nav-item {{
                display: flex;
                align-items: center;
                gap: 18px;
                padding: 18px 22px;
                margin-bottom: 12px;
                border-radius: 18px;
                cursor: pointer;
                transition: all 0.4s ease;
                color: var(--text-secondary);
                text-decoration: none;
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(255, 255, 255, 0.1);
            }}
            
            .nav-item:hover {{
                background: linear-gradient(135deg, var(--primary), var(--secondary));
                transform: translateX(12px) scale(1.02);
                box-shadow: 0 8px 25px rgba(139, 92, 246, 0.3);
                color: white;
            }}
            
            .nav-item.active {{
                background: linear-gradient(135deg, var(--primary), var(--secondary));
                color: white;
                box-shadow: 0 10px 30px rgba(139, 92, 246, 0.5);
                transform: translateX(8px);
            }}
            
            .nav-icon {{
                font-size: 1.3em;
                width: 28px;
                text-align: center;
            }}
            
            .main-content {{
                display: grid;
                gap: 35px;
            }}
            
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 30px;
                margin-bottom: 35px;
            }}
            
            .stat-card {{
                background: linear-gradient(135deg, rgba(255, 255, 255, 0.15) 0%, rgba(255, 255, 255, 0.08) 100%);
                backdrop-filter: blur(20px);
                padding: 40px 35px;
                border-radius: 25px;
                text-align: center;
                border: 1px solid rgba(255, 255, 255, 0.15);
                transition: all 0.5s ease;
                position: relative;
                overflow: hidden;
                box-shadow: 0 15px 35px rgba(0, 0, 0, 0.25);
                animation: cardAppear 0.6s ease-out;
            }}
            
            @keyframes cardAppear {{
                from {{ transform: scale(0.8); opacity: 0; }}
                to {{ transform: scale(1); opacity: 1; }}
            }}
            
            .stat-card::before {{
                content: '';
                position: absolute;
                top: 0;
                left: -100%;
                width: 100%;
                height: 100%;
                background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.1), transparent);
                transition: left 0.8s ease;
            }}
            
            .stat-card:hover::before {{
                left: 100%;
            }}
            
            .stat-card:hover {{
                transform: translateY(-15px) scale(1.05);
                box-shadow: 0 25px 50px rgba(0, 0, 0, 0.4);
                border-color: var(--primary-light);
            }}
            
            .stat-card h3 {{
                font-family: 'Orbitron', monospace;
                font-size: 4em;
                margin-bottom: 25px;
                background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 50%, var(--accent) 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
                font-weight: 800;
            }}
            
            .stat-card p {{
                color: var(--text-secondary);
                font-size: 1.2em;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 2px;
            }}
            
            .section {{
                background: linear-gradient(135deg, rgba(255, 255, 255, 0.12) 0%, rgba(255, 255, 255, 0.06) 100%);
                backdrop-filter: blur(25px);
                padding: 40px;
                border-radius: 25px;
                margin-bottom: 40px;
                border: 1px solid rgba(255, 255, 255, 0.12);
                position: relative;
                overflow: hidden;
                box-shadow: 0 20px 40px rgba(0, 0, 0, 0.25);
                animation: sectionSlide 0.8s ease-out;
            }}
            
            @keyframes sectionSlide {{
                from {{ transform: translateX(-30px); opacity: 0; }}
                to {{ transform: translateX(0); opacity: 1; }}
            }}
            
            .section::before {{
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 4px;
                background: linear-gradient(90deg, var(--primary), var(--secondary), var(--accent));
            }}
            
            .section h2 {{
                font-family: 'Orbitron', monospace;
                font-size: 2.2em;
                margin-bottom: 35px;
                color: var(--text);
                display: flex;
                align-items: center;
                gap: 20px;
                font-weight: 700;
            }}
            
            .section h2 i {{
                background: linear-gradient(135deg, var(--primary), var(--secondary));
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                font-size: 1.4em;
            }}
            
            table {{
                width: 100%;
                border-collapse: collapse;
                background: rgba(255, 255, 255, 0.05);
                border-radius: 20px;
                overflow: hidden;
                margin-top: 25px;
                box-shadow: 0 15px 30px rgba(0, 0, 0, 0.15);
            }}
            
            th, td {{
                padding: 20px 28px;
                text-align: left;
                border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            }}
            
            th {{
                background: linear-gradient(135deg, var(--primary-dark), var(--primary));
                color: white;
                font-weight: 700;
                font-family: 'Orbitron', monospace;
                text-transform: uppercase;
                letter-spacing: 2px;
                font-size: 1em;
                position: sticky;
                top: 0;
            }}
            
            td {{
                color: var(--text-secondary);
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
                gap: 8px;
                padding: 10px 18px;
                border-radius: 25px;
                font-size: 0.9em;
                font-weight: 600;
                font-family: 'Orbitron', monospace;
                letter-spacing: 1.5px;
                text-transform: uppercase;
            }}
            
            .badge-success {{
                background: linear-gradient(135deg, var(--success), #059669);
                color: white;
            }}
            
            .badge-info {{
                background: linear-gradient(135deg, var(--accent), #0891B2);
                color: white;
            }}
            
            .badge-warning {{
                background: linear-gradient(135deg, var(--warning), #D97706);
                color: white;
            }}
            
            .badge-purple {{
                background: linear-gradient(135deg, var(--primary), var(--primary-dark));
                color: white;
            }}
            
            .badge-danger {{
                background: linear-gradient(135deg, var(--danger), #DC2626);
                color: white;
            }}
            
            .file-type {{
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: 40px;
                height: 40px;
                border-radius: 12px;
                margin-right: 15px;
                font-weight: bold;
                font-size: 1.2em;
                color: white;
            }}
            
            .type-text {{ background: linear-gradient(135deg, var(--success), #059669); }}
            .type-photo {{ background: linear-gradient(135deg, var(--accent), #0891B2); }}
            .type-video {{ background: linear-gradient(135deg, var(--warning), #D97706); }}
            .type-document {{ background: linear-gradient(135deg, var(--primary), var(--primary-dark)); }}
            .type-voice {{ background: linear-gradient(135deg, var(--danger), #DC2626); }}
            
            .user-link {{
                color: var(--primary-light);
                text-decoration: none;
                font-weight: 600;
                transition: all 0.3s ease;
            }}
            
            .user-link:hover {{
                color: var(--secondary);
                text-decoration: underline;
            }}
            
            .link-url {{
                background: rgba(255,255,255,0.1);
                padding: 10px 15px;
                border-radius: 10px;
                font-family: monospace;
                font-size: 0.95em;
                color: var(--text-secondary);
                border: 1px solid rgba(255,255,255,0.2);
            }}
            
            .message-preview {{
                max-width: 350px;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
                color: var(--text-secondary);
            }}
            
            .conversation-view {{
                background: rgba(255,255,255,0.08);
                border-radius: 20px;
                padding: 25px;
                margin: 15px 0;
                border-left: 5px solid var(--primary);
                animation: slideIn 0.5s ease-out;
            }}
            
            @keyframes slideIn {{
                from {{ transform: translateX(-20px); opacity: 0; }}
                to {{ transform: translateX(0); opacity: 1; }}
            }}
            
            .message-bubble {{
                background: rgba(139, 92, 246, 0.2);
                border-radius: 18px;
                padding: 18px;
                margin: 15px 0;
                border: 1px solid rgba(139, 92, 246, 0.3);
                animation: messageAppear 0.6s ease-out;
            }}
            
            @keyframes messageAppear {{
                from {{ transform: translateY(20px); opacity: 0; }}
                to {{ transform: translateY(0); opacity: 1; }}
            }}
            
            .message-sender {{
                font-weight: bold;
                color: var(--primary-light);
                margin-bottom: 8px;
                font-size: 1.1em;
            }}
            
            .message-time {{
                font-size: 0.85em;
                color: var(--text-secondary);
                float: right;
            }}
            
            .footer {{
                text-align: center;
                margin-top: 70px;
                padding: 50px;
                background: linear-gradient(135deg, rgba(139, 92, 246, 0.15) 0%, rgba(236, 72, 153, 0.15) 100%);
                border-radius: 30px;
                border: 1px solid rgba(255, 255, 255, 0.15);
                animation: fadeIn 2s ease-in;
            }}
            
            @keyframes fadeIn {{
                from {{ opacity: 0; }}
                to {{ opacity: 1; }}
            }}
            
            .footer-text {{
                font-family: 'Orbitron', monospace;
                font-size: 1.5em;
                color: var(--primary-light);
                letter-spacing: 4px;
                margin-bottom: 20px;
                text-shadow: 0 0 20px rgba(139, 92, 246, 0.5);
            }}
            
            .user-avatar {{
                width: 50px;
                height: 50px;
                border-radius: 50%;
                background: linear-gradient(135deg, var(--primary), var(--secondary));
                display: flex;
                align-items: center;
                justify-content: center;
                font-weight: bold;
                color: white;
                margin-right: 15px;
                font-size: 1.3em;
                box-shadow: 0 5px 15px rgba(139, 92, 246, 0.4);
            }}
            
            .progress-bar {{
                width: 100%;
                height: 10px;
                background: rgba(255, 255, 255, 0.15);
                border-radius: 5px;
                overflow: hidden;
                margin-top: 12px;
            }}
            
            .progress-fill {{
                height: 100%;
                background: linear-gradient(90deg, var(--primary), var(--secondary), var(--accent));
                border-radius: 5px;
                transition: width 1.5s ease-in-out;
                animation: progressAnimation 2s ease-in-out infinite alternate;
            }}
            
            @keyframes progressAnimation {{
                0% {{ background-position: 0% 50%; }}
                100% {{ background-position: 100% 50%; }}
            }}
            
            .search-box {{
                background: rgba(255, 255, 255, 0.1);
                border: 1px solid rgba(255, 255, 255, 0.2);
                border-radius: 18px;
                padding: 18px 25px;
                color: white;
                font-size: 1.1em;
                width: 100%;
                margin-bottom: 25px;
                backdrop-filter: blur(15px);
                transition: all 0.3s ease;
            }}
            
            .search-box:focus {{
                outline: none;
                border-color: var(--primary);
                box-shadow: 0 0 20px rgba(139, 92, 246, 0.4);
                transform: scale(1.02);
            }}
            
            .search-box::placeholder {{
                color: var(--text-secondary);
            }}
            
            .view-conversation-btn {{
                background: linear-gradient(135deg, var(--primary), var(--secondary));
                color: white;
                border: none;
                padding: 12px 20px;
                border-radius: 15px;
                cursor: pointer;
                font-size: 1em;
                transition: all 0.3s ease;
                font-weight: 600;
            }}
            
            .view-conversation-btn:hover {{
                transform: translateY(-3px);
                box-shadow: 0 10px 25px rgba(139, 92, 246, 0.5);
            }}
            
            .conversation-message {{
                margin: 15px 0;
                padding: 15px;
                border-radius: 15px;
                background: rgba(255, 255, 255, 0.08);
                border-left: 4px solid var(--accent);
            }}
            
            .conversation-reply {{
                margin: 15px 0;
                padding: 15px;
                border-radius: 15px;
                background: rgba(139, 92, 246, 0.15);
                border-left: 4px solid var(--primary);
                margin-left: 30px;
            }}
            
            .media-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
                gap: 20px;
                margin-top: 20px;
            }}
            
            .media-item {{
                background: rgba(255, 255, 255, 0.08);
                border-radius: 15px;
                padding: 20px;
                text-align: center;
                transition: all 0.3s ease;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }}
            
            .media-item:hover {{
                transform: translateY(-5px);
                box-shadow: 0 10px 25px rgba(139, 92, 246, 0.3);
            }}
            
            .media-icon {{
                font-size: 2.5em;
                margin-bottom: 15px;
                color: var(--primary-light);
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
                    font-size: 3em;
                }}
                
                .stats-grid {{
                    grid-template-columns: 1fr;
                }}
                
                th, td {{
                    padding: 15px 20px;
                    font-size: 0.9em;
                }}
                
                .section {{
                    padding: 30px;
                }}
            }}
        </style>
    </head>
    <body>
        <!-- Preloader -->
        <div class="preloader" id="preloader">
            <div class="preloader-content">
                <div class="loader"></div>
                <h2 style="color: var(--primary-light); margin-bottom: 10px;">Загрузка панели администратора</h2>
                <p style="color: var(--text-secondary);">Инициализация системы мониторинга...</p>
            </div>
        </div>
        
        <div class="container">
            <!-- Заголовок -->
            <div class="header">
                <div class="header-content">
                    <h1><i class="fas fa-robot"></i> АДМИН ПАНЕЛЬ</h1>
                    <div class="subtitle">Расширенная система мониторинга анонимного бота</div>
                    <div class="timestamp">
                        <i class="fas fa-clock"></i> Отчет сгенерирован: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    </div>
                </div>
            </div>
            
            <div class="dashboard">
                <!-- Боковая панель -->
                <div class="sidebar">
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
                    <div class="nav-item" onclick="showSection('media')">
                        <div class="nav-icon"><i class="fas fa-photo-video"></i></div>
                        <div>Медиафайлы</div>
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
                            <div class="stat-card">
                                <h3>{data['stats']['users']}</h3>
                                <p><i class="fas fa-users"></i> Всего пользователей</p>
                                <div class="progress-bar">
                                    <div class="progress-fill" style="width: {min(data['stats']['users'] * 2, 100)}%"></div>
                                </div>
                            </div>
                            <div class="stat-card">
                                <h3>{data['stats']['links']}</h3>
                                <p><i class="fas fa-link"></i> Активных ссылок</p>
                                <div class="progress-bar">
                                    <div class="progress-fill" style="width: {min(data['stats']['links'] * 5, 100)}%"></div>
                                </div>
                            </div>
                            <div class="stat-card">
                                <h3>{data['stats']['messages']}</h3>
                                <p><i class="fas fa-envelope"></i> Всего сообщений</p>
                                <div class="progress-bar">
                                    <div class="progress-fill" style="width: {min(data['stats']['messages'] * 0.5, 100)}%"></div>
                                </div>
                            </div>
                            <div class="stat-card">
                                <h3>{data['stats']['replies']}</h3>
                                <p><i class="fas fa-reply"></i> Ответов</p>
                                <div class="progress-bar">
                                    <div class="progress-fill" style="width: {min(data['stats']['replies'] * 2, 100)}%"></div>
                                </div>
                            </div>
                        </div>
                        
                        <!-- Статистика файлов -->
                        <div class="stats-grid">
                            <div class="stat-card">
                                <h3>{data['stats']['photos']}</h3>
                                <p><i class="fas fa-image"></i> Фотографий</p>
                            </div>
                            <div class="stat-card">
                                <h3>{data['stats']['videos']}</h3>
                                <p><i class="fas fa-video"></i> Видео</p>
                            </div>
                            <div class="stat-card">
                                <h3>{data['stats']['documents']}</h3>
                                <p><i class="fas fa-file"></i> Документов</p>
                            </div>
                            <div class="stat-card">
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
                                                <div style="font-size: 0.85em; color: var(--text-secondary);">{html.escape(user[2]) if user[2] else 'No Name'}</div>
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
                                        <button class="view-conversation-btn" onclick="viewUserConversation({user[0]}, '{username_display}')">
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
                                        <div style="font-size: 0.85em; color: var(--text-secondary);">{html.escape(link[2]) if link[2] else 'Без описания'}</div>
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
                                        <button class="view-conversation-btn" onclick="viewLinkConversation('{link[0]}', '{html.escape(link[1])}')">
                                            <i class="fas fa-eye"></i> Переписка
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
                    
                    <!-- Медиафайлы -->
                    <div id="media-section" class="section" style="display: none;">
                        <h2><i class="fas fa-photo-video"></i> МЕДИАФАЙЛЫ</h2>
                        <div class="media-grid">
    '''
    
    for media in data['media_files'][:20]:
        media_icon = {
            'photo': '🖼️',
            'video': '🎥',
            'document': '📄',
            'voice': '🎤'
        }.get(media[1], '📁')
        
        file_size = f"{(media[3] // 1024):,} KB" if media[3] else 'N/A'
        user = f"@{media[6]}" if media[6] else (html.escape(media[7]) if media[7] else 'Аноним')
        
        html_content += f'''
                            <div class="media-item">
                                <div class="media-icon">{media_icon}</div>
                                <div style="font-weight: 600; margin-bottom: 8px;">{media[1].upper()}</div>
                                <div style="font-size: 0.9em; color: var(--text-secondary); margin-bottom: 5px;">{user}</div>
                                <div style="font-size: 0.8em; color: var(--text-secondary);">{file_size}</div>
                                <div style="font-size: 0.8em; color: var(--primary-light); margin-top: 5px;">{html.escape(media[8]) if media[8] else 'Без названия'}</div>
                            </div>
        '''
    
    html_content += '''
                        </div>
                    </div>
                    
                    <!-- Переписки -->
                    <div id="conversations-section" class="section" style="display: none;">
                        <h2><i class="fas fa-comments"></i> ПРОСМОТР ПЕРЕПИСОК</h2>
                        <div class="conversation-view">
                            <h3><i class="fas fa-info-circle"></i> Выберите пользователя или ссылку для просмотра переписки</h3>
                            <p>Используйте кнопки "Переписка" в таблице пользователей или "Переписка" в таблице ссылок для просмотра полной истории сообщений.</p>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Футер -->
            <div class="footer">
                <div class="footer-text">
                    <i class="fas fa-robot"></i> АНОНИМНЫЙ БОТ | РАСШИРЕННАЯ СИСТЕМА МОНИТОРИНГА
                </div>
                <div style="margin-top: 20px; color: var(--text-secondary); font-size: 1.1em;">
                    <i class="fas fa-shield-alt"></i> Защищенная система | <i class="fas fa-bolt"></i> Реальное время | <i class="fas fa-chart-line"></i> Полная аналитика
                </div>
                <div style="margin-top: 15px; color: var(--primary-light); font-family: 'Orbitron', monospace; font-size: 1em;">
                    SIROK228 | POWERED BY ADVANCED AI TECHNOLOGY
                </div>
            </div>
        </div>
        
        <script>
            // Скрываем прелоадер после загрузки
            window.addEventListener('load', function() {{
                setTimeout(() => {{
                    document.getElementById('preloader').style.opacity = '0';
                    setTimeout(() => {{
                        document.getElementById('preloader').style.display = 'none';
                    }}, 500);
                }}, 1000);
            }});
            
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
            function viewUserConversation(userId, username) {{
                showSection('conversations');
                const section = document.getElementById('conversations-section');
                section.innerHTML = `
                    <h2><i class="fas fa-comments"></i> ПЕРЕПИСКА ПОЛЬЗОВАТЕЛЯ: ${username}</h2>
                    <div class="conversation-view">
                        <div style="text-align: center; padding: 40px;">
                            <i class="fas fa-spinner fa-spin fa-3x" style="color: var(--primary); margin-bottom: 20px;"></i>
                            <h3>Загрузка переписки...</h3>
                            <p>Идет загрузка истории сообщений для пользователя ${username}</p>
                        </div>
                    </div>
                `;
                
                // Здесь будет AJAX запрос для загрузки реальных данных
                setTimeout(() => {{
                    loadUserConversation(userId, username);
                }}, 1000);
            }}
            
            function loadUserConversation(userId, username) {{
                // В реальной реализации здесь будет AJAX запрос к серверу
                // Сейчас используем демо-данные
                const section = document.getElementById('conversations-section');
                section.innerHTML = `
                    <h2><i class="fas fa-comments"></i> ПЕРЕПИСКА ПОЛЬЗОВАТЕЛЯ: ${username}</h2>
                    <div class="conversation-view">
                        <div class="conversation-message">
                            <div class="message-sender">${username} <span class="message-time">2024-01-15 14:30</span></div>
                            <div>Привет! Это тестовое сообщение от пользователя</div>
                        </div>
                        <div class="conversation-reply">
                            <div class="message-sender">Аноним <span class="message-time">2024-01-15 14:35</span></div>
                            <div>Это ответ на сообщение пользователя</div>
                        </div>
                        <div class="conversation-message">
                            <div class="message-sender">${username} <span class="message-time">2024-01-15 15:00</span></div>
                            <div>Спасибо за ответ! Как дела?</div>
                        </div>
                        <div class="conversation-reply">
                            <div class="message-sender">Аноним <span class="message-time">2024-01-15 15:05</span></div>
                            <div>Все отлично, работаю над улучшением бота!</div>
                        </div>
                        <button class="view-conversation-btn" onclick="showSection('users')" style="margin-top: 25px;">
                            <i class="fas fa-arrow-left"></i> Назад к пользователям
                        </button>
                    </div>
                `;
            }}
            
            // Просмотр переписки по ссылке
            function viewLinkConversation(linkId, linkTitle) {{
                showSection('conversations');
                const section = document.getElementById('conversations-section');
                section.innerHTML = `
                    <h2><i class="fas fa-comments"></i> ПЕРЕПИСКА ПО ССЫЛКЕ: ${linkTitle}</h2>
                    <div class="conversation-view">
                        <div style="text-align: center; padding: 40px;">
                            <i class="fas fa-spinner fa-spin fa-3x" style="color: var(--primary); margin-bottom: 20px;"></i>
                            <h3>Загрузка переписки...</h3>
                            <p>Идет загрузка истории сообщений для ссылки "${linkTitle}"</p>
                        </div>
                    </div>
                `;
                
                setTimeout(() => {{
                    loadLinkConversation(linkId, linkTitle);
                }}, 1000);
            }}
            
            function loadLinkConversation(linkId, linkTitle) {{
                const section = document.getElementById('conversations-section');
                section.innerHTML = `
                    <h2><i class="fas fa-comments"></i> ПЕРЕПИСКА ПО ССЫЛКЕ: ${linkTitle}</h2>
                    <div class="conversation-view">
                        <div class="conversation-message">
                            <div class="message-sender">Аноним <span class="message-time">2024-01-15 10:20</span></div>
                            <div>Привет! Это первое анонимное сообщение через ссылку</div>
                        </div>
                        <div class="conversation-reply">
                            <div class="message-sender">Владелец ссылки <span class="message-time">2024-01-15 10:25</span></div>
                            <div>Спасибо за сообщение! Рад вас слышать!</div>
                        </div>
                        <div class="conversation-message">
                            <div class="message-sender">Аноним <span class="message-time">2024-01-15 11:30</span></div>
                            <div>У меня есть вопрос по поводу функционала бота</div>
                        </div>
                        <div class="conversation-reply">
                            <div class="message-sender">Владелец ссылки <span class="message-time">2024-01-15 11:35</span></div>
                            <div>Конечно, задавайте! Постараюсь помочь с любыми вопросами</div>
                        </div>
                        <div class="conversation-message">
                            <div class="message-sender">Аноним <span class="message-time">2024-01-15 12:00</span></div>
                            <div>Как создать свою ссылку для анонимных сообщений?</div>
                        </div>
                        <div class="conversation-reply">
                            <div class="message-sender">Владелец ссылки <span class="message-time">2024-01-15 12:05</span></div>
                            <div>Просто используйте команду /start и выберите "Создать ссылку" в меню!</div>
                        </div>
                        <button class="view-conversation-btn" onclick="showSection('links')" style="margin-top: 25px;">
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

def message_details_keyboard(message_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Ответить", callback_data=f"reply_{message_id}")],
        [InlineKeyboardButton("📋 Просмотреть ответы", callback_data=f"view_replies_{message_id}")],
        [InlineKeyboardButton("🔄 Продолжить ответы", callback_data=f"continue_reply_{message_id}")],
        [InlineKeyboardButton("🔙 Назад к сообщениям", callback_data="my_messages")]
    ])

def reply_to_reply_keyboard(reply_id, message_id):
    """Клавиатура для ответа на ответ"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Ответить на этот ответ", callback_data=f"reply_to_reply_{reply_id}")],
        [InlineKeyboardButton("🔄 Несколько ответов", callback_data=f"multi_reply_to_reply_{reply_id}")],
        [InlineKeyboardButton("📋 Все ответы", callback_data=f"view_replies_{message_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"view_replies_{message_id}")]
    ])

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("📜 История переписки", callback_data="admin_history")],
        [InlineKeyboardButton("🎨 HTML Отчет", callback_data="admin_html_report")],
        [InlineKeyboardButton("📢 Оповещение", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
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
                    # ИСПРАВЛЕНО: экранирование символа #
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
        
        # Обработка ответов на сообщения
        elif data.startswith("reply_"):
            message_id = int(data.replace("reply_", ""))
            context.user_data['replying_to'] = message_id
            context.user_data['reply_mode'] = 'single'
            # ИСПРАВЛЕНО: экранирование символа #
            await query.edit_message_text(
                f"✍️ *Режим ответа на сообщение* \\#{message_id}\n\nВведите ваш ответ:",
                parse_mode='MarkdownV2', 
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Отправить один ответ", callback_data=f"confirm_reply_{message_id}")],
                    [InlineKeyboardButton("🔄 Режим нескольких ответов", callback_data=f"multi_reply_{message_id}")],
                    [InlineKeyboardButton("🔙 Назад", callback_data=f"view_replies_{message_id}")]
                ])
            )
            return
        
        elif data.startswith("multi_reply_"):
            message_id = int(data.replace("multi_reply_", ""))
            context.user_data['replying_to'] = message_id
            context.user_data['reply_mode'] = 'multi'
            context.user_data['multi_reply_count'] = 0
            # ИСПРАВЛЕНО: экранирование символа #
            await query.edit_message_text(
                f"🔄 *Режим нескольких ответов на сообщение* \\#{message_id}\n\nВведите первый ответ:\n\n_Вы можете отправлять несколько ответов подряд\\. Для завершения нажмите \"Завершить ответы\"_",
                parse_mode='MarkdownV2',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏹️ Завершить ответы", callback_data=f"end_multi_reply_{message_id}")],
                    [InlineKeyboardButton("🔙 Назад", callback_data=f"view_replies_{message_id}")]
                ])
            )
            return
        
        elif data.startswith("end_multi_reply_"):
            message_id = int(data.replace("end_multi_reply_", ""))
            count = context.user_data.get('multi_reply_count', 0)
            context.user_data.pop('replying_to', None)
            context.user_data.pop('reply_mode', None)
            context.user_data.pop('multi_reply_count', None)
            await query.edit_message_text(
                f"✅ *Режим ответов завершен*\n\nОтправлено ответов\\: {count}\n\nОтветы доставлены анонимно\\!",
                parse_mode='MarkdownV2',
                reply_markup=message_details_keyboard(message_id)
            )
            return
        
        elif data.startswith("continue_reply_"):
            message_id = int(data.replace("continue_reply_", ""))
            context.user_data['replying_to'] = message_id
            context.user_data['reply_mode'] = 'multi'
            current_count = context.user_data.get('multi_reply_count', 0)
            # ИСПРАВЛЕНО: экранирование символа #
            await query.edit_message_text(
                f"🔄 *Продолжение ответов на сообщение* \\#{message_id}\n\nТекущее количество ответов\\: {current_count}\n\nВведите следующий ответ:",
                parse_mode='MarkdownV2',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏹️ Завершить ответы", callback_data=f"end_multi_reply_{message_id}")],
                    [InlineKeyboardButton("🔙 Назад", callback_data=f"view_replies_{message_id}")]
                ])
            )
            return
        
        elif data.startswith("view_replies_"):
            message_id = int(data.replace("view_replies_", ""))
            replies = get_message_replies(message_id)
            if replies:
                # ИСПРАВЛЕНО: экранирование символа #
                text = f"💬 *Ответы на сообщение* \\#{message_id}\\:\n\n"
                for i, reply in enumerate(replies, 1):
                    reply_id, reply_text, created, username, first_name = reply
                    sender = f"@{username}" if username else (first_name or "Аноним")
                    created_str = format_datetime(created)
                    text += f"{i}\\. 👤 *{escape_markdown_v2(sender)}* \\(`{created_str}`\\)\\:\n{format_as_quote(reply_text)}\n\n"
                    # Добавляем кнопку для ответа на конкретный ответ
                    text += f"   └─ 💬 *Ответить на этот ответ* \\- /reply\\_to\\_{reply_id}\n\n"
                
                await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(message_id))
            else:
                # ИСПРАВЛЕНО: экранирование символа #
                await query.edit_message_text(
                    f"💬 На сообщение \\#{message_id} пока нет ответов\\.\n\nБудьте первым, кто ответит\\!",
                    parse_mode='MarkdownV2', 
                    reply_markup=message_details_keyboard(message_id)
                )
            return

        # Ответ на ответ
        elif data.startswith("reply_to_reply_"):
            reply_id = int(data.replace("reply_to_reply_", ""))
            context.user_data['replying_to_reply'] = reply_id
            context.user_data['reply_mode'] = 'single_reply'
            
            reply_info = run_query("SELECT r.reply_text, r.message_id FROM replies r WHERE r.reply_id = ?", (reply_id,), fetch="one")
            if reply_info:
                reply_text, message_id = reply_info
                # ИСПРАВЛЕНО: экранирование символа #
                await query.edit_message_text(
                    f"💬 *Ответ на ответ* \\#{reply_id}\n\n*Оригинальный ответ\\:*\n{format_as_quote(reply_text)}\n\nВведите ваш ответ\\:",
                    parse_mode='MarkdownV2',
                    reply_markup=reply_to_reply_keyboard(reply_id, message_id)
                )
            return

        elif data.startswith("multi_reply_to_reply_"):
            reply_id = int(data.replace("multi_reply_to_reply_", ""))
            context.user_data['replying_to_reply'] = reply_id
            context.user_data['reply_mode'] = 'multi_reply'
            context.user_data['multi_reply_count'] = 0
            
            reply_info = run_query("SELECT r.reply_text, r.message_id FROM replies r WHERE r.reply_id = ?", (reply_id,), fetch="one")
            if reply_info:
                reply_text, message_id = reply_info
                # ИСПРАВЛЕНО: экранирование символа #
                await query.edit_message_text(
                    f"🔄 *Несколько ответов на ответ* \\#{reply_id}\n\n*Оригинальный ответ\\:*\n{format_as_quote(reply_text)}\n\nВведите первый ответ\\:\n\n_Вы можете отправлять несколько ответов подряд\\. Для завершения нажмите \"Завершить ответы\"_",
                    parse_mode='MarkdownV2',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⏹️ Завершить ответы", callback_data=f"end_multi_reply_to_reply_{reply_id}")],
                        [InlineKeyboardButton("🔙 Назад", callback_data=f"view_replies_{message_id}")]
                    ])
                )
            return

        elif data.startswith("end_multi_reply_to_reply_"):
            reply_id = int(data.replace("end_multi_reply_to_reply_", ""))
            count = context.user_data.get('multi_reply_count', 0)
            context.user_data.pop('replying_to_reply', None)
            context.user_data.pop('reply_mode', None)
            context.user_data.pop('multi_reply_count', None)
            
            reply_info = run_query("SELECT r.message_id FROM replies r WHERE r.reply_id = ?", (reply_id,), fetch="one")
            message_id = reply_info[0] if reply_info else None
            
            await query.edit_message_text(
                f"✅ *Режим ответов завершен*\n\nОтправлено ответов\\: {count}\n\nОтветы доставлены анонимно\\!",
                parse_mode='MarkdownV2',
                reply_markup=message_details_keyboard(message_id) if message_id else back_to_main_keyboard()
            )
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
            
            elif data == "admin_history":
                if context.user_data.get('admin_authenticated'):
                    users = get_all_users_for_admin()
                    if users:
                        kb = []
                        for u in users[:15]:
                            username = u[1] or u[2] or f"ID\\: {u[0]}"
                            kb.append([InlineKeyboardButton(f"👤 {username}", callback_data=f"admin_view_user:{u[0]}")])
                        kb.append([InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")])
                        await query.edit_message_text("👥 *Выберите пользователя для просмотра истории\\:*", reply_markup=InlineKeyboardMarkup(kb))
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
            
            elif data.startswith("admin_view_user:"):
                if context.user_data.get('admin_authenticated'):
                    user_id = int(data.split(":")[1])
                    history = get_full_history_for_admin(user_id)
                    
                    if not history:
                        await query.edit_message_text("_История сообщений не найдена\\._", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                        return
                    
                    await query.edit_message_text(f"📜 *История переписки для пользователя ID {user_id}*\n*Всего сообщений\\: {len(history)}*", parse_mode='MarkdownV2')
                    
                    for i, msg in enumerate(history[:5]):
                        msg_id, msg_text, msg_type, file_id, file_size, file_name, created, from_user, from_name, to_user, to_name, link_title, link_id = msg
                        
                        created_str = format_datetime(created)
                        # ИСПРАВЛЕНО: экранирование символа #
                        header = f"*#{i+1}* \\| 🕒 `{created_str}`\n"
                        header += f"*От\\:* {escape_markdown_v2(from_user or from_name or 'Аноним')}\n"
                        header += f"*Кому\\:* {escape_markdown_v2(to_user or to_name or 'Аноним')}\n"
                        header += f"*Ссылка\\:* {escape_markdown_v2(link_title or 'N/A')}\n"
                        
                        if msg_type == 'text':
                            await query.message.reply_text(f"{header}\n{format_as_quote(msg_text)}", parse_mode='MarkdownV2')
                        else:
                            file_info = f"\n*Тип\\:* {msg_type}"
                            if file_size:
                                file_info += f" \\({(file_size or 0) // 1024} KB\\)"
                            if file_name:
                                file_info += f"\n*Файл\\:* {escape_markdown_v2(file_name)}"
                            
                            caption = f"{header}{file_info}"
                            await query.message.reply_text(f"{caption}\n\n*Содержание\\:* {format_as_quote(msg_text)}", parse_mode='MarkdownV2')
                    
                    if len(history) > 5:
                        await query.message.reply_text(f"*\\\\.\\\\.\\\\. и ещё {len(history) - 5} сообщений*\n_Для полного просмотра используйте HTML отчет_", parse_mode='MarkdownV2')
                    
                    await query.message.reply_text("🛠️ *Панель администратора*", reply_markup=admin_keyboard(), parse_mode='MarkdownV2')
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

        # Ответ на сообщение (одиночный режим)
        if 'replying_to' in context.user_data and context.user_data.get('reply_mode') == 'single':
            msg_id = context.user_data.pop('replying_to')
            context.user_data.pop('reply_mode', None)
            save_reply(msg_id, user.id, text)
            original_msg = run_query("SELECT m.from_user_id, m.message_text FROM messages m WHERE m.message_id = ?", (msg_id,), fetch="one")
            if original_msg:
                try:
                    reply_notification = f"💬 *Получен ответ на ваше сообщение\\:*\n{format_as_quote(original_msg[1])}\n\n*Ответ\\:*\n{format_as_quote(text)}"
                    await context.bot.send_message(original_msg[0], reply_notification, parse_mode='MarkdownV2')
                except Exception as e:
                    logging.error(f"Failed to send reply notification: {e}")
            await update.message.reply_text("✅ Ваш ответ отправлен анонимно\\!", reply_markup=main_keyboard(), parse_mode='MarkdownV2')
            return

        # Ответ на сообщение (режим нескольких ответов)
        if 'replying_to' in context.user_data and context.user_data.get('reply_mode') == 'multi':
            msg_id = context.user_data['replying_to']
            save_reply(msg_id, user.id, text)
            
            current_count = context.user_data.get('multi_reply_count', 0)
            context.user_data['multi_reply_count'] = current_count + 1
            
            original_msg = run_query("SELECT m.from_user_id, m.message_text FROM messages m WHERE m.message_id = ?", (msg_id,), fetch="one")
            if original_msg:
                try:
                    # ИСПРАВЛЕНО: экранирование символа #
                    reply_notification = f"💬 *Получен ответ на ваше сообщение\\:*\n{format_as_quote(original_msg[1])}\n\n*Ответ #{current_count + 1}\\:*\n{format_as_quote(text)}"
                    await context.bot.send_message(original_msg[0], reply_notification, parse_mode='MarkdownV2')
                except Exception as e:
                    logging.error(f"Failed to send reply notification: {e}")
            
            # ИСПРАВЛЕНО: экранирование символа #
            await update.message.reply_text(
                f"✅ *Ответ #{current_count + 1} отправлен\\!*\n\nМожете отправить следующий ответ или завершить режим ответов\\.",
                parse_mode='MarkdownV2',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Продолжить ответы", callback_data=f"continue_reply_{msg_id}")],
                    [InlineKeyboardButton("⏹️ Завершить ответы", callback_data=f"end_multi_reply_{msg_id}")],
                    [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
                ])
            )
            return

        # Ответ на ответ (одиночный режим)
        if 'replying_to_reply' in context.user_data and context.user_data.get('reply_mode') == 'single_reply':
            reply_id = context.user_data.pop('replying_to_reply')
            context.user_data.pop('reply_mode', None)
            
            # Получаем информацию об ответе
            reply_info = run_query("SELECT r.message_id, r.from_user_id, r.reply_text FROM replies r WHERE r.reply_id = ?", (reply_id,), fetch="one")
            if reply_info:
                message_id, original_reply_user_id, original_reply_text = reply_info
                
                # Сохраняем новый ответ
                new_reply_id = save_reply(message_id, user.id, text)
                
                # Отправляем уведомление автору оригинального ответа
                try:
                    reply_notification = f"💬 *Получен ответ на ваш ответ\\:*\n{format_as_quote(original_reply_text)}\n\n*Новый ответ\\:*\n{format_as_quote(text)}"
                    await context.bot.send_message(original_reply_user_id, reply_notification, parse_mode='MarkdownV2')
                except Exception as e:
                    logging.error(f"Failed to send reply notification: {e}")
            
            await update.message.reply_text("✅ Ваш ответ на ответ отправлен анонимно\\!", reply_markup=main_keyboard(), parse_mode='MarkdownV2')
            return

        # Ответ на ответ (режим нескольких ответов)
        if 'replying_to_reply' in context.user_data and context.user_data.get('reply_mode') == 'multi_reply':
            reply_id = context.user_data['replying_to_reply']
            
            # Получаем информацию об ответе
            reply_info = run_query("SELECT r.message_id, r.from_user_id, r.reply_text FROM replies r WHERE r.reply_id = ?", (reply_id,), fetch="one")
            if reply_info:
                message_id, original_reply_user_id, original_reply_text = reply_info
                
                # Сохраняем новый ответ
                save_reply(message_id, user.id, text)
                
                current_count = context.user_data.get('multi_reply_count', 0)
                context.user_data['multi_reply_count'] = current_count + 1
                
                # Отправляем уведомление автору оригинального ответа
                try:
                    # ИСПРАВЛЕНО: экранирование символа #
                    reply_notification = f"💬 *Получен ответ на ваш ответ\\:*\n{format_as_quote(original_reply_text)}\n\n*Ответ #{current_count + 1}\\:*\n{format_as_quote(text)}"
                    await context.bot.send_message(original_reply_user_id, reply_notification, parse_mode='MarkdownV2')
                except Exception as e:
                    logging.error(f"Failed to send reply notification: {e}")
            
            # ИСПРАВЛЕНО: экранирование символа #
            await update.message.reply_text(
                f"✅ *Ответ на ответ #{current_count + 1} отправлен\\!*\n\nМожете отправить следующий ответ или завершить режим ответов\\.",
                parse_mode='MarkdownV2',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Продолжить ответы", callback_data=f"continue_reply_to_reply_{reply_id}")],
                    [InlineKeyboardButton("⏹️ Завершить ответы", callback_data=f"end_multi_reply_to_reply_{reply_id}")],
                    [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
                ])
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

        # Рассылка от админа
        if is_admin and context.user_data.get('broadcasting') and context.user_data.get('admin_authenticated'):
            context.user_data.pop('broadcasting')
            users = run_query("SELECT user_id FROM users", fetch="all")
            sent_count = 0
            if users:
                for u in users:
                    try:
                        await context.bot.send_message(u[0], text, parse_mode='MarkdownV2')
                        sent_count += 1
                    except Exception as e:
                        logging.warning(f"Broadcast failed for user {u[0]}: {e}")
            await update.message.reply_text(
                f"📢 *Рассылка завершена*\n\nОтправлено\\: {sent_count}/{len(users) if users else 0} пользователям\\.",
                parse_mode='MarkdownV2', 
                reply_markup=admin_keyboard()
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
                    await context.bot.send_message(link_info[1], notification, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id))
                except Exception as e:
                    logging.error(f"Failed to send message notification: {e}")
                
                admin_notification = f"📨 *Новое сообщение*\nОт\\: {escape_markdown_v2(user.username or user.first_name or 'Аноним')} \\> Кому\\: {escape_markdown_v2(link_info[4] or 'Аноним')}\n\n{format_as_quote(text)}"
                await context.bot.send_message(ADMIN_ID, admin_notification, parse_mode='MarkdownV2')
                
                await update.message.reply_text("✅ Ваше сообщение отправлено анонимно\\!", reply_markup=main_keyboard(), parse_mode='MarkdownV2')
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
                admin_caption = f"📨 *Новый {msg_type}*\nОт\\: {escape_markdown_v2(user.username or user.first_name or 'Аноним')} \\> Кому\\: {escape_markdown_v2(link_info[4] or 'Аноним')}{file_info}\n\n{format_as_quote(caption)}"
                
                try:
                    if msg_type == 'photo': 
                        await context.bot.send_photo(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id))
                    elif msg_type == 'video': 
                        await context.bot.send_video(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id))
                    elif msg_type == 'document': 
                        await context.bot.send_document(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id))
                    elif msg_type == 'voice': 
                        await context.bot.send_voice(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(msg_id))
                except Exception as e: 
                    logging.error(f"Failed to send media to user: {e}")

                try:
                    if msg_type in ['photo', 'video', 'document']:
                        if msg_type == 'photo': 
                            await context.bot.send_photo(ADMIN_ID, file_id, caption=admin_caption, parse_mode='MarkdownV2')
                        elif msg_type == 'video': 
                            await context.bot.send_video(ADMIN_ID, file_id, caption=admin_caption, parse_mode='MarkdownV2')
                        elif msg_type == 'document': 
                            await context.bot.send_document(ADMIN_ID, file_id, caption=admin_caption, parse_mode='MarkdownV2')
                    elif msg_type == 'voice':
                        await context.bot.send_voice(ADMIN_ID, file_id, caption=admin_caption, parse_mode='MarkdownV2')
                except Exception as e: 
                    logging.error(f"Failed to send media to admin: {e}")
                
                await update.message.reply_text("✅ Ваше медиа отправлено анонимно\\!", reply_markup=main_keyboard(), parse_mode='MarkdownV2')

    except Exception as e:
        logging.error(f"Ошибка в обработчике медиа: {e}")
        await update.message.reply_text("❌ Произошла ошибка при отправке медиа\\.", parse_mode='MarkdownV2')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок."""
    logging.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

async def post_init(application: Application):
    """Функция, выполняемая после инициализации бота."""
    logging.info("Бот успешно инициализирован и готов к работе")
    # Создаем резервную копию БД при запуске
    backup_database()

async def post_stop(application: Application):
    """Функция, выполняемая перед остановкой бота."""
    logging.info("Бот останавливается...")
    # Создаем резервную копию БД перед остановкой
    backup_database()

def main():
    if not all([BOT_TOKEN, ADMIN_ID]):
        logging.critical("КРИТИЧЕСКАЯ ОШИБКА: Не установлены обязательные переменные окружения BOT_TOKEN и ADMIN_ID")
        return
    
    # Инициализация репозитория и БД с улучшенной обработкой ошибок
    try:
        setup_repo()
        init_db()
    except Exception as e:
        logging.error(f"Ошибка при инициализации: {e}")
        # Продолжаем работу даже при ошибках инициализации
    
    # Создание приложения с улучшенными настройками
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).post_stop(post_stop).build()
    
    # Добавление обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    media_filters = filters.PHOTO | filters.VIDEO | filters.VOICE | filters.Document.ALL
    application.add_handler(MessageHandler(media_filters & ~filters.COMMAND, handle_media))
    
    # Добавление обработчика ошибок
    application.add_error_handler(error_handler)
    
    logging.info("Бот запускается...")
    
    try:
        # Запуск бота с улучшенными настройками
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,  # Убираем старые updates чтобы избежать задержек
            close_loop=False,
            pool_timeout=20,  # Увеличиваем timeout
            read_timeout=20,
            connect_timeout=20
        )
    except Exception as e:
        logging.critical(f"Критическая ошибка при запуске бота: {e}")
        # Создаем резервную копию при критической ошибке
        backup_database()

if __name__ == "__main__":
    main()
