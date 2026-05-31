import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import sqlite3
from contextlib import asynccontextmanager
from json import loads
from urllib.parse import parse_qsl, unquote

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────

BOT_TOKEN  = "8864067739:AAGZZgGH9682pOVouzywEzvBXpYmo3GxBL4"
WEBAPP_URL = "https://fuufoy-production.up.railway.app"
OWNER_IDS  = [297562307, 6498621298]
PORT       = int(os.environ.get("PORT", 8000))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ─── GIFTS ────────────────────────────────────────────────────────────────────

GIFTS = [
    {"id": "torch",    "name": "Факел",      "file": "torch.png"},
    {"id": "ramen",    "name": "Рамен",      "file": "ramen.png"},
    {"id": "snake",    "name": "Змея",       "file": "snake.png"},
    {"id": "icecream", "name": "Мороженое",  "file": "icecream.png"},
    {"id": "happybday","name": "Happy B-day","file": "happybday.png"},
]
CIGAR = {"id": "cigar", "name": "Сигара", "file": "cigar.png"}

SPIN_COST   = 30
WIN_2_STARS = 5

# ─── DATABASE ─────────────────────────────────────────────────────────────────

DB = "casino.db"

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id  INTEGER PRIMARY KEY,
            username TEXT,
            balance  INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS inventory (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER,
            gift_id  TEXT,
            gift_name TEXT,
            gift_file TEXT,
            won_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    c.commit()
    c.close()

def ensure_user(user_id: int, username: str):
    c = db()
    c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?,?)", (user_id, username))
    c.commit(); c.close()

def get_balance(user_id: int) -> int:
    c = db()
    row = c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    return row["balance"] if row else 0

def add_balance(user_id: int, amount: int):
    c = db()
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
    c.commit(); c.close()

def deduct_balance(user_id: int, amount: int) -> bool:
    c = db()
    row = c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row or row["balance"] < amount:
        c.close(); return False
    c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, user_id))
    c.commit(); c.close()
    return True

def add_to_inventory(user_id: int, gift: dict) -> int:
    c = db()
    cur = c.execute(
        "INSERT INTO inventory (user_id, gift_id, gift_name, gift_file) VALUES (?,?,?,?)",
        (user_id, gift["id"], gift["name"], gift["file"])
    )
    item_id = cur.lastrowid
    c.commit(); c.close()
    return item_id

def get_inventory(user_id: int) -> list:
    c = db()
    rows = c.execute(
        "SELECT * FROM inventory WHERE user_id=? ORDER BY won_at DESC", (user_id,)
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]

def remove_from_inventory(item_id: int, user_id: int) -> dict | None:
    c = db()
    row = c.execute(
        "SELECT * FROM inventory WHERE id=? AND user_id=?", (item_id, user_id)
    ).fetchone()
    if not row:
        c.close(); return None
    c.execute("DELETE FROM inventory WHERE id=?", (item_id,))
    c.commit(); c.close()
    return dict(row)

# ─── SPIN LOGIC ───────────────────────────────────────────────────────────────

def do_spin() -> dict:
    r = random.random() * 100

    if r < 0.8:
        symbols = [CIGAR["id"]] * 3
        return {"type": "jackpot", "symbols": symbols, "gift": CIGAR}
    elif r < 3.3:
        gift = random.choice(GIFTS)
        symbols = [gift["id"]] * 3
        return {"type": "three", "symbols": symbols, "gift": gift}
    elif r < 43.3:
        sym = random.choice(GIFTS)["id"]
        others = [g["id"] for g in GIFTS if g["id"] != sym]
        third = random.choice(others)
        pos = random.randint(0, 2)
        symbols = [sym, sym, sym]
        symbols[pos] = third
        return {"type": "two", "symbols": symbols, "stars": WIN_2_STARS}
    else:
        chosen = random.sample([g["id"] for g in GIFTS], 3)
        return {"type": "nothing", "symbols": chosen}

# ─── TELEGRAM AUTH ────────────────────────────────────────────────────────────

def verify_init_data(init_data: str) -> dict | None:
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        hash_val = parsed.pop("hash", None)
        if not hash_val:
            return None
        check_str = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, hash_val):
            return None
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None

# ─── TELEGRAM BOT ─────────────────────────────────────────────────────────────

tg_app: Application = None

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)
    bal = get_balance(user.id)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎰 Играть", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(f"⭐ Баланс: {bal}", callback_data="balance")],
    ])
    await update.message.reply_text(
        f"Привет, {user.first_name}!\n\nДобро пожаловать в казино 🎰\nКрути — выигрывай подарки и звёзды.",
        reply_markup=kb
    )

async def cb_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    bal = get_balance(q.from_user.id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Пополнить", callback_data="topup")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")],
    ])
    await q.edit_message_text(f"⭐ Твой баланс: *{bal} Stars*", parse_mode="Markdown", reply_markup=kb)

