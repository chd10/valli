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


def download() -> str:
    token = os.environ["YADISK_TOKEN"]
    remote_path = os.environ.get("YADISK_FILE_PATH", "/price.xlsx")

    logger.info("Скачиваю прайс с Яндекс Диска: %s", remote_path)
    with yadisk.Client(token=token, session="httpx") as client:
        client.download(remote_path, PRICE_PATH)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(now + "\n")

    logger.info("Прайс обновлён: %s → %s", remote_path, now)
    return now


def notify_bot() -> None:
    result = subprocess.run(
        ["pgrep", "-f", "python3 bot.py"],
        capture_output=True, text=True,
    )
    pids = [p for p in result.stdout.strip().split() if p]
    if not pids:
        logger.warning("Процесс бота не найден, перезагрузка прайса пропущена")
        return
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGUSR1)
            logger.info("SIGUSR1 отправлен процессу %s", pid)
        except (ProcessLookupError, ValueError):
            pass


if __name__ == "__main__":
    try:
        download()
        notify_bot()
    except Exception:
        logger.exception("Ошибка обновления прайса")
        raise SystemExit(1)
