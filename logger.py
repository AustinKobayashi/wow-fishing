"""Append bot output to daily logs and stdout."""
import datetime
import threading
from configuration import DATA_DIR

LOG_FOLDER = DATA_DIR / 'logs'
LOG_FILE = '{}-log.txt'
_lock = threading.Lock()


def log(message):
    now = datetime.datetime.now()
    with _lock:
        LOG_FOLDER.mkdir(parents=True, exist_ok=True)
        with (LOG_FOLDER / LOG_FILE.format(now.strftime('%d-%m-%Y'))).open('a', encoding='utf-8') as output:
            output.write(f'[{now:%d-%m-%Y %H:%M:%S}] {message}\n')
        print(message, flush=True)
