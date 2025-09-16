import logging
import os
import secrets
import string
import sqlite3
import re
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode

# --- НАСТРОЙКИ ДЛЯ ХОСТИНГА ---
# Токен и данные админа теперь берутся из переменных окружения на Render
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))

# --- ПУТЬ К БАЗЕ ДАННЫХ ---
# База данных будет храниться на постоянном диске, подключенном к папке /data
DB_PATH = "/data/anon_bot.db"

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Таблица анонимных ссылок
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS links (
        link_id TEXT PRIMARY KEY,
        user_id INTEGER,
        title TEXT,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')
    
    # Таблица сообщений
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS messages (
        message_id INTEGER PRIMARY KEY AUTOINCREMENT,
        link_id TEXT,
        from_user_id INTEGER,
        to_user_id INTEGER,
        message_text TEXT,
        message_type TEXT DEFAULT 'text',
        file_id TEXT,
        is_anonymous BOOLEAN DEFAULT TRUE,
        parent_message_id INTEGER DEFAULT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (link_id) REFERENCES links (link_id),
        FOREIGN KEY (from_user_id) REFERENCES users (user_id),
        FOREIGN KEY (to_user_id) REFERENCES users (user_id),
        FOREIGN KEY (parent_message_id) REFERENCES messages (message_id)
    )
    ''')
    
    # Таблица ответов
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS replies (
        reply_id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER,
        from_user_id INTEGER,
        reply_text TEXT,
        is_anonymous BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (message_id) REFERENCES messages (message_id),
        FOREIGN KEY (from_user_id) REFERENCES users (user_id)
    )
    ''')
    
    conn.commit()
    conn.close()

# Сохранение пользователя в БД
def save_user(user_id, username, first_name):
    conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
    cursor = conn.cursor()
    cursor.execute(
        'INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)',
        (user_id, username, first_name)
    )
    conn.commit()
    conn.close()

# Генерация случайной строки для ссылок
def generate_link_id(length=10):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

# Создание анонимной ссылки
def create_anon_link(user_id, title, description):
    link_id = generate_link_id()
    expires_at = datetime.now() + timedelta(days=30)
    
    conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO links (link_id, user_id, title, description, expires_at) VALUES (?, ?, ?, ?, ?)',
        (link_id, user_id, title, description, expires_at)
    )
    conn.commit()
    conn.close()
    
    return link_id

# Получение ссылок пользователя
def get_user_links(user_id):
    conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
    cursor = conn.cursor()
    cursor.execute(
        'SELECT link_id, title, description, created_at FROM links WHERE user_id = ?',
        (user_id,)
    )
    links = cursor.fetchall()
    conn.close()
    return links

# Сохранение сообщения
def save_message(link_id, from_user_id, to_user_id, message_text, message_type='text', file_id=None, parent_message_id=None):
    conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO messages (link_id, from_user_id, to_user_id, message_text, message_type, file_id, parent_message_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (link_id, from_user_id, to_user_id, message_text, message_type, file_id, parent_message_id)
    )
    message_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return message_id

# Сохранение ответа на сообщение
def save_reply(message_id, from_user_id, reply_text, is_anonymous=True):
    conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO replies (message_id, from_user_id, reply_text, is_anonymous) VALUES (?, ?, ?, ?)',
        (message_id, from_user_id, reply_text, is_anonymous)
    )
    reply_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return reply_id

# Получение информации о ссылке
def get_link_info(link_id):
    conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
    cursor = conn.cursor()
    cursor.execute(
        'SELECT l.link_id, l.user_id, l.title, l.description, u.username FROM links l JOIN users u ON l.user_id = u.user_id WHERE l.link_id = ?',
        (link_id,)
    )
    link_info = cursor.fetchone()
    conn.close()
    return link_info

# Получение сообщений для пользователя
def get_user_messages(user_id, limit=20):
    conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
    cursor = conn.cursor()
    cursor.execute('''
    SELECT m.message_id, m.message_text, m.message_type, m.file_id, m.created_at, 
           u.username as from_username, l.title as link_title
    FROM messages m
    JOIN users u ON m.from_user_id = u.user_id
    JOIN links l ON m.link_id = l.link_id
    WHERE m.to_user_id = ?
    ORDER BY m.created_at DESC
    LIMIT ?
    ''', (user_id, limit))
    messages = cursor.fetchall()
    conn.close()
    return messages

