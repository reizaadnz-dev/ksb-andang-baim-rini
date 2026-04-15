"""
Telegram Relay Bot - Bot perantara untuk akun yang di-limit
Fitur Unik: Mood detector, Pesan terjadwal, Auto-translate, Statistik grafik ASCII,
            Anti-duplikat, Notif ulang tahun, Custom signature, Rekap harian otomatis
"""

import logging
import sqlite3
import time
import asyncio
import re
from datetime import datetime, timedelta
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ========================== KONFIGURASI ==========================
BOT_TOKEN = "8662884978:AAG_y234LzKnJCsamvm1KDoFj0cBhd_2w3c"
ADMIN_ID = 8736618159

# Rate limiting: DIMATIKAN TOTAL
RATE_LIMIT_ENABLED = False

# Auto-reply FAQ
FAQ = {
    "harga": "💰 Info harga bisa dilihat di profil saya ya!",
    "jadwal": "📅 Jadwal saya bisa berubah, silakan tanya langsung.",
    "kontak": "📞 Ini sudah kontak saya, silakan tinggalkan pesan!",
    "halo": "👋 Hai, btw uda makan blum? tinggalkan pesan disini.",
    "hi": "👋 Hi! Ada yang bisa dibantu?",
}

WELCOME_MESSAGE = (
    "👋 Halo! Kamu sedang menghubungi bot perantara.\n\n"
    "📨 Silakan kirim pesanmu, dan saya akan meneruskannya ke ej4 ganteng.\n"
    "⏳ Tunggu balasan dari si ej4 ya, btw muka kamu kok jerawatan"
)

OFFLINE_MESSAGE = "😴 Saya sedang tidak aktif saat ini. Pesanmu sudah diterima dan akan dibalas segera!"

# Signature custom untuk setiap balasan admin
ADMIN_SIGNATURE = "\n\n— ej4 🔥"

# Jam rekap harian otomatis dikirim ke admin (format 24 jam)
REKAP_JAM = 21  # jam 21:00

# Anti duplikat: abaikan pesan sama dalam X detik
ANTI_DUPLIKAT_DETIK = 10
# =================================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Storage sementara
last_messages = {}      # anti duplikat: {user_id: (teks, timestamp)}

