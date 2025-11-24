import discord
from discord.ext import commands, tasks
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import asyncio
import os
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool
import re

# Настройка бота
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)

# Подключение к PostgreSQL
DATABASE_URL = os.getenv('DATABASE_URL')

# Пул соединений для оптимизации
db_pool = None

def init_db_pool():
    """Инициализация пула соединений"""
    global db_pool
    try:
        db_pool = SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=DATABASE_URL
        )
        print("✅ Пул соединений с БД создан")
    except Exception as e:
        print(f"❌ Ошибка создания пула соединений: {e}")

def get_db_connection():
    """Получение соединения из пула"""
    return db_pool.getconn()

def release_db_connection(conn):
    """Возврат соединения в пул"""
    db_pool.putconn(conn)

# Инициализация базы данных
def init_database():
    """Создание таблиц если их нет"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Таблица пользователей
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                discord_id BIGINT UNIQUE NOT NULL,
                city TEXT NOT NULL,
                street TEXT NOT NULL,
                house_number TEXT NOT NULL,
                last_schedule TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        # Таблица уведомлений об отключениях
        cur.execute("""
            CREATE TABLE IF NOT EXISTS outage_notifications (
                id SERIAL PRIMARY KEY,
                discord_id BIGINT NOT NULL,
                outage_time TIMESTAMP NOT NULL,
                notified BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        # Индексы
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
        print("✅ Таблицы базы данных готовы")
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            release_db_connection(conn)

# Функция для получения графика отключений
def get_outage_schedule(city, street, house_number):
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        
        driver = webdriver.Chrome(options=chrome_options)
        driver.get('https://www.dtek-oem.com.ua/ua/shutdowns')
        
        wait = WebDriverWait(driver, 15)
        
        # Заполнение формы
        city_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[placeholder*="населений пункт"], input[name="city"]')))
        city_input.clear()
        city_input.send_keys(city)
        asyncio.sleep(2)
        
        street_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[placeholder*="вулиця"], input[name="street"]')))
        street_input.clear()
        street_input.send_keys(street)
        asyncio.sleep(2)
        
        house_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[placeholder*="будинок"], input[name="house"]')))
        house_input.clear()
        house_input.send_keys(house_number)
        asyncio.sleep(2)
        
        search_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[type="submit"], button:contains("Пошук")')))
        search_button.click()
        
        asyncio.sleep(5)
        
        schedule_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '.schedule, .outage-schedule, .result')))
        schedule_text = schedule_element.text
        
        # Получение детальной информации и времени отключений
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
        if 'driver' in locals():
            driver.quit()
        return {
            'schedule': f"Помилка при отриманні даних: {str(e)}",
            'outage_times': []
        }

def parse_outage_times(text):
    """Парсит время отключений из текста"""
    times = []
    # Паттерны для поиска времени (например: "14:00-17:00", "з 14:00 до 17:00")
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

# Сохранение/обновление пользователя в БД
def save_user_address(discord_id, city, street, house_number):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Проверка существования пользователя
        cur.execute("SELECT id FROM users WHERE discord_id = %s", (discord_id,))
        existing = cur.fetchone()
        
        if existing:
            # Обновление
            cur.execute("""
                UPDATE users 
                SET city = %s, street = %s, house_number = %s, updated_at = NOW()
                WHERE discord_id = %s
            """, (city, street, house_number, discord_id))
        else:
            # Создание
            cur.execute("""
                INSERT INTO users (discord_id, city, street, house_number)
                VALUES (%s, %s, %s, %s)
            """, (discord_id, city, street, house_number))
        
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"Ошибка сохранения адреса: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

# Получение адреса пользователя
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
        print(f"Ошибка получения адреса: {e}")
        return None
    finally:
        if conn:
            release_db_connection(conn)

