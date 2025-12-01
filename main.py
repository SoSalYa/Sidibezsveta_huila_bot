import os
import io
import re
import json
import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
import asyncpg
import discord
from discord import app_commands, File
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ============ КОНФІГУРАЦІЯ ============
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
MAX_CHECKS_PER_TICK = int(os.getenv("MAX_CHECKS_PER_TICK", "3"))  # Знижено для free плану
PLAYWRIGHT_USER_DATA = os.getenv("PLAYWRIGHT_USER_DATA", "/tmp/playwright_data")

LOG_GUILD_ID = int(os.getenv("LOG_GUILD_ID", "1218472302975520839"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "1366717075271323749"))

# Таймаути (менші для економії ресурсів)
PAGE_TIMEOUT = 15000
AUTOCOMPLETE_TIMEOUT = 1000
RESULT_TIMEOUT = 8000

# Селектори
CITY_SEL = "input#city.form__input"
STREET_SEL = "input#street.form__input"
HOUSE_SEL = "input#house_num.form__input"
RESULT_SELECTOR = ".discon-schedule-table"
AUTOCOMPLETE_ITEM = ".autocomplete-items div"

# Глобальні змінні
db_pool = None
AUTOCOMPLETE_DATA = {"cities": [], "streets_by_city": {}}

# ============ ЛОГУВАННЯ ============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("dtekbot")

intents = discord.Intents.default()
intents.message_content = False  # Не потрібен
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ============ DISCORD ЛОГУВАННЯ ============
async def send_log_message(text: str, level: str = "INFO"):
    """Надсилає лог у Discord з автоматичним поділом"""
    try:
        if not client.is_ready():
            return
        
        emoji = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "❌"}.get(level, "📝")
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        full_text = f"{emoji} `{timestamp}`\n{text}"
        
        # Ділимо на частини по 1900 символів
        for i in range(0, len(full_text), 1900):
            chunk = full_text[i:i+1900]
            channel = client.get_channel(LOG_CHANNEL_ID)
            if not channel:
                channel = await client.fetch_channel(LOG_CHANNEL_ID)
            await channel.send(chunk)
            if i + 1900 < len(full_text):
                await asyncio.sleep(0.5)  # Уникаємо rate limit
    except Exception as e:
        logger.error(f"Помилка відправки логу: {e}")

class DiscordLogHandler(logging.Handler):
    """Handler для відправки критичних логів у Discord"""
    def emit(self, record):
        if not client.is_ready() or record.levelno < logging.WARNING:
            return
        try:
            msg = self.format(record)
            level = "WARNING" if record.levelno == logging.WARNING else "ERROR"
            asyncio.create_task(send_log_message(msg, level))
        except:
            pass

