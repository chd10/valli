import os
import signal
import logging
import subprocess
from datetime import datetime

import yadisk

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PRICE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "price.xlsx")
LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "price_updated.txt")
STOCK_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "warehouse", "stock_ediscom.xlsx")


def _client() -> yadisk.Client:
    return yadisk.Client(token=os.environ["YADISK_TOKEN"], session="httpx")


def download() -> str:
    remote_path = os.environ.get("YADISK_FILE_PATH", "/price.xlsx")
    logger.info("Скачиваю прайс с Яндекс Диска: %s", remote_path)
    with _client() as client:
        client.download(remote_path, PRICE_PATH)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(now + "\n")
    logger.info("Прайс обновлён: %s → %s", remote_path, now)
    return now


def download_stock() -> None:
    remote_path = os.environ.get("YADISK_STOCK_PATH", "")
    if not remote_path:
        logger.warning("YADISK_STOCK_PATH не задан — пропускаю скачивание склада")
        return
    os.makedirs(os.path.dirname(STOCK_PATH), exist_ok=True)
    logger.info("Скачиваю склад с Яндекс Диска: %s", remote_path)
    with _client() as client:
        client.download(remote_path, STOCK_PATH)
    logger.info("Склад обновлён: %s", remote_path)


def notify_bot() -> None:
    result = subprocess.run(
        ["pgrep", "-f", "python3 bot.py"],
        capture_output=True, text=True,
    )
    pids = [p for p in result.stdout.strip().split() if p]
    if not pids:
        logger.warning("Процесс бота не найден, перезагрузка пропущена")
        return
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGUSR1)
            logger.info("SIGUSR1 отправлен процессу %s", pid)
        except (ProcessLookupError, ValueError):
            pass


if __name__ == "__main__":
    errors = []
    try:
        download()
    except Exception:
        logger.exception("Ошибка обновления прайса")
        errors.append("price")
    try:
        download_stock()
    except Exception:
        logger.exception("Ошибка обновления склада")
        errors.append("stock")
    notify_bot()
    if errors:
        raise SystemExit(1)
