"""
API на http://localhost:8000
"""

from flask import Flask, jsonify, request
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
import time
import re
from datetime import datetime
import logging

app = Flask(__name__)

# Налаштування логування
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DTEKParser:
    """Клас для роботи з сайтом ДТЕК"""
    
    def __init__(self):
        self.base_url = 'https://www.dtek-oem.com.ua/ua/shutdowns'
        self.driver = None
        
    def init_driver(self):
        """Ініціалізація браузера"""
        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--lang=uk-UA')
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        
        chrome_options.binary_location = '/usr/bin/chromium'
        chrome_options.add_argument('--disable-dev-shm-usage')
        self.driver = webdriver.Chrome(options=chrome_options)
            
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.driver.set_page_load_timeout(60)
        
    def close_modal(self):
        """Закриття модальних вікон"""
        logger.info("Закриваю модальні вікна...")
        
        close_scripts = [
            "document.querySelector('.modal__close')?.click()",
            "document.querySelector('.m-attention__close')?.click()",
            "document.querySelector('[data-dismiss=\"modal\"]')?.click()",
            "document.querySelector('button.close')?.click()",
            "document.querySelector('.close')?.click()",
        ]
        
        for script in close_scripts:
            try:
                self.driver.execute_script(script)
                time.sleep(0.5)
            except:
                pass
                
        try:
            ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
            time.sleep(1)
        except:
            pass
            
    def smart_input(self, selectors, text, field_name):
        """Розумне введення тексту з обробкою автозаповнення"""
        element = None
        
        # Пошук елемента
        for selector in selectors:
            try:
                if selector.startswith('//'):
                    element = WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.XPATH, selector))
                    )
                else:
                    element = WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                    )
                logger.info(f"Знайдено поле {field_name}")
                break
            except:
                continue
                
        if not element:
            raise Exception(f"Не знайдено поле {field_name}")
            
        # Прокрутка до елемента
        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});", element)
        time.sleep(1)
        
        # Очікування інтерактивності
        WebDriverWait(self.driver, 10).until(EC.element_to_be_clickable(element))
        
        # Спроби введення
        success = False
        methods = [
            lambda: self._method_js_value(element, text),
            lambda: self._method_clear_and_type(element, text),
            lambda: self._method_focus_and_type(element, text),
            lambda: self._method_click_and_type(element, text),
        ]
        
        for i, method in enumerate(methods, 1):
            try:
                logger.info(f"Спроба {i} для {field_name}...")
                method()
                time.sleep(1)
                
                current_value = element.get_attribute('value') or ''
                if text.lower() in current_value.lower() or len(current_value) >= len(text) - 2:
                    logger.info(f"✓ {field_name} введено: '{current_value}'")
                    self._trigger_events(element)
                    success = True
                    break
            except Exception as e:
                logger.warning(f"Метод {i} не вдався: {e}")
                continue
                
        if not success:
            current_value = element.get_attribute('value') or ''
            if len(current_value) > 0:
                logger.warning(f"Часткове введення для {field_name}: '{current_value}'")
                return element
            raise Exception(f"Не вдалося ввести текст у поле {field_name}")
            
        return element
        
    def _method_js_value(self, element, text):
        """Метод 1: JavaScript встановлення значення"""
        self.driver.execute_script(f"arguments[0].value = '{text}';", element)
        
    def _method_clear_and_type(self, element, text):
        """Метод 2: Очищення та посимвольне введення"""
        element.clear()
        time.sleep(0.5)
        for char in text:
            element.send_keys(char)
            time.sleep(0.1)
            
    def _method_focus_and_type(self, element, text):
        """Метод 3: Фокус через JavaScript та введення"""
        self.driver.execute_script("arguments[0].focus();", element)
        time.sleep(0.5)
        element.send_keys(text)
        
    def _method_click_and_type(self, element, text):
        """Метод 4: Клік та введення"""
        element.click()
        time.sleep(0.5)
        element.send_keys(text)
        
    def _trigger_events(self, element):
        """Генерація подій для елемента"""
        self.driver.execute_script("""
            arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
            arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
            arguments[0].dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
        """, element)
        
    def select_autocomplete(self, field_name):
        """Вибір з автозаповнення"""
        time.sleep(3)
        
        suggestions_selectors = [
            '.suggestions li',
            '.autocomplete-item',
            '.dropdown-item',
            '[role="option"]',
            'ul.dropdown-menu li',
            '.suggestion',
            'li[data-id]',
            '.select-dropdown li',
            '.ui-menu-item',
            '.pac-item'
        ]
        
        for selector in suggestions_selectors:
            try:
                suggestions = WebDriverWait(self.driver, 5).until(
                    EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector))
                )
                
                if suggestions and len(suggestions) > 0:
                    logger.info(f"Знайдено {len(suggestions)} підказок для {field_name}")
                    
                    first_suggestion = suggestions[0]
                    
                    try:
                        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", first_suggestion)
                        time.sleep(0.5)
                        self.driver.execute_script("arguments[0].click();", first_suggestion)
                        logger.info(f"✓ Вибрано автозаповнення для {field_name}")
                        time.sleep(2)
                        return True
                    except:
                        try:
                            first_suggestion.click()
                            logger.info(f"✓ Вибрано автозаповнення для {field_name}")
                            time.sleep(2)
                            return True
                        except:
                            pass
            except:
                continue
                
        # Альтернативний метод: Arrow Down + Enter
        logger.info(f"Пробую Arrow Down + Enter для {field_name}")
        try:
            ActionChains(self.driver).send_keys(Keys.ARROW_DOWN).perform()
            time.sleep(1)
            ActionChains(self.driver).send_keys(Keys.ENTER).perform()
            time.sleep(2)
            logger.info(f"✓ Використано Arrow Down + Enter для {field_name}")
            return True
        except:
            pass
            
        return False
        
    def parse_schedule_table(self):
        """Парсинг таблиці графіка"""
        schedule_text = ""
        outage_times = []
        
        # Дата оновлення
        try:
            update_elem = self.driver.find_element(By.XPATH, 
                "//*[contains(text(), 'Дата та час останнього оновлення') or contains(text(), 'Дата оновлення')]")
            update_text = update_elem.text
            schedule_text += f"ℹ️ {update_text}\n\n"
            logger.info("Знайдено дату оновлення")
        except:
            logger.warning("Дата оновлення не знайдена")
            
        # Пошук таблиць
        try:
            tables = self.driver.find_elements(By.CSS_SELECTOR, 'table')
            logger.info(f"Знайдено {len(tables)} таблиць")
            
            if tables:
                for idx, table in enumerate(tables):
                    try:
                        # Визначення заголовка
                        header = f"📅 {'Сьогодні' if idx == 0 else 'Завтра'}"
                        try:
                            parent_text = table.find_element(By.XPATH, './preceding-sibling::*[1]').text
                            if parent_text and len(parent_text) < 50:
                                header = parent_text
                        except:
                            pass
                            
                        schedule_text += f"\n{header}\n{'='*40}\n"
                        
                        rows = table.find_elements(By.TAG_NAME, 'tr')
                        confirmed = []
                        possible = []
                        
                        for row in rows[1:]:
                            cells = row.find_elements(By.TAG_NAME, 'td')
                            if len(cells) >= 2:
                                time_slot = cells[0].text.strip()
                                if not time_slot:
                                    continue
                                    
                                cell_html = cells[1].get_attribute('outerHTML')
                                cell_class = cells[1].get_attribute('class') or ''
                                
                                is_outage = any([
                                    'gray' in cell_class.lower(),
                                    'dark' in cell_class.lower(),
                                    'outage' in cell_class.lower(),
                                    'off' in cell_class.lower(),
                                    'background' in cell_html and 'gray' in cell_html.lower()
                                ])
                                
                                is_possible = any([
                                    'yellow' in cell_class.lower(),
                                    'warning' in cell_class.lower(),
                                    'possible' in cell_class.lower(),
                                    'maybe' in cell_class.lower()
                                ])
                                
                                if is_outage:
                                    confirmed.append(time_slot)
                                    try:
                                        start_time = time_slot.split('-')[0].strip()
                                        if ':' not in start_time:
                                            start_time = f"{start_time}:00"
                                        outage_times.append(start_time)
                                    except:
                                        pass
                                elif is_possible:
                                    possible.append(time_slot)
                                    
                        if confirmed:
                            schedule_text += "❌ ПІДТВЕРДЖЕНІ ВІДКЛЮЧЕННЯ:\n"
                            for slot in confirmed:
                                schedule_text += f"  • {slot}\n"
                                
                        if possible:
                            schedule_text += "\n⚠️ МОЖЛИВІ ВІДКЛЮЧЕННЯ:\n"
                            for slot in possible:
                                schedule_text += f"  • {slot}\n"
                                
                        if not confirmed and not possible:
                            schedule_text += "✅ Відключення не заплановані\n"
                            
                        schedule_text += "\n"
                        logger.info(f"Таблиця {idx+1}: {len(confirmed)} підтверджених, {len(possible)} можливих")
                        
                    except Exception as e:
                        logger.error(f"Помилка обробки таблиці {idx}: {e}")
        except Exception as e:
            logger.error(f"Помилка пошуку таблиць: {e}")
            
        return schedule_text.strip(), list(set(outage_times))
        
    def get_schedule(self, city, street, house_number):
        """Основна функція отримання графіка"""
        try:
            self.init_driver()
            
            logger.info("Відкриваю сайт ДТЕК...")
            self.driver.get(self.base_url)
            time.sleep(10)
            
            logger.info("Сторінка завантажена")
            self.close_modal()
            time.sleep(3)
            
            # Селектори для полів
            city_selectors = [
                'input[name="city"]',
                'input[placeholder*="населен"]',
                'input#city',
                '//input[contains(@placeholder, "населен") or contains(@name, "city")]'
            ]
            
            street_selectors = [
                'input[name="street"]',
                'input[placeholder*="вулиц"]',
                'input#street',
                '//input[contains(@placeholder, "вулиц") or contains(@name, "street")]'
            ]
            
            house_selectors = [
                'input[name="house"]',
                'input[placeholder*="будинок"]',
                'input#house',
                '//input[contains(@placeholder, "будинок") or contains(@name, "house")]'
            ]
            
            # Заповнення форми
            logger.info("Заповнюю місто...")
            self.smart_input(city_selectors, city, "Місто")
            self.select_autocomplete("Місто")
            time.sleep(2)
            
            logger.info("Заповнюю вулицю...")
            self.smart_input(street_selectors, street, "Вулиця")
            self.select_autocomplete("Вулиця")
            time.sleep(2)
            
            logger.info("Заповнюю будинок...")
            self.smart_input(house_selectors, house_number, "Будинок")
            self.select_autocomplete("Будинок")
            time.sleep(2)
            
            # Пошук кнопки
            logger.info("Натискаю кнопку пошуку...")
            button_selectors = [
                'button[type="submit"]',
                'button[class*="submit"]',
                'button[class*="search"]',
                '//button[@type="submit" or contains(text(), "Пошук")]'
            ]
            
            search_clicked = False
            for selector in button_selectors:
                try:
                    if selector.startswith('//'):
                        search_button = WebDriverWait(self.driver, 5).until(
                            EC.element_to_be_clickable((By.XPATH, selector))
                        )
                    else:
                        search_button = WebDriverWait(self.driver, 5).until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                        )
                    
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", search_button)
                    time.sleep(1)
                    self.driver.execute_script("arguments[0].click();", search_button)
                    logger.info("Кнопку натиснуто")
                    search_clicked = True
                    break
                except:
                    continue
                    
            if not search_clicked:
                logger.info("Пробую Enter")
                ActionChains(self.driver).send_keys(Keys.ENTER).perform()
                
            # Очікування результатів
            logger.info("Чекаю на результати...")
            time.sleep(20)
            
            # Парсинг
            schedule_text, outage_times = self.parse_schedule_table()
            
            if not schedule_text or len(schedule_text) < 30:
                schedule_text = "⚠️ Графік не знайдено або адреса не обслуговується"
                
            logger.info(f"Парсинг завершено. Знайдено {len(outage_times)} часів відключень")
            
            return {
                'success': True,
                'schedule': schedule_text,
                'outage_times': outage_times,
                'timestamp': datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Помилка: {e}")
            return {
                'success': False,
                'error': str(e),
                'schedule': f"❌ Помилка отримання даних: {str(e)}",
                'outage_times': [],
                'timestamp': datetime.now().isoformat()
            }
        finally:
            if self.driver:
                self.driver.quit()


# Flask API Routes
@app.route('/api/schedule', methods=['POST'])
def get_schedule():
    """
    Отримання графіка відключень
    
    POST /api/schedule
    {
        "city": "Київ",
        "street": "Хрещатик",
        "house": "1"
    }
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'Не надано JSON даних'}), 400
            
        city = data.get('city')
        street = data.get('street')
        house = data.get('house')
        
        if not all([city, street, house]):
            return jsonify({'error': 'Необхідні параметри: city, street, house'}), 400
            
        parser = DTEKParser()
        result = parser.get_schedule(city, street, house)
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/health', methods=['GET'])
def health_check():
    """Перевірка стану API"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat()
    })


