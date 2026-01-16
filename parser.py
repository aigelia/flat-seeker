import json
import logging
import time
import subprocess
from pathlib import Path
from typing import List, Dict, Optional

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

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

    def __init__(
        self,
        config_path: str = "config.json",
        headless: bool = True,
        kill_chromium: bool = False,  # ← опционально
    ):
        self.config = self._load_config(config_path)
        self.kill_chromium = kill_chromium
        self.driver = self._init_driver(headless)

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
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------- Driver ----------
    def _init_driver(self, headless: bool) -> webdriver.Chrome:
        options = Options()

        if headless:
            options.add_argument("--headless=new")

        options.binary_location = "/snap/bin/chromium"

        # антидетект — оставляем как было
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-software-rasterizer")
        options.add_argument("--disable-dev-tools")
        options.add_argument("--no-first-run")
        options.add_argument("--no-zygote")

        # ⚠️ ВАЖНО: single-process и remote-debugging УБРАНЫ

        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-translate")
        options.add_argument("--disable-features=TranslateUI")
        options.add_argument("--disable-features=BlinkGenPropertyTrees")
        options.add_argument("--disable-ipc-flooding-protection")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-client-side-phishing-detection")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-hang-monitor")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--disable-prompt-on-repost")
        options.add_argument("--metrics-recording-only")
        options.add_argument("--mute-audio")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--safebrowsing-disable-auto-update")
        options.add_argument("--password-store=basic")
        options.add_argument("--use-mock-keychain")
        options.add_argument("--force-color-profile=srgb")
        options.add_argument("--disk-cache-size=1")
        options.add_argument("--media-cache-size=1")
        options.add_argument("--js-flags=--max-old-space-size=128")

        # ❌ картинки
        prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2,
        }
        options.add_experimental_option("prefs", prefs)

        options.add_experimental_option(
            "excludeSwitches", ["enable-automation"]
        )
        options.add_experimental_option("useAutomationExtension", False)

        service = Service("/usr/local/bin/chromedriver")
        driver = webdriver.Chrome(service=service, options=options)

        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        return driver

    # ---------- URL ----------
    def build_search_url(self, page: int = 1) -> str:
        params = "&".join(
            f"{k}={v}" for k, v in self.config["search_params"].items()
        )
        url = f"{self.BASE_URL}/{self.config['type']}/{self.config['city']}/?{params}"
        if page > 1:
            url += f"&page={page}"
        return url

    # ---------- Parsing ----------
    def parse_all_pages(self) -> Optional[List[Dict]]:
        all_apartments = []
        seen_ids = set()
        max_pages = self.config.get("max_pages", 3)

        for page in range(1, max_pages + 1):
            url = self.build_search_url(page)
            apartments = self._parse_page(url)

            if apartments is None:
                logger.error(f"Критическая ошибка на странице {page}")
                return None

            if not apartments:
                break

            for apt in apartments:
                if apt["id"] not in seen_ids:
                    all_apartments.append(apt)
                    seen_ids.add(apt["id"])

            if page < max_pages:
                time.sleep(2)

        logger.info(f"Найдено {len(all_apartments)} уникальных квартир")
        return all_apartments

    def _parse_page(self, url: str) -> Optional[List[Dict]]:
        try:
            self.driver.get(url)

            if "403" in self.driver.title or "Access Denied" in self.driver.page_source:
                logger.error("Получен 403 или блокировка")
                return None

            time.sleep(3)

            try:
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located(
                        (By.CLASS_NAME, "list-row-v2")
                    )
                )
            except Exception:
                pass

            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            listings = soup.find_all("div", class_="list-row-v2")

            if not listings:
                return []

            results = []
            for l in listings:
                apt = self._parse_apartment(l)
                if apt:
                    results.append(apt)

            return results

        except Exception as e:
            logger.error(
                f"Ошибка при парсинге страницы: {e}",
                exc_info=True,
            )
            return None

    def _parse_apartment(self, listing) -> Optional[Dict]:
        try:
            save_btn = listing.find(
                "div", class_="advert-controls-save-v2"
            )
            if not save_btn or not save_btn.get("data-id"):
                return None

            def text(cls, tag="div"):
                el = listing.find(tag, class_=cls)
                return el.get_text(strip=True) if el else None

            apartment = {
                "id": save_btn["data-id"],
                "url": (
                    listing.find("a", href=True).get("href", "").split("?")[0]
                ),
                "address": (
                    ", ".join(
                        s.strip()
                        for s in listing.find(
                            "div", class_="list-adress-v2"
                        ).h3.stripped_strings
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
                "pet_friendly": bool(
                    listing.find("div", class_="pet_friendly_info")
                ),
                "price_change": text("price-change"),
            }

            return apartment

        except Exception as e:
            logger.warning(f"Ошибка при парсинге объявления: {e}")
            return None

    # ---------- Close ----------
    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                logger.error(f"Ошибка при закрытии драйвера: {e}")
            finally:
                self.driver = None

        if self.kill_chromium:
            try:
                subprocess.run(
                    ["pkill", "-f", "chromium"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass


# -------------------- Launcher --------------------
def fetch_new_apartments(
    config_path: str = "config.json",
    published_ids_path: str = "published_ids.json",  # ← ОБЯЗАТЕЛЬНО оставляем
    headless: bool = False,
    kill_chromium: bool = False,
) -> Optional[List[Dict]]:
    """
    Парсит все квартиры и возвращает их БЕЗ ФИЛЬТРАЦИИ.
    published_ids_path намеренно не используется — фильтрация в боте.
    """
    parser = None
    try:
        parser = AruodasParser(
            config_path=config_path,
            headless=headless,
            kill_chromium=kill_chromium,
        )
        return parser.parse_all_pages()

    except Exception as e:
        logger.error(f"Ошибка парсинга: {e}", exc_info=True)
        return None

    finally:
        if parser:
            parser.close()
