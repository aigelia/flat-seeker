import gc
import json
import logging
import os
import time
from pathlib import Path
from typing import List, Dict, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, TimeoutError as PlaywrightTimeout

# -------------------- Logging --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# -------------------- Parser --------------------
class AruodasParser:
    BASE_URL = "https://ru.aruodas.lt"

    def __init__(self, config_path: str = "config.json", headless: bool = False):
        self.config = self._load_config(config_path)
        self.headless = headless
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._init_browser()

    # ---------- Config ----------
    def _load_config(self, path: str) -> dict:
        file = Path(path)
        if not file.exists():
            logger.warning(f"Конфиг {path} не найден. Создаём дефолтный.")
            default = {
                "search_params": {
                    "FRadius": 5,
                    "FAreaOverAllMin": 60,
                    "FPriceMax": 1200,
                    "detailed_search": 1,
                    "pet_friendly": 1,
                },
                "city": "vilniuje",
                "type": "butu-nuoma",
                "max_pages": 3,
            }
            self._save_config(path, default)
            return default
        return json.loads(file.read_text(encoding="utf-8"))

    def _save_config(self, path: str, config: dict):
        Path(path).write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- Browser ----------
    def _init_browser(self):
        """
        Инициализация браузера Playwright с оптимизацией для памяти
        """
        try:
            self.playwright = sync_playwright().start()

            # Запускаем Chromium с оптимальными флагами
            self.browser = self.playwright.chromium.launch(
                headless=self.headless,
                executable_path='/snap/bin/chromium',  # Используем тот же Chromium что и Selenium
                args=[
                    '--disable-blink-features=AutomationControlled',  # КРИТИЧНО для обхода!
                    '--no-sandbox',
                    '--disable-dev-shm-usage',  # Критично для серверов с малой памятью
                    '--disable-gpu',
                    '--disable-software-rasterizer',
                    '--disable-extensions',
                    '--disable-background-networking',
                    '--disable-sync',
                    '--disable-translate',
                    '--disable-features=TranslateUI',
                    '--disable-features=BlinkGenPropertyTrees',
                    '--disable-default-apps',
                    '--disable-hang-monitor',
                    '--disable-prompt-on-repost',
                    '--metrics-recording-only',
                    '--mute-audio',
                    '--no-first-run',
                    '--safebrowsing-disable-auto-update',
                    '--disable-client-side-phishing-detection',
                    '--disable-component-extensions-with-background-pages',
                    '--disable-ipc-flooding-protection',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding',
                    '--disable-field-trial-config',
                    '--disable-back-forward-cache',
                    '--disable-breakpad',
                    '--disable-component-update',
                    '--disable-domain-reliability',
                    '--disable-features=AudioServiceOutOfProcess,IsolateOrigins,site-per-process',
                    '--disable-features=ImprovedCookieControls,LazyFrameLoading,GlobalMediaControls,DestroyProfileOnBrowserClose,MediaRouter,AcceptCHFrame',
                    '--disable-print-preview',
                    '--disable-setuid-sandbox',
                    '--disable-site-isolation-trials',
                    '--disable-speech-api',
                    '--disable-web-security',
                    '--disk-cache-size=1',
                    '--media-cache-size=1',
                    '--aggressive-cache-discard',
                    '--disable-cache',
                    '--disable-application-cache',
                    '--disable-offline-load-stale-cache',
                    '--disk-cache-size=0',
                    '--no-zygote',  # Экономит память
                    # Лимит памяти для JS
                    '--js-flags=--max-old-space-size=256',
                ],
                # Отключаем загрузку ненужных ресурсов
                chromium_sandbox=False,
            )

            # Создаём контекст с минимальными настройками
            self.context = self.browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                locale='lt-LT',
                timezone_id='Europe/Vilnius',
                java_script_enabled=True,
                # Дополнительные headers для обхода Cloudflare
                extra_http_headers={
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Accept-Language': 'lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7,ru;q=0.6',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-User': '?1',
                    'Upgrade-Insecure-Requests': '1',
                    'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="131", "Google Chrome";v="131"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-platform': '"Linux"',
                },
            )

            # Обход детекции автоматизации (усиленный)
            self.context.add_init_script("""
                // Удаляем webdriver
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });

                // Маскируем Chrome headless
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };

                // Переопределяем permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );

                // Маскируем headless признаки
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });

                Object.defineProperty(navigator, 'languages', {
                    get: () => ['lt-LT', 'lt', 'en-US', 'en']
                });

                // Переопределяем navigator.platform
                Object.defineProperty(navigator, 'platform', {
                    get: () => 'Linux x86_64'
                });

                // Добавляем fake battery API
                navigator.getBattery = () => Promise.resolve({
                    charging: true,
                    chargingTime: 0,
                    dischargingTime: Infinity,
                    level: 1
                });

                // Переопределяем Connection API
                Object.defineProperty(navigator, 'connection', {
                    get: () => ({
                        effectiveType: '4g',
                        rtt: 50,
                        downlink: 10,
                        saveData: false
                    })
                });

                // Маскируем отсутствие GPU
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) {
                        return 'Intel Inc.';
                    }
                    if (parameter === 37446) {
                        return 'Intel Iris OpenGL Engine';
                    }
                    return getParameter.call(this, parameter);
                };
            """)

            # Блокируем ненужные ресурсы для экономии памяти и скорости
            def block_resources(route):
                resource_type = route.request.resource_type
                if resource_type in ['image', 'media', 'font', 'stylesheet']:
                    route.abort()
                else:
                    url = route.request.url
                    if any(x in url for x in ['google-analytics', 'analytics.js', 'gtag', 'facebook.com', 'doubleclick']):
                        route.abort()
                    else:
                        route.continue_()

            self.context.route("**/*", block_resources)

            self.page = self.context.new_page()

            # Устанавливаем разумные таймауты
            self.page.set_default_timeout(30000)  # 30 секунд
            self.page.set_default_navigation_timeout(30000)

            logger.info("Playwright браузер инициализирован")

        except Exception as e:
            logger.error(f"Ошибка инициализации браузера: {e}", exc_info=True)
            self.close()
            raise

    # ---------- URL ----------
    def build_search_url(self, page: int = 1) -> str:
        params = "&".join(f"{k}={v}" for k, v in self.config["search_params"].items())
        url = f"{self.BASE_URL}/{self.config['type']}/{self.config['city']}/?{params}"
        if page > 1:
            url += f"&page={page}"
        return url

    # ---------- Parsing ----------
    def parse_all_pages(self) -> Optional[List[Dict]]:
        all_apartments = []
        seen_ids = set()
        max_pages = self.config.get("max_pages", 3)

        for page_num in range(1, max_pages + 1):
            url = self.build_search_url(page_num)
            apartments = self._parse_page(url)

            if apartments is None:
                logger.error(f"Критическая ошибка на странице {page_num}")
                return None

            if not apartments:
                logger.info(f"Страница {page_num} пуста, останавливаем парсинг")
                break

            # Дедупликация
            for apt in apartments:
                if apt["id"] not in seen_ids:
                    all_apartments.append(apt)
                    seen_ids.add(apt["id"])

            logger.info(f"Страница {page_num}: найдено {len(apartments)} объявлений")

            # Очистка после каждой страницы
            del apartments
            gc.collect()

            if page_num < max_pages:
                time.sleep(2)

        logger.info(f"Найдено {len(all_apartments)} уникальных квартир")
        return all_apartments

    def _parse_page(self, url: str) -> Optional[List[Dict]]:
        try:
            logger.info(f"Открываем страницу: {url}")

            # Небольшая задержка перед запросом (имитация человека)
            time.sleep(1)

            # Переходим на страницу (не ждём полной загрузки)
            response = self.page.goto(url, wait_until='commit', timeout=20000)

            if response is None or response.status != 200:
                logger.error(f"Ошибка загрузки страницы: статус {response.status if response else 'None'}")
                return None

            # Ждём загрузки списка объявлений (это главное)
            try:
                self.page.wait_for_selector(".list-row-v2", timeout=10000)
                logger.info("Объявления загружены")
            except PlaywrightTimeout:
                logger.warning("Таймаут ожидания объявлений, пробуем парсить то что есть")

            # Короткая пауза для завершения рендера
            time.sleep(1)

            # Получаем HTML и парсим через BeautifulSoup
            html = self.page.content()
            soup = BeautifulSoup(html, "lxml")
            listings = soup.find_all("div", class_="list-row-v2")

            if not listings:
                logger.warning("Не найдено объявлений на странице")
                soup.decompose()
                return []

            apartments = []
            for listing in listings:
                apt = self._parse_apartment(listing)
                if apt:
                    apartments.append(apt)

            # Очистка
            soup.decompose()
            del soup
            del listings
            del html

            return apartments

        except Exception as e:
            logger.error(f"Ошибка при парсинге страницы: {e}", exc_info=True)
            return None

    def _parse_apartment(self, listing) -> Optional[Dict]:
        try:
            save_btn = listing.find("div", class_="advert-controls-save-v2")
            if not save_btn or not save_btn.get("data-id"):
                return None

            def text(cls, tag="div"):
                el = listing.find(tag, class_=cls)
                return el.get_text(strip=True) if el else None

            apartment = {
                "id": save_btn["data-id"],
                "url": (listing.find("a", href=True) or {})
                .get("href", "")
                .split("?")[0],
                "address": (
                    ", ".join(
                        s.strip()
                        for s in (
                            listing.find(
                                "div", class_="list-adress-v2"
                            ).h3.stripped_strings
                        )
                        if "км до точки" not in s
                    )
                    if listing.find("div", class_="list-adress-v2")
                    else None
                ),
                "distance": text("accent", "span"),
                "price": text("list-item-price-v2", "span"),
                "price_per_m2": text("price-pm-v2", "span"),
                "rooms": text("list-RoomNum-v2"),
                "area": text("list-AreaOverall-v2"),
                "floor": text("list-Floors-v2"),
                "pet_friendly": bool(listing.find("div", class_="pet_friendly_info")),
                "price_change": text("price-change"),
            }
            return apartment
        except Exception as e:
            logger.warning(f"Ошибка при парсинге объявления: {e}")
            return None

    # ---------- Close ----------
    def close(self):
        """
        Полная очистка ресурсов браузера
        """
        try:
            if self.page:
                try:
                    self.page.close()
                except:
                    pass
                self.page = None

            if self.context:
                try:
                    self.context.close()
                except:
                    pass
                self.context = None

            if self.browser:
                try:
                    self.browser.close()
                except:
                    pass
                self.browser = None

            if self.playwright:
                try:
                    self.playwright.stop()
                except:
                    pass
                self.playwright = None

            # Небольшая пауза для завершения процессов браузера
            time.sleep(0.5)

            # Принудительная очистка памяти (дважды для надёжности)
            gc.collect()
            gc.collect()

            logger.info("Playwright браузер закрыт и очищен")

        except Exception as e:
            logger.error(f"Ошибка при закрытии браузера: {e}")