async def cb_topup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["awaiting_topup"] = True
    await q.edit_message_text(
        "💳 Введи сумму пополнения (целое число, минимум 1):\n\nНапример: `50`",
        parse_mode="Markdown"
    )

async def cb_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    bal = get_balance(user.id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎰 Играть", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(f"⭐ Баланс: {bal}", callback_data="balance")],
    ])
    await q.edit_message_text(
        f"Привет, {user.first_name}!\n\nДобро пожаловать в казино 🎰\nКрути — выигрывай подарки и звёзды.",
        reply_markup=kb
    )

async def msg_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_topup"):
        return
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("❌ Целое число, минимум 1. Попробуй ещё раз:")
        return
    amount = int(text)
    ctx.user_data["awaiting_topup"] = False
    await update.message.reply_invoice(
        title="Пополнение баланса",
        description=f"Пополнение на {amount} ⭐ Stars",
        payload=f"topup_{update.effective_user.id}_{amount}",
        currency="XTR",
        prices=[LabeledPrice("Stars", amount)],
    )

async def precheckout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def payment_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    payload = update.message.successful_payment.invoice_payload
    parts = payload.split("_")
    user_id, amount = int(parts[1]), int(parts[2])
    add_balance(user_id, amount)
    await update.message.reply_text(
        f"✅ Пополнено *{amount} ⭐*!\nНовый баланс: *{get_balance(user_id)} ⭐*",
        parse_mode="Markdown"
    )

def build_tg_app():
    global tg_app
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CallbackQueryHandler(cb_balance, pattern="^balance$"))
    tg_app.add_handler(CallbackQueryHandler(cb_topup,   pattern="^topup$"))
    tg_app.add_handler(CallbackQueryHandler(cb_back,    pattern="^back$"))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    tg_app.add_handler(PreCheckoutQueryHandler(precheckout))
    tg_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payment_done))
    return tg_app

# ─── FASTAPI ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    build_tg_app()
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()
    log.info("Bot polling started")
    yield
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()

api = FastAPI(lifespan=lifespan)
api.mount("/imgs", StaticFiles(directory="."), name="imgs")

@api.get("/")
async def serve_app():
    return FileResponse("index.html")

# ── Spin endpoint ──────────────────────────────────────────────────────────────

class SpinBody(BaseModel):
    init_data: str

@api.post("/api/spin")
async def route_spin(body: SpinBody):
    user = verify_init_data(body.init_data)
    if not user:
        raise HTTPException(403, "Невалидный initData")

    user_id  = user["id"]
    username = user.get("username") or user.get("first_name", "unknown")
    ensure_user(user_id, username)

    if not deduct_balance(user_id, SPIN_COST):
        raise HTTPException(400, "Недостаточно Stars")

    result = do_spin()
    result["balance"] = get_balance(user_id)

    if result["type"] in ("three", "jackpot"):
        gift = result["gift"]
        item_id = add_to_inventory(user_id, gift)
        result["inventory_item_id"] = item_id
        msg = (
            f"🎁 Новый выигрыш!\n\n"
            f"Подарок: *{gift['name']}*\n"
            f"ID: `{user_id}`\n"
            f"Юзер: @{username}"
        )
        asyncio.create_task(
            asyncio.gather(*[tg_app.bot.send_message(oid, msg, parse_mode="Markdown") for oid in OWNER_IDS])
        )
    elif result["type"] == "two":
        add_balance(user_id, WIN_2_STARS)
        result["balance"] = get_balance(user_id)

    return result

@api.get("/api/balance")
async def route_balance(init_data: str):
    user = verify_init_data(init_data)
    if not user:
        raise HTTPException(403, "Невалидный initData")
    ensure_user(user["id"], user.get("username", ""))
    return {"balance": get_balance(user["id"])}

@api.get("/api/inventory")
async def route_inventory(init_data: str):
    user = verify_init_data(init_data)
    if not user:
        raise HTTPException(403, "Невалидный initData")
    return {"items": get_inventory(user["id"])}

class WithdrawBody(BaseModel):
    init_data: str
    item_id: int

@api.post("/api/withdraw")
async def route_withdraw(body: WithdrawBody):
    user = verify_init_data(body.init_data)
    if not user:
        raise HTTPException(403, "Невалидный initData")

    user_id  = user["id"]
    username = user.get("username") or user.get("first_name", "unknown")
    item = remove_from_inventory(body.item_id, user_id)

    if not item:
        raise HTTPException(404, "Предмет не найден")

    msg = (
        f"📤 Запрос на вывод подарка!\n\n"
        f"Подарок: *{item['gift_name']}*\n"
        f"ID: `{user_id}`\n"
        f"Юзер: @{username}\n\n"
        f"Отправь подарок вручную 👆"
    )
    asyncio.create_task(
        asyncio.gather(*[tg_app.bot.send_message(oid, msg, parse_mode="Markdown") for oid in OWNER_IDS])
    )
    return {"ok": True}

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:api", host="0.0.0.0", port=PORT)
