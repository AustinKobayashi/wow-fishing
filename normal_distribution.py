import numpy as np
import scipy.stats as stats
import math
import matplotlib.pyplot as plt

def calculate_z_score(tail_probability):
    # Calculate the z-score using the CDF of the standard normal distribution
    if tail_probability >= 1:
        return float('inf')  # Maximum z-score for the right tail
    elif tail_probability <= 0:
        return float('-inf')  # Minimum z-score for the left tail
    else:
        # Calculate the z-score using the inverse CDF expression
        return math.sqrt(2) * math.erfinv(1 - 2 * tail_probability)
    
    
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


scipy_ranges = [stats.norm.ppf(1 - 0.005 / 2) for i in range(10000)]
numpy_ranges = [calculate_z_score(0.005) for i in range(10000)]

print(f'Scipy mean: {np.mean(scipy_ranges)}')
print(f'Numpy mean: {np.mean(numpy_ranges)}')

plt.figure(figsize=(12, 6))

plt.subplot(1, 2, 1)
plt.hist(scipy_ranges, bins=100, density=True, alpha=0.7, color='blue', edgecolor='black')
plt.xlabel('Seconds')
plt.ylabel('Frequency')
plt.title('Scipy Z-Score Distribution')

plt.subplot(1, 2, 2)
plt.hist(numpy_ranges, bins=100, density=True, alpha=0.7, color='blue', edgecolor='black')
plt.xlabel('Seconds')
plt.ylabel('Frequency')
plt.title('Numpy Z-Score Distribution')

plt.tight_layout()
plt.show()