# Получение всех сообщений для админа
def get_all_messages(limit=50):
    conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
    cursor = conn.cursor()
    cursor.execute('''
    SELECT m.message_id, m.link_id, m.message_text, m.message_type, m.created_at, 
           u_from.username as from_username, u_to.username as to_username,
           l.title as link_title, m.file_id
    FROM messages m
    JOIN users u_from ON m.from_user_id = u_from.user_id
    JOIN users u_to ON m.to_user_id = u_to.user_id
    JOIN links l ON m.link_id = l.link_id
    ORDER BY m.created_at DESC
    LIMIT ?
    ''', (limit,))
    messages = cursor.fetchall()
    conn.close()
    return messages
    
# Получение всех пользователей для админа
def get_all_users_for_admin():
    conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, username, first_name FROM users ORDER BY created_at DESC')
    users = cursor.fetchall()
    conn.close()
    return users

# Получение истории переписки для админа
def get_full_history_for_admin(user_id):
    conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
    cursor = conn.cursor()
    cursor.execute('''
        SELECT 
            m.message_text, 
            m.message_type, 
            m.file_id, 
            m.created_at, 
            u_from.username as from_username, 
            u_to.username as to_username
        FROM messages m
        JOIN users u_from ON m.from_user_id = u_from.user_id
        JOIN users u_to ON m.to_user_id = u_to.user_id
        WHERE m.from_user_id = ? OR m.to_user_id = ?
        ORDER BY m.created_at ASC
    ''', (user_id, user_id))
    history = cursor.fetchall()
    conn.close()
    return history

# Получение ответов на сообщение
def get_message_replies(message_id):
    conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
    cursor = conn.cursor()
    cursor.execute('''
    SELECT r.reply_text, r.created_at, u.username
    FROM replies r
    JOIN users u ON r.from_user_id = u.user_id
    WHERE r.message_id = ?
    ORDER BY r.created_at ASC
    ''', (message_id,))
    replies = cursor.fetchall()
    conn.close()
    return replies

# Получение статистики для админа
def get_admin_stats():
    conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) FROM users')
    users_count = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM links')
    links_count = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM messages')
    messages_count = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM replies')
    replies_count = cursor.fetchone()[0]
    
    conn.close()
    
    return {
        'users': users_count,
        'links': links_count,
        'messages': messages_count,
        'replies': replies_count
    }

# Форматирование текста для безопасного отображения в MarkdownV2
def escape_markdown(text: str) -> str:
    if not text:
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

# Клавиатуры
def main_keyboard():
    keyboard = [
        [InlineKeyboardButton("🟣 | 𝙰𝚞𝚋𝚎𝟷𝚐", callback_data="main_menu")],
        [InlineKeyboardButton("🔗 Мои ссылки", callback_data="my_links")],
        [InlineKeyboardButton("➕ Создать ссылку", callback_data="create_link")],
        [InlineKeyboardButton("📨 Мои сообщения", callback_data="my_messages")]
    ]
    return InlineKeyboardMarkup(keyboard)

def message_keyboard(message_id):
    keyboard = [
        [InlineKeyboardButton("💬 Ответить анонимно", callback_data=f"reply_{message_id}")],
        [InlineKeyboardButton("🔙 Назад к сообщениям", callback_data="my_messages")]
    ]
    return InlineKeyboardMarkup(keyboard)