# ============ БАЗА ДАНИХ ============
async def init_db():
    """Ініціалізація пулу з'єднань"""
    global db_pool
    if db_pool:
        return db_pool
    
    try:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=3,  # Мінімум для free плану
            command_timeout=10,
            max_inactive_connection_lifetime=300
        )
        
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
                    created_at TIMESTAMP DEFAULT now(),
                    error_count INT DEFAULT 0,
                    UNIQUE(discord_user_id, city, street, house)
                );
                CREATE INDEX IF NOT EXISTS idx_sub_last_checked 
                    ON subscriptions(last_checked) WHERE error_count < 5;
                CREATE INDEX IF NOT EXISTS idx_sub_user 
                    ON subscriptions(discord_user_id);
            """)
        
        logger.info("База даних ініціалізована")
        return db_pool
    except Exception as e:
        logger.exception("Помилка ініціалізації БД")
        raise

async def add_subscription(user_id: int, city: str, street: str, house: str):
    """Додає підписку з перевіркою на дублікати"""
    async with db_pool.acquire() as conn:
        try:
            await conn.execute("""
                INSERT INTO subscriptions (discord_user_id, city, street, house)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (discord_user_id, city, street, house) DO NOTHING
            """, user_id, city, street, house)
            return True
        except Exception as e:
            logger.error(f"Помилка додавання підписки: {e}")
            return False

async def remove_subscriptions_for_user(user_id: int):
    """Видаляє всі підписки користувача"""
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM subscriptions WHERE discord_user_id=$1",
            user_id
        )
        return int(result.split()[-1])  # Кількість видалених

async def get_user_subscriptions(user_id: int):
    """Отримує всі підписки користувача"""
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT city, street, house FROM subscriptions WHERE discord_user_id=$1",
            user_id
        )

async def fetch_n_oldest(n: int):
    """Вибирає N найстаріших підписок для перевірки"""
    async with db_pool.acquire() as conn:
        return await conn.fetch("""
            SELECT * FROM subscriptions 
            WHERE error_count < 5
            ORDER BY last_checked ASC NULLS FIRST 
            LIMIT $1
        """, n)

async def update_subscription_hash(sub_id: int, new_hash: str, success: bool = True):
    """Оновлює хеш та час перевірки"""
    async with db_pool.acquire() as conn:
        if success:
            await conn.execute("""
                UPDATE subscriptions 
                SET last_hash=$1, last_checked=now(), error_count=0
                WHERE id=$2
            """, new_hash, sub_id)
        else:
            await conn.execute("""
                UPDATE subscriptions 
                SET last_checked=now(), error_count=error_count+1
                WHERE id=$2
            """, sub_id)

async def get_total_subscriptions():
    """Підрахунок активних підписок"""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM subscriptions WHERE error_count < 5")
        return row['cnt']

# ============ АВТОКОМПЛІТ ============
def load_autocomplete_from_files():
    """Завантаження даних для автокомпліту"""
    global AUTOCOMPLETE_DATA
    
    # Спроба 1: discon-schedule.js
    try:
        with open("discon-schedule.js", "r", encoding="utf-8") as f:
            js = f.read()
            match = re.search(r"DisconSchedule\.streets\s*=\s*(\{[\s\S]*?\});", js)
            if match:
                obj_text = match.group(1)
                # Конвертація JS об'єкта в JSON
                jsonish = re.sub(r"(\w+)\s*:", r'"\1":', obj_text)
                jsonish = jsonish.replace("'", '"')
                jsonish = re.sub(r",\s*([\]}])", r"\1", jsonish)
                
                try:
                    parsed = json.loads(jsonish)
                    AUTOCOMPLETE_DATA["cities"] = sorted(parsed.keys())
                    AUTOCOMPLETE_DATA["streets_by_city"] = parsed
                    logger.info(f"Завантажено {len(parsed)} міст з discon-schedule.js")
                    return
                except json.JSONDecodeError:
                    logger.warning("Не вдалося розпарсити discon-schedule.js")
    except FileNotFoundError:
        logger.info("Файл discon-schedule.js не знайдено")
    
    # Спроба 2: shutdowns.txt (фолбек)
    try:
        with open("shutdowns.txt", "r", encoding="utf-8") as f:
            text = f.read()
            # Витягуємо кирилічні назви (міста)
            candidates = re.findall(r"\b[А-ЯЇЄІ][а-яіїє']{2,}(?:\s+[А-ЯЇЄІ][а-яіїє']{2,})?\b", text)
            freq = {}
            for c in candidates:
                freq[c] = freq.get(c, 0) + 1
            
            # Топ-100 найчастіших назв
            top = sorted(freq.items(), key=lambda x: -x[1])[:100]
            AUTOCOMPLETE_DATA["cities"] = [t[0] for t in top]
            logger.info(f"Завантажено {len(top)} міст з shutdowns.txt")
    except FileNotFoundError:
        logger.warning("Файл shutdowns.txt не знайдено")
        # Дефолтні міста
        AUTOCOMPLETE_DATA["cities"] = [
            "Кременчук", "Горішні Плавні", "Світловодськ",
            "Комсомольськ", "Глобине"
        ]

load_autocomplete_from_files()

# ============ PLAYWRIGHT (ОПТИМІЗОВАНО) ============
class PlaywrightManager:
    """Менеджер для економного використання Playwright"""
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._last_used = None
        self._lock = asyncio.Lock()
    
    async def _ensure_browser(self):
        """Створює браузер якщо потрібно"""
        if self._context and self._last_used:
            # Закриваємо якщо не використовувався 5 хвилин
            if datetime.now() - self._last_used > timedelta(minutes=5):
                await self.close()
        
        if not self._context:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    "--disable-extensions"
                ]
            )
            self._context = await self._browser.new_context(
                viewport={"width": 800, "height": 600},
                java_script_enabled=True,
                ignore_https_errors=True
            )
            
            # Блокуємо непотрібні ресурси
            async def block_resources(route):
                req = route.request
                if req.resource_type in ("image", "media", "font", "stylesheet"):
                    await route.abort()
                elif any(x in req.url for x in ["analytics", "gtm", "facebook", "doubleclick"]):
                    await route.abort()
                else:
                    await route.continue_()
            
            await self._context.route("**/*", block_resources)
            logger.info("Браузер запущено")
    
    @asynccontextmanager
    async def get_page(self):
        """Контекстний менеджер для отримання сторінки"""
        async with self._lock:
            await self._ensure_browser()
            page = await self._context.new_page()
            self._last_used = datetime.now()
        
        try:
            yield page
        finally:
            await page.close()
    
    async def close(self):
        """Закриває браузер"""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        
        self._context = None
        self._browser = None
        self._playwright = None
        logger.info("Браузер закрито")

pw_manager = PlaywrightManager()

async def fetch_schedule_html(city: str, street: str, house: str) -> str | None:
    """Отримує HTML графіку з сайту"""
    try:
        async with pw_manager.get_page() as page:
            await page.goto("https://www.dtek-krem.com.ua/ua/shutdowns", timeout=PAGE_TIMEOUT)
            
            # Місто
            await page.fill(CITY_SEL, city)
            await asyncio.sleep(0.2)
            
            try:
                await page.wait_for_selector(AUTOCOMPLETE_ITEM, timeout=AUTOCOMPLETE_TIMEOUT)
                items = await page.query_selector_all(AUTOCOMPLETE_ITEM)
                if items:
                    # Шукаємо точний збіг або кликаємо перший
                    for item in items:
                        text = (await item.inner_text()).strip().lower()
                        if city.lower() in text:
                            await item.click()
                            break
                    else:
                        await items[0].click()
            except PWTimeout:
                logger.debug(f"Автокомпліт міста не з'явився для {city}")
            
            # Вулиця
            await page.fill(STREET_SEL, street)
            await asyncio.sleep(0.2)
            
            try:
                await page.wait_for_selector(AUTOCOMPLETE_ITEM, timeout=AUTOCOMPLETE_TIMEOUT)
                items = await page.query_selector_all(AUTOCOMPLETE_ITEM)
                if items:
                    await items[0].click()
            except PWTimeout:
                logger.debug(f"Автокомпліт вулиці не з'явився для {street}")
            
            # Будинок
            await page.fill(HOUSE_SEL, house)
            await asyncio.sleep(0.3)
            
            # Чекаємо результат
            await page.wait_for_selector(RESULT_SELECTOR, timeout=RESULT_TIMEOUT)
            html = await page.inner_html(RESULT_SELECTOR)
            
            return html
    
    except PWTimeout:
        logger.warning(f"Таймаут при отриманні графіку: {city}, {street}, {house}")
        return None
    except Exception as e:
        logger.exception(f"Помилка fetch_schedule_html: {e}")
        return None

async def html_to_png(html: str) -> bytes | None:
    """Конвертує HTML в PNG"""
    try:
        async with pw_manager.get_page() as page:
            content = f"""
            <html>
            <head>
                <meta charset='utf-8'>
                <style>
                    body {{ margin: 20px; font-family: Arial, sans-serif; }}
                </style>
            </head>
            <body>{html}</body>
            </html>
            """
            await page.set_content(content, wait_until="domcontentloaded")
            png = await page.screenshot(full_page=True, type="png")
            return png
    except Exception as e:
        logger.exception(f"Помилка html_to_png: {e}")
        return None

# ============ ДОПОМІЖНІ ФУНКЦІЇ ============
def compute_hash(text: str) -> str:
    """Обчислює SHA256 хеш тексту"""
    normalized = re.sub(r"\s+", " ", text.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

async def send_update_to_user(user_id: int, city: str, street: str, house: str, png: bytes):
    """Надсилає оновлення користувачу"""
    try:
        user = await client.fetch_user(user_id)
        await user.send(
            content=f"🔔 **Оновлення графіку відключень**\n📍 {city}, {street}, {house}",
            file=File(io.BytesIO(png), filename="schedule.png")
        )
        return True
    except discord.Forbidden:
        logger.warning(f"Користувач {user_id} заблокував DM")
        return False
    except discord.NotFound:
        logger.warning(f"Користувач {user_id} не знайдений")
        return False
    except Exception as e:
        logger.error(f"Помилка відправки повідомлення {user_id}: {e}")
        return False

# ============ ВОРКЕР ============
async def worker_loop():
    """Основний цикл перевірки підписок"""
    await init_db()
    logger.info("Воркер запущено")
    
    while True:
        try:
            total = await get_total_subscriptions()
            logger.info(f"Перевірка підписок (всього активних: {total})")
            
            subs = await fetch_n_oldest(MAX_CHECKS_PER_TICK)
            
            if not subs:
                logger.debug("Немає підписок для перевірки")
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                continue
            
            for sub in subs:
                try:
                    # Отримуємо HTML
                    html = await fetch_schedule_html(sub["city"], sub["street"], sub["house"])
                    
                    if not html:
                        await update_subscription_hash(sub["id"], sub["last_hash"] or "", success=False)
                        await asyncio.sleep(2)
                        continue
                    
                    # Обчислюємо хеш
                    current_hash = compute_hash(html)
                    
                    # Якщо є зміни
                    if current_hash != (sub["last_hash"] or ""):
                        logger.info(f"Виявлено зміни для sub_id={sub['id']}: {sub['city']}, {sub['street']}, {sub['house']}")
                        
                        # Генеруємо скріншот
                        png = await html_to_png(html)
                        
                        if png:
                            # Відправляємо користувачу
                            success = await send_update_to_user(
                                sub["discord_user_id"],
                                sub["city"],
                                sub["street"],
                                sub["house"],
                                png
                            )
                            
                            if success:
                                await update_subscription_hash(sub["id"], current_hash)
                                await send_log_message(
                                    f"✅ Оновлення відправлено: {sub['city']}, {sub['street']}, {sub['house']} (sub_id={sub['id']})",
                                    "INFO"
                                )
                            else:
                                await update_subscription_hash(sub["id"], current_hash, success=False)
                        else:
                            await update_subscription_hash(sub["id"], sub["last_hash"] or "", success=False)
                    else:
                        # Без змін
                        await update_subscription_hash(sub["id"], current_hash)
                    
                    # Пауза між перевірками
                    await asyncio.sleep(3)
                
                except Exception as e:
                    logger.exception(f"Помилка при перевірці sub_id={sub['id']}: {e}")
                    await update_subscription_hash(sub["id"], sub["last_hash"] or "", success=False)
        
        except Exception as e:
            logger.exception(f"Критична помилка воркера: {e}")
            await send_log_message(f"❌ Критична помилка воркера: {e}", "ERROR")
        
        # Чекаємо до наступної перевірки
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# ============ SLASH-КОМАНДИ ============
async def city_autocomplete(interaction: discord.Interaction, current: str):
    """Автокомпліт для міст"""
    cities = AUTOCOMPLETE_DATA.get("cities", [])
    cur_lower = current.lower()
    
    matches = [
        app_commands.Choice(name=city, value=city)
        for city in cities
        if cur_lower in city.lower()
    ][:25]
    
    return matches

async def street_autocomplete(interaction: discord.Interaction, current: str):
    """Автокомпліт для вулиць"""
    try:
        # Отримуємо обране місто
        city = interaction.namespace.city
        if not city:
            return []
        
        streets = AUTOCOMPLETE_DATA.get("streets_by_city", {}).get(city, [])
        cur_lower = current.lower()
        
        matches = [
            app_commands.Choice(name=street, value=street)
            for street in streets
            if cur_lower in street.lower()
        ][:25]
        
        return matches
    except:
        return []

@tree.command(name="start", description="Підписатися на оновлення графіку відключень")
@app_commands.describe(
    city="Населений пункт (наприклад: Кременчук)",
    street="Назва вулиці",
    house="Номер будинку"
)
@app_commands.autocomplete(city=city_autocomplete, street=street_autocomplete)
async def cmd_start(interaction: discord.Interaction, city: str, street: str, house: str):
    await interaction.response.defer(ephemeral=True)
    
    try:
        city = city.strip()
        street = street.strip()
        house = house.strip()
        
        if not city or not street or not house:
            await interaction.followup.send("❌ Будь ласка, заповніть всі поля", ephemeral=True)
            return
        
        success = await add_subscription(interaction.user.id, city, street, house)
        
        if success:
            interval_min = CHECK_INTERVAL_SECONDS // 60
            await interaction.followup.send(
                f"✅ **Підписку створено!**\n"
                f"📍 Адреса: **{city}, {street}, {house}**\n"
                f"⏱️ Перевірка кожні {interval_min} хв\n"
                f"📬 Повідомлення надходитимуть у приватні повідомлення",
                ephemeral=True
            )
            await send_log_message(
                f"➕ Нова підписка: {interaction.user} ({interaction.user.id})\n"
                f"📍 {city}, {street}, {house}",
                "INFO"
            )
        else:
            await interaction.followup.send(
                "⚠️ Ця адреса вже додана до ваших підписок",
                ephemeral=True
            )
    
    except Exception as e:
        logger.exception(f"Помилка команди /start: {e}")
        await interaction.followup.send(
            "❌ Виникла помилка. Спробуйте пізніше.",
            ephemeral=True
        )

@tree.command(name="stop", description="Відписатися від усіх оновлень")
async def cmd_stop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    try:
        count = await remove_subscriptions_for_user(interaction.user.id)
        
        if count > 0:
            await interaction.followup.send(
                f"✅ Видалено **{count}** підписок\n"
                f"Для нових підписок використовуйте `/start`",
                ephemeral=True
            )
            await send_log_message(
                f"➖ Користувач відписався: {interaction.user} ({interaction.user.id})\n"
                f"Видалено підписок: {count}",
                "INFO"
            )
        else:
            await interaction.followup.send(
                "ℹ️ У вас немає активних підписок",
                ephemeral=True
            )
    
    except Exception as e:
        logger.exception(f"Помилка команди /stop: {e}")
        await interaction.followup.send(
            "❌ Виникла помилка. Спробуйте пізніше.",
            ephemeral=True
        )

@tree.command(name="list", description="Переглянути мої підписки")
async def cmd_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    try:
        subs = await get_user_subscriptions(interaction.user.id)
        
        if not subs:
            await interaction.followup.send(
                "ℹ️ У вас немає активних підписок\n"
                "Використайте `/start` для додавання адреси",
                ephemeral=True
            )
            return
        
        lines = ["📋 **Ваші підписки:**\n"]
        for i, sub in enumerate(subs, 1):
            lines.append(f"{i}. {sub['city']}, {sub['street']}, {sub['house']}")
        
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    
    except Exception as e:
        logger.exception(f"Помилка команди /list: {e}")
        await interaction.followup.send(
            "❌ Виникла помилка. Спробуйте пізніше.",
            ephemeral=True
        )

@tree.command(name="help", description="Довідка про бота")
async def cmd_help(interaction: discord.Interaction):
    help_text = """
