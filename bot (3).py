"""
Bot de Telegram - Alertas de Vuelos Baratos desde España
=========================================================
Requiere: pip install python-telegram-bot requests apscheduler

Configuración necesaria:
- BOT_TOKEN: Token de tu bot (@BotFather)
- CHANNEL_ID: ID o username de tu canal
- AVIASALES_TOKEN: Token de Travelpayouts
- ADMIN_ID: Tu ID de Telegram (para activar Premium manualmente)
"""

import logging
import sqlite3
from datetime import datetime, timedelta

import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ─────────────────────────────────────────────
#  CONFIGURACIÓN — rellena estos valores
# ─────────────────────────────────────────────
BOT_TOKEN        = "8701234420:AAHh8fW9RfljZ4B5vvn_4Qt8bI2dqIn71WU"
CHANNEL_ID       = "-1003742814878"
AVIASALES_TOKEN  = "2a77b302369bd3422f6832a5dac0aea6"

# Tu ID de Telegram (para usar comandos de admin)
# Consíguelo escribiéndole a @userinfobot en Telegram
ADMIN_ID         = 7508750121  # ← pon aquí tu ID numérico

# Precio del plan Premium (pago único, acceso de por vida)
PREMIUM_PRICE    = 5.00

# Tu ID de afiliado de Travelpayouts (aparece en tu cuenta)
TRAVELPAYOUTS_MARKER = "710935"  # ← pon aquí tu marker

# Aeropuertos de origen
ORIGIN_AIRPORTS  = ["MAD", "BCN", "VLC", "SVQ"]

# Umbral: oferta si el precio cae >= 50% respecto a la media histórica
DISCOUNT_THRESHOLD = 0.50

