import os
import sqlite3
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler,
)

TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])

# Codes to join — set these as Railway env vars too
WORKER_CODE  = os.environ.get("WORKER_CODE",  "WORKER123")
MANAGER_CODE = os.environ.get("MANAGER_CODE", "MANAGER456")

DB_PATH = os.environ.get("DB_PATH", "inventory.db")
os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur  = conn.cursor()

cur.executescript("""
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS inventory (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode  TEXT NOT NULL,
    row_name TEXT NOT NULL,
    position INTEGER NOT NULL,
    UNIQUE(barcode, row_name, position)
);

CREATE TABLE IF NOT EXISTS photos (
    barcode TEXT PRIMARY KEY,
    file_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    name  TEXT,
    role  TEXT NOT NULL DEFAULT 'worker'
);

CREATE TABLE IF NOT EXISTS search_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id   INTEGER NOT NULL,
    name    TEXT,
    query   TEXT NOT NULL,
    found   INTEGER NOT NULL DEFAULT 0,
    ts      TEXT DEFAULT (datetime('now','localtime'))
);
""")
conn.commit()

# ==========================
# STATES
# ==========================

S_JOIN   = 0   # waiting for join code
S_SEARCH = 1   # continuous search mode

# ==========================
# ROLE HELPERS
# ==========================

def get_role(tg_id):
    if tg_id == ADMIN_ID:
        return "admin"
    r = cur.execute("SELECT role FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    return r[0] if r else None

def is_registered(tg_id):
    return tg_id == ADMIN_ID or cur.execute(
        "SELECT 1 FROM users WHERE tg_id=?", (tg_id,)
    ).fetchone() is not None

def register(tg_id, name, role):
    cur.execute(
        "INSERT OR IGNORE INTO users (tg_id, name, role) VALUES (?,?,?)",
        (tg_id, name, role)
    )
    conn.commit()

# ==========================
# KEYBOARDS
# ==========================

def worker_menu():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🔍 Find item"), KeyboardButton("🛑 Stop search")]],
        resize_keyboard=True,
        is_persistent=True
    )

def manager_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔍 Find item"), KeyboardButton("🛑 Stop search")],
            [KeyboardButton("📋 Search log")],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def get_menu(tg_id):
    role = get_role(tg_id)
    if role in ("manager", "admin"):
        return manager_menu()
    return worker_menu()

# ==========================
# INVENTORY HELPERS
# ==========================

def get_next_position(row_name):
    cur.execute("SELECT COALESCE(MAX(position),0) FROM inventory WHERE row_name=?", (row_name,))
    return cur.fetchone()[0] + 1

def get_photo_file_id(barcode):
    r = cur.execute("SELECT file_id FROM photos WHERE barcode=?", (barcode,)).fetchone()
    return r[0] if r else None

async def send_card(msg, barcode, locs, tg_id):
    loc_lines = "\n".join(
        f"  📍 *{row_name}* — коробка *{pos}*" for row_name, pos in locs
    )
    text  = f"🔍 *{barcode}*\n\n{loc_lines}"
    photo = get_photo_file_id(barcode)
    kb    = get_menu(tg_id)
    if photo:
        await msg.reply_photo(photo=photo, caption=text, parse_mode="Markdown", reply_markup=kb)
    else:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)

def log_search(tg_id, name, query, found):
    cur.execute(
        "INSERT INTO search_log (tg_id, name, query, found) VALUES (?,?,?,?)",
        (tg_id, name, query, 1 if found else 0)
    )
    conn.commit()

# ==========================
# /start — registration gate
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id

    if tg_id == ADMIN_ID:
        register(tg_id, update.effective_user.full_name, "admin")
        await update.message.reply_text(
            "👑 *Admin access granted.*\n\nAll commands available.",
            parse_mode="Markdown",
            reply_markup=manager_menu()
        )
        return ConversationHandler.END

    if is_registered(tg_id):
        role = get_role(tg_id)
        await update.message.reply_text(
            f"👋 Welcome back! You are registered as *{role}*.",
            parse_mode="Markdown",
            reply_markup=get_menu(tg_id)
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👟 *Puma Depot Bot*\n\n"
        "To get access, enter the code your manager gave you:",
        parse_mode="Markdown"
    )
    return S_JOIN

