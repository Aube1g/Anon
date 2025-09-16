import logging
import os
import secrets
import string
import psycopg2 # Используем библиотеку для PostgreSQL
import re
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode

# --- НАСТРОЙКИ ДЛЯ ХОСТИНГА ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_ID = int(os.environ.get("ADMIN_ID")) if os.environ.get("ADMIN_ID") else None
# Эта переменная будет создана автоматически после привязки базы данных
DATABASE_URL = os.environ.get("DATABASE_URL")

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)


# --- ФУНКЦИИ ДЛЯ РАБОТЫ С POSTGRESQL ---

def get_db_connection():
    """Устанавливает соединение с базой данных PostgreSQL."""
    try:
        return psycopg2.connect(DATABASE_URL)
    except psycopg2.OperationalError as e:
        logging.error(f"Не удалось подключиться к базе данных PostgreSQL: {e}")
        return None

def init_db():
    """Создает таблицы в базе, если они еще не существуют."""
    conn = get_db_connection()
    if not conn: return
    # SQL команды, адаптированные под PostgreSQL
    commands = (
        "CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username VARCHAR(255), first_name VARCHAR(255), created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS links (link_id VARCHAR(10) PRIMARY KEY, user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE, title TEXT, description TEXT, created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMP WITH TIME ZONE)",
        "CREATE TABLE IF NOT EXISTS messages (message_id SERIAL PRIMARY KEY, link_id VARCHAR(10) REFERENCES links(link_id) ON DELETE CASCADE, from_user_id BIGINT, to_user_id BIGINT, message_text TEXT, message_type VARCHAR(50) DEFAULT 'text', file_id TEXT, created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS replies (reply_id SERIAL PRIMARY KEY, message_id INTEGER REFERENCES messages(message_id) ON DELETE CASCADE, from_user_id BIGINT, reply_text TEXT, created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP)"
    )
    try:
        with conn.cursor() as cur:
            for command in commands:
                cur.execute(command)
        conn.commit()
    except (Exception, psycopg2.DatabaseError) as error:
        logging.error(f"Ошибка при создании таблиц: {error}")
    finally:
        conn.close()

def execute_query(query, params=None, fetch=None):
    """Универсальная функция для выполнения запросов к БД."""
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            if fetch == "one": return cur.fetchone()
            if fetch == "all": return cur.fetchall()
            conn.commit() # Сохраняем изменения (INSERT, UPDATE, DELETE)
    except (Exception, psycopg2.DatabaseError) as error:
        logging.error(f"Ошибка выполнения запроса: {error}")
        return None
    finally:
        conn.close()

# --- Переписанные функции для работы с данными ---

def save_user(user_id, username, first_name):
    execute_query("INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING", (user_id, username, first_name))

def create_anon_link(user_id, title, description):
    link_id = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
    expires_at = datetime.now() + timedelta(days=30)
    execute_query("INSERT INTO links (link_id, user_id, title, description, expires_at) VALUES (%s, %s, %s, %s, %s)", (link_id, user_id, title, description, expires_at))
    return link_id

def save_message(link_id, from_user_id, to_user_id, message_text, message_type='text', file_id=None):
    result = execute_query("INSERT INTO messages (link_id, from_user_id, to_user_id, message_text, message_type, file_id) VALUES (%s, %s, %s, %s, %s, %s) RETURNING message_id", (link_id, from_user_id, to_user_id, message_text, message_type, file_id), fetch="one")
    return result[0] if result else None

def save_reply(message_id, from_user_id, reply_text):
    execute_query("INSERT INTO replies (message_id, from_user_id, reply_text) VALUES (%s, %s, %s)", (message_id, from_user_id, reply_text))

def get_link_info(link_id):
    return execute_query("SELECT l.link_id, l.user_id, l.title, l.description, u.username FROM links l LEFT JOIN users u ON l.user_id = u.user_id WHERE l.link_id = %s", (link_id,), fetch="one")

def get_user_links(user_id):
    return execute_query("SELECT link_id, title, description, created_at FROM links WHERE user_id = %s", (user_id,), fetch="all")
    
def get_user_messages(user_id, limit=20):
    return execute_query("SELECT m.message_id, m.message_text, m.message_type, m.file_id, m.created_at, l.title as link_title FROM messages m JOIN links l ON m.link_id = l.link_id WHERE m.to_user_id = %s ORDER BY m.created_at DESC LIMIT %s", (user_id, limit), fetch="all")

