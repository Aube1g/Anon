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
        shutil.rmtree(REPO_PATH)
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            logging.info(f"Клонирование репозитория {GITHUB_REPO}... (попытка {attempt + 1})")
            repo = Repo.clone_from(remote_url, REPO_PATH)
            repo.config_writer().set_value("user", "name", "AnonBot").release()
            repo.config_writer().set_value("user", "email", "bot@render.com").release()
            logging.info("Репозиторий успешно склонирован и настроен.")
            return
        except Exception as e:
            logging.error(f"Ошибка при клонировании репозитория (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                logging.critical("Не удалось склонировать репозиторий, создаю локальную БД")
                os.makedirs(REPO_PATH, exist_ok=True)

def push_db_to_github(commit_message):
    """Отправляет файл базы данных на GitHub."""
    if not repo:
        logging.error("Репозиторий не инициализирован, push невозможен.")
        return
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            repo.index.add([DB_PATH])
            if repo.is_dirty(index=True, working_tree=False):
                repo.index.commit(commit_message)
                origin = repo.remote(name='origin')
                origin.push()
                logging.info(f"База данных успешно отправлена на GitHub. Коммит: {commit_message}")
                return
            else:
                logging.info("Нет изменений в БД для отправки.")
                return
        except Exception as e:
            logging.error(f"Ошибка при отправке БД на GitHub (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(5)

# --- ФУНКЦИИ ДЛЯ РАБОТЫ С БД ---

def init_db():
    """Создает таблицы, если их нет."""
    try:
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
    except Exception as e:
        logging.error(f"Ошибка при инициализации БД: {e}")

def run_query(query, params=(), commit=False, fetch=None):
    """Универсальная функция для выполнения запросов к БД."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
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
        SELECT r.reply_text, r.created_at, u.username, u.first_name
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

def get_all_users_for_admin():
    return run_query("SELECT user_id, username, first_name, created_at FROM users ORDER BY created_at DESC", fetch="all")

def get_admin_stats():
    stats = {}
    stats['users'] = run_query("SELECT COUNT(*) FROM users", fetch="one")[0]
    stats['links'] = run_query("SELECT COUNT(*) FROM links WHERE is_active = 1", fetch="one")[0]
    stats['messages'] = run_query("SELECT COUNT(*) FROM messages", fetch="one")[0]
    stats['replies'] = run_query("SELECT COUNT(*) FROM replies", fetch="one")[0]
    
    stats['photos'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'photo'", fetch="one")[0]
    stats['videos'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'video'", fetch="one")[0]
    stats['documents'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'document'", fetch="one")[0]
    stats['voice'] = run_query("SELECT COUNT(*) FROM messages WHERE message_type = 'voice'", fetch="one")[0]
    
    return stats

def get_all_data_for_html():
    data = {}
    data['stats'] = get_admin_stats()
    data['users'] = run_query('''
        SELECT u.user_id, u.username, u.first_name, u.created_at,
               (SELECT COUNT(*) FROM links l WHERE l.user_id = u.user_id) as link_count,
               (SELECT COUNT(*) FROM messages m WHERE m.to_user_id = u.user_id) as received_messages,
               (SELECT COUNT(*) FROM messages m WHERE m.from_user_id = u.user_id) as sent_messages
        FROM users u
        ORDER BY u.created_at DESC
    ''', fetch="all")
    
    data['links'] = run_query('''
        SELECT l.link_id, l.title, l.description, l.created_at, l.expires_at,
               u.username, u.first_name, u.user_id,
               (SELECT COUNT(*) FROM messages m WHERE m.link_id = l.link_id) as message_count
        FROM links l
        LEFT JOIN users u ON l.user_id = u.user_id
        WHERE l.is_active = 1
        ORDER BY l.created_at DESC
    ''', fetch="all")
    
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
    ''', fetch="all")
    
    return data

def generate_html_report():
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
                    <div class="nav-item active">
                        <div class="nav-icon"><i class="fas fa-tachometer-alt"></i></div>
                        <div>Общая статистика</div>
                    </div>
                    <div class="nav-item">
                        <div class="nav-icon"><i class="fas fa-users"></i></div>
                        <div>Пользователи ({data['stats']['users']})</div>
                    </div>
                    <div class="nav-item">
                        <div class="nav-icon"><i class="fas fa-link"></i></div>
                        <div>Ссылки ({data['stats']['links']})</div>
                    </div>
                    <div class="nav-item">
                        <div class="nav-icon"><i class="fas fa-envelope"></i></div>
                        <div>Сообщения ({data['stats']['messages']})</div>
                    </div>
                    <div class="nav-item">
                        <div class="nav-icon"><i class="fas fa-reply"></i></div>
                        <div>Ответы ({data['stats']['replies']})</div>
                    </div>
                    <div class="nav-item">
                        <div class="nav-icon"><i class="fas fa-chart-bar"></i></div>
                        <div>Аналитика</div>
                    </div>
                </div>
                
                <!-- Основной контент -->
                <div class="main-content">
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
                    
                    <!-- Пользователи -->
                    <div class="section fade-in">
                        <h2><i class="fas fa-users"></i> АКТИВНЫЕ ПОЛЬЗОВАТЕЛИ</h2>
                        <input type="text" class="search-box" placeholder="🔍 Поиск пользователей...">
                        <table>
                            <thead>
                                <tr>
                                    <th>ID</th>
                                    <th>Информация</th>
                                    <th>Регистрация</th>
                                    <th>Активность</th>
                                    <th>Статистика</th>
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
                                </tr>
        '''
    
    html_content += '''
                            </tbody>
                        </table>
                    </div>
                    
                    <!-- Ссылки -->
                    <div class="section fade-in">
                        <h2><i class="fas fa-link"></i> АКТИВНЫЕ ССЫЛКИ</h2>
                        <input type="text" class="search-box" placeholder="🔍 Поиск ссылок...">
                        <table>
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
        link_url = f"https://t.me/your_bot_username?start={link[0]}"
        
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
                                        <span class="badge badge-info">
                                            <i class="fas fa-eye"></i> Просмотр
                                        </span>
                                    </td>
                                </tr>
        '''
    
    html_content += '''
                            </tbody>
                        </table>
                    </div>
                    
                    <!-- Последние сообщения -->
                    <div class="section fade-in">
                        <h2><i class="fas fa-envelope"></i> ПОСЛЕДНИЕ СООБЩЕНИЯ</h2>
                        <input type="text" class="search-box" placeholder="🔍 Поиск сообщений...">
                        <table>
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
            
            // Поиск по таблицам
            document.querySelectorAll('.search-box').forEach(searchBox => {{
                searchBox.addEventListener('input', function(e) {{
                    const searchTerm = e.target.value.toLowerCase();
                    const table = this.closest('.section').querySelector('tbody');
                    const rows = table.querySelectorAll('tr');
                    
                    rows.forEach(row => {{
                        const text = row.textContent.toLowerCase();
                        row.style.display = text.includes(searchTerm) ? '' : 'none';
                    }});
                }});
            }});
            
            // Навигация по боковой панели
            document.querySelectorAll('.nav-item').forEach(item => {{
                item.addEventListener('click', function() {{
                    document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
                    this.classList.add('active');
                }});
            }});
            
            // Автоматическое обновление времени
            function updateTime() {{
                const now = new Date();
                const timeString = now.toLocaleString('ru-RU', {{
                    year: 'numeric',
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit'
                }});
                const timeElement = document.querySelector('.timestamp');
                if (timeElement) {{
                    timeElement.innerHTML = `<i class="fas fa-clock"></i> Отчет сгенерирован: ${timeString}`;
                }}
            }}
            
            setInterval(updateTime, 1000);
        </script>
    </body>
    </html>
    '''
    
    return html_content

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def escape_markdown(text: str) -> str:
    """Экранирует специальные символы для MarkdownV2"""
    if not text: 
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

def format_as_quote(text: str) -> str:
    if not text: 
        return ""
    escaped_text = escape_markdown(text)
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
                text = f"🔗 *Анонимная ссылка*\n\n📝 *{escape_markdown(link_info[2])}*\n📋 {escape_markdown(link_info[3])}\n\n✍️ Напишите анонимное сообщение или отправьте медиафайл\\."
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
                    text += f"📝 *{escape_markdown(link[1])}*\n📋 {escape_markdown(link[2])}\n🔗 `{escape_markdown(link_url)}`\n🕒 `{created}`\n\n"
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
                    text += f"{type_icon} *{escape_markdown(link_title)}*\n{format_as_quote(preview)}\n🕒 `{created_str}` \\| 💬 Ответов\\: {reply_count}\n\n"
                
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
                text = f"💬 *Ответы на сообщение* \\#{message_id}\\:\n\n"
                for i, reply in enumerate(replies, 1):
                    reply_text, created, username, first_name = reply
                    sender = f"@{username}" if username else (first_name or "Аноним")
                    created_str = format_datetime(created)
                    text += f"{i}\\. 👤 *{escape_markdown(sender)}* \\(`{created_str}`\\)\\:\n{format_as_quote(reply_text)}\n\n"
                await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=message_details_keyboard(message_id))
            else:
                await query.edit_message_text(
                    f"💬 На сообщение \\#{message_id} пока нет ответов\\.\n\nБудьте первым, кто ответит\\!",
                    parse_mode='MarkdownV2', 
                    reply_markup=message_details_keyboard(message_id)
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
                        header = f"*#{i+1}* \\| 🕒 `{created_str}`\n"
                        header += f"*От\\:* {escape_markdown(from_user or from_name or 'Аноним')}\n"
                        header += f"*Кому\\:* {escape_markdown(to_user or to_name or 'Аноним')}\n"
                        header += f"*Ссылка\\:* {escape_markdown(link_title or 'N/A')}\n"
                        
                        if msg_type == 'text':
                            await query.message.reply_text(f"{header}\n{format_as_quote(msg_text)}", parse_mode='MarkdownV2')
                        else:
                            file_info = f"\n*Тип\\:* {msg_type}"
                            if file_size:
                                file_info += f" \\({(file_size or 0) // 1024} KB\\)"
                            if file_name:
                                file_info += f"\n*Файл\\:* {escape_markdown(file_name)}"
                            
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
                    reply_notification = f"💬 *Получен ответ на ваше сообщение\\:*\n{format_as_quote(original_msg[1])}\n\n*Ответ #{current_count + 1}\\:*\n{format_as_quote(text)}"
                    await context.bot.send_message(original_msg[0], reply_notification, parse_mode='MarkdownV2')
                except Exception as e:
                    logging.error(f"Failed to send reply notification: {e}")
            
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
                    f"✅ *Ссылка создана\\!*\n\n📝 *{escape_markdown(title)}*\n📋 {escape_markdown(text)}\n\n🔗 `{escape_markdown(link_url)}`\n\nПоделитесь ей, чтобы получать сообщения\\!",
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
                
                admin_notification = f"📨 *Новое сообщение*\nОт\\: {escape_markdown(user.username or user.first_name or 'Аноним')} \\> Кому\\: {escape_markdown(link_info[4] or 'Аноним')}\n\n{format_as_quote(text)}"
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
                    file_info += f"\n📄 `{escape_markdown(file_name)}`"
                
                user_caption = f"📨 *Новый анонимный {msg_type}*{file_info}\n\n{format_as_quote(caption)}"
                admin_caption = f"📨 *Новый {msg_type}*\nОт\\: {escape_markdown(user.username or user.first_name or 'Аноним')} \\> Кому\\: {escape_markdown(link_info[4] or 'Аноним')}{file_info}\n\n{format_as_quote(caption)}"
                
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

def main():
    if not all([BOT_TOKEN, ADMIN_ID, GITHUB_TOKEN, GITHUB_REPO, DB_FILENAME]):
        logging.critical("КРИТИЧЕСКАЯ ОШИБКА: Не установлены все переменные окружения")
        return
    
    setup_repo()
    init_db()
    
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    media_filters = filters.PHOTO | filters.VIDEO | filters.VOICE | filters.Document.ALL
    application.add_handler(MessageHandler(media_filters & ~filters.COMMAND, handle_media))
    
    application.add_error_handler(error_handler)
    
    logging.info("Бот запускается...")
    
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
            close_loop=False
        )
    except Exception as e:
        logging.critical(f"Критическая ошибка при запуске бота: {e}")

if __name__ == "__main__":
    main()
