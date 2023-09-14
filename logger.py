
import os
import datetime

LOG_FOLDER = 'logs'
LOG_FILE = '{}-log.txt'

def log(message):
    current_date = datetime.datetime.now().strftime('%d-%m-%Y')

    if not os.path.exists(LOG_FOLDER):
        os.makedirs(LOG_FOLDER)

    current_datetime = datetime.datetime.now().strftime('[%d-%m-%Y %H:%M:%S]')

    with open(os.path.join(LOG_FOLDER, LOG_FILE.format(current_date)), 'a') as log_file:
        log_file.write(f'[{current_datetime}] {message}\n')
    
    print(message)
