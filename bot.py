import time
import sqlite3
import textwrap
import threading
from flask import Flask, request, redirect
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from config import TOKEN, ADMIN_ID
from keep_alive import keep_alive

# 🔧 Flask-сервер для отслеживания переходов по ссылке
web_app = Flask('')

@web_app.route('/click')
def track_click():
    user_id = request.args.get('user_id')
    video_link = request.args.get('video_link')

    if user_id and video_link:
        conn = sqlite3.connect("likes_bot.db")
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS click_log (
                user_id INTEGER,
                video_link TEXT,
                UNIQUE(user_id, video_link)
            )
        """)
        cur.execute("INSERT OR IGNORE INTO click_log (user_id, video_link) VALUES (?, ?)", (user_id, video_link))
        conn.commit()
        conn.close()

        # ✅ Перенаправляем пользователя на оригинальное видео
        return redirect(video_link)

    return "❌ Ошибка: нет данных"

@web_app.route('/')
def home():
    return "✅ Я жив!"

def run_web():
    web_app.run(host='0.0.0.0', port=8080)

def keep_alive():
    threading.Thread(target=run_web).start()

# Отправка длинного сообщения частями
def send_long_message(update, text, max_len=4000):
    parts = textwrap.wrap(text, width=max_len, break_long_words=False, break_on_hyphens=False)
    for part in parts:
        update.message.reply_text(part)

def init_db():
    conn = sqlite3.connect("likes_bot.db")
    cur = conn.cursor()

    # Создание таблицы пользователей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            likes_given INTEGER DEFAULT 0,
            likes_received INTEGER DEFAULT 0
        )
    """)

    # Добавляем колонку invited_by, если её нет
    try:
        cur.execute("ALTER TABLE users ADD COLUMN invited_by INTEGER")
    except sqlite3.OperationalError:
        pass  # колонка уже существует — игнорируем ошибку

    # ✅ Добавляем колонку забанен ли
    try:
        cur.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # ✅ Добавляем колонку предупреждений
    try:
        cur.execute("ALTER TABLE users ADD COLUMN warnings INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Таблица видео
    cur.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            link TEXT UNIQUE,
            timestamp REAL
        )
    """)

    # Таблица заданий — с прогрессом task_done
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            user_id INTEGER PRIMARY KEY,
            links TEXT,
            task_time REAL,
            task_done INTEGER DEFAULT 0
        )
    """)

    # Таблица логов лайков — чтобы не повторялись видео
    cur.execute("""
        CREATE TABLE IF NOT EXISTS likes_log (
            user_id INTEGER,
            video_link TEXT,
            PRIMARY KEY (user_id, video_link)
        )
    """)

    # Таблица логов уведомлений — чтобы не спамить уведомлениями
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notify_log (
            user_id INTEGER PRIMARY KEY,
            last_notify REAL
        )
    """)

    # Таблица логов кликов по ссылкам (перейдено ли по ссылке)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS click_log (
            user_id INTEGER,
            video_link TEXT,
            timestamp REAL,
            PRIMARY KEY (user_id, video_link)
        )
    """)

    conn.commit()
    conn.close()

    
def cleanup_old_videos():
    conn = sqlite3.connect("likes_bot.db")
    cur = conn.cursor()
    cutoff = time.time() - 86400  # 24 часа

    # Получаем видео, которые будут удалены
    cur.execute("""
        SELECT user_id, link FROM videos
        WHERE timestamp < ?
        AND link NOT IN (
            SELECT video_link FROM likes_log
        )
    """, (cutoff,))
    to_delete = cur.fetchall()

    # Удаляем их
    cur.execute("""
        DELETE FROM videos
        WHERE timestamp < ?
        AND link NOT IN (
            SELECT video_link FROM likes_log
        )
    """, (cutoff,))
    conn.commit()
    conn.close()

    # Отправляем уведомления
    for user_id, link in to_delete:
        try:
            from config import TOKEN
            from telegram import Bot
            bot = Bot(token=TOKEN)
            bot.send_message(
                chat_id=user_id,
                text="📌 Твоё видео было удалено из очереди, потому что оно не получило лайков за 24 часа. Попробуй загрузить его снова! 🔁"
            )
        except Exception as e:
            print(f"❌ Не удалось уведомить пользователя {user_id}: {e}")

