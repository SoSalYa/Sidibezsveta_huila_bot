import os
import sys
os.environ["DISCORD_NO_AUDIO"] = "1"

import types
audioop = types.ModuleType("audioop")
sys.modules["audioop"] = audioop

import discord
from discord.ext import commands, tasks
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from functools import wraps
import asyncio
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool
import re
import threading
import secrets
import time

# Налаштування Flask
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(32))

# Пароль для доступу до сайту
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'changeme123')

# Налаштування бота
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)

# Підключення до PostgreSQL
DATABASE_URL = os.getenv('DATABASE_URL')

# Пул з'єднань для оптимізації
db_pool = None

def init_db_pool():
    """Ініціалізація пулу з'єднань"""
    global db_pool
    try:
        db_pool = SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=DATABASE_URL
        )
        print("✅ Пул з'єднань з БД створено")
    except Exception as e:
        print(f"❌ Помилка створення пулу з'єднань: {e}")

def get_db_connection():
    """Отримання з'єднання з пулу"""
    return db_pool.getconn()

def release_db_connection(conn):
    """Повернення з'єднання в пул"""
    db_pool.putconn(conn)

# Ініціалізація бази даних
def init_database():
    """Створення таблиць якщо їх немає"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Таблиця користувачів
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
        
        # Таблиця сповіщень про відключення
        cur.execute("""
            CREATE TABLE IF NOT EXISTS outage_notifications (
                id SERIAL PRIMARY KEY,
                discord_id BIGINT NOT NULL,
                outage_time TIMESTAMP NOT NULL,
                notified BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        # Індекси
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_discord_id ON users(discord_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_notifications_discord_id ON outage_notifications(discord_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_notifications_notified ON outage_notifications(notified)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_notifications_outage_time ON outage_notifications(outage_time)
        """)
        
        conn.commit()
        cur.close()
        print("✅ Таблиці бази даних готові")
    except Exception as e:
        print(f"❌ Помилка ініціалізації БД: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            release_db_connection(conn)

# Функція для геокодування адреси
def geocode_address(city, street, house_number):
    """Отримання координат за адресою"""
    try:
        from geopy.geocoders import Nominatim
        geolocator = Nominatim(user_agent="power_outage_bot")
        
        address = f"{house_number} {street}, {city}, Ukraine"
        location = geolocator.geocode(address, timeout=10)
        
        if location:
            return location.latitude, location.longitude
        return None, None
    except Exception as e:
        print(f"Помилка геокодування: {e}")
        return None, None

# Функція для отримання графіка відключень
def get_outage_schedule(city, street, house_number):
    driver = None
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        
        # Для Render.com - вказуємо шлях до chromium
        chrome_options.binary_location = '/usr/bin/chromium'
        
        driver = webdriver.Chrome(options=chrome_options)
        driver.get('https://www.dtek-oem.com.ua/ua/shutdowns')
        
        wait = WebDriverWait(driver, 15)
        
        # Заповнення форми
        city_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[placeholder*="населений пункт"], input[name="city"]')))
        city_input.clear()
        city_input.send_keys(city)
        time.sleep(2)
        
        street_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[placeholder*="вулиця"], input[name="street"]')))
        street_input.clear()
        street_input.send_keys(street)
        time.sleep(2)
        
        house_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[placeholder*="будинок"], input[name="house"]')))
        house_input.clear()
        house_input.send_keys(house_number)
        time.sleep(2)
        
        search_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[type="submit"], button:contains("Пошук")')))
        search_button.click()
        
        time.sleep(5)
        
        schedule_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '.schedule, .outage-schedule, .result')))
        schedule_text = schedule_element.text
        
        # Отримання детальної інформації та часу відключень
        outage_times = []
        try:
            details = driver.find_elements(By.CSS_SELECTOR, '.schedule-item, .outage-info, .time-slot')
            if details:
                schedule_text += "\n\nДетальна інформація:\n"
                for detail in details:
                    text = detail.text
                    schedule_text += text + "\n"
                    outage_times.extend(parse_outage_times(text))
        except:
            pass
        
        driver.quit()
        return {
            'schedule': schedule_text if schedule_text else "Графік відключень не знайдено",
            'outage_times': outage_times
        }
        
    except Exception as e:
        if driver:
            driver.quit()
        return {
            'schedule': f"Помилка при отриманні даних: {str(e)}",
            'outage_times': []
        }

def parse_outage_times(text):
    """Парсить час відключень з тексту"""
    times = []
    patterns = [
        r'(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})',
        r'з\s*(\d{1,2}:\d{2})\s*до\s*(\d{1,2}:\d{2})',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            start_time = match[0]
            times.append(start_time)
    
    return times

# Збереження/оновлення користувача в БД
def save_user_address(discord_id, city, street, house_number, username=None, avatar=None):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Отримуємо координати
        lat, lon = geocode_address(city, street, house_number)
        
        # Перевірка існування користувача
        cur.execute("SELECT id FROM users WHERE discord_id = %s", (discord_id,))
        existing = cur.fetchone()
        
        if existing:
            # Оновлення
            cur.execute("""
                UPDATE users 
                SET city = %s, street = %s, house_number = %s, 
                    latitude = %s, longitude = %s,
                    discord_username = %s, discord_avatar = %s,
                    updated_at = NOW()
                WHERE discord_id = %s
            """, (city, street, house_number, lat, lon, username, avatar, discord_id))
        else:
            # Створення
            cur.execute("""
                INSERT INTO users (discord_id, city, street, house_number, latitude, longitude, discord_username, discord_avatar)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (discord_id, city, street, house_number, lat, lon, username, avatar))
        
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"Помилка збереження адреси: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

# Отримання адреси користувача
def get_user_address(discord_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("SELECT * FROM users WHERE discord_id = %s", (discord_id,))
        result = cur.fetchone()
        
        cur.close()
        return dict(result) if result else None
    except Exception as e:
        print(f"Помилка отримання адреси: {e}")
        return None
    finally:
        if conn:
            release_db_connection(conn)

# Оновлення графіка користувача
def update_user_schedule(discord_id, schedule):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            UPDATE users 
            SET last_schedule = %s, updated_at = NOW()
            WHERE discord_id = %s
        """, (schedule, discord_id))
        
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"Помилка оновлення графіка: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

# Отримання всіх користувачів
def get_all_users():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("SELECT * FROM users")
        results = cur.fetchall()
        
        cur.close()
        return [dict(row) for row in results]
    except Exception as e:
        print(f"Помилка отримання користувачів: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)

# Збереження сповіщення про відключення
def save_outage_notification(discord_id, outage_time):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            INSERT INTO outage_notifications (discord_id, outage_time, notified)
            VALUES (%s, %s, FALSE)
        """, (discord_id, outage_time))
        
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"Помилка збереження сповіщення: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

# Отримання невідправлених сповіщень
def get_pending_notifications():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("""
            SELECT * FROM outage_notifications 
            WHERE notified = FALSE
            ORDER BY outage_time
        """)
        results = cur.fetchall()
        
        cur.close()
        return [dict(row) for row in results]
    except Exception as e:
        print(f"Помилка отримання сповіщень: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)

# Позначити сповіщення як відправлене
def mark_notification_sent(notification_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            UPDATE outage_notifications 
            SET notified = TRUE 
            WHERE id = %s
        """, (notification_id,))
        
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"Помилка оновлення сповіщення: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

# Видалення старих сповіщень
def delete_old_notifications(hours=24):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            DELETE FROM outage_notifications 
            WHERE outage_time < NOW() - INTERVAL '%s hours'
        """, (hours,))
        
        conn.commit()
        deleted = cur.rowcount
        cur.close()
        return deleted
    except Exception as e:
        print(f"Помилка видалення старих сповіщень: {e}")
        if conn:
            conn.rollback()
        return 0
    finally:
        if conn:
            release_db_connection(conn)

# Видалення користувача
def delete_user(discord_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("DELETE FROM outage_notifications WHERE discord_id = %s", (discord_id,))
        cur.execute("DELETE FROM users WHERE discord_id = %s", (discord_id,))
        
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"Помилка видалення користувача: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

# Декоратор для захисту маршрутів
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Flask Routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    """Сторінка входу"""
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
    """Вихід"""
    session.pop('authenticated', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    """Головна сторінка з картою"""
    return render_template('index.html')

@app.route('/api/users')
@login_required
def api_users():
    """API для отримання користувачів з координатами"""
    users = get_all_users()
    
    # Фільтруємо користувачів з координатами
    users_with_coords = []
    for user in users:
        if user.get('latitude') and user.get('longitude'):
            users_with_coords.append({
                'id': user['id'],
                'discord_id': user['discord_id'],
                'username': user.get('discord_username', 'Користувач'),
                'avatar': user.get('discord_avatar', ''),
                'city': user['city'],
                'street': user['street'],
                'house': user['house_number'],
                'latitude': user['latitude'],
                'longitude': user['longitude'],
                'last_schedule': user.get('last_schedule', 'Немає даних')
            })
    
    return jsonify(users_with_coords)

@app.route('/api/stats')
@login_required
def api_stats():
    """API для статистики"""
    users = get_all_users()
    notifications = get_pending_notifications()
    
    return jsonify({
        'total_users': len(users),
        'pending_notifications': len(notifications),
        'users_with_coords': len([u for u in users if u.get('latitude') and u.get('longitude')])
    })

# Discord Bot Commands
@bot.event
async def on_ready():
    print(f'🤖 {bot.user} успішно запущено!')
    init_db_pool()
    init_database()
    check_schedule_updates.start()
    check_upcoming_outages.start()
    print('✅ Бот готовий до роботи')

@bot.command(
    name='колисвітло',
    help='Перевіряє графік відключень електроенергії',
    brief='Перевірка графіка відключень',
    description='Перевіряє графік відключень для вказаної адреси. Приклад: /колисвітло Київ Хрещатик 1'
)
async def check_power(ctx, city: str = None, street: str = None, house: str = None):
    """Перевіряє графік відключень електроенергії"""
    discord_id = ctx.author.id
    username = str(ctx.author)
    avatar = str(ctx.author.avatar.url) if ctx.author.avatar else None
    
    if not city or not street or not house:
        user_data = get_user_address(discord_id)
        if user_data:
            city = user_data['city']
            street = user_data['street']
            house = user_data['house_number']
            await ctx.send(f'📍 Використовую збережену адресу: {city}, вул. {street}, буд. {house}')
        else:
            await ctx.send('❌ Адреса не знайдена! Вкажи адресу: `/колисвітло Київ Хрещатик 1`')
            return
    else:
        if save_user_address(discord_id, city, street, house, username, avatar):
            await ctx.send(f'✅ Адресу збережено!')
    
    await ctx.send(f'🔍 Перевіряю графік відключень...\n⏳ Зачекай трохи...')
    
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, get_outage_schedule, city, street, house)
    
    schedule = result['schedule']
    outage_times = result['outage_times']
    
    update_user_schedule(discord_id, schedule)
    
    for time_str in outage_times:
        try:
            now = datetime.now()
            hour, minute = map(int, time_str.split(':'))
            outage_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            if outage_time < now:
                outage_time += timedelta(days=1)
            
            save_outage_notification(discord_id, outage_time)
        except:
            pass
    
    embed = discord.Embed(
        title="⚡ Графік відключень електроенергії",
        description=schedule,
        color=discord.Color.blue()
    )
    embed.add_field(name="📍 Адреса", value=f"{city}, вул. {street}, буд. {house}", inline=False)
    embed.set_footer(text="Дані з сайту ДТЕК • Автоматична перевірка активна")
    
    await ctx.send(embed=embed)

@bot.command(
    name='моядреса',
    help='Показує збережену адресу',
    brief='Переглянути адресу',
    description='Показує твою збережену адресу для перевірки графіків відключень'
)
async def my_address(ctx):
    """Показує збережену адресу"""
    user_data = get_user_address(ctx.author.id)
    
    if user_data:
        embed = discord.Embed(
            title="📍 Твоя збережена адреса",
            color=discord.Color.green()
        )
        embed.add_field(name="Місто", value=user_data['city'], inline=True)
        embed.add_field(name="Вулиця", value=user_data['street'], inline=True)
        embed.add_field(name="Будинок", value=user_data['house_number'], inline=True)
        embed.set_footer(text=f"Оновлено: {user_data['updated_at']}")
        await ctx.send(embed=embed)
    else:
        await ctx.send('❌ Адреса не знайдена! Використай `/колисвітло` щоб зберегти адресу.')

@bot.command(
    name='видалитиадресу',
    help='Видаляє збережену адресу',
    brief='Видалити адресу',
    description='Видаляє збережену адресу та вимикає автоматичні сповіщення'
)
async def delete_address(ctx):
    """Видаляє збережену адресу"""
    if delete_user(ctx.author.id):
        await ctx.send('✅ Адресу видалено!')
    else:
        await ctx.send('❌ Помилка при видаленні адреси')

@tasks.loop(minutes=30)
async def check_schedule_updates():
    """Перевіряє оновлення графіка для всіх користувачів"""
    try:
        users = get_all_users()
        print(f"🔄 Перевірка оновлень графіка для {len(users)} користувачів...")
        
        for user in users:
            discord_id = user['discord_id']
            city = user['city']
            street = user['street']
            house = user['house_number']
            old_schedule = user.get('last_schedule', '')
            
            result = await asyncio.get_event_loop().run_in_executor(
                None, get_outage_schedule, city, street, house
            )
            new_schedule = result['schedule']
            
            if new_schedule != old_schedule and old_schedule:
                update_user_schedule(discord_id, new_schedule)
                
                try:
                    user_obj = await bot.fetch_user(discord_id)
                    embed = discord.Embed(
                        title="🔔 Графік відключень оновився!",
                        description=new_schedule,
                        color=discord.Color.orange()
                    )
                    embed.add_field(name="📍 Адреса", value=f"{city}, вул. {street}, буд. {house}", inline=False)
                    embed.set_footer(text="Автоматичне сповіщення")
                    
                    await user_obj.send(embed=embed)
                    print(f"✅ Сповіщення надіслано користувачу {discord_id}")
                except Exception as e:
                    print(f"❌ Не вдалося надіслати сповіщення користувачу {discord_id}: {e}")
            
            await asyncio.sleep(5)
            
    except Exception as e:
        print(f"❌ Помилка при перевірці оновлень: {e}")

@tasks.loop(minutes=5)
async def check_upcoming_outages():
    """Сповіщає користувачів за 30 хвилин до відключення"""
    try:
        now = datetime.now()
        notification_time = now + timedelta(minutes=30)

        notifications = get_pending_notifications()
        print(f"⏰ Перевірка {len(notifications)} запланованих сповіщень...")

        for notif in notifications:
            outage_time = notif['outage_time']

            if now < outage_time <= notification_time:
                discord_id = notif['discord_id']

                try:
                    user = await bot.fetch_user(discord_id)
                    user_data = get_user_address(discord_id)

                    time_until = outage_time - now
                    minutes = int(time_until.total_seconds() / 60)

                    embed = discord.Embed(
                        title="⚠️ Попередження про відключення!",
                        description=f"Електроенергію буде відключено через **{minutes} хвилин**\n\n🕐 Час відключення: **{outage_time.strftime('%H:%M')}**",
                        color=discord.Color.red()
                    )

                    if user_data:
                        embed.add_field(
                            name="📍 Адреса",
                            value=f"{user_data['city']}, вул. {user_data['street']}, буд. {user_data['house_number']}",
                            inline=False
                        )

                    embed.set_footer(text="Не забудь зарядити пристрої!")

                    await user.send(embed=embed)
                    mark_notification_sent(notif['id'])
                    print(f"✅ Попередження надіслано користувачу {discord_id}")

                except Exception as e:
                    print(f"❌ Не вдалося надіслати попередження користувачу {discord_id}: {e}")

        deleted = delete_old_notifications(24)
        if deleted > 0:
            print(f"🗑️ Видалено {deleted} старих сповіщень")

    except Exception as e:
        print(f"❌ Помилка при перевірці майбутніх відключень: {e}")

@bot.command(
    name='довідка',
    help='Показує список команд',
    brief='Довідка',
    description='Показує повну довідку по всіх доступних командах бота'
)
async def help_command(ctx):
    """Показує довідку по командах"""
    embed = discord.Embed(
        title="📋 Довідка по командах",
        description="Бот для перевірки графіків відключень електроенергії з автоматичними сповіщеннями",
        color=discord.Color.green()
    )
    embed.add_field(
        name="/колисвітло *місто* *вулиця* *будинок*",
        value="Перевіряє та зберігає адресу. При повторному виклику без параметрів використає збережену адресу.",
        inline=False
    )
    embed.add_field(
        name="/моядреса",
        value="Показує твою збережену адресу",
        inline=False
    )
    embed.add_field(
        name="/видалитиадресу",
        value="Видаляє збережену адресу та вимикає сповіщення",
        inline=False
    )
    embed.add_field(
        name="🔔 Автоматичні сповіщення",
        value="• Сповіщення про зміни в графіку (кожні 30 хв)\n• Попередження за 30 хв до відключення\n• Всі сповіщення надходять в особисті повідомлення",
        inline=False
    )
    embed.set_footer(text="Бот зроблено завдяки вірі в пельмені 🥟")
    await ctx.send(embed=embed)

def run_bot():
    """Запуск Discord бота в окремому потоці"""
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ DISCORD_BOT_TOKEN не встановлено!")

def run_flask():
    """Запуск Flask сервера"""
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    # Запускаємо бота в окремому потоці
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Запускаємо Flask в основному потоці
    run_flask()