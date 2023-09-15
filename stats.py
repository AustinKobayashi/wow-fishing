import matplotlib.pyplot as plt

# Initialize variables with default values
maximum_volume = None
mean_volume = None
idle_counts = None
reel_times = []
cast_times = []

# Read data from the file
try:
    with open("stats-back.txt", "r") as data_file:
        for line in data_file:
            line = line.strip()
            if line.startswith("Maximum volume:"):
                maximum_volume = float(line.split(":")[1].strip())
            elif line.startswith("Mean volume:"):
                mean_volume = float(line.split(":")[1].strip())
            elif line.startswith("Idle counts:"):
                idle_counts = int(line.split(":")[1].strip())
            elif line.startswith("Reel times:"):
                reel_times = [float(x.strip()) for x in line.split(":")[1].strip()[1:-1].split(",")]
            elif line.startswith("Cast times:"):
                cast_times = [float(x.strip()) for x in line.split(":")[1].strip()[1:-1].split(",")]

except FileNotFoundError:
    print("Data file 'data.txt' not found.")
except Exception as e:
    print(f"Error reading data: {e}")

# Test: Print the extracted values
print(f"Maximum volume: {maximum_volume}")
print(f"Mean volume: {mean_volume}")
print(f"Idle counts: {idle_counts}")
print(f"Reel times: {reel_times}")
print(f"Cast times: {cast_times}")

plt.figure(figsize=(10, 5))

plt.subplot(1, 2, 1)
plt.hist(reel_times, bins=20, edgecolor='black', alpha=0.7)
plt.title("Reel Times Histogram")
plt.xlabel("Reel Time")
plt.ylabel("Frequency")

plt.subplot(1, 2, 2)
plt.hist(cast_times, bins=20, edgecolor='black', alpha=0.7)
plt.title("Cast Times Histogram")
plt.xlabel("Cast Time")
plt.ylabel("Frequency")

plt.tight_layout()  # Adjust layout to prevent overlap
plt.show()