def is_tiktok_link(text):
    return "tiktok.com" in text

def register_user(user_id, invited_by=None):
    conn = sqlite3.connect("likes_bot.db")
    cur = conn.cursor()
    if invited_by:
        cur.execute("INSERT OR IGNORE INTO users (user_id, likes_given, likes_received, invited_by) VALUES (?, 0, 0, ?)", (user_id, invited_by))
    else:
        cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def add_video(user_id, link, is_admin=False):
    conn = sqlite3.connect("likes_bot.db")
    cur = conn.cursor()

    # Проверяем, есть ли уже такое видео у пользователя
    cur.execute("SELECT link FROM videos WHERE user_id=? AND link=?", (user_id, link))
    if cur.fetchone():
        conn.close()
        return "⚠️ Это видео уже добавлено."

    # Ограничение для обычных пользователей: не более 1 видео
    if not is_admin:
        cur.execute("SELECT COUNT(*) FROM videos WHERE user_id=?", (user_id,))
        count = cur.fetchone()[0]
        if count >= 1:
            conn.close()
            return "❗ Вы можете добавить только 1 видео. Чтобы оно попало в очередь, выполните задания!"

    # Добавляем видео с текущим временем (timestamp)
    cur.execute(
        "INSERT INTO videos (user_id, link, timestamp) VALUES (?, ?, ?)",
        (user_id, link, time.time())
    )

    conn.commit()
    conn.close()

    return "✅ Видео добавлено! Чтобы оно попало в очередь, лайкните 3 видео."

