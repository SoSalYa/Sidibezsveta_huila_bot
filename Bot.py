# ===== PART 1: imports & init =====
import os
import sys
os.environ["DISCORD_NO_AUDIO"] = "1"

import io
import re
import time
import json
import threading
import secrets
import asyncio
from datetime import datetime, timedelta
from functools import wraps

# HTTP / parsing / maps
import requests
import requests_cache
from bs4 import BeautifulSoup

# try import googlemaps (may be missing)
try:
    import googlemaps
except Exception:
    googlemaps = None

# Discord / Flask / DB
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, render_template, jsonify, request, session, redirect, url_for

# Postgres
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

# Optional selenium fallback (import only when used)
# from selenium import webdriver
# from selenium.webdriver.common.by import By
# ...

# ---------- Configuration ----------
# HTTP cache to reduce hits to DTEK (expire_after seconds)
REQUESTS_CACHE_EXPIRE = int(os.getenv('REQUESTS_CACHE_EXPIRE', 1800))  # default 30 min
requests_cache.install_cache('dtek_cache', backend='sqlite', expire_after=REQUESTS_CACHE_EXPIRE)

# Google Maps client (optional — for autocomplete)
GOOGLE_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY')
gmaps = None
if GOOGLE_API_KEY and googlemaps:
    try:
        gmaps = googlemaps.Client(key=GOOGLE_API_KEY)
        print("✅ Google Maps client initialized")
    except Exception as e:
        print(f"⚠️ Google Maps init error: {e}")
else:
    if not GOOGLE_API_KEY:
        print("⚠️ GOOGLE_MAPS_API_KEY not set — autocomplete disabled")
    else:
        print("⚠️ googlemaps package not installed — autocomplete disabled")

# Selenium fallback toggle (if site needs JS rendering)
USE_SELENIUM = os.getenv('USE_SELENIUM', 'false').lower() in ('1','true','yes')

# Discord & Flask settings
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(32))
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'changeme123')

# Postgres pool
DATABASE_URL = os.getenv('DATABASE_URL')
db_pool = None

def init_db_pool():
    global db_pool
    if db_pool:
        return
    try:
        db_pool = SimpleConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL)
        print("✅ DB pool created")
    except Exception as e:
        print(f"❌ DB pool error: {e}")

def get_db_connection():
    return db_pool.getconn()

def release_db_connection(conn):
    db_pool.putconn(conn)

# ===== end PART 1 =====
# ===== PART 2: core functions (DB helpers, geocode, parser) =====