# Обновление графика пользователя
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
        print(f"Ошибка обновления графика: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

# Получение всех пользователей
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
        print(f"Ошибка получения пользователей: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)

# Сохранение уведомления об отключении
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
        print(f"Ошибка сохранения уведомления: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

# Получение неотправленных уведомлений
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
        print(f"Ошибка получения уведомлений: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)

# Пометить уведомление как отправленное
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
        print(f"Ошибка обновления уведомления: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

# Удаление старых уведомлений
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
        print(f"Ошибка удаления старых уведомлений: {e}")
        if conn:
            conn.rollback()
        return 0
    finally:
        if conn:
            release_db_connection(conn)

# Удаление пользователя
def delete_user(discord_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Удаляем уведомления пользователя
        cur.execute("DELETE FROM outage_notifications WHERE discord_id = %s", (discord_id,))
        # Удаляем пользователя
        cur.execute("DELETE FROM users WHERE discord_id = %s", (discord_id,))
        
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"Ошибка удаления пользователя: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_db_connection(conn)

@bot.event
async def on_ready():
    print(f'🤖 {bot.user} успішно запущений!')
    init_db_pool()
    init_database()
    check_schedule_updates.start()
    check_upcoming_outages.start()
    print('✅ Бот готовий до роботи')

@bot.command(name='когдасвет')
async def check_power(ctx, city: str = None, street: str = None, house: str = None):
    """
    Перевіряє графік відключень електроенергії
    Використання: /когдасвет *місто* *вулиця* *номер_будинку*
    Або просто: /когдасвет (використає збережену адресу)
    """
    discord_id = ctx.author.id
    
    # Если адрес не указан, проверяем сохраненный
    if not city or not street or not house:
        user_data = get_user_address(discord_id)
        if user_data:
            city = user_data['city']
            street = user_data['street']
            house = user_data['house_number']
            await ctx.send(f'📍 Використовую збережену адресу: {city}, вул. {street}, буд. {house}')
        else:
            await ctx.send('❌ Адреса не знайдена! Вкажи адресу: `/когдасвет Київ Хрещатик 1`')
            return
    else:
        # Сохраняем новый адрес
        if save_user_address(discord_id, city, street, house):
            await ctx.send(f'✅ Адресу збережено!')
    
    await ctx.send(f'🔍 Перевіряю графік відключень...\n⏳ Зачекайте...')
    
    # Получение графика
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, get_outage_schedule, city, street, house)
    
    schedule = result['schedule']
    outage_times = result['outage_times']
    
    # Сохранение графика
    update_user_schedule(discord_id, schedule)
    
    # Сохранение времени отключений для уведомлений
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
    
    # Отправка результата
    embed = discord.Embed(
        title="⚡ Графік відключень електроенергії",
        description=schedule,
        color=discord.Color.blue()
    )
    embed.add_field(name="📍 Адреса", value=f"{city}, вул. {street}, буд. {house}", inline=False)
    embed.set_footer(text="Дані з сайту ДТЕК • Автоматична перевірка активна")
    
    await ctx.send(embed=embed)

@bot.command(name='моядреса')
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
        await ctx.send('❌ Адреса не знайдена! Використай `/когдасвет` щоб зберегти адресу.')

@bot.command(name='видалитиадресу')
async def delete_address(ctx):
    """Видаляє збережену адресу"""
    if delete_user(ctx.author.id):
        await ctx.send('✅ Адресу видалено!')
    else:
        await ctx.send('❌ Помилка при видаленні адреси')

# Фоновая задача проверки обновлений графика (каждые 30 минут)
@tasks.loop(minutes=30)
async def check_schedule_updates():
    """Проверяет обновления графика для всех пользователей"""
    try:
        users = get_all_users()
        print(f"🔄 Перевірка оновлень графіка для {len(users)} користувачів...")
        
        for user in users:
            discord_id = user['discord_id']
            city = user['city']
            street = user['street']
            house = user['house_number']
            old_schedule = user.get('last_schedule', '')
            
            # Получаем новый график
            result = await asyncio.get_event_loop().run_in_executor(
                None, get_outage_schedule, city, street, house
            )
            new_schedule = result['schedule']
            
            # Если график изменился
            if new_schedule != old_schedule and old_schedule:
                update_user_schedule(discord_id, new_schedule)
                
                # Отправляем уведомление в ЛС
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
                    print(f"✅ Уведомление отправлено пользователю {discord_id}")
                except Exception as e:
                    print(f"❌ Не удалось отправить уведомление пользователю {discord_id}: {e}")
            
            await asyncio.sleep(5)
            
    except Exception as e:
        print(f"❌ Ошибка при проверке обновлений: {e}")

# Фоновая задача проверки предстоящих отключений (каждые 5 минут)
@tasks.loop(minutes=5)
async def check_upcoming_outages():
    """Уведомляет пользователей за 30 минут до отключения"""
    try:
        now = datetime.now()
        notification_time = now + timedelta(minutes=30)
        
        notifications = get_pending_notifications()
        print(f"⏰ Перевірка {len(notifications)} запланованих сповіщень...")
        
        for notif in notifications:
            outage_time = notif['outage_time']
            
            # Если до отключения осталось менее 35 минут и более 25 минут
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
                    print(f"✅ Предупреждение отправлено пользователю {discord_id}")
                    
                except Exception as e:
                    print(f"❌ Не удалось отправить предупреждение пользователю {discord_id}: {e}")
        
        # Удаляем старые уведомления
        deleted = delete_old_notifications(24)
        if deleted > 0:
            print(f"🗑️ Удалено {deleted} старых уведомлений")
        
    except Exception as e:
        print(f"❌ Ошибка при проверке предстоящих отключений: {e}")

@bot.command(name='help')
async def help_command(ctx):
    """Показує допомогу по командах"""
    embed = discord.Embed(
        title="📋 Довідка по командах",
        description="Бот для перевірки графіків відключень електроенергії з автоматичними сповіщеннями",
        color=discord.Color.green()
    )
    embed.add_field(
        name="/когдасвет *місто* *вулиця* *будинок*",
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
    await ctx.send(embed=embed)

@bot.command(name='статистика')
@commands.has_permissions(administrator=True)
async def stats(ctx):
    """Статистика бота (только для администраторов)"""
    users = get_all_users()
    notifications = get_pending_notifications()
    
    embed = discord.Embed(
        title="📊 Статистика бота",
        color=discord.Color.blue()
    )
    embed.add_field(name="👥 Користувачів", value=str(len(users)), inline=True)
    embed.add_field(name="🔔 Заплановано сповіщень", value=str(len(notifications)), inline=True)
    embed.add_field(name="🤖 Сервери", value=str(len(bot.guilds)), inline=True)
    
    await ctx.send(embed=embed)

# Запуск бота
if __name__ == '__main__':
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    if not TOKEN:
        print("❌ Ошибка: DISCORD_BOT_TOKEN не установлен!")
    elif not DATABASE_URL:
        print("❌ Ошибка: DATABASE_URL не установлен!")
    else:
        bot.run(TOKEN)

