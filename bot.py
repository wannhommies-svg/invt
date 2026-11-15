import logging
import secrets
import sqlite3
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ==================== CONFIG ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "123456789").split(",")]
DB_PATH = os.environ.get("DB_PATH", "data.db")

# Conversation States
(
    W_ADMIN_UID, W_ADMIN_AMOUNT, W_ADMIN_RATE,
    W_WD_AMOUNT, W_WD_ACCOUNT, W_WD_PIN,
) = range(6)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

PAYOUT_METHODS = {
    "cashapp": "💵 CashApp",
    "paypal":  "🅿️ PayPal",
    "bank":    "🏦 Bank Transfer",
    "crypto":  "₿ Crypto",
    "other":   "📦 Other",
}

# ==================== DATABASE LAYER ====================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db_connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            balance     REAL DEFAULT 0,
            earn_rate   REAL DEFAULT 0,
            joined      TEXT
        );

        CREATE TABLE IF NOT EXISTS payouts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            method      TEXT,
            amount      REAL,
            account     TEXT,
            created     TEXT
        );

        CREATE TABLE IF NOT EXISTS withdrawals (
            req_id      TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL,
            amount      REAL,
            method      TEXT,
            account     TEXT,
            pin         TEXT,
            status      TEXT DEFAULT 'pending',
            created     TEXT,
            decided     TEXT
        );

        CREATE TABLE IF NOT EXISTS meta (
            key         TEXT PRIMARY KEY,
            value       TEXT
        );
        """)
        row = conn.execute("SELECT value FROM meta WHERE key='req_counter'").fetchone()
        if not row:
            conn.execute("INSERT INTO meta(key,value) VALUES('req_counter','1000')")
        conn.commit()

def db_get_user(uid: int, username: str = None) -> dict:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users(user_id, username, balance, earn_rate, joined) VALUES(?,?,0,0,?)",
                (uid, username or f"User{uid}", datetime.now().strftime("%Y-%m-%d"))
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        elif username and row["username"] != username:
            conn.execute("UPDATE users SET username=? WHERE user_id=?", (username, uid))
            conn.commit()
        return dict(row)

def db_adj_balance(uid: int, delta: float):
    with db_connect() as conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, uid))
        conn.commit()

def db_set_rate(uid: int, rate: float):
    with db_connect() as conn:
        conn.execute("UPDATE users SET earn_rate=? WHERE user_id=?", (rate, uid))
        conn.commit()

def db_list_users() -> list:
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY joined DESC").fetchall()
        return [dict(r) for r in rows]

def db_add_payout(uid: int, method: str, amount: float, account: str):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO payouts(user_id, method, amount, account, created) VALUES(?,?,?,?,?)",
            (uid, method, amount, account, datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()

def db_last_payout(uid: int):
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM payouts WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        return dict(row) if row else None

def db_next_req_id() -> str:
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='req_counter'").fetchone()
        n = int(row["value"]) + 1
        conn.execute("UPDATE meta SET value=? WHERE key='req_counter'", (str(n),))
        conn.commit()
        return str(n)

def db_create_withdrawal(uid: int, amount: float, method: str, account: str) -> str:
    rid = db_next_req_id()
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO withdrawals(req_id, user_id, amount, method, account, status, created) VALUES(?,?,?,?,?, 'pending', ?)",
            (rid, uid, amount, method, account, datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
    return rid

def db_get_withdrawal(rid: str):
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM withdrawals WHERE req_id=?", (rid,)).fetchone()
        return dict(row) if row else None

def db_pending_for_user(uid: int):
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM withdrawals WHERE user_id=? AND status='pending' LIMIT 1", (uid,)).fetchone()
        return dict(row) if row else None

def db_pending_all() -> list:
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM withdrawals WHERE status='pending' ORDER BY created").fetchall()
        return [dict(r) for r in rows]

def db_set_pin(rid: str, pin: str):
    with db_connect() as conn:
        conn.execute("UPDATE withdrawals SET pin=? WHERE req_id=?", (pin, rid))
        conn.commit()

def db_close_withdrawal(rid: str, status: str):
    with db_connect() as conn:
        conn.execute("UPDATE withdrawals SET status=?, decided=? WHERE req_id=?", (status, datetime.now().strftime("%Y-%m-%d %H:%M"), rid))
        conn.commit()

# ==================== UI HELPERS ====================
def is_admin(uid): return uid in ADMIN_IDS

def main_menu(uid):
    kb = [
        [InlineKeyboardButton("💼 My Earnings", callback_data="earnings"),
         InlineKeyboardButton("💸 Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton("🔑 Enter PIN", callback_data="enter_pin"),
         InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
    ]
    if is_admin(uid):
        kb.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(kb)

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Earnings", callback_data="admin_add"),
         InlineKeyboardButton("📈 Edit Earning Rate", callback_data="admin_rate")],
        [InlineKeyboardButton("➖ Remove Funds", callback_data="admin_remove")],
        [InlineKeyboardButton("📥 Withdrawal Requests", callback_data="admin_wd")],
        [InlineKeyboardButton("👥 List Users", callback_data="admin_list")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])

def method_choose_kb():
    rows, buf = [], []
    for key, label in PAYOUT_METHODS.items():
        buf.append(InlineKeyboardButton(label, callback_data=f"pm_{key}"))
        if len(buf) == 2:
            rows.append(buf); buf = []
    if buf: rows.append(buf)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="pm_cancel")])
    return InlineKeyboardMarkup(rows)

def fmt_dash(uid, name):
    u = db_get_user(uid, name)
    lp = db_last_payout(uid)
    last_payout = f"{PAYOUT_METHODS.get(lp['method'], lp['method'])} `${lp['amount']:,.2f}`" if lp else "—"
    
    return (
        f"💼 *EARNINGS DASHBOARD*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *Name:* {name}\n"
        f"🆔 *Account:* `{uid}`\n"
        f"📅 *Member Since:* {u['joined']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Available Balance:* `${u['balance']:,.2f}`\n"
        f"📈 *Current Earning Rate:* `{u['earn_rate']}%`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 *Last Payout:* {last_payout}\n"
    )

def gen_pin():
    return f"{secrets.randbelow(1000000):06d}"

# ==================== COMMANDS & ROUTER ====================
async def start(update: Update, ctx):
    u = update.effective_user
    db_get_user(u.id, u.first_name)
    await update.message.reply_text(
        f"👋 Welcome *{u.first_name}*!\n\n"
        f"Your professional earnings dashboard is ready.",
        parse_mode="Markdown", reply_markup=main_menu(u.id),
    )

async def button_handler(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    u = q.from_user
    d = q.data

    if d in ("earnings", "refresh", "back"):
        await q.edit_message_text(fmt_dash(u.id, u.first_name), parse_mode="Markdown", reply_markup=main_menu(u.id))

    elif d == "withdraw":
        r = db_pending_for_user(u.id)
        if r:
            await q.edit_message_text(
                f"⏳ *Pending Withdrawal Found*\n\n"
                f"🆔 Request: `{r['req_id']}`\n💰 Amount: `${r['amount']:,.2f}`\n"
                f"💳 Payout: {PAYOUT_METHODS.get(r['method'], r['method'])} → `{r['account']}`\n\n"
                f"Tap *🔑 Enter PIN* once the admin gives it to you.",
                parse_mode="Markdown", reply_markup=main_menu(u.id))
            return
        await q.edit_message_text(
            "💸 *New Withdrawal*\n\n"
            "Enter the amount you want to withdraw:\n"
            "_(Send a number, e.g. `50`, or /cancel)_",
            parse_mode="Markdown")
        return W_WD_AMOUNT

    elif d == "enter_pin":
        r = db_pending_for_user(u.id)
        if not r:
            await q.edit_message_text("❌ You have no pending withdrawal.", parse_mode="Markdown", reply_markup=main_menu(u.id))
            return
        await q.edit_message_text(
            f"🔑 *Enter Withdrawal PIN*\n\n"
            f"🆔 Request: `{r['req_id']}`\n💰 Amount: `${r['amount']:,.2f}`\n"
            f"💳 Payout: {PAYOUT_METHODS.get(r['method'])} → `{r['account']}`\n\n"
            f"Send the 6-digit PIN from the admin.\n_(Or /cancel)_",
            parse_mode="Markdown")
        return W_WD_PIN

    elif d.startswith("pm_"):
        if d == "pm_cancel":
            await q.edit_message_text("❌ Withdrawal cancelled.", reply_markup=main_menu(u.id))
            return ConversationHandler.END
        method = d.replace("pm_", "")
        ctx.user_data["wd_method"] = method
        prompts = {
            "cashapp": "Enter your *$CashTag* (e.g. `$johndoe`)",
            "paypal":  "Enter your *PayPal email* (e.g. `john@email.com`)",
            "bank":    "Enter *Bank name + account number*\n(e.g. `Chase 1234567890`)",
            "crypto":  "Enter *wallet address* and coin\n(e.g. `BTC: bc1q...`)",
            "other":   "Enter your payout account details",
        }
        await q.edit_message_text(
            f"💸 *Withdraw Details*\n\n"
            f"💳 Method: {PAYOUT_METHODS[method]}\n\n"
            f"{prompts[method]}\n\n_(Or /cancel)_",
            parse_mode="Markdown")
        return W_WD_ACCOUNT

    elif d == "admin" and is_admin(u.id):
        await q.edit_message_text("⚙️ *Admin Panel*\nChoose an action:", parse_mode="Markdown", reply_markup=admin_menu())

    elif d == "admin_list" and is_admin(u.id):
        users = db_list_users()
        if not users:
            text = "👥 No users yet."
        else:
            lines = ["👥 *All Users*\n━━━━━━━━━━━━━━━━━━━━"]
            for i in users:
                lines.append(f"🆔 `{i['user_id']}` — {i['username']}\n   💰 `${i['balance']:,.2f}` | 📈 `{i['earn_rate']}%`")
            text = "\n".join(lines)
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=admin_menu())

    elif d == "admin_wd" and is_admin(u.id):
        pending = db_pending_all()
        if not pending:
            await q.edit_message_text("📥 *No pending withdrawals.*", parse_mode="Markdown", reply_markup=admin_menu())
            return
        rows = []
        for r in pending:
            m = PAYOUT_METHODS.get(r["method"], r["method"]).split(" ", 1)[-1]
            rows.append([InlineKeyboardButton(f"#{r['req_id']} — {m} — ${r['amount']:,.2f}", callback_data=f"wd_view_{r['req_id']}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin")])
        await q.edit_message_text(f"📥 *Pending Withdrawals* ({len(pending)})\nTap one to review:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))

    elif d.startswith("wd_view_") and is_admin(u.id):
        rid = d.replace("wd_view_", "")
        r = db_get_withdrawal(rid)
        if not r:
            await q.edit_message_text("❌ Not found.", reply_markup=admin_menu()); return
        usr = db_get_user(r["user_id"])
        pin_display = r["pin"] if r["pin"] else "_not yet generated_"
        method_label = PAYOUT_METHODS.get(r["method"], r["method"])
        await q.edit_message_text(
            f"📥 *Withdrawal #{rid}*\n━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 User: {usr['username']} (`{r['user_id']}`)\n"
            f"💰 Amount: `${r['amount']:,.2f}`\n"
            f"📊 Balance: `${usr['balance']:,.2f}`\n"
            f"🕐 Created: {r['created']}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💳 *Method:* {method_label}\n"
            f"🏦 *Account:* `{r['account']}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔑 PIN: `{pin_display}`\n",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Generate & Send PIN", callback_data=f"wd_pin_{rid}")],
                [InlineKeyboardButton("✅ Force Approve", callback_data=f"wd_approve_{rid}")],
                [InlineKeyboardButton("❌ Reject", callback_data=f"wd_reject_{rid}")],
                [InlineKeyboardButton("⬅️ Back", callback_data="admin_wd")],
            ]))

    elif d.startswith("wd_pin_") and is_admin(u.id):
        rid = d.replace("wd_pin_", "")
        r = db_get_withdrawal(rid)
        if not r or r["status"] != "pending":
            await q.edit_message_text("❌ Request is no longer pending.", reply_markup=admin_menu()); return
        if not r["pin"]:
            db_set_pin(rid, gen_pin())
            r["pin"] = db_get_withdrawal(rid)["pin"]
        method_label = PAYOUT_METHODS.get(r["method"], r["method"])
        try:
            await ctx.bot.send_message(
                r["user_id"],
                f"🔑 *Your Withdrawal PIN*\n\n"
                f"🆔 Request: `{rid}`\n💰 Amount: `${r['amount']:,.2f}`\n"
                f"💳 Payout: {method_label} → `{r['account']}`\n"
                f"🔑 PIN: `{r['pin']}`\n\n"
                f"Tap *🔑 Enter PIN* in the bot to complete.",
                parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"DM failed: {e}")
        await q.edit_message_text(
            f"✅ PIN generated and sent to `{r['user_id']}`.\n\n🔑 PIN: `{r['pin']}`\n💳 Payout: {method_label} → `{r['account']}`",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"wd_view_{rid}")]]))

    elif d.startswith("wd_approve_") and is_admin(u.id):
        rid = d.replace("wd_approve_", "")
        r = db_get_withdrawal(rid)
        if not r or r["status"] != "pending":
            await q.edit_message_text("❌ Already processed.", reply_markup=admin_menu()); return
        usr = db_get_user(r["user_id"])
        if usr["balance"] < r["amount"]:
            await q.edit_message_text("❌ User balance insufficient.", reply_markup=admin_menu()); return
        db_adj_balance(r["user_id"], -r["amount"])
        db_close_withdrawal(rid, "approved")
        db_add_payout(r["user_id"], r["method"], r["amount"], r["account"])
        method_label = PAYOUT_METHODS.get(r["method"], r["method"])
        new_bal = db_get_user(r["user_id"])["balance"]
        try:
            await ctx.bot.send_message(r["user_id"], f"✅ *Withdrawal Approved*\n\n🆔 `{rid}`\n💰 `${r['amount']:,.2f}`\n💳 {method_label} → `{r['account']}`\n💵 New balance: `${new_bal:,.2f}`", parse_mode="Markdown")
        except Exception: pass
        await q.edit_message_text(f"✅ Withdrawal #{rid} approved.\n💳 {method_label} → `{r['account']}`\n💵 New balance: `${new_bal:,.2f}`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_wd")]]))

    elif d.startswith("wd_reject_") and is_admin(u.id):
        rid = d.replace("wd_reject_", "")
        r = db_get_withdrawal(rid)
        if not r or r["status"] != "pending":
            await q.edit_message_text("❌ Already processed.", reply_markup=admin_menu()); return
        db_close_withdrawal(rid, "rejected")
        try:
            await ctx.bot.send_message(r["user_id"], f"❌ *Withdrawal Rejected*\n\n🆔 `{rid}`\n💰 `${r['amount']:,.2f}`\nContact admin for details.", parse_mode="Markdown")
        except Exception: pass
        await q.edit_message_text(f"❌ Withdrawal #{rid} rejected.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_wd")]]))

    elif d in ("admin_add", "admin_remove", "admin_rate") and is_admin(u.id):
        ctx.user_data["admin_action"] = d
        action_name = "add earnings" if d == "admin_add" else "remove funds" if d == "admin_remove" else "edit earning rate"
        await q.edit_message_text(f"👤 Enter the *User ID* to {action_name}:\n_(Or /cancel)_", parse_mode="Markdown")
        return W_ADMIN_UID

# ==================== CONVERSATIONS ====================
async def wd_amount(update: Update, ctx):
    txt = update.message.text.strip()
    if txt == "/cancel":
        await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu(update.effective_user.id)); return ConversationHandler.END
    try: amount = float(txt)
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Send a number or /cancel."); return W_WD_AMOUNT
    if amount <= 0:
        await update.message.reply_text("❌ Amount must be greater than 0."); return W_WD_AMOUNT
    usr = db_get_user(update.effective_user.id, update.effective_user.first_name)
    if amount > usr["balance"]:
        await update.message.reply_text(f"❌ *Insufficient balance.*\nRequested: `${amount:,.2f}`\nAvailable: `${usr['balance']:,.2f}`", parse_mode="Markdown"); return W_WD_AMOUNT
    ctx.user_data["wd_amount"] = amount
    await update.message.reply_text(f"💸 *Withdraw*\n\n💰 Amount: `${amount:,.2f}`\n\n💳 *Choose your payout method:*", parse_mode="Markdown", reply_markup=method_choose_kb())
    return ConversationHandler.END

async def wd_account(update: Update, ctx):
    txt = update.message.text.strip()
    if txt == "/cancel":
        await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu(update.effective_user.id)); return ConversationHandler.END
    if len(txt) < 3:
        await update.message.reply_text("❌ Account details too short."); return W_WD_ACCOUNT
    method, amount, uid = ctx.user_data.get("wd_method"), ctx.user_data.get("wd_amount"), update.effective_user.id
    if not method or not amount:
        await update.message.reply_text("❌ Session expired. Tap 💸 Withdraw again.", reply_markup=main_menu(uid)); return ConversationHandler.END
    rid = db_create_withdrawal(uid, amount, method, txt)
    method_label = PAYOUT_METHODS[method]
    for aid in ADMIN_IDS:
        try: await ctx.bot.send_message(aid, f"📥 *New Withdrawal Request*\n🆔 `{rid}`\n👤 {update.effective_user.first_name} (`{uid}`)\n💰 `${amount:,.2f}`\n💳 {method_label}\n🏦 `{txt}`\n\nOpen *Admin Panel* to review.", parse_mode="Markdown")
        except: pass
    await update.message.reply_text(f"✅ *Withdrawal Request Submitted!*\n\n🆔 Request: `{rid}`\n💰 Amount: `${amount:,.2f}`\n💳 Payout: {method_label}\n🏦 Account: `{txt}`\n\n⏳ Waiting for admin to issue a PIN.", parse_mode="Markdown", reply_markup=main_menu(uid))
    return ConversationHandler.END

async def wd_pin(update: Update, ctx):
    txt = update.message.text.strip()
    if txt == "/cancel":
        await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu(update.effective_user.id)); return ConversationHandler.END
    r = db_pending_for_user(update.effective_user.id)
    if not r:
        await update.message.reply_text("❌ No pending withdrawal.", reply_markup=main_menu(update.effective_user.id)); return ConversationHandler.END
    if not r["pin"]:
        await update.message.reply_text("⏳ Admin has not issued a PIN yet."); return W_WD_PIN
    if txt != r["pin"]:
        await update.message.reply_text("❌ Incorrect PIN. Try again or /cancel."); return W_WD_PIN
    usr = db_get_user(r["user_id"])
    if usr["balance"] < r["amount"]:
        db_close_withdrawal(r["req_id"], "rejected")
        await update.message.reply_text("❌ Insufficient balance. Request cancelled.", reply_markup=main_menu(update.effective_user.id)); return ConversationHandler.END
    db_adj_balance(r["user_id"], -r["amount"])
    db_close_withdrawal(r["req_id"], "completed")
    db_add_payout(r["user_id"], r["method"], r["amount"], r["account"])
    method_label = PAYOUT_METHODS.get(r["method"], r["method"])
    new_bal = db_get_user(r["user_id"])["balance"]
    await update.message.reply_text(f"✅ *Withdrawal Complete!*\n\n🆔 Request: `{r['req_id']}`\n💸 Withdrawn: `${r['amount']:,.2f}`\n💳 Sent to: {method_label} → `{r['account']}`\n💵 New balance: `${new_bal:,.2f}`", parse_mode="Markdown", reply_markup=main_menu(update.effective_user.id))
    for aid in ADMIN_IDS:
        try: await ctx.bot.send_message(aid, f"✅ *Withdrawal #{r['req_id']} completed via PIN*\n👤 `{r['user_id']}`\n💰 `${r['amount']:,.2f}`\n💳 {method_label} → `{r['account']}`\n\n⚠️ Send the payout manually.", parse_mode="Markdown")
        except: pass
    return ConversationHandler.END

async def admin_get_uid(update: Update, ctx):
    if not is_admin(update.effective_user.id): return ConversationHandler.END
    if update.message.text.strip() == "/cancel":
        await update.message.reply_text("❌ Cancelled."); return ConversationHandler.END
    try: tid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID. Try again or /cancel."); return W_ADMIN_UID
    ctx.user_data["target_id"] = tid
    usr = db_get_user(tid)
    action = ctx.user_data["admin_action"]
    if action == "admin_rate":
        await update.message.reply_text(f"👤 Target: `{tid}`\n📈 Current Rate: `{usr['earn_rate']}%`\n\nEnter the new *Earning Rate* (e.g. `1.5`):", parse_mode="Markdown")
        return W_ADMIN_RATE
    else:
        label = "add earnings to" if action == "admin_add" else "remove funds from"
        await update.message.reply_text(f"👤 Target: `{tid}`\n💰 Current: `${usr['balance']:,.2f}`\n\nHow much to *{label}* this user?", parse_mode="Markdown")
        return W_ADMIN_AMOUNT

async def admin_apply_amount(update: Update, ctx):
    if not is_admin(update.effective_user.id): return ConversationHandler.END
    if update.message.text.strip() == "/cancel":
        await update.message.reply_text("❌ Cancelled."); return ConversationHandler.END
    try: amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Try again."); return W_ADMIN_AMOUNT
    tid, action = ctx.user_data["target_id"], ctx.user_data["admin_action"]
    usr = db_get_user(tid)
    old = usr["balance"]
    new = old + amt if action == "admin_add" else old - amt
    db_adj_balance(tid, amt if action == "admin_add" else -amt)
    await update.message.reply_text(f"✅ *Balance Updated*\n👤 `{tid}`\n📉 Old: `${old:,.2f}`\n📈 New: `${new:,.2f}`", parse_mode="Markdown", reply_markup=admin_menu())
    try: await ctx.bot.send_message(tid, f"🔔 *Earnings Update*\nYour new balance is `${new:,.2f}`.", parse_mode="Markdown")
    except: pass
    return ConversationHandler.END

async def admin_apply_rate(update: Update, ctx):
    if not is_admin(update.effective_user.id): return ConversationHandler.END
    if update.message.text.strip() == "/cancel":
        await update.message.reply_text("❌ Cancelled."); return ConversationHandler.END
    try: rate = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid rate. Send a number."); return W_ADMIN_RATE
    tid = ctx.user_data["target_id"]
    db_set_rate(tid, rate)
    await update.message.reply_text(f"✅ *Rate Updated*\n👤 `{tid}`\n📈 New Earning Rate: `{rate}%`", parse_mode="Markdown", reply_markup=admin_menu())
    try: await ctx.bot.send_message(tid, f"🔔 *Rate Update*\nYour earning rate has been updated to `{rate}%`.", parse_mode="Markdown")
    except: pass
    return ConversationHandler.END

async def cancel(update: Update, ctx):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ==================== MAIN ====================
def main():
    db_init()
    app = Application.builder().token(BOT_TOKEN).build()

    wd_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^(withdraw|enter_pin)$")],
        states={
            W_WD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, wd_amount)],
            W_WD_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, wd_account)],
            W_WD_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, wd_pin)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^admin_(add|remove|rate)$")],
        states={
            W_ADMIN_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_get_uid)],
            W_ADMIN_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_apply_amount)],
            W_ADMIN_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_apply_rate)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(wd_conv)
    app.add_handler(admin_conv)
    app.add_handler(CallbackQueryHandler(button_handler))

    print("✅ Professional Earnings Bot is running...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
