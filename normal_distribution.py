"""The lower-truncated timing model used for both sampling and UI predictions."""
import math
import numpy as np
from scipy.stats import norm, truncnorm


def calculate_z_score(tail_probability):
    return float(norm.isf(tail_probability))


def distribution(min_value, max_value, tail_probability, mean_max_modifier, mean=None):
    if min_value >= max_value or not 0 < tail_probability < 1:
        raise ValueError('Invalid timing range or tail probability.')
    sigma = (max_value - min_value) / (2 * norm.isf(tail_probability / 2))
    mu = mean if mean is not None else (min_value + max_value * mean_max_modifier) / 2
    return truncnorm((min_value - mu) / sigma, math.inf, loc=mu, scale=sigma)


def get_normal_distribution(min_value, max_value, tail_probability,
                            mean_max_modifier, under_min_modifier, mean=None):
    # under_min_modifier remains accepted for existing callers, but was always unused.
    # Sampling the truncated normal directly avoids recursive rejection overflow.
    return float(distribution(min_value, max_value, tail_probability,
                              mean_max_modifier, mean).rvs())


def for_config(config, prefix, mean=None):
    modifier = config['REEL_TIME_MEAN_MAX_MODIFIER'] if prefix == 'REEL_TIME' else .6
    return distribution(config[prefix + '_MIN'], config[prefix + '_MAX'],
                        config[prefix + '_TAIL_PROBABILITY'], modifier, mean)


def preview(config, mode, scenario):
    if mode not in ('reel', 'cast'):
        raise ValueError('Choose reel or cast.')
    if isinstance(scenario, bool) or not isinstance(scenario, (int, float)):
        raise ValueError('Scenario must be numeric.')
    if not math.isfinite(scenario) or not 0 <= scenario <= 600:
        raise ValueError('Scenario must be between 0 and 600 seconds.')
    if mode == 'cast' and scenario < config['REEL_TIME_MIN']:
        raise ValueError('The conditional reel sample must be at least the reel minimum.')
    reel = for_config(config, 'REEL_TIME')
    model = reel if mode == 'reel' else for_config(config, 'CAST_TIME', scenario)
    prefix = 'REEL_TIME' if mode == 'reel' else 'CAST_TIME'
    minimum = config[prefix + '_MIN']
    mu, sigma = float(model.kwds['loc']), float(model.kwds['scale'])
    cutoff = config['DONT_REEL_TIME']
    # Quantiles keep a useful viewport even for strongly truncated distributions.
    right = max(minimum + .1, float(model.ppf(.9995)))
    if mode == 'reel':
        right = max(right, min(cutoff * 1.1, float(model.ppf(.999999))))
    xs = np.linspace(minimum, right, 321)
    if mode == 'reel' and minimum < cutoff < right:
        xs = np.sort(np.append(xs, cutoff))
    effective = min(cutoff, (config['WOW_FISHING_TIME'] - scenario) / 2)
    return dict(mode=mode, scenario=scenario, minimum=minimum, mu=mu, sigma=sigma,
                mean=float(model.mean()), cutoff=cutoff,
                xmin=max(0, minimum - (right - minimum) * .08), xmax=right,
                ymax=float(model.pdf(max(minimum, mu))) * 1.15,
                points=np.column_stack((xs, model.pdf(xs))).tolist(),
                cutoff_skip=float(reel.sf(cutoff)), effective_limit=effective,
                combined_skip=float(reel.sf(effective)),
                guard_blocks=scenario <= config['REEL_TIME_DELAY_AFTER_CAST'],
                recast_due=scenario > config['LAST_CAST_MAX'],
                above_reference=float(model.sf(config[prefix + '_MAX'])))