async def join_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    name  = update.effective_user.full_name
    code  = update.message.text.strip()

    if code == MANAGER_CODE:
        register(tg_id, name, "manager")
        await update.message.reply_text(
            f"✅ Welcome, *{name}*!\nYou are registered as *manager*.\n\n"
            "You can search items and view the search log.",
            parse_mode="Markdown",
            reply_markup=manager_menu()
        )
        return ConversationHandler.END

    if code == WORKER_CODE:
        register(tg_id, name, "worker")
        await update.message.reply_text(
            f"✅ Welcome, *{name}*!\nYou are registered as *worker*.\n\n"
            "Tap 🔍 Find item to search for products.",
            parse_mode="Markdown",
            reply_markup=worker_menu()
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "❌ Invalid code. Please try again or contact your manager."
    )
    return S_JOIN

# ==========================
# CONTINUOUS SEARCH
# ==========================

async def search_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not is_registered(tg_id):
        await update.message.reply_text("❌ Please send /start to register first.")
        return ConversationHandler.END
    context.user_data["searching"] = True
    await update.message.reply_text(
        "🔍 *Search mode ON*\n\nType any barcode or partial code.\n"
        "Tap 🛑 Stop search when you're done.",
        parse_mode="Markdown",
        reply_markup=get_menu(tg_id)
    )
    return S_SEARCH

async def search_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    name  = update.effective_user.full_name
    q     = update.message.text.strip()

    # Stop button
    if q == "🛑 Stop search":
        return await search_stop(update, context)

    cur.execute(
        "SELECT barcode, row_name, position FROM inventory "
        "WHERE barcode LIKE ? ORDER BY barcode, row_name, position",
        (f"%{q}%",)
    )
    rows = cur.fetchall()

    if not rows:
        log_search(tg_id, name, q, False)
        await update.message.reply_text(
            f"❌ Nothing found for `{q}`.\nKeep typing or tap 🛑 Stop search.",
            parse_mode="Markdown",
            reply_markup=get_menu(tg_id)
        )
        return S_SEARCH

    grouped = {}
    for barcode, row_name, position in rows:
        grouped.setdefault(barcode, []).append((row_name, position))

    log_search(tg_id, name, q, True)

    if len(grouped) == 1:
        barcode = list(grouped.keys())[0]
        await send_card(update.message, barcode, grouped[barcode], tg_id)
        return S_SEARCH  # stay in search mode

    buttons = [
        [InlineKeyboardButton(b, callback_data=f"show:{b}")]
        for b in grouped
    ]
    await update.message.reply_text(
        f"🔍 *{len(grouped)}* matches for `{q}` — tap one:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return S_SEARCH  # stay in search mode

async def search_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    context.user_data.pop("searching", None)
    await update.message.reply_text(
        "🛑 Search mode OFF.",
        reply_markup=get_menu(tg_id)
    )
    return ConversationHandler.END

async def cb_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    await q.answer()
    tg_id   = q.from_user.id
    barcode = q.data.split(":", 1)[1]
    cur.execute(
        "SELECT row_name, position FROM inventory WHERE barcode=? ORDER BY row_name, position",
        (barcode,)
    )
    locs = cur.fetchall()
    if not locs:
        await q.message.reply_text("❌ Not found.")
        return
    await send_card(q.message, barcode, locs, tg_id)

# ==========================
# SEARCH LOG (manager/admin)
# ==========================

async def search_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    role  = get_role(tg_id)
    if role not in ("manager", "admin"):
        await update.message.reply_text("❌ Manager access only.")
        return

    rows = cur.execute(
        "SELECT ts, name, query, found FROM search_log "
        "ORDER BY id DESC LIMIT 50"
    ).fetchall()

    if not rows:
        await update.message.reply_text("📭 No searches yet.", reply_markup=get_menu(tg_id))
        return

    text = "📋 *Last 50 searches:*\n\n"
    for ts, name, query, found in rows:
        icon  = "✅" if found else "❌"
        time  = ts[11:16] if ts else ""
        uname = (name or "?")[:15]
        text += f"`{time}` {icon} *{uname}* — `{query}`\n"

    # Split if too long for one message
    if len(text) > 4000:
        text = text[:4000] + "\n…"

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_menu(tg_id))