# -------------------- Memory monitoring --------------------
def log_memory_usage(stage: str):
    """Логирует использование памяти процессом"""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        mem_mb = mem_info.rss / 1024 / 1024
        logger.info(f"[{stage}] Память: {mem_mb:.1f} MB")
    except ImportError:
        # psutil не установлен - пропускаем
        pass
    except Exception as e:
        logger.warning(f"Не удалось получить информацию о памяти: {e}")


# -------------------- Launcher for bot --------------------
def fetch_new_apartments(
    config_path: str = "config.json",
    published_ids_path: str = "published_ids.json",
    headless: bool = False,
) -> Optional[List[Dict]]:
    """
    Парсит все квартиры и возвращает их БЕЗ ФИЛЬТРАЦИИ.
    Фильтрацию делает бот, т.к. файл published_ids может измениться во время парсинга.
    """
    parser = None
    try:
        log_memory_usage("До инициализации браузера")

        parser = AruodasParser(config_path=config_path, headless=headless)
        log_memory_usage("После инициализации браузера")

        all_apartments = parser.parse_all_pages()
        log_memory_usage("После парсинга всех страниц")

        if all_apartments is None:
            logger.error("Критическая ошибка парсинга")
            return None

        return all_apartments

    except Exception as e:
        logger.error(f"Ошибка парсинга: {e}", exc_info=True)
        return None

    finally:
        if parser is not None:
            parser.close()
            del parser
            gc.collect()
            log_memory_usage("После закрытия браузера")
