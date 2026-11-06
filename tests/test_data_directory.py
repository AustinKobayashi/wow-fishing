import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DataDirectoryTests(unittest.TestCase):
    def test_data_directory_persists_settings_logs_and_stats_across_processes(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data'
            env = dict(os.environ, FISHING_DATA_DIR=str(data))
            script = '''
from configuration import CONFIG_PATH, DEFAULTS, STATS_PATH, save_config
from logger import LOG_FOLDER, log
from main import STATS_PATH as runtime_stats
assert CONFIG_PATH.parent == LOG_FOLDER.parent == STATS_PATH.parent
assert runtime_stats == STATS_PATH
save_config(dict(DEFAULTS, THRESHOLD=8000))
log('Volume persistence test')
STATS_PATH.write_text('test stats', encoding='utf-8')
'''
            subprocess.run([sys.executable, '-c', script], env=env, check=True, capture_output=True)
            subprocess.run([sys.executable, '-c',
                            'from configuration import load_config\nassert load_config()["THRESHOLD"] == 8000'],
                           env=env, check=True, capture_output=True)
            self.assertEqual((data / 'stats.txt').read_text(encoding='utf-8'), 'test stats')
            logs = list((data / 'logs').glob('*-log.txt'))
            self.assertEqual(len(logs), 1)
            self.assertIn('Volume persistence test', logs[0].read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
