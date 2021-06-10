import random
import numpy as np
import tensorflow as tf
from collections import deque


class PrioritizedReplayBuffer():
    def __init__(self, maxlen):
        self.curlen = 0
        self.buffer = deque(maxlen=maxlen)
        self.priorities = deque(maxlen=maxlen)

    def add(self, experience):
        self.curlen+=1
        self.buffer.append(experience)
        self.priorities.append(max(self.priorities, default=1))

    def get_probabilities(self, priority_scale):
        scaled_priorities = np.array(self.priorities) ** priority_scale
        sample_probabilities = scaled_priorities / sum(scaled_priorities)
        return sample_probabilities

    def get_importance(self, probabilities):
        importance = 1 / len(self.buffer) * 1 / probabilities
        importance_normalized = importance / max(importance)
        return importance_normalized

    def sample(self, batch_size, priority_scale=1.0):
        sample_size = min(len(self.buffer), batch_size)
        sample_probs = self.get_probabilities(priority_scale)
        sample_indices = random.choices(range(len(self.buffer)), k=sample_size, weights=sample_probs)
        samples = np.array(self.buffer, dtype=object)[sample_indices]
        importance = self.get_importance(sample_probs[sample_indices])
        return samples, importance, sample_indices

    def set_priorities(self, indices, errors, offset=0.1):
        for i, e in zip(indices, errors):
            if int(i) >= self.curlen or int(i) <= 0:
                print("HERE IS THE I:", i, "as int", int(i), "max", self.curlen, "len", len(self.priorities), flush=True)
            self.priorities[int(i)] = abs(e) + offset

    def get_buffer_length(self):
        return len(self.buffer)