def get_tasks(user_id):
    conn = sqlite3.connect("likes_bot.db")
    cur = conn.cursor()

    # Проверяем, есть ли активное задание
    cur.execute("SELECT links, task_done FROM tasks WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row:
        links, task_done = row
        conn.close()
        return links.split(","), task_done

    # Получаем ссылки, которые пользователь уже лайкал
    cur.execute("SELECT video_link FROM likes_log WHERE user_id=?", (user_id,))
    liked_links = set(row[0] for row in cur.fetchall())

    # Получаем ссылки других пользователей, которые он ещё не лайкал
    cur.execute("SELECT link FROM videos WHERE user_id != ?", (user_id,))
    all_links = [row[0] for row in cur.fetchall()]
    unique_links = [link for link in all_links if link not in liked_links]

    # Берём до 3 новых заданий
    links = unique_links[:3]

    if links:
        now = time.time()
        cur.execute("REPLACE INTO tasks (user_id, links, task_time, task_done) VALUES (?, ?, ?, 0)",
                    (user_id, ",".join(links), now))
        conn.commit()
    
    # Возвращаем ранее добавленное видео в очередь, если задание выполнено
    cur.execute("SELECT links, task_done FROM tasks WHERE user_id=? AND task_done=3", (user_id,))
    row = cur.fetchone()
    if row:
        link_to_restore, _ = row
        cur.execute("INSERT OR IGNORE INTO videos (user_id, link, timestamp) VALUES (?, ?, ?)",
                    (user_id, link_to_restore, time.time()))
        cur.execute("DELETE FROM tasks WHERE user_id=? AND task_done=3", (user_id,))
        conn.commit()
    
    conn.close()
    return links, 0

def confirm_likes(user_id):
    conn = sqlite3.connect("likes_bot.db")
    cur = conn.cursor()

    # Проверка: не забанен ли пользователь
    cur.execute("SELECT banned FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row and row[0] == 1:
        conn.close()
        return "⛔ Вы заблокированы за нечестное выполнение заданий."

    # Получаем задание
    cur.execute("SELECT links, task_time, task_done FROM tasks WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return "❌ Нет активных заданий."

    links, task_time, task_done = row
    link_list = links.split(",")

    # Безопасность: чтобы не выйти за пределы списка
    if task_done >= len(link_list):
        conn.close()
        return "✅ Все задания уже подтверждены."

    current_link = link_list[task_done]

    # Проверка таймера
    elapsed = time.time() - task_time
    if elapsed < 30:
        conn.close()
        return f"⏱ Пожалуйста, подожди минимум 30 секунд. Осталось {int(30 - elapsed)} сек."

    # Проверка перехода по ссылке
    cur.execute("SELECT 1 FROM click_log WHERE user_id=? AND video_link=?", (user_id, current_link))
    clicked = cur.fetchone()

    if not clicked:
        # Увеличиваем количество предупреждений
        cur.execute("SELECT warnings FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        warnings = row[0] if row else 0
        warnings += 1
        cur.execute("UPDATE users SET warnings=? WHERE user_id=?", (warnings, user_id))

        if warnings >= 3:
            cur.execute("UPDATE users SET banned=1 WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()

            # Сообщаем админу (если нужно)
            try:
                from config import ADMIN_ID
                context = CallbackContext.from_update(update)  # нужно передавать update извне или глобально
                context.bot.send_message(ADMIN_ID, f"🚫 Пользователь {user_id} заблокирован за 3 обмана.")
            except Exception as e:
                print(f"Ошибка отправки админу: {e}")

            return "⛔ Вы заблокированы за обман (3 из 3 предупреждений)."

        conn.commit()
        conn.close()
        return f"⚠️ Переход по ссылке не зафиксирован. Будь честным!\n\nПредупреждение {warnings} из 3."

    # Всё честно — засчитываем лайк
    cur.execute("INSERT OR IGNORE INTO likes_log (user_id, video_link) VALUES (?, ?)", (user_id, current_link))

    # Обновляем статистику
    cur.execute("SELECT user_id FROM videos WHERE link=?", (current_link,))
    owner_row = cur.fetchone()
    if owner_row:
        owner_id = owner_row[0]
        cur.execute("UPDATE users SET likes_received = likes_received + 1 WHERE user_id=?", (owner_id,))
        cur.execute("UPDATE users SET likes_given = likes_given + 1 WHERE user_id=?", (user_id,))

        cur.execute("SELECT COUNT(*) FROM likes_log WHERE video_link=?", (current_link,))
        total_likes = cur.fetchone()[0]

        if total_likes >= 3:
            cur.execute("DELETE FROM videos WHERE link=?", (current_link,))
            # Повторная постановка в очередь
            cur.execute("INSERT OR IGNORE INTO tasks (user_id, links, task_time, task_done) VALUES (?, ?, 0, 3)", (owner_id, current_link))

    # Продвигаем пользователя по заданиям
    task_done += 1
    now = time.time()

    if task_done >= len(link_list):
        cur.execute("DELETE FROM tasks WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        return "✅ Все задания выполнены! Твоё видео снова в очереди на 3 лайка! 🎉"

    # Сохраняем прогресс
    cur.execute("UPDATE tasks SET task_done=?, task_time=? WHERE user_id=?", (task_done, now, user_id))
    conn.commit()
    conn.close()

    next_link = link_list[task_done]
    return (
        f"✅ Лайк засчитан!\n\n"
        f"🔗 Следующее видео:\n{next_link}\n\n"
        f"⏳ Жди 30 секунд и нажми ✅ Подтвердить лайки\n"
        f"📊 Прогресс: {task_done + 1} из {len(link_list)}"
    )

def get_top(limit=5):
    conn = sqlite3.connect("likes_bot.db")
    cur = conn.cursor()
    cur.execute("SELECT user_id, likes_given, likes_received FROM users ORDER BY likes_given DESC LIMIT ?", (limit,))
    top = cur.fetchall()
    conn.close()
    return top

def start(update: Update, context: CallbackContext):
    user = update.effective_user
    args = context.args
    invited_by = int(args[0]) if args and args[0].isdigit() and int(args[0]) != user.id else None
    register_user(user.id, invited_by)

    keyboard = [
        [KeyboardButton("🔗 Добавить видео")],
        [KeyboardButton("📋 Получить задания"), KeyboardButton("✅ Подтвердить лайки")],
        [KeyboardButton("📊 Топ участников"), KeyboardButton("📜 Правила")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="✋🏻 Добро пожаловать! Используй кнопки ниже 👇",
        reply_markup=reply_markup
    )

def handle_invate(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    register_user(user_id)
    referral_link = f"https://t.me/{context.bot.username}?start={user_id}"
    update.message.reply_text(
        f"👥 Пригласи друга и получи бонус!\n\n"
        f"Вот твоя ссылка:\n{referral_link}\n\n"
        f"Как только друг выполнит своё первое задание, твоё видео можно будет добавить без лайков!"
    )

def handle_message(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    register_user(user_id)

    # 🔓 Разблокировка
    if text.startswith("/unblock") and user_id == ADMIN_ID:
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            unblock_id = int(parts[1])
            conn = sqlite3.connect("likes_bot.db")
            cur = conn.cursor()
            cur.execute("UPDATE users SET is_blocked=0, warnings=0 WHERE user_id=?", (unblock_id,))
            conn.commit()
            conn.close()
            update.message.reply_text(f"✅ Пользователь {unblock_id} разблокирован и предупреждения сброшены.")
        else:
            update.message.reply_text("❌ Используй команду так: /unblock ID")
        return

    # 🚫 Список заблокированных
    if text == "/banned" and user_id == ADMIN_ID:
        conn = sqlite3.connect("likes_bot.db")
        cur = conn.cursor()
        cur.execute("SELECT user_id, warnings FROM users WHERE is_blocked=1")
        banned = cur.fetchall()
        conn.close()
        if not banned:
            update.message.reply_text("✅ Нет заблокированных пользователей.")
        else:
            msg = "<b>🚫 Заблокированные пользователи:</b>\n\n"
            for uid, w in banned:
                msg += f"🔒 ID <code>{uid}</code> — {w} предупреждений\n"
            update.message.reply_text(msg, parse_mode="HTML")
        return

    if text == "/admin_stats" and user_id == ADMIN_ID:
        conn = sqlite3.connect("likes_bot.db")
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM videos")
        total_videos = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tasks")
        active_tasks = cur.fetchone()[0]
        cur.execute("SELECT user_id, likes_given, likes_received FROM users ORDER BY likes_given DESC LIMIT 20")
        top = cur.fetchall()
        cur.execute("SELECT invited_by, COUNT(*) FROM users WHERE invited_by IS NOT NULL GROUP BY invited_by ORDER BY COUNT(*) DESC")
        invites = cur.fetchall()
        msg = f"""📊 <b>Статистика бота:</b>

👥 Пользователей: <b>{total_users}</b>
🎞 Видео в очереди: <b>{total_videos}</b>
🧩 Активных заданий: <b>{active_tasks}</b>

🏆 <b>Топ 20 участников:</b>
"""
        for i, (uid, given, received) in enumerate(top, start=1):
            msg += f"{i}. ID <code>{uid}</code> — 👍🏻 {given} / ❤️ {received}\n"
        msg += "\n👥 <b>Реферальный отчёт:</b>\n"
        for inviter, count in invites:
            msg += f"ID <code>{inviter}</code> пригласил {count} человек(а)\n"
        conn.close()
        update.message.reply_text(msg, parse_mode="HTML")
        return

    if text == "/invites" and user_id == ADMIN_ID:
        conn = sqlite3.connect("likes_bot.db")
        cur = conn.cursor()
        cur.execute("SELECT invited_by, COUNT(*) FROM users WHERE invited_by IS NOT NULL GROUP BY invited_by ORDER BY COUNT(*) DESC")
        results = cur.fetchall()
        conn.close()
        if not results:
            update.message.reply_text("📭 Пока нет приглашённых пользователей.")
            return
        msg = "<b>👥 Приглашения:</b>\n\n"
        for inviter_id, count in results:
            msg += f"🔸 ID <code>{inviter_id}</code> — пригласил <b>{count}</b> пользователей\n"
        update.message.reply_text(msg, parse_mode="HTML")
        return

    if text == "/video" and user_id == ADMIN_ID:
        conn = sqlite3.connect("likes_bot.db")
        cur = conn.cursor()
        cur.execute("SELECT id, link FROM videos")
        videos = cur.fetchall()
        conn.close()
        if not videos:
            update.message.reply_text("Очередь пуста.")
        else:
            msg = "\n".join([f"{v[0]}. {v[1]}" for v in videos])
            msg += "\n\nЧтобы удалить видео, напиши /delete ID"
            send_long_message(update, msg)
        return

    if text.startswith("/delete") and user_id == ADMIN_ID:
        parts = text.split()
        if len(parts) < 2:
            update.message.reply_text("❌ Используй: /delete ID [ID2 ID3...]")
            return

        conn = sqlite3.connect("likes_bot.db")
        cur = conn.cursor()
        deleted = []

        for pid in parts[1:]:
            if pid.isdigit():
                video_id = int(pid)
                cur.execute("DELETE FROM videos WHERE id=?", (video_id,))
                if cur.rowcount > 0:
                    deleted.append(video_id)

        conn.commit()
        conn.close()

        if deleted:
            update.message.reply_text(f"🗑 Удалены видео с ID: {', '.join(map(str, deleted))}")
        else:
            update.message.reply_text("❌ Ничего не удалено (возможно, ID не найдены)")
        return

    if text == "/test_notify" and user_id == ADMIN_ID:
        context.bot.send_message(chat_id=user_id, text="🔔 Это тестовое уведомление о новом задании.")
        return

    if text == "/invite":
        update.message.reply_text(f"🎁 Пригласи друга и получи 1 бесплатную загрузку видео!\nТвоя ссылка:\nhttps://t.me/{context.bot.username}?start={user_id}")
        return

    if text == "📜 Правила":
        update.message.reply_text(
            "📋 <b>Правила:</b>\n\n"
            "1. Отправь ссылку на своё видео с TikTok 🔗\n"
            "2. Получи 3 задания с чужими видео 📋\n"
            "3. Поставь на каждое видео ❤️ и ПОСМОТРИ каждое видео не менее 30 секунд\n"
            "4. Подтверди, что лайкнул! ВНИМАНИЕ‼️ Бот следит за нечестным выполнением заданий и блокирует пользователя в случае обнаружения нарушений ПЕРМАНЕНТНО. Будь ЧЕСТНЫМ ✅\n"
            "5. После подтверждения твоё видео добавится в очередь\n\n"
            "6. Оно будет удалено через 24 часа\n\n"
            "7. Как только оно получит 3 лайка, оно вновь пропадает из очереди и чтобы его вернуть обратно, нужно выполнить еще 3 задания\n\n"
            "8. Сколько поставил лайков ты, столько же получаешь в ответ! Все честно!🤗\n\n"
            "🎁 <b>Хочешь пропустить задания?</b>\nПригласи друга — и твоё видео сразу попадёт в очередь!\nКоманда: /invite\n\n"
            "📊 Статистика по кнопке 📊\n👮 Нечестные пользователи блокируются\n❓ Вопросы и разбан — @mihei_1985",
            parse_mode="HTML"
        )
        return

    if text == "🔗 Добавить видео":
        update.message.reply_text("🔗 Пришли ссылку на TikTok-видео")
        return

    if is_tiktok_link(text):
        is_admin = user_id == ADMIN_ID
        result = add_video(user_id, text, is_admin=is_admin)
        update.message.reply_text(result)
        return

    if text == "📋 Получить задания":
        tasks, done = get_tasks(user_id)

        if not tasks or done >= len(tasks):
            update.message.reply_text("📭 Нет доступных заданий. Попробуй позже.")
            return

        current_link = tasks[done]

        # ✅ Оборачиваем ссылку через редирект-трекер
        wrapped_link = f"https://cbf57f61-4dc7-450e-9033-9707613b8d28-00-26gc7501m05x3.spock.replit.dev/click?user_id={user_id}&video_link={current_link}"

        # 🔧 Сохраняем время начала задания
        conn = sqlite3.connect("likes_bot.db")
        cur = conn.cursor()
        cur.execute("UPDATE tasks SET task_time=? WHERE user_id=?", (time.time(), user_id))
        conn.commit()
        conn.close()

        update.message.reply_text(
            f"👍 Поставь лайк этому видео:\n\n"
            f"🔗 <a href=\"{wrapped_link}\">Нажми здесь, чтобы открыть видео</a>\n\n"
            f"📱 Если открылось в браузере — нажми ⋮ и выбери <b>“Открыть в приложении TikTok”</b>\n\n"
            f"⏳ Жди минимум 30 секунд, затем нажми <b>✅ Подтвердить лайки</b>\n"
            f"📊 Прогресс: {done + 1} из {len(tasks)}",
        parse_mode="HTML",
        disable_web_page_preview=True
        )
        return

    if text == "✅ Подтвердить лайки":
        result = confirm_likes(user_id)
        update.message.reply_text(result)
        return

    if text == "📊 Топ участников":
        top = get_top(limit=20)
        if not top:
            update.message.reply_text("😢 Пока нет участников.")
        else:
            msg = "🏆 <b>Топ участников:</b>\n\n"
            for i, (uid, given, received) in enumerate(top, start=1):
                msg += f"{i}. ID {uid} — 👍🏻 {given} / ❤️ {received}\n"
            update.message.reply_text(msg, parse_mode="HTML")
        return

    update.message.reply_text("❓ Неизвестная команда. Нажми /start или выбери кнопку ниже.")

def unblock_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ У вас нет прав для этой команды.")
        return

    if not context.args:
        update.message.reply_text("❗ Укажите ID пользователя. Пример:\n/unblock 123456789")
        return

    try:
        unblock_id = int(context.args[0])
    except ValueError:
        update.message.reply_text("❗ Неверный формат ID.")
        return

    conn = sqlite3.connect("likes_bot.db")
    cur = conn.cursor()
    cur.execute("UPDATE users SET banned=0, warnings=0 WHERE user_id=?", (unblock_id,))
    conn.commit()
    conn.close()

    update.message.reply_text(f"✅ Пользователь {unblock_id} разблокирован.")

def banned_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ У вас нет прав для этой команды.")
        return

    conn = sqlite3.connect("likes_bot.db")
    cur = conn.cursor()
    cur.execute("SELECT user_id, warnings FROM users WHERE banned=1")  # убрал is_blocked
    banned = cur.fetchall()
    conn.close()

    if not banned:
        update.message.reply_text("✅ Нет заблокированных пользователей.")
    else:
        msg = "<b>🚫 Заблокированные пользователи:</b>\n\n"
        for uid, w in banned:
            msg += f"🔒 ID <code>{uid}</code> — {w} предупреждений\n"
        update.message.reply_text(msg, parse_mode="HTML")

def main():
    keep_alive()
    init_db()

    # 🔄 Запускаем автоудаление старых видео каждые 10 минут
    def run_cleanup():
        while True:
            cleanup_old_videos()
            time.sleep(600)  # 600 секунд = 10 минут

    threading.Thread(target=run_cleanup, daemon=True).start()

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Запускаем фоновое уведомление о заданиях
    threading.Thread(target=auto_notify_new_tasks, args=(updater.bot,), daemon=True).start()

    # Обработчики команд и сообщений
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("invite", handle_invate))
    dp.add_handler(CommandHandler("video", handle_message))
    dp.add_handler(CommandHandler("admin_stats", handle_message))
    dp.add_handler(CommandHandler("delete", handle_message))
    dp.add_handler(CommandHandler("test_notify", handle_message))
    dp.add_handler(CommandHandler("invites", handle_message))
    dp.add_handler(CommandHandler("unblock", unblock_command))
    dp.add_handler(CommandHandler("banned", banned_command))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    # Команды для обычных пользователей
    updater.bot.set_my_commands([
        BotCommand("start", "Начать работу"),
        BotCommand("invite", "👥 Пригласи друга и получи бонус 🎁"),
    ], scope=BotCommandScopeDefault())

    # Команды для админа
    updater.bot.set_my_commands([
        BotCommand("start", "🚀 Начать работу"),
        BotCommand("admin_stats", "📊 Статистика"),
        BotCommand("video", "📼 Список видео"),
        BotCommand("delete", "🗑 Удалить видео"),
        BotCommand("test_notify", "🔔 Тест уведомление"),
        BotCommand("invite", "👥 Пригласи друга и получи бонус 🎁"),
        BotCommand("invites", "👥 Приглашения пользователей"),
        BotCommand("unblock", "🔓 Разблокировать пользователя"),
        BotCommand("banned", "🚫 Список заблокированных"),
    ], scope=BotCommandScopeChat(chat_id=ADMIN_ID))

    updater.start_polling()
    updater.idle()


def auto_notify_new_tasks(bot):
    import time
    import sqlite3

    notify_interval = 3600  # 1 час в секундах

    print("Авто-уведомления запущены")
    while True:
        time.sleep(60)  # раз в минуту

        conn = sqlite3.connect("likes_bot.db")
        cur = conn.cursor()

        # Получаем всех пользователей
        cur.execute("SELECT user_id FROM users")
        users = [row[0] for row in cur.fetchall()]
        print(f"Пользователей для проверки: {len(users)}")

        now = time.time()

        for user_id in users:
            # Проверяем, есть ли активное задание
            cur.execute("SELECT links FROM tasks WHERE user_id=?", (user_id,))
            row = cur.fetchone()
            if row:
                print(f"Пользователь {user_id} уже имеет активное задание, пропускаем.")
                continue

            # Проверяем когда последний раз отправляли уведомление
            cur.execute("SELECT last_notify FROM notify_log WHERE user_id=?", (user_id,))
            last_notify_row = cur.fetchone()
            if last_notify_row:
                last_notify = last_notify_row[0]
                if now - last_notify < notify_interval:
                    print(f"Пользователь {user_id} получил уведомление недавно, пропускаем.")
                    continue

            # Получаем ссылки видео, которые не принадлежат пользователю
            cur.execute("SELECT link FROM videos WHERE user_id != ?", (user_id,))
            all_links = set(row[0] for row in cur.fetchall())

            # Получаем ссылки видео, которые пользователь уже лайкал
            cur.execute("SELECT video_link FROM likes_log WHERE user_id=?", (user_id,))
            liked_links = set(row[0] for row in cur.fetchall())

            # Фильтруем ссылки
            available_links = all_links - liked_links

            print(f"Пользователь {user_id}: доступно видео для заданий - {len(available_links)}")

            if len(available_links) >= 3:
                try:
                    bot.send_message(chat_id=user_id, text="📢 Доступно новое задание! Нажми 📋 Получить задания")
                    print(f"Уведомление отправлено пользователю {user_id}")
                    # Обновляем время последнего уведомления
                    cur.execute("""
                        INSERT INTO notify_log (user_id, last_notify)
                        VALUES (?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET last_notify=excluded.last_notify
                    """, (user_id, now))
                    conn.commit()
                except Exception as e:
                    print(f"Ошибка при отправке уведомления {user_id}: {e}")

        conn.close()
        
if __name__ == '__main__':
    main()

