"""Editable runtime settings shared by the bot and web UI."""
import json
import math
import os
from pathlib import Path
from scipy.stats import norm

SCHEMA = {'timing': [['REEL_TIME_MIN', 'Reel minimum', 's', 0.985, 0.001, 20, 0.001],
            ['REEL_TIME_MAX', 'Reel spread reference', 's', 4, 0.002, 60, 0.001],
            ['REEL_TIME_TAIL_PROBABILITY',
             'Reel tail parameter',
             'probability',
             0.0005,
             1e-10,
             0.9,
             'any'],
            ['REEL_TIME_MEAN_MAX_MODIFIER', 'Reel mean modifier', 'ratio', 0.4, 0, 2, 0.01],
            ['DONT_REEL_TIME', 'Hard reel cutoff', 's', 2.1, 0, 60, 0.01],
            ['CAST_TIME_MIN', 'Cast minimum', 's', 0.985, 0.001, 20, 0.001],
            ['CAST_TIME_MAX', 'Cast spread reference', 's', 3, 0.002, 60, 0.001],
            ['CAST_TIME_TAIL_PROBABILITY',
             'Cast tail parameter',
             'probability',
             5e-07,
             1e-10,
             0.9,
             'any'],
            ['WOW_FISHING_TIME', 'Fishing window', 's', 28, 1, 120, 0.1],
            ['REEL_TIME_DELAY_AFTER_CAST', 'Ignore audio after cast', 's', 1.5, 0, 60, 0.1]],
 'detection': [['THRESHOLD', 'Audio threshold', 'amplitude', 7000, 0, 32768, 1],
               ['CHECK_INTERVAL', 'Check interval', 's', 0.01, 0.001, 5, 0.001],
               ['RATE', 'Sample rate', 'Hz', 48000, 8000, 192000, 1],
               ['CHANNELS', 'Audio channels', 'channels', 1, 1, 2, 1]],
 'session': [['MAX_RUN_TIME', 'Maximum runtime', 's', 18000, 1, 86400, 1],
             ['LAST_CAST_MAX', 'Recast timeout', 's', 60, 1, 600, 1],
             ['IDLE_TIME_MIN', 'Idle minimum', 's', 10, 0, 3600, 1],
             ['IDLE_TIME_MAX', 'Idle maximum', 's', 120, 0, 3600, 1],
             ['IDLE_PROBABILITY',
              'Idle probability per event',
              'probability',
              0.0013,
              0,
              1,
              'any']],
 'servo': [['SERVO_CLICK', 'Press pulse width', 'µs', 945, 500, 2500, 1],
           ['SERVO_SPEED', 'Press / release hold', 's', 0.1, 0.01, 2, 0.01]]}
DEFAULTS = {row[0]: row[3] for rows in SCHEMA.values() for row in rows}
DATA_DIR = Path(os.environ.get('FISHING_DATA_DIR', Path(__file__).resolve().parent))
CONFIG_PATH = DATA_DIR / 'config.json'
STATS_PATH = DATA_DIR / 'stats.txt'


def validate(values):
    if not isinstance(values, dict) or values.keys() != DEFAULTS.keys():
        raise ValueError('Supply exactly the editable configuration fields.')
    result = {}
    for rows in SCHEMA.values():
        for key, label, unit, default, low, high, step in rows:
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f'{label} must be a number.')
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{label} must be between {low} and {high}.')
            if key in {'RATE', 'CHANNELS', 'THRESHOLD'}:
                if value != int(value):
                    raise ValueError(f'{label} must be a whole number.')
                value = int(value)
            result[key] = value
    for prefix in ('REEL_TIME', 'CAST_TIME'):
        minimum, maximum = result[prefix + '_MIN'], result[prefix + '_MAX']
        if minimum >= maximum:
            raise ValueError(f'{prefix}: minimum must be less than spread reference.')
        sigma = (maximum - minimum) / (2 * norm.isf(result[prefix + '_TAIL_PROBABILITY'] / 2))
        mean = ((minimum + maximum * result['REEL_TIME_MEAN_MAX_MODIFIER']) / 2
                if prefix == 'REEL_TIME' else result['REEL_TIME_MIN'])
        if norm.sf((minimum - mean) / sigma) < 1e-9:
            raise ValueError(f'{prefix}: minimum is too far above the mean. Increase the spread or reel mean.')
    if result['IDLE_TIME_MIN'] > result['IDLE_TIME_MAX']:
        raise ValueError('Idle minimum must not exceed idle maximum.')
    return result


def load_config(path=CONFIG_PATH):
    path = Path(path)
    if not path.exists():
        return DEFAULTS.copy()
    return validate(json.loads(path.read_text(encoding='utf-8')))


def save_config(values, path=CONFIG_PATH):
    values = validate(values)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(values, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
    return values