# ==========================
# /find command (also works outside search mode)
# ==========================

async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not is_registered(tg_id):
        await update.message.reply_text("❌ Please send /start to register first.")
        return
    if not context.args:
        await update.message.reply_text("❌ Usage: `/find BARCODE`", parse_mode="Markdown")
        return
    barcode = context.args[0]
    name    = update.effective_user.full_name
    cur.execute(
        "SELECT row_name, position FROM inventory WHERE barcode=? ORDER BY row_name, position",
        (barcode,)
    )
    locs = cur.fetchall()
    if not locs:
        log_search(tg_id, name, barcode, False)
        await update.message.reply_text(f"❌ `{barcode}` not found.", parse_mode="Markdown")
        return
    log_search(tg_id, name, barcode, True)
    await send_card(update.message, barcode, locs, tg_id)

# ==========================
# INVENTORY COMMANDS (admin/manager)
# ==========================

def require_admin_or_manager(role):
    return role in ("admin", "manager")

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    role  = get_role(tg_id)
    if not require_admin_or_manager(role):
        await update.message.reply_text("❌ Manager/admin only.")
        return
    if len(context.args) != 3:
        await update.message.reply_text("❌ Usage: `/add BARCODE ROW POSITION`", parse_mode="Markdown")
        return
    barcode, row_name = context.args[0], context.args[1]
    try:
        position = int(context.args[2])
    except ValueError:
        await update.message.reply_text("❌ Position must be a number.")
        return
    try:
        cur.execute(
            "INSERT INTO inventory (barcode, row_name, position) VALUES (?,?,?)",
            (barcode, row_name, position)
        )
        conn.commit()
        await update.message.reply_text(
            f"✅ Saved `{barcode}` → *{row_name}*, position {position}",
            parse_mode="Markdown"
        )
    except sqlite3.IntegrityError:
        await update.message.reply_text(
            f"⚠️ `{barcode}` already exists at *{row_name}*, position {position}.",
            parse_mode="Markdown"
        )

async def addrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not require_admin_or_manager(get_role(tg_id)):
        await update.message.reply_text("❌ Manager/admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage: `/addrow ROW item1 item2 ...`\nComma-separate for same position.",
            parse_mode="Markdown"
        )
        return
    row_name = context.args[0]
    count = skipped = 0
    for position, group in enumerate(context.args[1:], start=1):
        for barcode in [x.strip() for x in group.split(",") if x.strip()]:
            try:
                cur.execute(
                    "INSERT INTO inventory (barcode, row_name, position) VALUES (?,?,?)",
                    (barcode, row_name, position)
                )
                count += 1
            except sqlite3.IntegrityError:
                skipped += 1
    conn.commit()
    msg = f"✅ Added *{count}* barcode(s) to *{row_name}*."
    if skipped:
        msg += f" Skipped {skipped} duplicate(s)."
    await update.message.reply_text(msg, parse_mode="Markdown")

async def appendrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not require_admin_or_manager(get_role(tg_id)):
        await update.message.reply_text("❌ Manager/admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: `/appendrow ROW item1 item2 ...`", parse_mode="Markdown")
        return
    row_name = context.args[0]
    position = get_next_position(row_name)
    count = skipped = 0
    for group in context.args[1:]:
        for barcode in [x.strip() for x in group.split(",") if x.strip()]:
            try:
                cur.execute(
                    "INSERT INTO inventory (barcode, row_name, position) VALUES (?,?,?)",
                    (barcode, row_name, position)
                )
                count += 1
            except sqlite3.IntegrityError:
                skipped += 1
        position += 1
    conn.commit()
    msg = f"✅ Appended *{count}* barcode(s) to *{row_name}*."
    if skipped:
        msg += f" Skipped {skipped} duplicate(s)."
    await update.message.reply_text(msg, parse_mode="Markdown")

