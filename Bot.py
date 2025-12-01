# Логи (LOG_GUILD_ID / LOG_CHANNEL_ID).

import os
import io
import re
import json
import asyncio
import hashlib
import logging
from datetime import datetime
import asyncpg
import discord
from discord import app_commands, File
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from asyncio import Semaphore

# -----------------------
# Конфіг з ENV (постав у Render secrets)
# -----------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # postgres://user:pass@host:port/dbname
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))  # 5 min default
MAX_CHECKS_PER_TICK = int(os.getenv("MAX_CHECKS_PER_TICK", "4"))

PLAYWRIGHT_USER_DATA = os.getenv("PLAYWRIGHT_USER_DATA", "/tmp/playwright_user_data")

# IDs для логів (твої значення за замовчуванням)
LOG_GUILD_ID = int(os.getenv("LOG_GUILD_ID", "1218472302975520839"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "1366717075271323749"))

# Селектори сторінки
CITY_SEL = "input#city.form__input"
STREET_SEL = "input#street.form__input"
HOUSE_SEL = "input#house_num.form__input"
RESULT_SELECTOR = ".discon-schedule-table"
AUTOCOMPLETE_ITEM = ".autocomplete-items div"

# Семафор (щоб не запускати багато одночасно)
FETCH_SEMAPHORE = Semaphore(1)

# Globals
_playwright = None
_browser_ctx = None
db_pool = None

# Автокомпліт дані (будемо завантажувати з discon-schedule.js або shutdowns.txt)
AUTOCOMPLETE_DATA = {"cities": [], "streets_by_city": {}}

# -----------------------
# Logging setup
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dtekbot")

# Discord client
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# -----------------------
# Helper: send log message to configured channel
# -----------------------
async def send_log_message(text: str):
    """Надсилає лог у Discord, автоматично ділить на частини по 1900 символів."""
    try:
        if not client.is_ready():
            return

        max_len = 1900
        timestamp = f"📝 `{datetime.utcnow().isoformat()} UTC`\n"
        full_text = timestamp + text

        # Ріжемо на шматки
        chunks = [full_text[i:i+max_len] for i in range(0, len(full_text), max_len)]

        # Надсилаємо всі частини по черзі
        channel = client.get_channel(LOG_CHANNEL_ID) or await client.fetch_channel(LOG_CHANNEL_ID)

        for part in chunks:
            await channel.send(part)

    except Exception as e:
        logging.error("Не вдалося надіслати лог у канал: %s", e)

class DiscordLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            # відправимо асинхронно, якщо бот готовий
            if client.is_ready():
                asyncio.create_task(send_log_message(f"{record.levelname}: {msg}"))
        except Exception:
            pass

# додамо handler — але прив'язка відбудеться після on_ready, щоб не кидати помилки до готовності
# -----------------------
# DB helpers
# -----------------------
async def init_db():
    global db_pool
    if db_pool:
        return db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    async with db_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id SERIAL PRIMARY KEY,
            discord_user_id BIGINT NOT NULL,
            city TEXT NOT NULL,
            street TEXT NOT NULL,
            house TEXT NOT NULL,
            last_hash TEXT,
            last_checked TIMESTAMP DEFAULT now(),
            created_at TIMESTAMP DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_sub_last_checked ON subscriptions(last_checked);
        """)
    return db_pool

async def add_subscription(discord_user_id: int, city: str, street: str, house: str):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO subscriptions (discord_user_id, city, street, house, last_checked)
            VALUES ($1,$2,$3,$4, now())
            ON CONFLICT DO NOTHING
        """, discord_user_id, city, street, house)

