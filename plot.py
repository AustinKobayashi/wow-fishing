import matplotlib.pyplot as plt

def get_plots(reel_time_samples, cast_time_samples):
    plt.figure(figsize=(12, 6))

    plt.subplot(1, 2, 1)
    plt.hist(reel_time_samples, bins=100, density=True, alpha=0.7, color='blue', edgecolor='black')
    plt.xlabel('Seconds')
    plt.ylabel('Frequency')
    plt.title('Reel Time Distribution')

    plt.subplot(1, 2, 2)
    plt.hist(cast_time_samples, bins=100, density=True, alpha=0.7, color='blue', edgecolor='black')
    plt.xlabel('Seconds')
    plt.ylabel('Frequency')
    plt.title('Cast Time Distribution')

    plt.tight_layout()
    plt.show()