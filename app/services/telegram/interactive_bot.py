# -*- coding: utf-8 -*-
####################################
#          Sokolov Dmitry          #
#       xx.sokolov@gmail.com       #
#        https://t.me/ZbxNTg       #
####################################
# https://github.com/xxsokolov/znt #
####################################
"""
Интерактивный Telegram-бот для просмотра метрик Zabbix по IP-адресу хоста.
Поддерживает пагинацию, графики с водяными знаками и навигацию.
"""

import logging
import io
import re
from typing import Optional, Dict, List, Any

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from PIL import Image, ImageDraw, ImageFont
import requests

from app import config, logger as znt_logger
from app.classes.integration import ZabbixReq

# Настройка логирования
logging.basicConfig(
    level=logging.INFO if not config.getboolean('logging', 'exc_info') else logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

ITEMS_PER_PAGE = 10


class ZabbixInteractiveClient:
    """Клиент для интерактивного взаимодействия с Zabbix API"""
    
    def __init__(self):
        self.session = requests.Session()
        self.base_url = config.get('zabbix', 'url').rstrip('/')
        self.login = config.get('zabbix', 'login')
        self.password = config.get('zabbix', 'password')
        self.auth_token: Optional[str] = None
        self.headers = {'Content-Type': 'application/json-rpc'}
        self.connect_timeout = int(config.get('zabbix', 'connect_timeout'))
        
    def login(self) -> bool:
        """Двойная аутентификация: через веб-форму и API"""
        try:
            # 1. Аутентификация через веб-форму для получения cookie
            login_url = f"{self.base_url}/index.php"
            login_data = {
                "name": self.login,
                "password": self.password,
                "enter": "Sign in"
            }
            
            response = self.session.post(
                login_url,
                data=login_data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=self.connect_timeout,
                verify=False
            )
            
            if response.status_code != 200:
                logger.error(f"Ошибка веб-аутентификации: HTTP {response.status_code}")
                return False
            
            # 2. Аутентификация через API для получения токена
            api_url = f"{self.base_url}/api_jsonrpc.php"
            payload = {
                "jsonrpc": "2.0",
                "method": "user.login",
                "params": {
                    "user": self.login,
                    "password": self.password
                },
                "id": 1
            }
            
            response = self.session.post(
                api_url,
                json=payload,
                headers=self.headers,
                timeout=self.connect_timeout,
                verify=False
            )
            
            if response.status_code != 200:
                logger.error(f"Ошибка API аутентификации: HTTP {response.status_code}")
                return False
                
            result = response.json()
            
            if 'result' in result:
                self.auth_token = result['result']
                logger.info("Аутентификация успешна (cookie + token)")
                return True
                
            error = result.get('error', {})
            logger.error(f"Ошибка API: {error.get('message')} (код: {error.get('code')})")
            return False
            
        except Exception as e:
            logger.error(f"Ошибка при аутентификации: {str(e)}")
            return False
    
    def api_request(self, method: str, params: Dict[str, Any]) -> Optional[Any]:
        """Универсальный метод для API запросов"""
        if not self.auth_token and not self.login():
            raise Exception("Не удалось аутентифицироваться в Zabbix API")
            
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": 1,
                "auth": self.auth_token
            }
            
            api_url = f"{self.base_url}/api_jsonrpc.php"
            response = self.session.post(
                api_url,
                json=payload,
                headers=self.headers,
                timeout=self.connect_timeout,
                verify=False
            )
            
            if response.status_code != 200:
                raise Exception(f"HTTP ошибка {response.status_code}")
                
            result = response.json()
            
            if 'error' in result:
                error = result['error']
                if error.get('code') in [-32602, -32603]:  # Ошибки авторизации
                    logger.warning("Сессия устарела, пробуем переаутентифицироваться...")
                    self.auth_token = None
                    if self.login():
                        return self.api_request(method, params)
                raise Exception(f"API ошибка: {error.get('message')}")
            
            return result.get('result')
            
        except Exception as e:
            logger.error(f"Ошибка API запроса: {str(e)}")
            raise

    def get_graph(self, itemid: str, width: int = 900, height: int = 200, period: int = 3600) -> Optional[bytes]:
        """Получение графика с водяным знаком"""
        try:
            chart_name = "Graph"
            range_time = period
            
            graph_url = config.get('zabbix', 'chart_url').format(
                name=chart_name,
                itemid=itemid,
                zabbix_server=self.base_url + '/',
                range_time=range_time
            )
            
            response = self.session.get(graph_url, verify=False, timeout=self.connect_timeout)
            
            if response.status_code == 200 and response.content:
                img = Image.open(io.BytesIO(response.content))
                
                # Добавляем водяной знак если включено
                if config.getboolean('znt.settings', 'watermark'):
                    try:
                        draw = ImageDraw.Draw(img)
                        font = ImageFont.load_default()
                        watermark_label = config.get('znt.settings', 'watermark_label')
                        draw.text((10, 10), watermark_label, fill="gray", font=font)
                    except Exception as e:
                        logger.warning(f"Не удалось добавить водяной знак: {str(e)}")
                
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='PNG')
                return img_byte_arr.getvalue()
                
            logger.error(f"Ошибка получения графика: HTTP {response.status_code}")
            return None
            
        except Exception as e:
            logger.error(f"Ошибка при обработке графика: {str(e)}")
            return None

    def find_host_by_ip(self, ip_address: str) -> Optional[Dict[str, Any]]:
        """Поиск хоста по IP адресу"""
        try:
            hosts = self.api_request('host.get', {
                'output': ['hostid', 'name'],
                'selectInterfaces': ['ip'],
                'filter': {'interface_ip': ip_address}
            })
            
            if hosts:
                return hosts[0]
            return None
        except Exception as e:
            logger.error(f"Ошибка поиска хоста: {str(e)}")
            return None

    def get_host_items(self, host_id: str) -> List[Dict[str, Any]]:
        """Получение элементов хоста с обработкой ошибок"""
        try:
            items = self.api_request('item.get', {
                'output': ['itemid', 'name', 'lastvalue', 'units'],
                'hostids': host_id,
                'sortfield': 'name'
            }) or []
            
            # Фильтрация элементов без имени
            return [item for item in items if item.get('name')]
        except Exception as e:
            logger.error(f"Ошибка получения элементов: {str(e)}")
            return []


