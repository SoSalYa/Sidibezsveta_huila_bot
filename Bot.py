import os
import sys
os.environ["DISCORD_NO_AUDIO"] = "1"

import types
audioop = types.ModuleType("audioop")
sys.modules["audioop"] = audioop

import discord
from discord import app_commands
from discord.ext import commands, tasks
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
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Налаштування Flask
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(32))

# Пароль для доступу до сайту
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'changeme123')

# URL API для отримання графіків (буде запущений окремо)
DTEK_API_URL = os.getenv('DTEK_API_URL', 'http://localhost:8000')

# Налаштування бота
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)

# Підключення до PostgreSQL
DATABASE_URL = os.getenv('DATABASE_URL')

# Пул з'єднань
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
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(discord_id, outage_time)
            )
        """)
        
        # Індекси
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_discord_id ON users(discord_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_discord_id ON outage_notifications(discord_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_notified ON outage_notifications(notified)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_outage_time ON outage_notifications(outage_time)")
        
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

# ===== НОВА ФУНКЦІЯ: Виклик API для отримання графіку =====
def get_outage_schedule_from_api(city, street, house_number):
    """
    Отримання графіку відключень через API
    Замість Selenium викликаємо окремий API сервіс
    """
    try:
        # Налаштування retry для стабільності
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504]
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        
        print(f"📡 Викликаю API для {city}, {street}, {house_number}...")
        
        # Виклик API
        response = session.post(
            f"{DTEK_API_URL}/api/schedule",
            json={
                "city": city,
                "street": street,
                "house": house_number
            },
            timeout=180  # 3 хвилини таймаут
        )
        
        if response.status_code == 200:
            data = response.json()
            
            if data.get('success'):
                print("✅ Графік успішно отримано з API")
                return {
                    'schedule': data.get('schedule', 'Немає даних'),
                    'outage_times': data.get('outage_times', [])
                }
            else:
                error = data.get('error', 'Невідома помилка')
                print(f"❌ API повернув помилку: {error}")
                return {
                    'schedule': f"❌ Помилка отримання даних: {error}",
                    'outage_times': []
                }
        else:
            print(f"❌ API повернув код {response.status_code}")
            return {
                'schedule': f"❌ Помилка зв'язку з API (код {response.status_code})",
                'outage_times': []
            }
            
    except requests.exceptions.Timeout:
        print("⏱️ Таймаут запиту до API")
        return {
            'schedule': "⏱️ Таймаут: API не встиг відповісти за 2 хвилини. Спробуйте пізніше.",
            'outage_times': []
        }
    except requests.exceptions.ConnectionError:
        print("🔌 Помилка підключення до API")
        return {
            'schedule': "🔌 Помилка: API сервіс недоступний. Спробуйте пізніше.",
            'outage_times': []
        }
    except Exception as e:
        print(f"❌ Несподівана помилка: {e}")
        return {
            'schedule': f"❌ Помилка: {str(e)}",
            'outage_times': []
        }

# Функції роботи з БД
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
                UPDATE users 
                SET city = %s, street = %s, house_number = %s, 
                    latitude = %s, longitude = %s,
                    discord_username = %s, discord_avatar = %s,
                    updated_at = NOW()
                WHERE discord_id = %s
            """, (city, street, house_number, lat, lon, username, avatar, discord_id))
        else:
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

def save_outage_notification(discord_id, outage_time):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO outage_notifications (discord_id, outage_time, notified)
            VALUES (%s, %s, FALSE)
            ON CONFLICT (discord_id, outage_time) DO NOTHING
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

# Flask декоратор
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
    users = get_all_users()
    notifications = get_pending_notifications()
    return jsonify({
        'total_users': len(users),
        'pending_notifications': len(notifications),
        'users_with_coords': len([u for u in users if u.get('latitude') and u.get('longitude')])
    })

# Discord Bot
@bot.event
async def on_ready():
    print(f'🤖 {bot.user} успішно запущено!')
    init_db_pool()
    init_database()
    
    try:
        synced = await bot.tree.sync()
        print(f'✅ Синхронізовано {len(synced)} slash команд')
    except Exception as e:
        print(f'❌ Помилка синхронізації команд: {e}')
    
    check_schedule_updates.start()
    check_upcoming_outages.start()
    print('✅ Бот готовий до роботи')