# ---------- DB init (create tables) ----------
def init_database():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                discord_id BIGINT UNIQUE NOT NULL,
                discord_username TEXT,
                discord_avatar TEXT,
                city TEXT NOT NULL,
                street TEXT NOT NULL,
                house_number TEXT NOT NULL,
                latitude FLOAT,
                longitude FLOAT,
                last_schedule TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS outage_notifications (
                id SERIAL PRIMARY KEY,
                discord_id BIGINT NOT NULL,
                outage_time TIMESTAMP NOT NULL,
                notified BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_discord_id ON users(discord_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_discord_id ON outage_notifications(discord_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_notified ON outage_notifications(notified)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_outage_time ON outage_notifications(outage_time)")
        conn.commit()
        cur.close()
        print("✅ Database ready")
    except Exception as e:
        print(f"❌ init_database error: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            release_db_connection(conn)

# ---------- Geocoding fallback (geopy) ----------
def geocode_address(city, street, house_number):
    try:
        from geopy.geocoders import Nominatim
        geolocator = Nominatim(user_agent="power_outage_bot")
        address = f"{house_number} {street}, {city}, Ukraine"
        loc = geolocator.geocode(address, timeout=10)
        if loc:
            return loc.latitude, loc.longitude
        return None, None
    except Exception as e:
        print(f"geocode error: {e}")
        return None, None

# ---------- Parse outage times helper ----------
def parse_outage_times(text):
    times = set()
    # interval 10:00-12:00
    for a,b in re.findall(r'(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})', text):
        times.add(a)
    # "з 10:00 до 12:00"
    for a,b in re.findall(r'з\s*(\d{1,2}:\d{2})\s*до\s*(\d{1,2}:\d{2})', text, flags=re.IGNORECASE):
        times.add(a)
    # standalone times
    for t in re.findall(r'\b(?:о\s*)?(\d{1,2}:\d{2})\b', text):
        times.add(t)
    normalized = []
    for t in times:
        try:
            hh, mm = map(int, t.split(':'))
            normalized.append(f"{hh:02d}:{mm:02d}")
        except:
            pass
    return sorted(set(normalized))

# ---------- get_outage_schedule: requests + BS, selenium fallback optional ----------
def get_outage_schedule(city, street, house_number):
    """
    Повертає dict {'schedule': str, 'outage_times': [str,...]}
    Стратегія: requests -> search useful selectors -> table parsing -> time regex.
    Якщо сторінка рендериться JS і USE_SELENIUM==True — робить fallback через Selenium.
    """
    try:
        base_url = "https://www.dtek-krem.com.ua/ua/shutdowns"
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; power_outage_bot/1.0)'}
        params = {'city': city, 'street': street, 'house': house_number}
        resp = requests.get(base_url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, 'lxml')

        # 1. try selectors likely containing schedule
        selectors = [
            '[class*="schedule"]',
            '[id*="schedule"]',
            '[class*="shutdown"]',
            '[id*="shutdown"]',
            '.result',
            '.outage-list',
            '.table',
            '.card'
        ]
        parts = []
        for sel in selectors:
            for el in soup.select(sel):
                txt = el.get_text(separator="\n", strip=True)
                if txt and len(txt) > 30:
                    parts.append(txt)

        # 2. if no parts, try the first meaningful table
        if not parts:
            table = soup.find('table')
            if table:
                rows = []
                for tr in table.find_all('tr'):
                    cols = [td.get_text(strip=True) for td in tr.find_all(['th','td'])]
                    if cols:
                        rows.append(cols)
                if rows:
                    md = []
                    md.append("| " + " | ".join(rows[0]) + " |")
                    md.append("| " + " | ".join("---" for _ in rows[0]) + " |")
                    for r in rows[1:]:
                        md.append("| " + " | ".join(r) + " |")
                    parts.append("```\n" + "\n".join(md) + "\n```")

        # 3. if still empty, extract time patterns from body
        if not parts:
            body = soup.get_text(separator="\n", strip=True)
            times = re.findall(r'\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}', body)
            if times:
                parts.append("📋 Знайдені часи відключень:\n" + "\n".join(f"• {t}" for t in times))

        # 4. selenium fallback (optional)
        if not parts and USE_SELENIUM:
            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC
                chrome_options = Options()
                chrome_options.add_argument('--headless')
                chrome_options.add_argument('--no-sandbox')
                chrome_options.add_argument('--disable-dev-shm-usage')
                chrome_options.add_argument('--disable-gpu')
                chrome_options.add_argument('--window-size=1920,1080')
                # you may need to set chrome_options.binary_location = '/usr/bin/chromium' in some hosts
                driver = webdriver.Chrome(options=chrome_options)
                driver.get(base_url)
                wait = WebDriverWait(driver, 20)
                time.sleep(2)
                # try to fill form if inputs present
                try:
                    city_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input')))
                    city_input.clear()
                    city_input.send_keys(city)
                    time.sleep(1)
                except:
                    pass
                time.sleep(2)
                body_text = driver.find_element(By.TAG_NAME, 'body').text
                times = re.findall(r'\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}', body_text)
                if times:
                    parts.append("📋 (selenium) Знайдені часи:\n" + "\n".join(f"• {t}" for t in times))
                driver.quit()
            except Exception as e:
                print(f"selenium fallback error: {e}")

        # 5. if still nothing -> return template
        if not parts:
            return {
                'schedule': "⚠️ Графік відключень не знайдено або адреса не обслуговується ДТЕК.\nДеталізований графік: (його немає)",
                'outage_times': []
            }

        schedule_text = "\n\n".join(parts).strip()
        outage_times = parse_outage_times(schedule_text)
        return {'schedule': schedule_text, 'outage_times': outage_times}

    except Exception as e:
        print(f"get_outage_schedule error: {e}")
        return {'schedule': f"❌ Помилка при отриманні даних: {e}", 'outage_times': []}

# ---------- DB helper wrappers (save/get/update user, notifications) ----------
def save_user_address(discord_id, city, street, house_number, username=None, avatar=None):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        lat, lon = geocode_address(city, street, house_number)
        cur.execute("SELECT id FROM users WHERE discord_id = %s", (discord_id,))
        existing = cur.fetchone()
        if existing:
            cur.execute("""
                UPDATE users SET city=%s, street=%s, house_number=%s, latitude=%s, longitude=%s,
                    discord_username=%s, discord_avatar=%s, updated_at=NOW()
                WHERE discord_id=%s
            """, (city, street, house_number, lat, lon, username, avatar, discord_id))
        else:
            cur.execute("""
                INSERT INTO users (discord_id, city, street, house_number, latitude, longitude, discord_username, discord_avatar)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (discord_id, city, street, house_number, lat, lon, username, avatar))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"save_user_address error: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

def get_user_address(discord_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE discord_id = %s", (discord_id,))
        res = cur.fetchone()
        cur.close()
        return dict(res) if res else None
    except Exception as e:
        print(f"get_user_address error: {e}")
        return None
    finally:
        if conn:
            release_db_connection(conn)

def update_user_schedule(discord_id, schedule):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET last_schedule = %s, updated_at = NOW() WHERE discord_id = %s", (schedule, discord_id))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"update_user_schedule error: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

def get_all_users():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users")
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"get_all_users error: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)

def save_outage_notification(discord_id, outage_time):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO outage_notifications (discord_id, outage_time, notified) VALUES (%s, %s, false)", (discord_id, outage_time))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"save_outage_notification error: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

def get_pending_notifications():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM outage_notifications WHERE notified = FALSE ORDER BY outage_time")
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"get_pending_notifications error: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)

def mark_notification_sent(notif_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE outage_notifications SET notified = TRUE WHERE id = %s", (notif_id,))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"mark_notification_sent error: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

def delete_old_notifications(hours=24):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM outage_notifications WHERE outage_time < NOW() - INTERVAL '%s hours'", (hours,))
        conn.commit()
        cnt = cur.rowcount
        cur.close()
        return cnt
    except Exception as e:
        print(f"delete_old_notifications error: {e}")
        if conn:
            conn.rollback()
        return 0
    finally:
        if conn:
            release_db_connection(conn)

# ===== end PART 2 =====
# ===== PART 3: bot commands, autocomplete, tasks, run =====

# ---------- Flask routes (admin panel) ----------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == ADMIN_PASSWORD:
            session['authenticated'] = True
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error='Неправильний пароль!')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/api/users')
@login_required
def api_users():
    users = get_all_users()
    out = []
    for u in users:
        if u.get('latitude') and u.get('longitude'):
            out.append({
                'id': u['id'],
                'discord_id': u['discord_id'],
                'username': u.get('discord_username'),
                'city': u['city'],
                'street': u['street'],
                'house': u['house_number'],
                'latitude': u['latitude'],
                'longitude': u['longitude'],
                'last_schedule': u.get('last_schedule')
            })
    return jsonify(out)

@app.route('/api/stats')
@login_required
def api_stats():
    users = get_all_users()
    notifications = get_pending_notifications()
    return jsonify({
        'total_users': len(users),
        'pending_notifications': len(notifications),
        'users_with_coords': len([u for u in users if u.get('latitude') and u.get('longitude')])
    })

# ---------- On ready ----------
@bot.event
async def on_ready():
    print(f"🤖 {bot.user} запущено")
    init_db_pool()
    init_database()
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"❌ sync error: {e}")
    check_schedule_updates.start()
    check_upcoming_outages.start()
    print("✅ Tasks started")

# ---------- Slash command and autocomplete ----------
@bot.tree.command(name="колисвітло", description="Перевірка графіка відключень електроенергії")
@app_commands.describe(city="Місто", street="Вулиця", house="Номер будинку")
async def slash_check_power(interaction: discord.Interaction, city: str = None, street: str = None, house: str = None):
    await interaction.response.defer()
    discord_id = interaction.user.id
    username = str(interaction.user)
    avatar = str(interaction.user.avatar.url) if interaction.user.avatar else None

    if not city or not street or not house:
        data = get_user_address(discord_id)
        if data:
            city = data['city']; street = data['street']; house = data['house_number']
            await interaction.followup.send(f"📍 Використовую збережену адресу: {city}, {street}, {house}")
        else:
            await interaction.followup.send("❌ Адреса не знайдена, вкажи параметри.")
            return
    else:
        save_user_address(discord_id, city, street, house, username, avatar)
        await interaction.followup.send("✅ Адреса збережена")

    await interaction.followup.send("🔍 Перевіряю графік...")

    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, get_outage_schedule, city, street, house)

    schedule = res.get('schedule', '')
    outage_times = res.get('outage_times', [])

    update_user_schedule(discord_id, schedule)

    # schedule may be long
    if schedule and len(schedule) > 1900:
        await interaction.followup.send(file=discord.File(fp=io.BytesIO(schedule.encode('utf-8')), filename='schedule.txt'))
    else:
        embed = discord.Embed(title="⚡ Графік відключень електроенергії", description=schedule or "Немає деталей", color=discord.Color.blue())
        embed.add_field(name="📍 Адреса", value=f"{city}, {street}, {house}", inline=False)
        embed.set_footer(text="Дані отримані автоматично")
        await interaction.followup.send(embed=embed)

    # schedule -> extract times and save notifications
    for t in outage_times:
        try:
            now = datetime.now()
            hh, mm = map(int, t.split(':'))
            ot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if ot < now:
                ot += timedelta(days=1)
            save_outage_notification(discord_id, ot)
        except Exception:
            pass

# Autocomplete handlers
@slash_check_power.autocomplete('city')
async def city_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    if gmaps and current:
        try:
            preds = gmaps.places_autocomplete(input_text=current, components={'country':['UA']}, types="(regions)")
            for p in preds[:25]:
                desc = p.get('description')
                if desc:
                    choices.append(app_commands.Choice(name=desc, value=desc))
        except Exception as e:
            print(f"city autocomplete error: {e}")
    return choices[:25]

@slash_check_power.autocomplete('street')
async def street_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    selected_city = getattr(interaction.namespace, 'city', None)
    if gmaps and current and selected_city:
        try:
            q = f"{current}, {selected_city}"
            preds = gmaps.places_autocomplete(input_text=q, components={'country':['UA']}, types="address")
            for p in preds[:25]:
                choices.append(app_commands.Choice(name=p.get('description'), value=p.get('description')))
        except Exception as e:
            print(f"street autocomplete error: {e}")
    return choices[:25]

@slash_check_power.autocomplete('house')
async def house_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    sel_city = getattr(interaction.namespace, 'city', None)
    sel_street = getattr(interaction.namespace, 'street', None)
    if sel_city and sel_street:
        base = f"{sel_street}, {sel_city}"
        examples = []
        if current and any(ch.isdigit() for ch in current):
            examples.append(f"{current} {sel_street}, {sel_city}")
        examples += [f"1 {base}", f"2 {base}", f"10 {base}"]
        for ex in examples[:25]:
            choices.append(app_commands.Choice(name=ex, value=ex))
    return choices[:25]

# ---------- Text-command compatibility (optional) ----------
@bot.command(name='колисвітло')
async def check_power_cmd(ctx, city: str = None, street: str = None, house: str = None):
    # keep similar behavior as slash
    discord_id = ctx.author.id
    username = str(ctx.author)
    avatar = str(ctx.author.avatar.url) if ctx.author.avatar else None

    if not city or not street or not house:
        data = get_user_address(discord_id)
        if data:
            city = data['city']; street = data['street']; house = data['house_number']
            await ctx.send(f"📍 Використовую збережену адресу: {city}, {street}, {house}")
        else:
            await ctx.send("❌ Адреса не знайдена. Використай /колисвітло або вкажи параметри.")
            return
    else:
        save_user_address(discord_id, city, street, house, username, avatar)
        await ctx.send("✅ Адреса збережена")

    await ctx.send("🔍 Перевіряю графік...")
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, get_outage_schedule, city, street, house)
    schedule = res.get('schedule','')
    outage_times = res.get('outage_times',[])
    if schedule and len(schedule) > 1900:
        await ctx.send(file=discord.File(fp=io.BytesIO(schedule.encode('utf-8')), filename='schedule.txt'))
    else:
        embed = discord.Embed(title="⚡ Графік відключень", description=schedule or "Немає деталей", color=discord.Color.blue())
        embed.add_field(name="📍 Адреса", value=f"{city}, {street}, {house}", inline=False)
        await ctx.send(embed=embed)

# ---------- Background tasks ----------
@tasks.loop(minutes=30)
async def check_schedule_updates():
    try:
        users = get_all_users()
        for u in users:
            discord_id = u['discord_id']
            city = u['city']; street = u['street']; house = u['house_number']
            old = u.get('last_schedule','')
            res = await asyncio.get_event_loop().run_in_executor(None, get_outage_schedule, city, street, house)
            new = res.get('schedule','')
            if new and old and new != old:
                update_user_schedule(discord_id, new)
                try:
                    user_obj = await bot.fetch_user(discord_id)
                    embed = discord.Embed(title="🔔 Графік оновився", description=new, color=discord.Color.orange())
                    embed.add_field(name="📍 Адреса", value=f"{city}, {street}, {house}", inline=False)
                    await user_obj.send(embed=embed)
                except Exception as e:
                    print(f"notify user error: {e}")
            await asyncio.sleep(1)
    except Exception as e:
        print(f"check_schedule_updates error: {e}")

@tasks.loop(minutes=5)
async def check_upcoming_outages():
    try:
        now = datetime.now()
        notify_at = now + timedelta(minutes=30)
        notifs = get_pending_notifications()
        for n in notifs:
            ot = n['outage_time']
            if now < ot <= notify_at:
                discord_id = n['discord_id']
                try:
                    user = await bot.fetch_user(discord_id)
                    minutes = int((ot - now).total_seconds() / 60)
                    embed = discord.Embed(title="⚠️ Попередження про відключення", description=f"Через **{minutes} хв.**\n🕐 {ot.strftime('%H:%M')}", color=discord.Color.red())
                    ud = get_user_address(discord_id)
                    if ud:
                        embed.add_field(name="📍 Адреса", value=f"{ud['city']}, {ud['street']}, {ud['house_number']}", inline=False)
                    await user.send(embed=embed)
                    mark_notification_sent(n['id'])
                except Exception as e:
                    print(f"send upcoming outage error: {e}")
        deleted = delete_old_notifications(24)
        if deleted:
            print(f"Deleted {deleted} old notifications")
    except Exception as e:
        print(f"check_upcoming_outages error: {e}")

# ---------- Run helpers ----------
def run_bot():
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    if not TOKEN:
        print("❌ DISCORD_BOT_TOKEN not set")
        return
    bot.run(TOKEN)

def run_flask():
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

# ---------- Main entry ----------
if __name__ == '__main__':
    # start bot in thread and run flask in main thread
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    run_flask()

# ===== end PART 3 =====