async def addphoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not require_admin_or_manager(get_role(tg_id)):
        await update.message.reply_text("❌ Manager/admin only.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("❌ Usage: `/addphoto BARCODE` then send the photo.", parse_mode="Markdown")
        return
    barcode = context.args[0]
    cur.execute("SELECT COUNT(*) FROM inventory WHERE barcode=?", (barcode,))
    if cur.fetchone()[0] == 0:
        await update.message.reply_text(f"❌ `{barcode}` not found.", parse_mode="Markdown")
        return
    context.user_data["pending_photo_barcode"] = barcode
    note = " _(replaces existing)_" if get_photo_file_id(barcode) else ""
    await update.message.reply_text(f"📷 Now send the photo for `{barcode}`{note}.", parse_mode="Markdown")

async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    barcode = context.user_data.get("pending_photo_barcode")
    if not barcode:
        return
    file_id = update.message.photo[-1].file_id
    cur.execute(
        "INSERT INTO photos (barcode, file_id) VALUES (?,?) "
        "ON CONFLICT(barcode) DO UPDATE SET file_id=excluded.file_id",
        (barcode, file_id)
    )
    conn.commit()
    context.user_data.pop("pending_photo_barcode", None)
    await update.message.reply_text(f"✅ Photo saved for `{barcode}`!", parse_mode="Markdown")

async def removephoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not require_admin_or_manager(get_role(tg_id)):
        await update.message.reply_text("❌ Manager/admin only.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("❌ Usage: `/removephoto BARCODE`", parse_mode="Markdown")
        return
    barcode = context.args[0]
    cur.execute("DELETE FROM photos WHERE barcode=?", (barcode,))
    conn.commit()
    if cur.rowcount == 0:
        await update.message.reply_text(f"⚠️ No photo for `{barcode}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"🗑️ Photo removed for `{barcode}`.", parse_mode="Markdown")

async def showrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("❌ Usage: `/showrow ROW_NAME`", parse_mode="Markdown")
        return
    row_name = context.args[0]
    cur.execute("SELECT position, barcode FROM inventory WHERE row_name=? ORDER BY position", (row_name,))
    results = cur.fetchall()
    if not results:
        await update.message.reply_text(f"❌ Row *{row_name}* not found.", parse_mode="Markdown")
        return
    positions = {}
    for position, barcode in results:
        positions.setdefault(position, []).append(barcode)
    text = f"📦 *{row_name}*:\n\n"
    for pos in sorted(positions):
        labeled = [f"{b} 📷" if get_photo_file_id(b) else b for b in positions[pos]]
        text += f"`{pos}.` {', '.join(labeled)}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def rows_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur.execute("SELECT row_name, COUNT(*) FROM inventory GROUP BY row_name ORDER BY row_name")
    results = cur.fetchall()
    if not results:
        await update.message.reply_text("📭 No rows yet.")
        return
    text = "📋 *All rows:*\n\n"
    for row_name, total in results:
        text += f"• *{row_name}* — {total} barcode(s)\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not require_admin_or_manager(get_role(tg_id)):
        await update.message.reply_text("❌ Manager/admin only.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("❌ Usage: `/rename OLD NEW`", parse_mode="Markdown")
        return
    old, new = context.args[0], context.args[1]
    cur.execute("SELECT COUNT(*) FROM inventory WHERE row_name=?", (old,))
    count = cur.fetchone()[0]
    if count == 0:
        await update.message.reply_text(f"❌ Row *{old}* not found.", parse_mode="Markdown")
        return
    cur.execute("UPDATE inventory SET row_name=? WHERE row_name=?", (new, old))
    conn.commit()
    await update.message.reply_text(f"✅ Renamed *{old}* → *{new}* ({count} items).", parse_mode="Markdown")

async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not require_admin_or_manager(get_role(tg_id)):
        await update.message.reply_text("❌ Manager/admin only.")
        return
    if len(context.args) != 3:
        await update.message.reply_text("❌ Usage: `/delete BARCODE ROW POSITION`", parse_mode="Markdown")
        return
    barcode, row_name = context.args[0], context.args[1]
    try:
        position = int(context.args[2])
    except ValueError:
        await update.message.reply_text("❌ Position must be a number.")
        return
    cur.execute(
        "DELETE FROM inventory WHERE barcode=? AND row_name=? AND position=?",
        (barcode, row_name, position)
    )
    conn.commit()
    if cur.rowcount == 0:
        await update.message.reply_text(f"❌ Not found.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"🗑️ Deleted `{barcode}` from *{row_name}*, pos {position}.", parse_mode="Markdown")

async def deleterow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not require_admin_or_manager(get_role(tg_id)):
        await update.message.reply_text("❌ Manager/admin only.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("❌ Usage: `/deleterow ROW_NAME`", parse_mode="Markdown")
        return
    row_name = context.args[0]
    cur.execute("SELECT COUNT(*) FROM inventory WHERE row_name=?", (row_name,))
    count = cur.fetchone()[0]
    if count == 0:
        await update.message.reply_text(f"❌ Row *{row_name}* not found.", parse_mode="Markdown")
        return
    cur.execute("DELETE FROM inventory WHERE row_name=?", (row_name,))
    conn.commit()
    await update.message.reply_text(f"🗑️ Deleted *{row_name}* ({count} items).", parse_mode="Markdown")

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_role(update.effective_user.id) != "admin":
        await update.message.reply_text("❌ Admin only.")
        return
    cur.execute("DELETE FROM inventory")
    cur.execute("DELETE FROM photos")
    conn.commit()
    await update.message.reply_text("⚠️ Database cleared.")

# ==========================
# WHO AM I
# ==========================

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    role  = get_role(tg_id) or "not registered"
    await update.message.reply_text(
        f"👤 You are: *{role}*\nID: `{tg_id}`",
        parse_mode="Markdown",
        reply_markup=get_menu(tg_id)
    )

# ==========================
# ERROR HANDLER
# ==========================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    print("ERROR:", "".join(traceback.format_exception(
        type(context.error), context.error, context.error.__traceback__)))
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Something went wrong. Try again.")
        except Exception:
            pass

# ==========================
# APP SETUP
# ==========================

app = Application.builder().token(TOKEN).build()

# Registration conversation
app.add_handler(ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        S_JOIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, join_code)],
    },
    fallbacks=[],
    per_user=True,
    per_chat=False,
    allow_reentry=True,
    name="register",
))