def get_full_history_for_admin(user_id):
    return execute_query("SELECT m.message_text, m.message_type, m.file_id, m.created_at, u_from.username as from_username, u_to.username as to_username FROM messages m LEFT JOIN users u_from ON m.from_user_id = u_from.user_id LEFT JOIN users u_to ON m.to_user_id = u_to.user_id WHERE m.from_user_id = %s OR m.to_user_id = %s ORDER BY m.created_at ASC", (user_id, user_id), fetch="all")

def get_all_users_for_admin():
    return execute_query("SELECT user_id, username, first_name FROM users ORDER BY created_at DESC", fetch="all")

def get_admin_stats():
    users = execute_query("SELECT COUNT(*) FROM users", fetch="one")[0]
    links = execute_query("SELECT COUNT(*) FROM links", fetch="one")[0]
    messages = execute_query("SELECT COUNT(*) FROM messages", fetch="one")[0]
    replies = execute_query("SELECT COUNT(*) FROM replies", fetch="one")[0]
    return {'users': users, 'links': links, 'messages': messages, 'replies': replies}

def escape_markdown(text: str) -> str:
    if not text: return ""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', str(text))

def format_as_quote(text: str) -> str:
    if not text: return ""
    return '\n'.join([f"> {line}" for line in escape_markdown(text).split('\n')])

# --- Код бота (без изменений) ---

def main_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🟣 | 𝙰𝚞𝚋𝚎𝟷𝚐", callback_data="main_menu")], [InlineKeyboardButton("🔗 Мои ссылки", callback_data="my_links")], [InlineKeyboardButton("➕ Создать ссылку", callback_data="create_link")], [InlineKeyboardButton("📨 Мои сообщения", callback_data="my_messages")]])

def message_keyboard(message_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("💬 Ответить", callback_data=f"reply_{message_id}")], [InlineKeyboardButton("🔙 Назад", callback_data="my_messages")]])

def admin_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")], [InlineKeyboardButton("📜 История переписки", callback_data="admin_history")], [InlineKeyboardButton("📢 Оповещение", callback_data="admin_broadcast")], [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]])