class InteractiveBot:
    """Класс интерактивного бота для работы с пользователями"""
    
    def __init__(self, token: str, proxy: Optional[str] = None, proxy_use: bool = False):
        self.token = token
        self.proxy = proxy
        self.proxy_use = proxy_use
        self.zabbix_client = ZabbixInteractiveClient()
        self.application: Optional[Application] = None
        
    def start_bot(self):
        """Запуск бота"""
        try:
            # Проверка подключения к Zabbix
            if not self.zabbix_client.login():
                logger.error("Не удалось подключиться к Zabbix. Проверьте настройки.")
                return False

            # Настройка прокси если нужно
            proxy_url = None
            if self.proxy_use and self.proxy:
                proxy_url = f"http://{self.proxy}"
            
            # Создаем приложение
            builder = Application.builder().token(self.token)
            if proxy_url:
                builder.proxy_url(proxy_url).get_updates_proxy_url(proxy_url)
            
            self.application = builder.build()
            
            # Регистрация обработчиков
            self.application.add_handler(CommandHandler("start", self._handle_start))
            self.application.add_handler(CommandHandler("help", self._handle_help))
            self.application.add_handler(CommandHandler("exit", self._handle_exit))
            self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

            logger.info("Интерактивный бот запущен и готов к работе")
            self.application.run_polling(allowed_updates=Update.ALL_TYPES)
            return True
            
        except Exception as e:
            logger.error(f"Критическая ошибка при запуске бота: {str(e)}", exc_info=True)
            return False
    
    def stop_bot(self):
        """Остановка бота"""
        if self.application and self.application.running:
            self.application.stop()
            logger.info("Интерактивный бот остановлен")

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка команды /start"""
        user = update.effective_user
        welcome_msg = (
            f"Привет, {user.first_name}!\n"
            "Я бот для мониторинга Zabbix.\n\n"
            "Отправьте мне IP адрес хоста, и я покажу доступные метрики."
        )
        await update.message.reply_text(welcome_msg)

    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка команды /help"""
        help_msg = (
            "📌 Доступные команды:\n"
            "/start - Начало работы\n"
            "/help - Эта справка\n"
            "/exit - Сбросить текущее состояние\n\n"
            "Просто отправьте IP адрес хоста для получения данных."
        )
        await update.message.reply_text(help_msg)

    async def _handle_exit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка команды /exit"""
        context.user_data.clear()
        await update.message.reply_text("✅ Состояние сброшено. Можете ввести новый IP адрес.")

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка входящих сообщений"""
        text = update.message.text.strip()
        
        # Проверяем стадию взаимодействия
        stage = context.user_data.get('stage', 'input_ip')
        
        if stage == 'input_ip':
            await self._handle_ip_input(update, context, text)
        elif stage == 'choose_item':
            await self._handle_item_choice(update, context, text)

    async def _handle_ip_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, ip_address: str):
        """Обработка ввода IP адреса"""
        try:
            logger.info(f"Поиск хоста по IP: {ip_address}")

            # Проверка формата IP
            if not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip_address):
                await update.message.reply_text("⚠️ Неверный формат IP адреса.")
                return

            # Поиск хоста по IP
            host = self.zabbix_client.find_host_by_ip(ip_address)

            if not host:
                await update.message.reply_text("🔍 Хост с таким IP не найден.")
                return

            items = self.zabbix_client.get_host_items(host['hostid'])

            if not items:
                await update.message.reply_text(f"ℹ️ У хоста {host['name']} нет доступных метрик.")
                return

            # Сохраняем данные в контексте
            context.user_data.update({
                'items': items,
                'host_id': host['hostid'],
                'host_name': host['name'],
                'ip_address': ip_address,
                'page': 0,
                'stage': 'choose_item'
            })

            reply_markup = self._create_keyboard(items, 0)
            await update.message.reply_text(
                f"📊 Выберите метрику для хоста {host['name']}:",
                reply_markup=reply_markup
            )

        except Exception as e:
            logger.error(f"Ошибка обработки IP: {str(e)}", exc_info=True)
            await update.message.reply_text("⚠️ Произошла ошибка. Попробуйте позже.")

    async def _handle_item_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """Обработка выбора метрики"""
        try:
            items = context.user_data.get('items', [])
            page = context.user_data.get('page', 0)

            if text == '<< Предыдущая':
                page = max(0, page - 1)
                context.user_data['page'] = page
                reply_markup = self._create_keyboard(items, page)
                await update.message.reply_text(f"Страница {page + 1}:", reply_markup=reply_markup)
                return
            elif text == 'Следующая >>':
                page += 1
                context.user_data['page'] = page
                reply_markup = self._create_keyboard(items, page)
                await update.message.reply_text(f"Страница {page + 1}:", reply_markup=reply_markup)
                return
            elif text == 'Выход':
                context.user_data.clear()
                await update.message.reply_text("Введите новый IP адрес.")
                return

            # Поиск выбранного элемента
            selected_item = next((item for item in items if item['name'] == text), None)
            
            if not selected_item:
                await update.message.reply_text("❌ Метрика не найдена.")
                return

            # Получаем график и данные
            graph_period = config.getint('znt.settings', 'zabbix_graph_period_default', fallback=3600)
            graph = self.zabbix_client.get_graph(selected_item['itemid'], period=graph_period)
            value = selected_item.get('lastvalue', 'N/A')
            units = selected_item.get('units', '')
            
            message = f"📈 {selected_item['name']}\n🔢 Значение: {value} {units}"

            if graph:
                await update.message.reply_photo(
                    photo=graph,
                    caption=message
                )
            else:
                await update.message.reply_text(f"{message}\n\n⚠️ График недоступен")

        except Exception as e:
            logger.error(f"Ошибка выбора элемента: {str(e)}", exc_info=True)
            await update.message.reply_text("⚠️ Произошла ошибка. Попробуйте снова.")

    def _create_keyboard(self, items: List[Dict[str, Any]], page: int) -> ReplyKeyboardMarkup:
        """Создание клавиатуры с пагинацией"""
        start = page * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]
        
        keyboard = [[KeyboardButton(item['name'])] for item in page_items if item.get('name')]
        
        # Кнопки навигации
        nav_buttons = []
        if page > 0:
            nav_buttons.append(KeyboardButton('<< Предыдущая'))
        if end < len(items):
            nav_buttons.append(KeyboardButton('Следующая >>'))
        nav_buttons.append(KeyboardButton('Выход'))
        
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