def admin_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👁️ Все сообщения", callback_data="admin_view_messages")],
        [InlineKeyboardButton("📜 История переписки", callback_data="admin_history")],
        [InlineKeyboardButton("📢 Оповещение", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def back_to_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.username, user.first_name)
    
    if context.args:
        link_id = context.args[0]
        link_info = get_link_info(link_id)
        
        if link_info:
            context.user_data['current_link'] = link_id
            link_title = escape_markdown(link_info[2])
            link_desc = escape_markdown(link_info[3])
            welcome_text = (
                f"🔗 *Анонимная ссылка*\n\n"
                f"📝 *{link_title}*\n"
                f"📋 {link_desc}\n\n"
                f"💬 Вы можете отправить анонимное сообщение владельцу этой ссылки\\.\n"
                f"✍️ Просто напишите ваше сообщение или отправьте медиа\\!"
            )
            await update.message.reply_text(welcome_text, parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
            return
    
    welcome_text = (
        "👋 *Добро пожаловать в Анонимный Бот\\!*\n\n"
        "✨ *Создавайте анонимные ссылки для получения вопросов и сообщений*\n\n"
        "🪄 *Возможности:*\n"
        "• Создание персональных анонимных ссылок\n"
        "• Получение анонимных сообщений\n"
        "• Отправка анонимных вопросов другим\n"
        "• Поддержка текста, фото, видео и голосовых сообщений\n"
        "• Полная конфиденциальность \\(админ видит всё для безопасности\\)"
    )
    await update.message.reply_text(welcome_text, reply_markup=main_keyboard(), parse_mode='MarkdownV2')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    
    parts = query.data.split(':')
    command = parts[0]
    
    if command == "main_menu":
        await query.edit_message_text(
            "🎭 *Анонимный Бот \\| 🟣 \\| 𝙰𝚞𝚋𝚎𝟷𝚐*\n\n"
            "🪄 *Доступные функции:*\n"
            "• Создание анонимных ссылок\n"
            "• Получение анонимных сообщений\n"
            "• Отправка анонимных вопросов",
            reply_markup=main_keyboard(),
            parse_mode='MarkdownV2'
        )
    
    elif command == "my_links":
        links = get_user_links(user.id)
        if links:
            links_text = "🔗 *Ваши анонимные ссылки:*\n\n"
            for link in links:
                link_url = f"https://t.me/{context.bot.username}?start={link[0]}"
                title = escape_markdown(link[1])
                desc = escape_markdown(link[2])
                date = escape_markdown(link[3][:10])
                links_text += f"📝 *{title}*\n{desc}\n🔗 `{link_url}`\n📅 {date}\n\n"
            
            keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
            await query.edit_message_text(links_text, parse_mode='MarkdownV2', reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.edit_message_text("У вас пока нет созданных ссылок\\.", reply_markup=main_keyboard(), parse_mode='MarkdownV2')
    
    elif command == "create_link":
        context.user_data['creating_link'] = True
        context.user_data['link_stage'] = 'title'
        await query.edit_message_text(
            "🔗 *Создание анонимной ссылки*\n\n"
            "📝 Введите название для вашей ссылки:",
            parse_mode='MarkdownV2',
            reply_markup=back_to_main_keyboard()
        )
    
    elif command == "my_messages":
        messages = get_user_messages(user.id)
        if messages:
            messages_text = "📨 *Ваши анонимные сообщения:*\n\n"
            for i, msg in enumerate(messages, 1):
                msg_text = msg[1] if msg[1] is not None else ""
                msg_preview = escape_markdown(msg_text[:50] + "..." if len(msg_text) > 50 else msg_text)
                link_title = escape_markdown(msg[6])
                date = escape_markdown(msg[4][:16])
                messages_text += f"{i}\\. *{link_title}* \n💬 {msg_preview}\n📅 {date}\n\n"
            
            keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
            await query.edit_message_text(messages_text, parse_mode='MarkdownV2', reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.edit_message_text("У вас пока нет сообщений\\.", parse_mode='MarkdownV2', reply_markup=main_keyboard())
    
    elif command.startswith("reply_"):
        message_id = int(command.replace("reply_", ""))
        context.user_data['replying_to'] = message_id
        await query.edit_message_text(
            f"📝 *Ответ на сообщение \\#{message_id}*\n\n"
            f"Напишите ваш ответ\\. Он будет отправлен анонимно\\.",
            parse_mode='MarkdownV2',
            reply_markup=back_to_main_keyboard()
        )
    
    is_admin = user.username == ADMIN_USERNAME or user.id == ADMIN_ID
    if not is_admin and command.startswith("admin_"):
        await query.answer("⛔️ У вас нет доступа к этой функции.", show_alert=True)
        return

    if is_admin:
        if command == "admin_panel":
             await query.edit_message_text(
                "🛠️ *Панель администратора*\n\nДоступные функции:",
                reply_markup=admin_keyboard(),
                parse_mode='MarkdownV2'
            )
        elif command == "admin_stats":
            stats = get_admin_stats()
            stats_text = (
                f"📊 *Статистика админа*\n\n"
                f"👥 Пользователей: {stats['users']}\n"
                f"🔗 Ссылок: {stats['links']}\n"
                f"📨 Сообщений: {stats['messages']}\n"
                f"💬 Ответов: {stats['replies']}"
            )
            await query.edit_message_text(stats_text, parse_mode='MarkdownV2', reply_markup=admin_keyboard())
        
        elif command == "admin_view_messages":
            messages = get_all_messages(10)
            if messages:
                messages_text = "👁️ *Последние сообщения*\n\n"
                for i, msg in enumerate(messages, 1):
                    msg_text = msg[2] if msg[2] is not None else ""
                    message_preview = escape_markdown(msg_text[:50] + "..." if len(msg_text) > 50 else msg_text)
                    message_type = "📝 Текст"
                    if msg[3] == 'photo': message_type = "🖼️ Фото"
                    elif msg[3] == 'video': message_type = "🎥 Видео"
                    elif msg[3] == 'voice': message_type = "🎵 Голосовое"
                    elif msg[3] == 'video_note': message_type = "📹 Видео-кружок"
                    elif msg[3] == 'document': message_type = "📄 Документ"
                    
                    messages_text += (
                        f"{i}\\. *{escape_markdown(msg[7])}*\n"
                        f"👤 от {escape_markdown(msg[5])} → к {escape_markdown(msg[6])}\n"
                        f"📦 {message_type}\n"
                        f"💬 {message_preview}\n"
                        f"📅 {escape_markdown(msg[4][:16])}\n\n"
                    )
                await query.edit_message_text(messages_text, parse_mode='MarkdownV2', reply_markup=admin_keyboard())
            else:
                await query.edit_message_text("Нет сообщений для отображения\\.", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
        
        elif command == "admin_broadcast":
            context.user_data['broadcasting'] = True
            await query.edit_message_text(
                "📢 *Создание оповещения*\n\nВведите сообщение для рассылки всем пользователям:",
                parse_mode='MarkdownV2',
                reply_markup=back_to_main_keyboard()
            )

        elif command == "admin_history":
            users_list = get_all_users_for_admin()
            if not users_list:
                await query.edit_message_text("👥 Пользователей не найдено\\.", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
                return
            
            keyboard = []
            for u in users_list:
                user_display = u[1] or u[2] or f"ID: {u[0]}"
                keyboard.append([InlineKeyboardButton(user_display, callback_data=f"admin_view_user:{u[0]}")])
            keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")])
            
            await query.edit_message_text("👥 *Выберите пользователя для просмотра истории:*", reply_markup=InlineKeyboardMarkup(keyboard))
            
        elif command == "admin_view_user":
            target_user_id = int(parts[1])
            history = get_full_history_for_admin(target_user_id)
            
            await query.message.reply_text(f"📜 *История переписки для пользователя ID {target_user_id}*", parse_mode='MarkdownV2')
            
            if not history:
                await query.message.reply_text("Сообщений не найдено\\.", parse_mode='MarkdownV2')
                return
            
            for msg in history:
                text, msg_type, file_id, date, from_user, to_user = msg
                header = f"*{escape_markdown(from_user or 'Unknown')}* ➡️ *{escape_markdown(to_user or 'Unknown')}*\n_{escape_markdown(date[:16])}_"
                caption_text = f"{header}\n\n{escape_markdown(text)}" if text else header
                
                try:
                    if msg_type == 'text':
                        await query.message.reply_text(f"{header}\n\n{escape_markdown(text)}", parse_mode='MarkdownV2')
                    elif msg_type == 'photo':
                        await query.message.reply_photo(photo=file_id, caption=caption_text, parse_mode='MarkdownV2')
                    elif msg_type == 'video':
                        await query.message.reply_video(video=file_id, caption=caption_text, parse_mode='MarkdownV2')
                    elif msg_type == 'voice':
                        await query.message.reply_voice(voice=file_id, caption=header, parse_mode='MarkdownV2')
                    elif msg_type == 'video_note':
                        await query.message.reply_video_note(video_note=file_id)
                        await query.message.reply_text(header, parse_mode='MarkdownV2')
                    elif msg_type == 'document':
                        await query.message.reply_document(document=file_id, caption=caption_text, parse_mode='MarkdownV2')
                except Exception as e:
                    logging.error(f"Не удалось отправить сообщение истории админу: {e}")
                    await query.message.reply_text(f"Ошибка при отправке сообщения типа `{msg_type}`\\.", parse_mode='MarkdownV2')

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.via_bot: return
    user = update.effective_user
    message_text = update.message.text
    save_user(user.id, user.username, user.first_name)
    is_admin = user.username == ADMIN_USERNAME or user.id == ADMIN_ID
    
    if 'replying_to' in context.user_data:
        message_id = context.user_data.pop('replying_to')
        save_reply(message_id, user.id, message_text)
        
        conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
        cursor = conn.cursor()
        cursor.execute('SELECT from_user_id, message_text, (SELECT title FROM links WHERE link_id = messages.link_id) FROM messages WHERE message_id = ?', (message_id,))
        original_msg = cursor.fetchone()
        conn.close()
        
        if original_msg:
            try:
                reply_text = (
                    f"💬 *Вы получили ответ на ваше сообщение:*\n\n"
                    f"📝 *{escape_markdown(original_msg[2])}*\n"
                    f"💭 _{escape_markdown(original_msg[1])}_\n\n"
                    f"📨 *Ответ:*\n{escape_markdown(message_text)}"
                )
                await context.bot.send_message(chat_id=original_msg[0], text=reply_text, parse_mode='MarkdownV2')
            except Exception as e:
                logging.error(f"Не удалось отправить ответ пользователю {original_msg[0]}: {e}")
        
        await update.message.reply_text("✅ Ваш ответ отправлен анонимно!", reply_markup=main_keyboard())
        return

    if context.user_data.get('creating_link'):
        stage = context.user_data.get('link_stage')
        if stage == 'title':
            context.user_data['link_title'] = message_text
            context.user_data['link_stage'] = 'description'
            await update.message.reply_text("📋 Теперь введите описание для вашей ссылки:", reply_markup=back_to_main_keyboard())
            return
        
        elif stage == 'description':
            link_title = context.user_data.pop('link_title')
            context.user_data.pop('creating_link')
            context.user_data.pop('link_stage')
            
            link_id = create_anon_link(user.id, link_title, message_text)
            link_url = f"https://t.me/{context.bot.username}?start={link_id}"
            
            await update.message.reply_text(
                f"✅ *Анонимная ссылка создана\\!*\n\n"
                f"📝 *{escape_markdown(link_title)}*\n"
                f"📋 {escape_markdown(message_text)}\n\n"
                f"🔗 *Ваша ссылка:*\n`{link_url}`\n\n"
                f"📢 Поделитесь этой ссылкой, чтобы получать анонимные сообщения\\!",
                parse_mode='MarkdownV2',
                reply_markup=main_keyboard()
            )
            return

    if is_admin and context.user_data.get('broadcasting'):
        context.user_data.pop('broadcasting')
        conn = sqlite3.connect(DB_PATH) # ИЗМЕНЕНО
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM users')
        all_users = cursor.fetchall()
        conn.close()
        
        success_count = 0
        for u in all_users:
            try:
                await context.bot.send_message(chat_id=u[0], text=message_text, parse_mode='MarkdownV2')
                success_count += 1
            except Exception as e:
                logging.warning(f"Не удалось отправить оповещение пользователю {u[0]}: {e}")
                
        await update.message.reply_text(f"📢 Оповещение отправлено {success_count} из {len(all_users)} пользователей\\!", reply_markup=admin_keyboard(), parse_mode='MarkdownV2')
        return

    if context.user_data.get('current_link'):
        link_id = context.user_data.pop('current_link')
        link_info = get_link_info(link_id)
        
        if link_info:
            message_id = save_message(link_id, user.id, link_info[1], message_text)
            
            try:
                await context.bot.send_message(
                    chat_id=link_info[1],
                    text=f"📨 *Новое анонимное сообщение*\n\n{escape_markdown(message_text)}",
                    parse_mode='MarkdownV2',
                    reply_markup=message_keyboard(message_id)
                )
            except Exception as e:
                logging.error(f"Не удалось отправить сообщение пользователю {link_info[1]}: {e}")
            
            admin_msg = (
                f"📨 *Новое анонимное сообщение*\n\n"
                f"👤 От: {escape_markdown(user.username or user.first_name)}\n"
                f"👤 Кому: {escape_markdown(link_info[4])}\n"
                f"📝 Текст: {escape_markdown(message_text)}"
            )
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, parse_mode='MarkdownV2')
            
            await update.message.reply_text("✅ Ваше сообщение отправлено анонимно!", reply_markup=main_keyboard())
        return

    await update.message.reply_text("👋 Используйте кнопки ниже для навигации:", reply_markup=main_keyboard())

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.username, user.first_name)
    
    message_type, file_id, caption = 'unknown', None, ""
    
    if update.message.photo:
        message_type, file_id, caption = 'photo', update.message.photo[-1].file_id, update.message.caption
    elif update.message.video:
        message_type, file_id, caption = 'video', update.message.video.file_id, update.message.caption
    elif update.message.voice:
        message_type, file_id = 'voice', update.message.voice.file_id
    elif update.message.video_note:
        message_type, file_id = 'video_note', update.message.video_note.file_id
    elif update.message.document:
        message_type, file_id, caption = 'document', update.message.document.file_id, update.message.caption
    
    if context.user_data.get('current_link'):
        link_id = context.user_data.pop('current_link')
        link_info = get_link_info(link_id)
        
        if link_info:
            caption_text = caption if caption else ""
            message_id = save_message(link_id, user.id, link_info[1], caption_text, message_type, file_id)
            
            try:
                if message_type == 'photo':
                    await context.bot.send_photo(link_info[1], file_id, caption=f"📨 *Новое анонимное фото*\n\n{escape_markdown(caption_text)}", parse_mode='MarkdownV2', reply_markup=message_keyboard(message_id))
                elif message_type == 'video':
                    await context.bot.send_video(link_info[1], file_id, caption=f"📨 *Новое анонимное видео*\n\n{escape_markdown(caption_text)}", parse_mode='MarkdownV2', reply_markup=message_keyboard(message_id))
                elif message_type == 'voice':
                    await context.bot.send_voice(link_info[1], file_id, caption="📨 *Новое анонимное голосовое сообщение*", parse_mode='MarkdownV2', reply_markup=message_keyboard(message_id))
                elif message_type == 'video_note':
                    await context.bot.send_video_note(link_info[1], file_id)
                    await context.bot.send_message(link_info[1], text="📨 *Новое анонимное видео-сообщение*", parse_mode='MarkdownV2', reply_markup=message_keyboard(message_id))
                elif message_type == 'document':
                    await context.bot.send_document(link_info[1], file_id, caption=f"📨 *Новый анонимный документ*\n\n{escape_markdown(caption_text)}", parse_mode='MarkdownV2', reply_markup=message_keyboard(message_id))
            except Exception as e:
                logging.error(f"Не удалось отправить медиа пользователю {link_info[1]}: {e}")
            
            admin_msg = (
                f"📨 *Новое анонимное медиа-сообщение*\n\n"
                f"👤 От: {escape_markdown(user.username or user.first_name)}\n"
                f"👤 Кому: {escape_markdown(link_info[4])}\n"
                f"📦 Тип: {message_type}"
            )
            
            try:
                if message_type == 'photo': await context.bot.send_photo(ADMIN_ID, file_id, caption=admin_msg, parse_mode='MarkdownV2')
                elif message_type == 'video': await context.bot.send_video(ADMIN_ID, file_id, caption=admin_msg, parse_mode='MarkdownV2')
                elif message_type == 'voice': await context.bot.send_voice(ADMIN_ID, file_id, caption=admin_msg, parse_mode='MarkdownV2')
                elif message_type == 'video_note':
                    await context.bot.send_video_note(ADMIN_ID, file_id)
                    await context.bot.send_message(ADMIN_ID, text=admin_msg, parse_mode='MarkdownV2')
                elif message_type == 'document': await context.bot.send_document(ADMIN_ID, file_id, caption=admin_msg, parse_mode='MarkdownV2')
            except Exception as e:
                logging.error(f"Не удалось отправить медиа админу: {e}")
            
            await update.message.reply_text("✅ Ваше медиа-сообщение отправлено анонимно!", reply_markup=main_keyboard())
        return

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.username == ADMIN_USERNAME or user.id == ADMIN_ID:
        await update.message.reply_text(
            "🛠️ *Панель администратора*\n\nДоступные функции:",
            reply_markup=admin_keyboard(),
            parse_mode='MarkdownV2'
        )
    else:
        await update.message.reply_text("⛔️ У вас нет доступа к этой команде\\.", parse_mode='MarkdownV2')

def main():
    # Проверка наличия токена перед запуском
    if not BOT_TOKEN:
        logging.error("Ошибка: BOT_TOKEN не найден. Проверьте переменные окружения.")
        return
        
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    media_filters = filters.PHOTO | filters.VIDEO | filters.VOICE | filters.VIDEO_NOTE | filters.Document.ALL
    application.add_handler(MessageHandler(media_filters & ~filters.COMMAND, handle_media))

    application.run_polling()

if __name__ == "__main__":
    main()