def back_to_main_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    parts = query.data.split(':')
    command = parts[0]
    is_admin = user.username == ADMIN_USERNAME or user.id == ADMIN_ID

    if command == "main_menu":
        await query.edit_message_text("🎭 *Главное меню*", reply_markup=main_keyboard(), parse_mode='MarkdownV2')
    elif command == "my_links":
        links = get_user_links(user.id)
        if links:
            text = "🔗 *Ваши анонимные ссылки:*\n\n"
            for link in links:
                link_url = f"https://t.me/{context.bot.username}?start={link[0]}"
                text += f"📝 *{escape_markdown(link[1])}*\n🔗 `{link_url}`\n\n"
            await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
        else:
            await query.edit_message_text("У вас пока нет созданных ссылок\\.", reply_markup=back_to_main_keyboard(), parse_mode='MarkdownV2')
    elif command == "create_link":
        context.user_data['creating_link'] = True
        context.user_data['link_stage'] = 'title'
        await query.edit_message_text("📝 Введите *название* для вашей ссылки:", parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
    elif command == "my_messages":
        messages = get_user_messages(user.id)
        if messages:
            text = "📨 *Ваши последние сообщения:*\n\n"
            for msg in messages:
                msg_text = msg[1] or f"_{msg[2]}_"
                preview = (msg_text[:50] + '...') if len(msg_text) > 50 else msg_text
                text += f"*{escape_markdown(msg[5])}:*\n{format_as_quote(preview)}\n_Нажмите кнопку 'Ответить' под сообщением, чтобы ответить\\._\n\n"
            await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
        else:
            await query.edit_message_text("У вас пока нет сообщений\\.", parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())
    elif command.startswith("reply_"):
        message_id = int(command.replace("reply_", ""))
        context.user_data['replying_to'] = message_id
        await query.edit_message_text(f"✍️ Введите ваш ответ на сообщение \\#{message_id}:", parse_mode='MarkdownV2', reply_markup=back_to_main_keyboard())

    if is_admin:
        if command == "admin_panel":
             await query.edit_message_text("🛠️ *Панель администратора*", reply_markup=admin_keyboard(), parse_mode='MarkdownV2')
        elif command == "admin_stats":
            stats = get_admin_stats()
            text = f"📊 *Статистика:*\n👥 Пользователей: {stats.get('users', 0)}\n🔗 Ссылок: {stats.get('links', 0)}\n📨 Сообщений: {stats.get('messages', 0)}\n💬 Ответов: {stats.get('replies', 0)}"
            await query.edit_message_text(text, parse_mode='MarkdownV2', reply_markup=admin_keyboard())
        elif command == "admin_history":
            users = get_all_users_for_admin()
            if users:
                kb = [[InlineKeyboardButton(u[1] or u[2] or f"ID: {u[0]}", callback_data=f"admin_view_user:{u[0]}")] for u in users]
                kb.append([InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")])
                await query.edit_message_text("👥 *Выберите пользователя для просмотра истории:*", reply_markup=InlineKeyboardMarkup(kb))
            else:
                await query.edit_message_text("Пользователей не найдено\\.", parse_mode='MarkdownV2', reply_markup=admin_keyboard())
        elif command == "admin_view_user":
            user_id = int(parts[1])
            history = get_full_history_for_admin(user_id)
            await query.message.reply_text(f"📜 *История переписки для ID {user_id}*", parse_mode='MarkdownV2')
            if history:
                for msg in history:
                    text, msg_type, file_id, date, from_u, to_u = msg
                    header = f"*{escape_markdown(from_u or '???')}* ➡️ *{escape_markdown(to_u or '???')}* `({date.strftime('%H:%M')})`"
                    if msg_type == 'text':
                        await query.message.reply_text(f"{header}\n{format_as_quote(text)}", parse_mode='MarkdownV2')
                    else:
                        caption = f"{header}\n{format_as_quote(text)}" if text else header
                        try:
                            if msg_type == 'photo': await query.message.reply_photo(file_id, caption=caption, parse_mode='MarkdownV2')
                            elif msg_type == 'video': await query.message.reply_video(file_id, caption=caption, parse_mode='MarkdownV2')
                            elif msg_type == 'document': await query.message.reply_document(file_id, caption=caption, parse_mode='MarkdownV2')
                            elif msg_type == 'voice': await query.message.reply_voice(file_id, caption=header, parse_mode='MarkdownV2')
                        except Exception as e:
                            logging.error(f"Failed to send media history: {e}")
                            await query.message.reply_text(f"{header}\n_{escape_markdown(msg_type)} не может быть отображен_", parse_mode='MarkdownV2')
            else:
                await query.message.reply_text("_Сообщений не найдено\\._", parse_mode='MarkdownV2')
        elif command == "admin_broadcast":
            context.user_data['broadcasting'] = True
            await query.edit_message_text("📢 Введите сообщение для рассылки всем пользователям:", reply_markup=back_to_main_keyboard())

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    save_user(user.id, user.username, user.first_name)
    is_admin = user.username == ADMIN_USERNAME or user.id == ADMIN_ID

    if 'replying_to' in context.user_data:
        msg_id = context.user_data.pop('replying_to')
        save_reply(msg_id, user.id, text)
        original_msg = execute_query("SELECT m.from_user_id, m.message_text FROM messages m WHERE m.message_id = %s", (msg_id,), fetch="one")
        if original_msg:
            try:
                reply_notification = f"💬 *Получен ответ на ваше сообщение:*\n{format_as_quote(original_msg[1])}\n\n*Ответ:*\n{format_as_quote(text)}"
                await context.bot.send_message(original_msg[0], reply_notification, parse_mode='MarkdownV2')
            except Exception as e:
                logging.error(f"Failed to send reply notification: {e}")
        await update.message.reply_text("✅ Ваш ответ отправлен анонимно!", reply_markup=main_keyboard())
        return

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
            link_url = f"https://t.me/{context.bot.username}?start={link_id}"
            await update.message.reply_text(f"✅ *Ссылка создана\\!*\n\n📝 *{escape_markdown(title)}*\n📋 {escape_markdown(text)}\n\n🔗 `{link_url}`\n\nПоделитесь ей, чтобы получать сообщения\\!", parse_mode='MarkdownV2', reply_markup=main_keyboard())
        return

    if is_admin and context.user_data.get('broadcasting'):
        context.user_data.pop('broadcasting')
        users = execute_query("SELECT user_id FROM users", fetch="all")
        sent_count = 0
        if users:
            for u in users:
                try:
                    await context.bot.send_message(u[0], text, parse_mode='MarkdownV2')
                    sent_count += 1
                except Exception as e:
                    logging.warning(f"Broadcast failed for user {u[0]}: {e}")
        await update.message.reply_text(f"📢 Рассылка завершена. Отправлено {sent_count}/{len(users) if users else 0} пользователям.", reply_markup=admin_keyboard())
        return

    if context.user_data.get('current_link'):
        link_id = context.user_data.pop('current_link')
        link_info = get_link_info(link_id)
        if link_info:
            msg_id = save_message(link_id, user.id, link_info[1], text)
            notification = f"📨 *Новое анонимное сообщение*\n\n{format_as_quote(text)}"
            try:
                await context.bot.send_message(link_info[1], notification, parse_mode='MarkdownV2', reply_markup=message_keyboard(msg_id))
            except Exception as e:
                logging.error(f"Failed to send message notification: {e}")
            admin_notification = f"📨 *Новое сообщение*\nОт: {escape_markdown(user.username or user.first_name)} -> Кому: {escape_markdown(link_info[4])}\n\n{format_as_quote(text)}"
            await context.bot.send_message(ADMIN_ID, admin_notification, parse_mode='MarkdownV2')
            await update.message.reply_text("✅ Ваше сообщение отправлено анонимно!", reply_markup=main_keyboard())
        return

    await update.message.reply_text("Используйте кнопки для навигации.", reply_markup=main_keyboard())

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.username, user.first_name)
    msg = update.message
    caption = msg.caption or ""
    file_id, msg_type = None, "unknown"

    if msg.photo: file_id, msg_type = msg.photo[-1].file_id, "photo"
    elif msg.video: file_id, msg_type = msg.video.file_id, "video"
    elif msg.voice: file_id, msg_type = msg.voice.file_id, "voice"
    elif msg.document: file_id, msg_type = msg.document.file_id, "document"

    if context.user_data.get('current_link') and file_id:
        link_id = context.user_data.pop('current_link')
        link_info = get_link_info(link_id)
        if link_info:
            msg_id = save_message(link_id, user.id, link_info[1], caption, msg_type, file_id)
            user_caption = f"📨 *Новый анонимный медиафайл*\n\n{format_as_quote(caption)}"
            admin_caption = f"📨 *Новый медиафайл*\nОт: {escape_markdown(user.username or user.first_name)} -> Кому: {escape_markdown(link_info[4])}\n\n{format_as_quote(caption)}"
            
            try: # Send to user
                if msg_type == 'photo': await context.bot.send_photo(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_keyboard(msg_id))
                elif msg_type == 'video': await context.bot.send_video(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_keyboard(msg_id))
                elif msg_type == 'document': await context.bot.send_document(link_info[1], file_id, caption=user_caption, parse_mode='MarkdownV2', reply_markup=message_keyboard(msg_id))
                elif msg_type == 'voice': 
                    await context.bot.send_voice(link_info[1], file_id)
                    await context.bot.send_message(link_info[1], "📨 _Получено новое голосовое сообщение_", parse_mode='MarkdownV2', reply_markup=message_keyboard(msg_id))
            except Exception as e: logging.error(f"Failed to send media notification to user: {e}")

            try: # Send to admin
                if msg_type == 'photo': await context.bot.send_photo(ADMIN_ID, file_id, caption=admin_caption, parse_mode='MarkdownV2')
                elif msg_type == 'video': await context.bot.send_video(ADMIN_ID, file_id, caption=admin_caption, parse_mode='MarkdownV2')
                elif msg_type == 'document': await context.bot.send_document(ADMIN_ID, file_id, caption=admin_caption, parse_mode='MarkdownV2')
                elif msg_type == 'voice': await context.bot.send_voice(ADMIN_ID, file_id, caption=admin_caption, parse_mode='MarkdownV2')
            except Exception as e: logging.error(f"Failed to send media notification to admin: {e}")
            
            await update.message.reply_text("✅ Ваше медиа отправлено анонимно!", reply_markup=main_keyboard())

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.username == ADMIN_USERNAME or user.id == ADMIN_ID:
        await update.message.reply_text("🛠️ *Панель администратора*", reply_markup=admin_keyboard(), parse_mode='MarkdownV2')
    else:
        await update.message.reply_text("⛔️ Доступ запрещен\\.", parse_mode='MarkdownV2')

def main():
    if not BOT_TOKEN or not ADMIN_ID or not DATABASE_URL:
        logging.critical("КРИТИЧЕСКАЯ ОШИБКА: BOT_TOKEN, ADMIN_ID, или DATABASE_URL не найдены. Проверьте переменные окружения и привязку базы данных.")
        return

    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    media_filters = filters.PHOTO | filters.VIDEO | filters.VOICE | filters.Document.ALL
    application.add_handler(MessageHandler(media_filters & ~filters.COMMAND, handle_media))

    application.run_polling()
    
if __name__ == "__main__":
    main()