# Slash команди
@bot.tree.command(name="колисвітло", description="Перевірка графіка відключень електроенергії")
@app_commands.describe(
    city="Місто (наприклад: Київ)",
    street="Вулиця (наприклад: Хрещатик)",
    house="Номер будинку (наприклад: 1)"
)
async def slash_check_power(interaction: discord.Interaction, city: str = None, street: str = None, house: str = None):
    await interaction.response.defer()
    
    discord_id = interaction.user.id
    username = str(interaction.user)
    avatar = str(interaction.user.avatar.url) if interaction.user.avatar else None
    
    if not city or not street or not house:
        user_data = get_user_address(discord_id)
        if user_data:
            city = user_data['city']
            street = user_data['street']
            house = user_data['house_number']
            await interaction.followup.send(f'📍 Використовую збережену адресу: {city}, вул. {street}, буд. {house}')
        else:
            await interaction.followup.send('❌ Адреса не знайдена! Вкажи адресу через параметри команди.')
            return
    else:
        if save_user_address(discord_id, city, street, house, username, avatar):
            await interaction.followup.send(f'✅ Адресу збережено!')
    
    await interaction.followup.send(f'🔍 Перевіряю графік відключень...\n⏳ Зачекай трохи...')
    
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, get_outage_schedule_from_api, city, street, house)
    
    schedule = result['schedule']
    outage_times = result['outage_times']
    
    update_user_schedule(discord_id, schedule)
    
    for time_str in outage_times:
        try:
            now = datetime.now()
            if ':' in time_str:
                hour, minute = map(int, time_str.split(':'))
            else:
                hour = int(time_str)
                minute = 0
            
            outage_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            if outage_time < now:
                outage_time += timedelta(days=1)
            
            save_outage_notification(discord_id, outage_time)
        except Exception as e:
            print(f"⚠️ Помилка парсингу часу {time_str}: {e}")
    
    embed = discord.Embed(
        title="⚡ Графік відключень електроенергії",
        description=schedule,
        color=discord.Color.blue()
    )
    embed.add_field(name="📍 Адреса", value=f"{city}, вул. {street}, буд. {house}", inline=False)
    embed.set_footer(text="Дані з сайту ДТЕК • Автоматична перевірка активна")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="моядреса", description="Показати збережену адресу")
async def slash_my_address(interaction: discord.Interaction):
    user_data = get_user_address(interaction.user.id)
    
    if user_data:
        embed = discord.Embed(
            title="📍 Твоя збережена адреса",
            color=discord.Color.green()
        )
        embed.add_field(name="Місто", value=user_data['city'], inline=True)
        embed.add_field(name="Вулиця", value=user_data['street'], inline=True)
        embed.add_field(name="Будинок", value=user_data['house_number'], inline=True)
        embed.set_footer(text=f"Оновлено: {user_data['updated_at']}")
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message('❌ Адреса не знайдена! Використай `/колисвітло` щоб зберегти адресу.')

@bot.tree.command(name="видалитиадресу", description="Видалити збережену адресу")
async def slash_delete_address(interaction: discord.Interaction):
    if delete_user(interaction.user.id):
        await interaction.response.send_message('✅ Адресу видалено!')
    else:
        await interaction.response.send_message('❌ Помилка при видаленні адреси')

@bot.tree.command(name="довідка", description="Показати список команд")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📋 Довідка по командам",
        description="Бот для перевірки графіків відключень електроенергії з автоматичними сповіщеннями",
        color=discord.Color.green()
    )
    embed.add_field(
        name="/колисвітло",
        value="Перевіряє та зберігає адресу. Параметри: місто, вулиця, будинок.",
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
        value="• Сповіщення про зміни в графіку (кожні 30 хв)\n• Попередження за 30 хв до відключення",
        inline=False
    )
    embed.set_footer(text="Бот зроблено завдяки вірі в пельмені 🥟")
    await interaction.response.send_message(embed=embed)

# Background tasks
@tasks.loop(minutes=30)
async def check_schedule_updates():
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
                None, get_outage_schedule_from_api, city, street, house
            )
            new_schedule = result['schedule']
            
            # Порівнюємо графіки (ігноруємо дату оновлення)
            old_clean = re.sub(r'ℹ️.*?\n\n', '', old_schedule)
            new_clean = re.sub(r'ℹ️.*?\n\n', '', new_schedule)
            
            if new_clean != old_clean and old_schedule:
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

# Запуск
def run_bot():
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ DISCORD_BOT_TOKEN не встановлено!")

def run_flask():
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    run_flask()