# ========================== DATABASE ==========================
def init_db():
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            first_seen TEXT,
            last_seen TEXT,
            message_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            language TEXT DEFAULT 'id',
            birthday TEXT DEFAULT NULL,
            note TEXT DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            direction TEXT,
            content TEXT,
            timestamp TEXT,
            status TEXT DEFAULT 'pending',
            mood TEXT DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT,
            timestamp TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS scheduled (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id INTEGER,
            message TEXT,
            send_at TEXT,
            sent INTEGER DEFAULT 0
        );
    """)
    c.execute("INSERT OR IGNORE INTO settings VALUES ('bot_active', '1')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('mode', 'public')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('mood_detect', '1')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('signature', '1')")
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()

def upsert_user(user):
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("""
        INSERT INTO users (user_id, username, full_name, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name,
            last_seen=excluded.last_seen,
            message_count=message_count+1
    """, (user.id, user.username, user.full_name, now, now))
    conn.commit()
    conn.close()

def get_user_status(user_id):
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    c.execute("SELECT status FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "active"

def set_user_status(user_id, status):
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    c.execute("UPDATE users SET status=? WHERE user_id=?", (status, user_id))
    conn.commit()
    conn.close()

def set_user_note(user_id, note):
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    c.execute("UPDATE users SET note=? WHERE user_id=?", (note, user_id))
    conn.commit()
    conn.close()

def get_user_info(user_id):
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def log_message(user_id, direction, content, mood=None):
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (user_id, direction, content, timestamp, mood) VALUES (?,?,?,?,?)",
        (user_id, direction, content, datetime.now().isoformat(), mood)
    )
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE status='blocked'")
    blocked = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM messages WHERE direction='in'")
    total_msgs = c.fetchone()[0]
    today = datetime.now().date().isoformat()
    c.execute("SELECT COUNT(*) FROM messages WHERE direction='in' AND timestamp LIKE ?", (f"{today}%",))
    today_msgs = c.fetchone()[0]
    conn.close()
    return total_users, blocked, total_msgs, today_msgs

def get_all_users():
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    c.execute("SELECT user_id, username, full_name FROM users WHERE status='active'")
    rows = c.fetchall()
    conn.close()
    return rows

def get_weekly_stats():
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    result = []
    for i in range(6, -1, -1):
        day = (datetime.now() - timedelta(days=i)).date().isoformat()
        c.execute("SELECT COUNT(*) FROM messages WHERE direction='in' AND timestamp LIKE ?", (f"{day}%",))
        count = c.fetchone()[0]
        result.append((day[-5:], count))
    conn.close()
    return result

def get_mood_stats():
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    c.execute("SELECT mood, COUNT(*) FROM messages WHERE mood IS NOT NULL GROUP BY mood")
    rows = c.fetchall()
    conn.close()
    return dict(rows)

def export_log(user_id=None):
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    if user_id:
        c.execute("SELECT * FROM messages WHERE user_id=? ORDER BY timestamp", (user_id,))
    else:
        c.execute("SELECT * FROM messages ORDER BY timestamp")
    rows = c.fetchall()
    conn.close()
    return rows

def get_todays_birthdays():
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    today = datetime.now().strftime("%m-%d")
    c.execute("SELECT user_id, full_name FROM users WHERE birthday LIKE ?", (f"%-{today}",))
    rows = c.fetchall()
    conn.close()
    return rows

def add_scheduled(target_id, message, send_at):
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    c.execute("INSERT INTO scheduled (target_id, message, send_at) VALUES (?,?,?)",
              (target_id, message, send_at))
    conn.commit()
    conn.close()

def get_pending_scheduled():
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("SELECT id, target_id, message FROM scheduled WHERE send_at <= ? AND sent=0", (now,))
    rows = c.fetchall()
    conn.close()
    return rows

def mark_scheduled_sent(sid):
    conn = sqlite3.connect("relay_bot.db")
    c = conn.cursor()
    c.execute("UPDATE scheduled SET sent=1 WHERE id=?", (sid,))
    conn.commit()
    conn.close()

# ========================== MOOD DETECTOR ==========================
def detect_mood(text):
    text = text.lower()
    mood_map = {
        "😡 Marah":   ["marah", "kesal", "benci", "sialan", "anjing", "bangsat", "kampret", "nyebelin"],
        "😢 Sedih":   ["sedih", "nangis", "menangis", "galau", "patah hati", "kecewa", "down", "depresi"],
        "😄 Senang":  ["senang", "bahagia", "gembira", "asik", "seru", "mantap", "keren", "hore", "yeay"],
        "😰 Panik":   ["panik", "takut", "urgent", "darurat", "tolong", "bantuin", "help", "sos", "gawat"],
        "😍 Kagum":   ["wow", "keren banget", "amazing", "luar biasa", "kagum", "salut", "mantul"],
        "🤔 Bingung": ["bingung", "gimana", "caranya", "ga ngerti", "tidak mengerti", "confused"],
        "😴 Bosan":   ["bosan", "boring", "gabut", "ngantuk", "males", "ga ada kerjaan"],
    }
    for mood, keywords in mood_map.items():
        for kw in keywords:
            if kw in text:
                return mood
    return "😐 Netral"

# ========================== ASCII CHART ==========================
def make_ascii_chart(data):
    if not data:
        return "Tidak ada data."
    max_val = max(v for _, v in data) or 1
    lines = []
    for label, val in data:
        bar_len = int((val / max_val) * 15)
        bar = "█" * bar_len
        lines.append(f"{label} │{bar} {val}")
    return "\n".join(lines)

# ========================== HANDLERS ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == ADMIN_ID:
        await update.message.reply_text(
            "👑 *Selamat datang, Admin!*\n\n"
            "📋 *Perintah:*\n"
            "/panel — Panel utama\n"
            "/toggle — Aktif/nonaktifkan bot\n"
            "/broadcast <pesan> — Kirim ke semua user\n"
            "/stats — Statistik + grafik ASCII\n"
            "/moodstats — Statistik mood user\n"
            "/schedule <user\\_id> <menit> <pesan> — Jadwalkan pesan\n"
            "/note <user\\_id> <catatan> — Catatan untuk user\n"
            "/userinfo <user\\_id> — Info lengkap user\n"
            "/block <id> | /unblock <id> | /whitelist <id>\n"
            "/togglesignature — ON/OFF signature balasan\n"
            "/togglemood — ON/OFF mood detector\n"
            "/exportlog — Export semua log",
            parse_mode="Markdown"
        )
        return

    upsert_user(user)
    mode = get_setting("mode")
    if mode == "whitelist":
        status = get_user_status(user.id)
        if status != "whitelisted":
            await update.message.reply_text("⛔ Maaf, bot ini dalam mode privat.")
            return

    await update.message.reply_text(WELCOME_MESSAGE)

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    if user.id == ADMIN_ID:
        return

    status = get_user_status(user.id)
    if status == "blocked":
        await msg.reply_text("⛔ Kamu telah diblokir dari layanan ini.")
        return

    mode = get_setting("mode")
    if mode == "whitelist" and status != "whitelisted":
        await msg.reply_text("⛔ Bot ini dalam mode privat.")
        return

    upsert_user(user)
    text = msg.text or "[non-teks]"

    # ── Anti Duplikat ──
    last = last_messages.get(user.id)
    now_ts = time.time()
    if last and last[0] == text and (now_ts - last[1]) < ANTI_DUPLIKAT_DETIK:
        await msg.reply_text("⚠️ Pesan yang sama sudah terkirim barusan, tunggu sebentar ya.")
        return
    last_messages[user.id] = (text, now_ts)

    # ── Mood Detection ──
    mood = None
    if get_setting("mood_detect") == "1" and msg.text:
        mood = detect_mood(msg.text)

    log_message(user.id, "in", text, mood)

    # ── Cek FAQ ──
    if msg.text:
        for keyword, reply in FAQ.items():
            if keyword.lower() in msg.text.lower():
                await msg.reply_text(reply)
                break

    # ── Offline message ──
    bot_active = get_setting("bot_active") == "1"
    if not bot_active:
        await msg.reply_text(OFFLINE_MESSAGE)

    # ── Build header relay ──
    nama = user.full_name or "Tanpa Nama"
    username_str = f"@{user.username}" if user.username else "tidak ada username"
    user_info = get_user_info(user.id)
    note_str = f"\n📝 _{user_info[9]}_" if user_info and user_info[9] else ""
    mood_str = f"\n🎭 {mood}" if mood and mood != "😐 Netral" else ""
    msg_count = user_info[5] if user_info else "?"

    header = (
        f"📨 *Pesan baru dari:*\n"
        f"👤 {nama} ({username_str})\n"
        f"🆔 `{user.id}`\n"
        f"📊 Pesan ke-{msg_count}"
        f"{mood_str}"
        f"{note_str}\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"{'─'*28}\n"
    )

    keyboard = [
        [InlineKeyboardButton("💬 Balas", callback_data=f"reply_{user.id}"),
         InlineKeyboardButton("🚫 Blokir", callback_data=f"block_{user.id}")],
        [InlineKeyboardButton("✅ Selesai", callback_data=f"done_{user.id}"),
         InlineKeyboardButton("👤 Info", callback_data=f"info_{user.id}")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if msg.text:
            await context.bot.send_message(
                ADMIN_ID, header + msg.text,
                parse_mode="Markdown", reply_markup=reply_markup
            )
        elif msg.photo:
            await context.bot.send_photo(
                ADMIN_ID, msg.photo[-1].file_id,
                caption=header + (msg.caption or ""),
                parse_mode="Markdown", reply_markup=reply_markup
            )
        elif msg.document:
            await context.bot.send_document(
                ADMIN_ID, msg.document.file_id,
                caption=header + (msg.caption or ""),
                parse_mode="Markdown", reply_markup=reply_markup
            )
        elif msg.voice:
            await context.bot.send_voice(
                ADMIN_ID, msg.voice.file_id,
                caption=header, parse_mode="Markdown", reply_markup=reply_markup
            )
        elif msg.video:
            await context.bot.send_video(
                ADMIN_ID, msg.video.file_id,
                caption=header + (msg.caption or ""),
                parse_mode="Markdown", reply_markup=reply_markup
            )
        elif msg.sticker:
            await context.bot.send_sticker(ADMIN_ID, msg.sticker.file_id)
            await context.bot.send_message(
                ADMIN_ID, header + "🎭 [Stiker]",
                parse_mode="Markdown", reply_markup=reply_markup
            )
        elif msg.location:
            await context.bot.send_location(ADMIN_ID, msg.location.latitude, msg.location.longitude)
            await context.bot.send_message(
                ADMIN_ID, header + "📍 [Lokasi]",
                parse_mode="Markdown", reply_markup=reply_markup
            )
        elif msg.contact:
            await context.bot.send_contact(
                ADMIN_ID, msg.contact.phone_number, msg.contact.first_name
            )
            await context.bot.send_message(
                ADMIN_ID, header + "📱 [Kontak]",
                parse_mode="Markdown", reply_markup=reply_markup
            )
    except Exception as e:
        logger.error(f"Gagal relay: {e}")

async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        return

    msg = update.message
    if not msg.reply_to_message:
        return

    original_text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
    target_id = None

    for line in original_text.split("\n"):
        if "🆔" in line:
            try:
                target_id = int(re.sub(r"[^\d]", "", line))
            except:
                pass

    if not target_id:
        target_id = context.user_data.get("reply_to")

    if not target_id:
        await msg.reply_text("⚠️ Tidak bisa menemukan ID user tujuan.")
        return

    signature = ADMIN_SIGNATURE if get_setting("signature") == "1" else ""

    try:
        if msg.text:
            await context.bot.send_message(
                target_id,
                f"📩 *Balasan:*\n{msg.text}{signature}",
                parse_mode="Markdown"
            )
            log_message(target_id, "out", msg.text)
        elif msg.photo:
            await context.bot.send_photo(target_id, msg.photo[-1].file_id, caption=(msg.caption or "") + signature)
        elif msg.document:
            await context.bot.send_document(target_id, msg.document.file_id, caption=(msg.caption or "") + signature)
        elif msg.voice:
            await context.bot.send_voice(target_id, msg.voice.file_id)
        elif msg.video:
            await context.bot.send_video(target_id, msg.video.file_id, caption=(msg.caption or "") + signature)
        elif msg.sticker:
            await context.bot.send_sticker(target_id, msg.sticker.file_id)
        await msg.reply_text("✅ Pesan terkirim!")
    except Exception as e:
        await msg.reply_text(f"❌ Gagal kirim: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != ADMIN_ID:
        return

    data = query.data
    action, user_id_str = data.rsplit("_", 1)
    user_id = int(user_id_str)

    if action == "reply":
        context.user_data["reply_to"] = user_id
        await query.message.reply_text(
            f"💬 Mode balas ke `{user_id}` aktif.\nReply pesan ini untuk kirim.",
            parse_mode="Markdown"
        )
    elif action == "block":
        set_user_status(user_id, "blocked")
        await query.message.reply_text(f"🚫 User `{user_id}` diblokir.", parse_mode="Markdown")
        try:
            await context.bot.send_message(user_id, "⛔ Kamu telah diblokir dari layanan ini.")
        except:
            pass
    elif action == "done":
        await query.message.reply_text(f"✅ Percakapan dengan `{user_id}` ditandai selesai.", parse_mode="Markdown")
    elif action == "info":
        info = get_user_info(user_id)
        if not info:
            await query.message.reply_text("User tidak ditemukan.")
            return
        uid, uname, fname, first_seen, last_seen, msg_count, status, lang, bday, note = info
        await query.message.reply_text(
            f"👤 *Info User*\n\n"
            f"Nama: {fname or '-'}\n"
            f"Username: @{uname or '-'}\n"
            f"ID: `{uid}`\n"
            f"Status: {status}\n"
            f"Total pesan: {msg_count}\n"
            f"Pertama chat: {(first_seen or '')[:10]}\n"
            f"Terakhir chat: {(last_seen or '')[:10]}\n"
            f"Ulang tahun: {bday or '-'}\n"
            f"Catatan: {note or '-'}",
            parse_mode="Markdown"
        )

# ========================== ADMIN COMMANDS ==========================
async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    bot_active = get_setting("bot_active") == "1"
    mode = get_setting("mode")
    mood_on = get_setting("mood_detect") == "1"
    sig_on = get_setting("signature") == "1"
    total_users, blocked, total_msgs, today_msgs = get_stats()

    keyboard = [
        [InlineKeyboardButton("🔁 Toggle Bot", callback_data="admin_toggle"),
         InlineKeyboardButton("🔒 Toggle Mode", callback_data="admin_mode")],
        [InlineKeyboardButton("📊 Statistik", callback_data="admin_stats"),
         InlineKeyboardButton("📋 List User", callback_data="admin_users")],
        [InlineKeyboardButton("🎭 Toggle Mood", callback_data="admin_mood"),
         InlineKeyboardButton("✍️ Toggle Signature", callback_data="admin_sig")],
    ]
    await update.message.reply_text(
        f"⚙️ *Panel Admin*\n\n"
        f"{'🟢' if bot_active else '🔴'} Bot: {'Aktif' if bot_active else 'Nonaktif'}\n"
        f"{'🌐' if mode == 'public' else '🔒'} Mode: {mode.capitalize()}\n"
        f"🎭 Mood Detect: {'ON' if mood_on else 'OFF'}\n"
        f"✍️ Signature: {'ON' if sig_on else 'OFF'}\n\n"
        f"👥 Total user: {total_users} | 🚫 Diblokir: {blocked}\n"
        f"📨 Total pesan: {total_msgs} | 📅 Hari ini: {today_msgs}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        return

    data = query.data
    if data == "admin_toggle":
        val = "0" if get_setting("bot_active") == "1" else "1"
        set_setting("bot_active", val)
        await query.message.reply_text(f"Bot: {'🟢 Aktif' if val == '1' else '🔴 Nonaktif'}")
    elif data == "admin_mode":
        new = "whitelist" if get_setting("mode") == "public" else "public"
        set_setting("mode", new)
        await query.message.reply_text(f"Mode: {'🔒 Whitelist' if new == 'whitelist' else '🌐 Publik'}")
    elif data == "admin_mood":
        val = "0" if get_setting("mood_detect") == "1" else "1"
        set_setting("mood_detect", val)
        await query.message.reply_text(f"Mood Detect: {'ON 🎭' if val == '1' else 'OFF'}")
    elif data == "admin_sig":
        val = "0" if get_setting("signature") == "1" else "1"
        set_setting("signature", val)
        await query.message.reply_text(f"Signature: {'ON ✍️' if val == '1' else 'OFF'}")
    elif data == "admin_stats":
        weekly = get_weekly_stats()
        chart = make_ascii_chart(weekly)
        total_users, blocked, total_msgs, today_msgs = get_stats()
        await query.message.reply_text(
            f"📊 *Statistik 7 Hari Terakhir*\n\n"
            f"`{chart}`\n\n"
            f"👥 Total user: {total_users} | 🚫 Diblokir: {blocked}\n"
            f"📨 Total pesan: {total_msgs} | 📅 Hari ini: {today_msgs}",
            parse_mode="Markdown"
        )
    elif data == "admin_users":
        users = get_all_users()
        if not users:
            await query.message.reply_text("Belum ada user.")
            return
        text = "📋 *Daftar User Aktif:*\n\n"
        for uid, uname, fname in users[:20]:
            text += f"• {fname or '-'} (@{uname or '-'}) — `{uid}`\n"
        await query.message.reply_text(text, parse_mode="Markdown")

async def toggle_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    val = "0" if get_setting("bot_active") == "1" else "1"
    set_setting("bot_active", val)
    await update.message.reply_text(f"Bot: {'🟢 Aktif' if val == '1' else '🔴 Nonaktif'}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    weekly = get_weekly_stats()
    chart = make_ascii_chart(weekly)
    total_users, blocked, total_msgs, today_msgs = get_stats()
    await update.message.reply_text(
        f"📊 *Statistik 7 Hari Terakhir*\n\n"
        f"`{chart}`\n\n"
        f"👥 Total user: {total_users} | 🚫 Diblokir: {blocked}\n"
        f"📨 Total pesan: {total_msgs} | 📅 Hari ini: {today_msgs}",
        parse_mode="Markdown"
    )

async def mood_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    data = get_mood_stats()
    if not data:
        await update.message.reply_text("Belum ada data mood.")
        return
    chart = make_ascii_chart(list(data.items()))
    await update.message.reply_text(
        f"🎭 *Statistik Mood User*\n\n`{chart}`",
        parse_mode="Markdown"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Gunakan: /broadcast <pesan>")
        return
    text = " ".join(context.args)
    users = get_all_users()
    success = 0
    for uid, _, _ in users:
        try:
            await context.bot.send_message(uid, f"📢 *Broadcast:*\n{text}", parse_mode="Markdown")
            success += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await update.message.reply_text(f"✅ Broadcast terkirim ke {success}/{len(users)} user.")

async def block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Gunakan: /block <user_id>")
        return
    uid = int(context.args[0])
    set_user_status(uid, "blocked")
    await update.message.reply_text(f"🚫 User `{uid}` diblokir.", parse_mode="Markdown")

async def unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Gunakan: /unblock <user_id>")
        return
    uid = int(context.args[0])
    set_user_status(uid, "active")
    await update.message.reply_text(f"✅ User `{uid}` di-unblokir.", parse_mode="Markdown")

async def whitelist_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Gunakan: /whitelist <user_id>")
        return
    uid = int(context.args[0])
    set_user_status(uid, "whitelisted")
    await update.message.reply_text(f"✅ User `{uid}` dimasukkan whitelist.", parse_mode="Markdown")

async def note_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Gunakan: /note <user_id> <catatan>")
        return
    uid = int(context.args[0])
    note = " ".join(context.args[1:])
    set_user_note(uid, note)
    await update.message.reply_text(f"📝 Catatan untuk `{uid}` disimpan: _{note}_", parse_mode="Markdown")

async def user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Gunakan: /userinfo <user_id>")
        return
    uid = int(context.args[0])
    info = get_user_info(uid)
    if not info:
        await update.message.reply_text("User tidak ditemukan.")
        return
    uid_, uname, fname, first_seen, last_seen, msg_count, status, lang, bday, note = info
    await update.message.reply_text(
        f"👤 *Info User*\n\n"
        f"Nama: {fname or '-'}\n"
        f"Username: @{uname or '-'}\n"
        f"ID: `{uid_}`\n"
        f"Status: {status}\n"
        f"Total pesan: {msg_count}\n"
        f"Pertama: {(first_seen or '')[:10]}\n"
        f"Terakhir: {(last_seen or '')[:10]}\n"
        f"Ulang tahun: {bday or '-'}\n"
        f"Catatan: {note or '-'}",
        parse_mode="Markdown"
    )

async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 3:
        await update.message.reply_text("Gunakan: /schedule <user_id> <menit> <pesan>")
        return
    try:
        target = int(context.args[0])
        menit = int(context.args[1])
        pesan = " ".join(context.args[2:])
        send_at = (datetime.now() + timedelta(minutes=menit)).isoformat()
        add_scheduled(target, pesan, send_at)
        await update.message.reply_text(
            f"⏰ Pesan dijadwalkan ke `{target}` dalam *{menit} menit*.\n📝 _{pesan}_",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def toggle_signature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    val = "0" if get_setting("signature") == "1" else "1"
    set_setting("signature", val)
    await update.message.reply_text(f"Signature: {'ON ✍️' if val == '1' else 'OFF'}")

async def toggle_mood(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    val = "0" if get_setting("mood_detect") == "1" else "1"
    set_setting("mood_detect", val)
    await update.message.reply_text(f"Mood Detect: {'ON 🎭' if val == '1' else 'OFF'}")

async def export_log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    rows = export_log()
    if not rows:
        await update.message.reply_text("Belum ada log.")
        return
    lines = ["ID | UserID | Arah | Mood | Pesan | Waktu"]
    for r in rows:
        lines.append(f"{r[0]} | {r[1]} | {r[2]} | {r[6] or '-'} | {str(r[3])[:50]} | {r[4]}")
    content = "\n".join(lines)
    with open("log_export.txt", "w", encoding="utf-8") as f:
        f.write(content)
    await update.message.reply_document(open("log_export.txt", "rb"), filename="log_export.txt")

# ========================== BACKGROUND JOBS ==========================
async def job_scheduled_messages(context: ContextTypes.DEFAULT_TYPE):
    rows = get_pending_scheduled()
    for sid, target_id, message in rows:
        try:
            await context.bot.send_message(target_id, f"⏰ *Pesan Terjadwal:*\n{message}", parse_mode="Markdown")
            mark_scheduled_sent(sid)
        except Exception as e:
            logger.error(f"Gagal kirim scheduled: {e}")

async def job_birthday_check(context: ContextTypes.DEFAULT_TYPE):
    birthdays = get_todays_birthdays()
    for uid, fname in birthdays:
        try:
            await context.bot.send_message(
                uid,
                f"🎂 *Selamat Ulang Tahun, {fname}!* 🎉\nSemoga harimu menyenangkan!",
                parse_mode="Markdown"
            )
            await context.bot.send_message(
                ADMIN_ID,
                f"🎂 Hari ini ulang tahun: *{fname}* (`{uid}`)",
                parse_mode="Markdown"
            )
        except:
            pass

async def job_daily_recap(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    if now.hour != REKAP_JAM:
        return
    total_users, blocked, total_msgs, today_msgs = get_stats()
    mood_data = get_mood_stats()
    mood_str = "\n".join([f"  {k}: {v}" for k, v in mood_data.items()]) or "  Tidak ada data"
    weekly = get_weekly_stats()
    chart = make_ascii_chart(weekly)
    await context.bot.send_message(
        ADMIN_ID,
        f"📋 *Rekap Harian — {now.strftime('%d/%m/%Y')}*\n\n"
        f"📅 Pesan hari ini: *{today_msgs}*\n"
        f"👥 Total user: {total_users}\n\n"
        f"🎭 Mood:\n{mood_str}\n\n"
        f"📊 Grafik 7 hari:\n`{chart}`",
        parse_mode="Markdown"
    )

# ========================== MAIN ==========================
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("panel", panel))
    app.add_handler(CommandHandler("toggle", toggle_bot))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("moodstats", mood_stats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("block", block_user))
    app.add_handler(CommandHandler("unblock", unblock_user))
    app.add_handler(CommandHandler("whitelist", whitelist_user))
    app.add_handler(CommandHandler("note", note_user))
    app.add_handler(CommandHandler("userinfo", user_info))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler("togglesignature", toggle_signature))
    app.add_handler(CommandHandler("togglemood", toggle_mood))
    app.add_handler(CommandHandler("exportlog", export_log_cmd))

    app.add_handler(CallbackQueryHandler(button_callback, pattern=r"^(reply|block|done|info)_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin_"))

    app.add_handler(MessageHandler(
        filters.User(ADMIN_ID) & filters.REPLY,
        handle_admin_reply
    ))
    app.add_handler(MessageHandler(~filters.User(ADMIN_ID) & ~filters.COMMAND, handle_user_message))

    # Background jobs
    job_queue = app.job_queue
    job_queue.run_repeating(job_scheduled_messages, interval=30, first=10)
    job_queue.run_repeating(job_birthday_check, interval=3600, first=10)
    job_queue.run_repeating(job_daily_recap, interval=3600, first=10)

    logger.info("✅ Bot kontol!")
    app.run_polling()

if __name__ == "__main__":
    main()