async def remove_subscriptions_for_user(discord_user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM subscriptions WHERE discord_user_id=$1", discord_user_id)

async def fetch_n_oldest(n):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM subscriptions ORDER BY last_checked ASC NULLS FIRST LIMIT $1", n)
        return rows

async def update_subscription_hash_and_time(sub_id: int, new_hash: str):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE subscriptions SET last_hash=$1, last_checked=now() WHERE id=$2", new_hash, sub_id)

# -----------------------
# Autocomplete data loader (пробуємо discon-schedule.js, fallback shutdowns.txt)
# -----------------------
def load_autocomplete_from_files():
    # Спроба 1: discon-schedule.js має логіку і в деяких випадках inline об'єкт streets
    try:
        with open("discon-schedule.js", "r", encoding="utf-8") as f:
            js = f.read()
            # Пошук об'єкта streets = {...}
            m = re.search(r"DisconSchedule\.streets\s*=\s*(\{[\s\S]*?\});", js)
            if m:
                obj_text = m.group(1)
                # Невелика трансформація в JSON-подібний
                jsonish = re.sub(r"(\w+)\s*:", r'"\1":', obj_text)
                jsonish = jsonish.replace("'", '"')
                jsonish = re.sub(r",\s*([\]}])", r"\1", jsonish)
                try:
                    parsed = json.loads(jsonish)
                    AUTOCOMPLETE_DATA["cities"] = list(parsed.keys())
                    AUTOCOMPLETE_DATA["streets_by_city"] = parsed
                    logger.info("Loaded autocomplete from discon-schedule.js")
                    return
                except Exception:
                    logger.info("discon-schedule.js: не вдалось парсити як JSON, продовжуємо...")
    except FileNotFoundError:
        pass

    # Спроба 2: shutdowns.txt — дістати повторювані назви (fallback)
    try:
        with open("shutdowns.txt", "r", encoding="utf-8") as f:
            txt = f.read()
            candidates = re.findall(r"\b[А-ЯЇЄІ][а-яіїє']{2,}(?:\s+[А-ЯЇЄІ][а-яіїє']{2,})?\b", txt)
            freq = {}
            for c in candidates:
                freq[c] = freq.get(c, 0) + 1
            top = sorted(freq.items(), key=lambda x: -x[1])[:200]
            cities = [t[0] for t in top]
            AUTOCOMPLETE_DATA["cities"] = cities
            logger.info("Loaded fallback autocomplete cities from shutdowns.txt (count=%d)", len(cities))
    except FileNotFoundError:
        logger.info("No shutdowns.txt found; autocomplete empty.")

# load right away
load_autocomplete_from_files()

# -----------------------
# Playwright helpers (економічні)
# -----------------------
async def start_playwright():
    global _playwright, _browser_ctx
    if _browser_ctx:
        return _browser_ctx
    _playwright = await async_playwright().start()
    _browser_ctx = await _playwright.chromium.launch_persistent_context(
        user_data_dir=PLAYWRIGHT_USER_DATA,
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    async def route_intercept(route):
        req = route.request
        typ = req.resource_type
        url = req.url
        if typ in ("image", "media", "font", "stylesheet", "websocket"):
            await route.abort()
            return
        if "google-analytics" in url or "googletagmanager" in url:
            await route.abort()
            return
        await route.continue_()
    await _browser_ctx.route("**/*", route_intercept)
    return _browser_ctx

async def fetch_schedule_html(city: str, street: str, house: str, timeout=20000):
    async with FETCH_SEMAPHORE:
        ctx = await start_playwright()
        page = await ctx.new_page()
        try:
            await page.goto("https://www.dtek-krem.com.ua/ua/shutdowns", timeout=timeout)
            await page.fill(CITY_SEL, city)
            await asyncio.sleep(0.3)
            try:
                await page.wait_for_selector(AUTOCOMPLETE_ITEM, timeout=1500)
                items = await page.query_selector_all(AUTOCOMPLETE_ITEM)
                for it in items:
                    txt = (await it.inner_text()).strip().lower()
                    if city.lower() in txt or city.split()[0].lower() in txt:
                        await it.click()
                        break
                else:
                    if items:
                        await items[0].click()
            except PWTimeout:
                pass
            await page.fill(STREET_SEL, street)
            await asyncio.sleep(0.25)
            try:
                await page.wait_for_selector(AUTOCOMPLETE_ITEM, timeout=1200)
                items = await page.query_selector_all(AUTOCOMPLETE_ITEM)
                if items:
                    await items[0].click()
            except PWTimeout:
                pass
            await page.fill(HOUSE_SEL, house)
            await asyncio.sleep(0.25)
            await page.wait_for_selector(RESULT_SELECTOR, timeout=10000)
            html = await page.inner_html(RESULT_SELECTOR)
            return html
        except Exception as e:
            logger.exception("fetch_schedule_html error: %s", e)
            await send_log_message(f"Помилка доступу до сайту для {city}, {street}, {house}: {e}")
            return None
        finally:
            await page.close()

async def html_to_png(schedule_html: str) -> bytes:
    ctx = await start_playwright()
    page = await ctx.new_page()
    try:
        content = f"<html><head><meta charset='utf-8'></head><body>{schedule_html}</body></html>"
        await page.set_content(content, wait_until="networkidle")
        img = await page.screenshot(full_page=True)
        return img
    finally:
        await page.close()

# -----------------------
# Discord commands (українською) + autocomplete
# -----------------------
async def city_autocomplete(interaction: discord.Interaction, current: str):
    options = []
    cur = current.lower()
    for c in AUTOCOMPLETE_DATA.get("cities", [])[:500]:
        if cur in c.lower():
            options.append(app_commands.Choice(name=c, value=c))
        if len(options) >= 25:
            break
    return options

async def street_autocomplete(interaction: discord.Interaction, current: str):
    # спробуємо отримати city з interaction (discord py autocomplete namespace quirk)
    city = None
    try:
        # interaction.namespace доступний коли slash викликається в певних реалізаціях
        city = getattr(interaction.namespace, "city", None)
    except Exception:
        city = None
    if not city:
        return []
    streets = AUTOCOMPLETE_DATA.get("streets_by_city", {}).get(city, [])
    if not streets:
        return []
    options = []
    cur = current.lower()
    for s in streets:
        if cur in s.lower():
            options.append(app_commands.Choice(name=s, value=s))
        if len(options) >= 25:
            break
    return options

@tree.command(name="start", description="Підписатися на оновлення графіку (нас.пункт, вулиця, будинок)")
@app_commands.describe(city="Населений пункт", street="Вулиця", house="Номер будинку")
@app_commands.autocomplete(city=city_autocomplete, street=street_autocomplete)
async def cmd_start(interaction: discord.Interaction, city: str, street: str, house: str):
    await interaction.response.defer(thinking=True)
    try:
        await init_db()
        await add_subscription(interaction.user.id, city.strip(), street.strip(), house.strip())
        await interaction.followup.send(f"✅ Ви підписані на оновлення для: **{city}, {street}, {house}**. Я перевірятиму графік кожні {CHECK_INTERVAL_SECONDS//60} хвилин і писатиму в приватні повідомлення при змінах.", ephemeral=True)
        await send_log_message(f"Нова підписка: {interaction.user} ({interaction.user.id}) → {city}, {street}, {house}")
    except Exception as e:
        logger.exception("cmd_start error: %s", e)
        await interaction.followup.send("❌ Помилка при додаванні підписки. Спробуйте пізніше.", ephemeral=True)
        await send_log_message(f"Помилка при /start: {e}")

@tree.command(name="довідка", description="Отримати довідкову інформацію про бота")
async def cmd_help(interaction: discord.Interaction):
    text = (
        "Я бот, що моніторить графік відключень на сайті ДТЕК і надсилає оновлення.\n\n"
        "Команди:\n"
        "/start <нас.пункт> <вул> <буд> — підписатися на адресу\n"
        "/відписатись_від_бота — відписатися від всіх повідомлень\n"
        "/довідка — цей текст\n\n"
        "Підказки: вводьте назви українською (кирилицею)."
    )
    await interaction.response.send_message(text, ephemeral=True)

@tree.command(name="відписатись_від_бота", description="Відписатися від всіх повідомлень бота")
async def cmd_unsubscribe(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        await init_db()
        await remove_subscriptions_for_user(interaction.user.id)
        await interaction.followup.send("✅ Ви успішно відписані від бота. Якщо передумаєте — використайте /start.", ephemeral=True)
        await send_log_message(f"Користувач відписався: {interaction.user} ({interaction.user.id})")
    except Exception as e:
        logger.exception("cmd_unsubscribe error: %s", e)
        await interaction.followup.send("❌ Помилка при відписці. Спробуйте пізніше.", ephemeral=True)

# -----------------------
# Worker loop: вибирає MAX_CHECKS_PER_TICK підписок і перевіряє їх
# -----------------------
def compute_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

async def worker_loop():
    await init_db()
    while True:
        try:
            subs = await fetch_n_oldest(MAX_CHECKS_PER_TICK)
            if not subs:
                logger.info("Нема підписок — чекаю.")
            for s in subs:
                # помічаємо, що перевіряємо (щоб не дублювати)
                await update_subscription_hash_and_time(s["id"], s["last_hash"] or "")
                html = await fetch_schedule_html(s["city"], s["street"], s["house"])
                if not html:
                    await asyncio.sleep(1)
                    continue
                h = compute_hash(re.sub(r"\s+", " ", html.strip()))
                if h != (s["last_hash"] or ""):
                    try:
                        png = await html_to_png(html)
                        user = await client.fetch_user(s["discord_user_id"])
                        if user:
                            try:
                                await user.send(content=f"🔔 Оновлення графіку для **{s['city']}, {s['street']}, {s['house']}**:", file=File(io.BytesIO(png), filename="shutdowns.png"))
                            except Exception as e_send:
                                logger.exception("Не вдалося надіслати DM: %s", e_send)
                        await update_subscription_hash_and_time(s["id"], h)
                        await send_log_message(f"Оновлення надіслано для {s['city']}, {s['street']}, {s['house']} (sub id={s['id']})")
                    except Exception as e:
                        logger.exception("Помилка при рендері/відправці: %s", e)
                        await send_log_message(f"Помилка при рендері/відправці для sub id={s['id']}: {e}")
                else:
                    await update_subscription_hash_and_time(s["id"], s["last_hash"] or "")
                await asyncio.sleep(1.0)
        except Exception as e:
            logger.exception("worker_loop error: %s", e)
            await send_log_message(f"Помилка воркера: {e}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# -----------------------
# Startup / on_ready
# -----------------------
@client.event
async def on_ready():
    logger.info(f"Бот увімкнений: {client.user} (id={client.user.id})")
    # додаємо лог-хендлер для відправки в discord-канал
    h = DiscordLogHandler()
    h.setLevel(logging.WARNING)  # відправляти у канал тільки WARN/ERROR (можеш змінити)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    logging.getLogger().addHandler(h)
    # синхронізуємо slash-команди
    await tree.sync()
    # залітаємо воркер
    client.loop.create_task(worker_loop())
    # повідомимо адміну (channel) що бот стартнув
    await send_log_message(f"Бот стартував: {client.user} (guild target {LOG_GUILD_ID}, channel {LOG_CHANNEL_ID})")

if __name__ == "__main__":
    if not DISCORD_TOKEN or not DATABASE_URL:
        logger.error("Відсутні DISCORD_TOKEN або DATABASE_URL.")
        raise SystemExit(1)
    client.run(DISCORD_TOKEN)