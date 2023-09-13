import numpy as np
import scipy.stats as stats

def get_normal_distribution(min_value, max_value, tail_probability, mean_max_modifier, under_min_modifier, mean=None):
    if min_value >= max_value:
        raise ValueError('Minimum value must be less than maximum value')
    
    # Calculate the desired range
    range = max_value - min_value
    
    # Calculate the z-score corresponding to the tail probability
    z_score = stats.norm.ppf(1 - tail_probability / 2)
    
    # Calculate the standard deviation to achieve the desired range
    std_deviation = range / (2 * z_score)
    
    # Calculate the mean as the midpoint between min and max
    mean = mean if mean else (min_value + max_value * mean_max_modifier) / 2
    
    # Generate a random value from a normal distribution
    random_value = np.random.normal(mean, std_deviation)
    
    # Ensure the value is within the specified range
    if random_value < min_value:
        random_value = mean + np.random.uniform(0, under_min_modifier * ((min_value + max_value) / 2))
    
    return random_value