# Continuous search conversation
app.add_handler(ConversationHandler(
    entry_points=[
        MessageHandler(filters.Regex(r"^🔍 Find item$"), search_entry),
    ],
    states={
        S_SEARCH: [
            MessageHandler(filters.Regex(r"^🛑 Stop search$"), search_stop),
            MessageHandler(filters.TEXT & ~filters.COMMAND, search_run),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", search_stop),
        MessageHandler(filters.Regex(r"^🛑 Stop search$"), search_stop),
    ],
    per_user=True,
    per_chat=False,
    allow_reentry=True,
    name="search",
))

# Inline button taps
app.add_handler(CallbackQueryHandler(cb_show, pattern=r"^show:"))

# Manager search log button
app.add_handler(MessageHandler(filters.Regex(r"^📋 Search log$"), search_log))

# Commands
app.add_handler(CommandHandler("find",        find))
app.add_handler(CommandHandler("whoami",      whoami))
app.add_handler(CommandHandler("add",         add))
app.add_handler(CommandHandler("addrow",      addrow))
app.add_handler(CommandHandler("appendrow",   appendrow))
app.add_handler(CommandHandler("addphoto",    addphoto))
app.add_handler(CommandHandler("removephoto", removephoto))
app.add_handler(CommandHandler("showrow",     showrow))
app.add_handler(CommandHandler("rows",        rows_cmd))
app.add_handler(CommandHandler("rename",      rename))
app.add_handler(CommandHandler("delete",      delete))
app.add_handler(CommandHandler("deleterow",   deleterow))
app.add_handler(CommandHandler("clear",       clear))

# Photo handler last
app.add_handler(MessageHandler(filters.PHOTO, receive_photo))

app.add_error_handler(error_handler)

print("Puma Depot Bot running...")
app.run_polling()