# Límites de avisos por plan
PLAN_LIMITS = {
    "free":    10,
    "premium": 30,
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  BASE DE DATOS SQLite
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id         INTEGER PRIMARY KEY,
            username        TEXT,
            plan            TEXT    DEFAULT 'free',
            alerts_today    INTEGER DEFAULT 0,
            last_reset      TEXT    DEFAULT '',
            premium_until   TEXT    DEFAULT ''
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            origin      TEXT,
            destination TEXT,
            price       REAL,
            recorded_at TEXT
        )
    """)
    conn.commit()
    conn.close()


# ── Usuarios ──────────────────────────────────
def get_user(user_id: int):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return dict(zip(["user_id","username","plan","alerts_today","last_reset","premium_until"], row))


def register_user(user_id: int, username: str):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()


def increment_alert(user_id: int):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT last_reset FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row and row[0] != today:
        c.execute("UPDATE users SET alerts_today=1, last_reset=? WHERE user_id=?", (today, user_id))
    else:
        c.execute("UPDATE users SET alerts_today=alerts_today+1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_alerts_today(user_id: int) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT alerts_today, last_reset FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row or row[1] != today:
        return 0
    return row[0]


def activate_premium(user_id: int):
    """Pago único — acceso Premium de por vida."""
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute(
        "UPDATE users SET plan='premium', premium_until='vitalicio' WHERE user_id=?",
        (user_id,)
    )
    conn.commit()
    conn.close()


def get_all_users():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT user_id, plan, alerts_today, last_reset FROM users")
    rows = c.fetchall()
    conn.close()
    return rows


# ── Historial de precios ──────────────────────
def save_price(origin: str, destination: str, price: float):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO price_history (origin, destination, price, recorded_at) VALUES (?,?,?,?)",
        (origin, destination, price, datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    c.execute(
        "DELETE FROM price_history WHERE origin=? AND destination=? AND recorded_at < ?",
        (origin, destination, cutoff),
    )
    conn.commit()
    conn.close()


def get_average_price(origin: str, destination: str):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute(
        "SELECT AVG(price) FROM price_history WHERE origin=? AND destination=?",
        (origin, destination),
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


# ─────────────────────────────────────────────
#  COMANDOS DEL BOT
# ─────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username or user.first_name)

    kb = [
        [InlineKeyboardButton("📋 Mi plan", callback_data="myplan"),
         InlineKeyboardButton("⭐ Ir a Premium", callback_data="gopremium")],
        [InlineKeyboardButton("📢 Canal de ofertas",
                              url=f"https://t.me/{CHANNEL_ID.lstrip('@')}")],
    ]
    await update.message.reply_text(
        f"¡Hola, {user.first_name}! 👋\n\n"
        "Soy el bot de *Vuelos Baratos España* ✈️\n\n"
        "Monitorizo vuelos desde Madrid, Barcelona, Valencia y Sevilla "
        "y aviso solo cuando el precio baja *más del 50% de la media histórica*.\n\n"
        "📌 *Planes:*\n"
        f"🆓 Free → 2 avisos/día\n"
        f"⭐ Premium → 6 avisos/día · {PREMIUM_PRICE}€ pago único\n\n"
        "Únete al canal para verlo todo 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u = get_user(user_id)
    if not u:
        register_user(user_id, update.effective_user.username or "")
        u = get_user(user_id)

    plan    = u["plan"]
    limit   = PLAN_LIMITS[plan]
    used    = get_alerts_today(user_id)
    p_until = u["premium_until"]

    extra = f"\n♾️ Acceso: *{p_until}*" if plan == "premium" else ""
    kb = None if plan == "premium" else InlineKeyboardMarkup(
        [[InlineKeyboardButton("⭐ Activar Premium", callback_data="gopremium")]]
    )
    await update.message.reply_text(
        f"📋 *Tu plan:* {'⭐ Premium' if plan == 'premium' else '🆓 Free'}{extra}\n"
        f"🔔 Avisos hoy: *{used}/{limit}*",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra instrucciones para contratar el plan Premium por transferencia."""
    user = update.effective_user
    register_user(user.id, user.username or user.first_name)

    await update.message.reply_text(
        f"⭐ *Plan Premium — {PREMIUM_PRICE}€ pago único*\n\n"
        "✅ 6 avisos de ofertas al día\n"
        "✅ Alertas antes que los usuarios Free\n"
        "✅ Acceso de *por vida*, sin suscripción\n\n"
        "💳 *¿Cómo contratar?*\n"
        f"1. Realiza una transferencia de *{PREMIUM_PRICE}€* a nuestra cuenta\n"
        "2. Contacta con el administrador para facilitar el comprobante\n"
        "3. Tu Premium se activará en menos de 24h\n\n"
        "👉 Pulsa el botón de abajo para contactar con el admin 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📩 Contactar con el admin", url="https://t.me/tu_usuario_admin")]
        ]),
    )

    # Notificar al admin que hay un usuario interesado
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"💰 *Nuevo interesado en Premium*\n\n"
                 f"👤 Usuario: @{user.username or user.first_name}\n"
                 f"🆔 ID: `{user.id}`\n\n"
                 f"Para activar su Premium usa:\n"
                 f"`/activar {user.id}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"No se pudo notificar al admin: {e}")