@app.route('/', methods=['GET'])
def index():
    """Головна сторінка з документацією"""
    return """
    <html>
    <head>
        <title>DTEK API Service</title>
        <style>
            body { font-family: Arial; max-width: 800px; margin: 50px auto; padding: 20px; }
            code { background: #f4f4f4; padding: 2px 6px; border-radius: 3px; }
            pre { background: #f4f4f4; padding: 15px; border-radius: 5px; overflow-x: auto; }
        </style>
    </head>
    <body>
        <h1>⚡ DTEK API Service</h1>
        <p>API для отримання графіків відключень електроенергії</p>
        
        <h2>Endpoints</h2>
        
        <h3>POST /api/schedule</h3>
        <p>Отримання графіка відключень для конкретної адреси</p>
        <pre>
{
    "city": "Київ",
    "street": "Хрещатик",
    "house": "1"
}
        </pre>
        
        <h3>GET /api/health</h3>
        <p>Перевірка стану API</p>
        
        <h2>Приклад використання (curl)</h2>
        <pre>
curl -X POST http://localhost:8000/api/schedule \
  -H "Content-Type: application/json" \
  -d '{"city":"Київ","street":"Хрещатик","house":"1"}'
        </pre>
    </body>
    </html>
    """


if __name__ == '__main__':
    print("="*60)
    print("⚡ DTEK API Service")
    print("="*60)
    print("API запущено на http://localhost:8000")
    print("Документація: http://localhost:8000")
    print("Health check: http://localhost:8000/api/health")
    print("="*60)
    
    app.run(host='0.0.0.0', port=8000, debug=False)