**📖 Довідка про бота моніторингу відключень ДТЕК**

**Команди:**
• `/start` — Підписатися на адресу
• `/stop` — Відписатися від усіх оновлень
• `/list` — Переглянути мої підписки
• `/help` — Ця довідка

**Як це працює:**
1. Підпишіться на адресу через `/start`
2. Бот автоматично перевіряє графік кожні кілька хвилин
3. При змінах ви отримаєте повідомлення в DM зі скріншотом

**Підказки:**
• Вводьте назви українською (кирилицею)
• Використовуйте автопідказки при введенні
• Переконайтеся що у вас відкриті приватні повідомлення

**Підтримка:** Натисніть 👎 під повідомленням бота для звіту про проблему
    """
    await interaction.response.send_message(help_text.strip(), ephemeral=True)

@tree.command(name="stats", description="Статистика бота (тільки для адміністраторів)")
async def cmd_stats(interaction: discord.Interaction):
    # Перевіряємо права адміністратора
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ Ця команда доступна тільки адміністраторам",
            ephemeral=True
        )
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        total = await get_total_subscriptions()
        
        async with db_pool.acquire() as conn:
            users = await conn.fetchval("SELECT COUNT(DISTINCT discord_user_id) FROM subscriptions")
            errors = await conn.fetchval("SELECT COUNT(*) FROM subscriptions WHERE error_count >= 5")
        
        stats_text = f"""