async def cmd_activar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Comando exclusivo del admin para activar el Premium de un usuario.
    Uso: /activar <user_id>
    Ejemplo: /activar 123456789
    """
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return

    if not context.args:
        await update.message.reply_text(
            "Uso: `/activar <user_id>`\nEjemplo: `/activar 123456789`",
            parse_mode="Markdown"
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ El ID debe ser un número.")
        return

    u = get_user(target_id)
    if not u:
        await update.message.reply_text("❌ Usuario no encontrado en la base de datos.")
        return

    activate_premium(target_id, days=30)
    await update.message.reply_text(
        f"✅ Premium activado para el usuario `{target_id}` durante 30 días.",
        parse_mode="Markdown"
    )

    # Notificar al usuario
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="🎉 *¡Tu plan Premium ha sido activado!*\n\n"
                 "Ya recibirás hasta *6 avisos diarios* de vuelos increíblemente baratos.\n"
                 "📅 Tu Premium es válido durante *30 días*.\n\n"
                 "¡A volar barato! ✈️",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"No se pudo notificar al usuario {target_id}: {e}")


async def cmd_usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando de admin para ver todos los usuarios registrados."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ No tienes permisos.")
        return

    users = get_all_users()
    if not users:
        await update.message.reply_text("No hay usuarios registrados aún.")
        return

    total     = len(users)
    premiumes = sum(1 for u in users if u[1] == "premium")
    frees     = total - premiumes

    msg = (
        f"👥 *Usuarios registrados: {total}*\n"
        f"⭐ Premium: {premiumes}\n"
        f"🆓 Free: {frees}\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆘 *Ayuda*\n\n"
        "/start — Menú principal\n"
        "/plan — Ver tu plan y uso del día\n"
        "/premium — Contratar plan Premium\n"
        "/help — Esta ayuda\n\n"
        "Las ofertas se publican automáticamente cuando "
        "el precio cae más del 50% respecto a la media histórica.",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────
#  CALLBACKS INLINE
# ─────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "myplan":
        u     = get_user(query.from_user.id)
        plan  = u["plan"] if u else "free"
        used  = get_alerts_today(query.from_user.id)
        limit = PLAN_LIMITS.get(plan, 2)
        await query.edit_message_text(
            f"📋 Plan: {'⭐ Premium' if plan == 'premium' else '🆓 Free'}\n"
            f"🔔 Avisos hoy: {used}/{limit}",
        )

    elif query.data == "gopremium":
        await query.edit_message_text(
            f"⭐ *Plan Premium — {PREMIUM_PRICE}€ pago único*\n\n"
            "6 avisos/día · Acceso de por vida · Sin suscripción\n\n"
            "Usa /premium para ver las instrucciones de pago 👇",
            parse_mode="Markdown",
        )


# ─────────────────────────────────────────────
#  BÚSQUEDA DE VUELOS Y DETECCIÓN DE OFERTAS
# ─────────────────────────────────────────────
EUROPEAN_AIRPORTS = {
    "LHR","CDG","FCO","AMS","FRA","LIS","ATH","VIE","ZRH","BRU",
    "CPH","ARN","HEL","DUB","WAW","PRG","BUD","OTP","ZAG","BEG",
    "SOF","OSL","MXP","NCE","MAN","EDI","TXL","MUC","HAM","DUS",
    "STN","LGW","ORY","NAP","VCE","PMO","OPO","AGP","PMI","TFS",
    "LPA","IBZ","ACE","FUE","MRS","BOD","LYS","TLS","GVA","BSL",
    "BHX","LBA","GLA","BFS","ORK","CIA","BGY","TSF","PSA","CTA",
    "BLQ","TRN","VRN","BRI","REG","CAG","RHO","HER","CFU","SKG",
}

def is_european(iata: str) -> bool:
    return iata in EUROPEAN_AIRPORTS


def fetch_all_prices() -> dict:
    prices = {}
    for origin in ORIGIN_AIRPORTS:
        try:
            url = "https://api.travelpayouts.com/v1/prices/cheap"
            params = {
                "origin":      origin,
                "currency":    "EUR",
                "token":       AVIASALES_TOKEN,
                "period_type": "month",
                "one_way":     "true",
            }
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()
            if data.get("success") and data.get("data"):
                for dest, month_data in data["data"].items():
                    for _, flight in month_data.items():
                        price = flight.get("price")
                        dep   = flight.get("departure_at", "")
                        if price:
                            key = (origin, dest)
                            if key not in prices or price < prices[key]["price"]:
                                prices[key] = {
                                    "price":     price,
                                    "departure": dep[:10] if dep else "flexible",
                                }
        except Exception as e:
            logger.error(f"Error obteniendo precios desde {origin}: {e}")
    return prices


def find_deals(prices: dict) -> list:
    deals = []
    for (origin, dest), info in prices.items():
        price = info["price"]
        avg   = get_average_price(origin, dest)

        is_deal      = False
        discount_pct = None

        if avg and avg > 0:
            discount_pct = (avg - price) / avg
            if discount_pct >= DISCOUNT_THRESHOLD:
                is_deal = True
        else:
            fallback = 50 if is_european(dest) else 200
            if price <= fallback:
                is_deal      = True
                discount_pct = 0.0

        if is_deal:
            affiliate_link = (
                f"https://www.aviasales.es/search/{origin}{info['departure'].replace('-','')}"
                f"1?marker={TRAVELPAYOUTS_MARKER}&destination={dest}"
            )
            deals.append({
                "origin":      origin,
                "destination": dest,
                "price":       price,
                "avg_price":   round(avg, 2) if avg else None,
                "discount":    round(discount_pct * 100) if discount_pct else None,
                "departure":   info["departure"],
                "is_european": is_european(dest),
                "link":        affiliate_link,
            })

        save_price(origin, dest, price)

    deals.sort(key=lambda x: x["price"])
    return deals


def format_deal(deal: dict) -> str:
    flag         = "🇪🇺" if deal["is_european"] else "🌍"
    origin_names = {"MAD":"Madrid","BCN":"Barcelona","VLC":"Valencia","SVQ":"Sevilla"}
    origin       = origin_names.get(deal["origin"], deal["origin"])
    avg_txt      = f"_(media: {deal['avg_price']}€)_" if deal["avg_price"] else ""
    disc_txt     = f"🔻 *{deal['discount']}% más barato* de lo habitual\n" if deal["discount"] else ""

    return (
        f"🚨 *¡OFERTA BRUTAL!* {flag}\n\n"
        f"✈️ *{origin} → {deal['destination']}*\n"
        f"💶 *Solo {deal['price']}€* {avg_txt}\n"
        f"{disc_txt}"
        f"📅 Salida aprox: {deal['departure']}\n\n"
        f"👉 [Reservar ahora]({deal['link']})\n\n"
        f"⚡ ¡Las plazas son limitadas!"
    )


# ─────────────────────────────────────────────
#  TAREA PROGRAMADA
# ─────────────────────────────────────────────
async def search_and_publish(app: Application):
    logger.info("⏱ Buscando vuelos baratos...")
    prices = fetch_all_prices()
    deals  = find_deals(prices)

    if not deals:
        logger.info("Sin ofertas nuevas esta vez.")
        return

    logger.info(f"✅ {len(deals)} ofertas encontradas.")
    today = datetime.now().strftime("%Y-%m-%d")
    users = get_all_users()

    for deal in deals[:3]:
        msg = format_deal(deal)

        # Publicar en el canal
        try:
            await app.bot.send_message(
                chat_id=CHANNEL_ID,
                text=msg,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
        except Exception as e:
            logger.error(f"Error publicando en canal: {e}")

        # Notificar usuarios según su límite diario
        for user_id, plan, alerts_today, last_reset in users:
            limit = PLAN_LIMITS.get(plan, 2)
            used  = 0 if last_reset != today else alerts_today
            if used < limit:
                try:
                    await app.bot.send_message(
                        chat_id=user_id,
                        text=msg,
                        parse_mode="Markdown",
                        disable_web_page_preview=False,
                    )
                    increment_alert(user_id)
                except Exception:
                    pass


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
async def post_init(app: Application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        search_and_publish,
        "interval",
        hours=2,
        args=[app],
        next_run_time=datetime.now(),
    )
    scheduler.start()
    logger.info("Scheduler arrancado.")


def main():
    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("plan",     cmd_plan))
    app.add_handler(CommandHandler("premium",  cmd_premium))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("activar",   cmd_activar))
    app.add_handler(CommandHandler("usuarios",  cmd_usuarios))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot arrancado correctamente.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
