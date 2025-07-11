from flask import Flask, request, redirect
from threading import Thread
import sqlite3
import time

app = Flask(__name__)

@app.route('/')
def home():
    return "Я жив!"

@app.route('/redirect')
def redirect_to_video():
    user_id = request.args.get('uid')
    link = request.args.get('to')

    if not user_id or not link:
        return "❌ Неверные параметры", 400

    try:
        conn = sqlite3.connect("likes_bot.db")
        cur = conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO click_log (user_id, video_link, timestamp)
            VALUES (?, ?, ?)
        """, (int(user_id), link, time.time()))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Ошибка при логировании перехода:", e)

    return redirect(link)

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()