**📊 Статистика бота**

👥 Користувачів: **{users}**
📍 Активних підписок: **{total}**
⚠️ Проблемних адрес: **{errors}**
⏱️ Інтервал перевірки: **{CHECK_INTERVAL_SECONDS // 60} хв**
🔄 Адрес за раз: **{MAX_CHECKS_PER_TICK}**
        """
        
        await interaction.followup.send(stats_text.strip(), ephemeral=True)
    
    except Exception as e:
        logger.exception(f"Помилка команди /stats: {e}")
        await interaction.followup.send(
            "❌ Виникла помилка при отриманні статистики",
            ephemeral=True
        )

# ============ ПОДІЇ DISCORD ============
@client.event
async def on_ready():
    logger.info(f"✅ Бот увімкнено: {client.user} (ID: {client.user.id})")
    
    # Додаємо Discord log handler
    discord_handler = DiscordLogHandler()
    discord_handler.setLevel(logging.WARNING)
    discord_handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(discord_handler)
    
    # Синхронізуємо команди
    try:
        synced = await tree.sync()
        logger.info(f"Синхронізовано {len(synced)} команд")
    except Exception as e:
        logger.exception(f"Помилка синхронізації команд: {e}")
    
    # Запускаємо воркер
    client.loop.create_task(worker_loop())
    
    # Повідомляємо про старт
    await send_log_message(
        f"🚀 **Бот запущено**\n"
        f"👤 Користувач: {client.user}\n"
        f"🆔 ID: {client.user.id}\n"
        f"📊 Guild: {LOG_GUILD_ID}\n"
        f"📝 Log канал: {LOG_CHANNEL_ID}",
        "INFO"
    )

@client.event
async def on_command_error(interaction: discord.Interaction, error):
    """Обробка помилок команд"""
    logger.error(f"Помилка команди від {interaction.user}: {error}")
    
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "❌ Виникла помилка при виконанні команди",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ Виникла помилка при виконанні команди",
                ephemeral=True
            )
    except:
        pass

# ============ GRACEFUL SHUTDOWN ============
async def shutdown():
    """Коректне завершення роботи"""
    logger.info("Завершення роботи бота...")
    
    try:
        # Закриваємо браузер
        await pw_manager.close()
        
        # Закриваємо пул БД
        if db_pool:
            await db_pool.close()
        
        # Закриваємо Discord з'єднання
        await client.close()
        
        logger.info("Бот завершив роботу")
    except Exception as e:
        logger.exception(f"Помилка при завершенні: {e}")

# ============ MAIN ============
async def main():
    """Головна функція запуску"""
    if not DISCORD_TOKEN or not DATABASE_URL:
        logger.error("❌ Відсутні обов'язкові змінні: DISCORD_TOKEN або DATABASE_URL")
        raise SystemExit(1)
    
    try:
        # Ініціалізуємо БД
        await init_db()
        
        # Запускаємо бота
        async with client:
            await client.start(DISCORD_TOKEN)
    except KeyboardInterrupt:
        logger.info("Отримано сигнал зупинки")
    except Exception as e:
        logger.exception(f"Критична помилка: {e}")
        await send_log_message(f"💀 Критична помилка: {e}", "ERROR")
    finally:
        await shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Програму зупинено користувачем")