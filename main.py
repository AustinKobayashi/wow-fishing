import os
import datetime
import pyaudio
import numpy as np
import time
import matplotlib.pyplot as plt
import logger as lg
import servo as sv
import normal_distribution as nd

# Configuration
FORMAT = pyaudio.paInt16  # Format of audio data (16-bit)
CHANNELS = 1              # Number of audio channels (1 for mono)
RATE = 44100              # Sample rate (samples per second)
THRESHOLD = 7000          # Adjust this threshold as needed
CHECK_INTERVAL = 0.01     # Check interval in seconds

REEL_TIME_MIN = 0.9
REEL_TIME_MAX = 2
REEL_TIME_TAIL_PROBABILITY = 0.005
REEL_TIME_MEAN_MAX_MODIFIER = 0.6
REEL_TIME_UNDER_MIN_MODIFIER = 10

CAST_TIME_MIN = 1.1
CAST_TIME_MAX = 3
CAST_TIME_TAIL_PROBABILITY = 0.0000005
CAST_TIME_MEAN_MAX_MODIFIER = 0.6
CAST_TIME_UNDER_MIN_MODIFIER = 2

LAST_CAST_MAX = 60

IDLE_TIME_MIN = 10
IDLE_TIME_MAX = 120
IDLE_PROBABILITY = 0.013

MAX_RUN_TIME = 60 * 60 * 3

PLOTS_FOLDER = 'plots'


def idle(min_duration, max_duration):
    sleep_duration = np.random.uniform(min_duration, max_duration)
    lg.log(f'Idle for {sleep_duration:.2f} seconds')
    time.sleep(sleep_duration)


def main():  
    try:
        start_time = time.time()
        last_cast_time = time.time()

        max_volume = 0
        volumes = []
        reel_times = []
        cast_times = []
        while True:
            if time.time() - start_time > MAX_RUN_TIME:
                lg.log('Max run time reached, exiting...')
                break

            if time.time() - last_cast_time > LAST_CAST_MAX:
                lg.log('Did not cast, recasting...')
                sv.press_fishing_button('Casting')
                last_cast_time = time.time()

            audio = pyaudio.PyAudio()
    
            stream = audio.open(format=FORMAT,
                                channels=CHANNELS,
                                rate=RATE,
                                input=True,
                                frames_per_buffer=1024)

            time.sleep(0.01)

            audio_data = np.frombuffer(stream.read(1024), dtype=np.int16)
            audio_level = np.abs(audio_data).mean()
            
            if audio_level > THRESHOLD:
                lg.log(f'\t\tAudio level above threshold: {audio_level}')

                reel_time = nd.get_normal_distribution(REEL_TIME_MIN, REEL_TIME_MAX, REEL_TIME_TAIL_PROBABILITY, REEL_TIME_MEAN_MAX_MODIFIER, REEL_TIME_UNDER_MIN_MODIFIER)
                reel_times.append(reel_time)

                lg.log(f'Reel time: {reel_time}')
                time.sleep(reel_time)
                
                sv.press_fishing_button('Reeling')

                cast_time = nd.get_normal_distribution(CAST_TIME_MIN, CAST_TIME_MAX, CAST_TIME_TAIL_PROBABILITY, CAST_TIME_MEAN_MAX_MODIFIER, CAST_TIME_UNDER_MIN_MODIFIER, reel_time)
                cast_times.append(cast_time)

                lg.log(f'Cast time: {cast_time}')
                time.sleep(cast_time)

                sv.press_fishing_button('Casting')

                volumes.append(audio_level)
                if audio_level > max_volume:
                    max_volume = audio_level

                last_cast_time = time.time()

                if np.random.rand() < IDLE_PROBABILITY:
                    idle(IDLE_TIME_MIN, IDLE_TIME_MAX)
                    last_cast_time = time.time()
                    
            stream.stop_stream()
            stream.close()
            audio.terminate()

            time.sleep(CHECK_INTERVAL)
    
    except KeyboardInterrupt:
        pass
    
    lg.log('Exiting...')
    sv.cleanup()
    lg.log(f'Maximum volume: {max_volume}')
    plt.figure(figsize=(15, 5))

    plt.subplot(1, 3, 1)
    plt.hist(volumes, bins=100, density=True, alpha=0.7, color='blue', edgecolor='black')
    plt.xlabel('Volume')
    plt.ylabel('Frequency')
    plt.title('Volume Distribution')

    plt.subplot(1, 3, 2)
    plt.hist(reel_times, bins=100, density=True, alpha=0.7, color='blue', edgecolor='black')
    plt.xlabel('Seconds')
    plt.ylabel('Frequency')
    plt.title('Reel Time Distribution')

    plt.subplot(1, 3, 3)
    plt.hist(cast_times, bins=100, density=True, alpha=0.7, color='blue', edgecolor='black')
    plt.xlabel('Seconds')
    plt.ylabel('Frequency')
    plt.title('Cast Time Distribution')

    plt.tight_layout()
    plt.savefig(os.path.join(PLOTS_FOLDER, f'{datetime.datetime.now().strftime("%d-%m-%Y")}-plot.png'))

if __name__ == '__main__':
    main()
