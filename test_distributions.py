import normal_distribution as nd
import plot as pl

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

NUM_SAMPLES = 10000


reel_time_samples = [nd.get_normal_distribution(REEL_TIME_MIN, REEL_TIME_MAX, REEL_TIME_TAIL_PROBABILITY, REEL_TIME_MEAN_MAX_MODIFIER, REEL_TIME_UNDER_MIN_MODIFIER) for _ in range(NUM_SAMPLES)]
cast_time_samples = []
for i in range(len(reel_time_samples)):
    cast_time_samples.append(nd.get_normal_distribution(CAST_TIME_MIN, CAST_TIME_MAX, CAST_TIME_TAIL_PROBABILITY, CAST_TIME_MEAN_MAX_MODIFIER, CAST_TIME_UNDER_MIN_MODIFIER, reel_time_samples[i]))

pl.get_plots(reel_time_samples, cast_time_samples)