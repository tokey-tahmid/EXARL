import time
import os
import math
import json
import csv
import random
import tensorflow as tf
import torch
import sys
import gym
import pickle
import exarl as erl
from exarl.base.comm_base import ExaComm
from tensorflow import keras
from collections import deque
from datetime import datetime
import numpy as np
from exarl.agents.agent_vault._prioritized_replay import PrioritizedReplayBuffer
import exarl.utils.candleDriver as cd
from exarl.utils import log
from exarl.utils.introspect import introspectTrace
from tensorflow.compat.v1.keras.backend import set_session

import bindsnet.network as network
from bindsnet.network import Network
from bindsnet.network.nodes import Input, LIFNodes
from bindsnet.network.topology import Connection
from bindsnet.learning import PostPre
from bindsnet.encoding import poisson

if ExaComm.num_learners > 1:
    import horovod.tensorflow as hvd
    multiLearner = True
else:
    multiLearner = False

logger = log.setup_logger(__name__, cd.lookup_params('log_level', [3, 3]))

class LossHistory(keras.callbacks.Callback):
    def on_train_begin(self, logs={}):
        self.loss = []

    def on_batch_end(self, batch, logs={}):
        self.loss.append(logs.get('loss'))

class DQN(erl.ExaAgent):
    def __init__(self, env, is_learner):

        self.is_learner = is_learner
        self.model = None
        self.target_model = None
        self.target_weights = None
        self.device = None
        self.mirrored_strategy = None

        self.env = env
        self.agent_comm = ExaComm.agent_comm
        self.rank = self.agent_comm.rank
        self.size = self.agent_comm.size
        self.training_time = 0
        self.ntraining_time = 0
        self.dataprep_time = 0
        self.ndataprep_time = 0

        self.enable_xla = True if cd.run_params['xla'] == "True" else False
        if self.enable_xla:
            tf.config.optimizer.set_jit(True)
            from tensorflow.keras.mixed_precision import experimental as mixed_precision
            policy = mixed_precision.Policy('mixed_float16')
            mixed_precision.set_policy(policy)
        self.results_dir = cd.run_params['output_dir']
        self.gamma = cd.run_params['gamma']
        self.epsilon = cd.run_params['epsilon']
        self.epsilon_min = cd.run_params['epsilon_min']
        self.epsilon_decay = cd.run_params['epsilon_decay']
        self.learning_rate = cd.run_params['learning_rate']
        self.batch_size = cd.run_params['batch_size']
        self.tau = cd.run_params['tau']
        self.model_type = cd.run_params['model_type']

        if self.model_type == 'MLP':
            self.dense = cd.run_params['dense']

        if self.model_type == 'LSTM':
            self.lstm_layers = cd.run_params['lstm_layers']
            self.gauss_noise = cd.run_params['gauss_noise']
            self.regularizer = cd.run_params['regularizer']
            self.clipnorm = cd.run_params['clipnorm']
            self.clipvalue = cd.run_params['clipvalue']
        self.activation = cd.run_params['activation']
        self.out_activation = cd.run_params['out_activation']
        self.optimizer = cd.run_params['optimizer']
        self.loss = cd.run_params['loss']
        self.n_actions = cd.run_params['nactions']
        self.priority_scale = cd.run_params['priority_scale']
        self.is_discrete = (type(env.action_space) == gym.spaces.discrete.Discrete)
        if not self.is_discrete:
            env.action_space.n = self.n_actions
            self.actions = np.linspace(env.action_space.low, env.action_space.high, self.n_actions)
        self.dtype_action = np.array(self.env.action_space.sample()).dtype
        self.dtype_observation = self.env.observation_space.sample().dtype
        if ExaComm.is_learner():
            logger.info("Setting GPU rank", self.rank)
            config = tf.compat.v1.ConfigProto(device_count={'GPU': 1, 'CPU': 1})
        else:
            logger.info("Setting no GPU rank", self.rank)
            config = tf.compat.v1.ConfigProto(device_count={'GPU': 0, 'CPU': 1})
        self.device = self._get_device()

        config.gpu_options.allow_growth = True
        sess = tf.compat.v1.Session(config=config)
        tf.compat.v1.keras.backend.set_session(sess)
        if self.is_learner:
            with tf.device(self.device):
                self.model = self._build_model()
                self.model.compile(loss=self.loss, optimizer=self.optimizer)
                self.model.summary()
        else:
            self.model = None
        with tf.device('/CPU:0'):
            self.target_model = self._build_model()
            self.target_model._name = "target_model"
            self.target_model.compile(loss=self.loss, optimizer=self.optimizer)
            self.target_weights = self.target_model.get_weights()

        if multiLearner and ExaComm.is_learner():
            hvd.init(comm=ExaComm.learner_comm.raw())
            self.first_batch = 1
            self.loss_fn = cd.candle.build_loss(self.loss, cd.kerasDefaults, reduction='none')
            self.opt = cd.candle.build_optimizer(self.optimizer, self.learning_rate * hvd.size(), cd.kerasDefaults)
        self.maxlen = cd.run_params['mem_length']
        self.replay_buffer = PrioritizedReplayBuffer(maxlen=self.maxlen)

    def _get_device(self):
        cpus = tf.config.experimental.list_physical_devices('CPU')
        gpus = tf.config.experimental.list_physical_devices('GPU')
        ngpus = len(gpus)
        logger.info('Number of available GPUs: {}'.format(ngpus))
        if ngpus > 0:
            gpu_id = self.rank % ngpus
            return '/GPU:{}'.format(gpu_id)
        else:
            return '/CPU:0'

    def _build_model(self):
        if self.model_type == 'SNN':
            return self._build_snn_model()
        elif self.model_type == 'MLP':
            from exarl.agents.agent_vault._build_mlp import build_model
            return build_model(self)
        elif self.model_type == 'LSTM':
            from exarl.agents.agent_vault._build_lstm import build_model
            return build_model(self)
        else:
            sys.exit("Oops! That was not a valid model type. Try again...")

    def _build_snn_model(self):
        net = Network()
        input_layer = Input(n=self.env.observation_space.shape[0])  # Use the observation space size as input size
        middle_layer = LIFNodes(n=128)  # Example size, you can adjust
        output_layer = LIFNodes(n=self.env.action_space.n)  # Use the action space size as output size

        # Connect layers
        input_middle_conn = Connection(source=input_layer, target=middle_layer, rule=PostPre, nu=(1e-4, 1e-2))
        middle_output_conn = Connection(source=middle_layer, target=output_layer, rule=PostPre, nu=(1e-4, 1e-2))

        # Add layers and connections to the network
        net.add_layer(input_layer, name="Input")
        net.add_layer(middle_layer, name="Middle")
        net.add_layer(output_layer, name="Output")
        net.add_connection(input_middle_conn, source="Input", target="Middle")
        net.add_connection(middle_output_conn, source="Middle", target="Output")

        return net

    def set_learner(self):
        logger.debug(
            "Agent[{}] - Creating active model for the learner".format(self.rank)
        )

    def remember(self, state, action, reward, next_state, done):
        lost_data = self.replay_buffer.add((state, action, reward, next_state, done))
        if lost_data and self.priority_scale:
            print("Priority replay buffer size too small. Data loss negates replay effect!", flush=True)

    def get_action(self, state):
        random.seed(datetime.now())
        random_data = os.urandom(4)
        np.random.seed(int.from_bytes(random_data, byteorder="big"))
        rdm = np.random.rand()
        if rdm <= self.epsilon:
            self.epsilon_adj()
            action = random.randrange(self.env.action_space.n)
            return action, 0
        else:
            # Use BindsNET's run function:
            print(state)
            encoded_state = poisson(datum=torch.tensor(state, dtype=torch.float), time=25)
            # Convert PyTorch tensor to NumPy array
            encoded_state_np = encoded_state.detach().cpu().numpy()
            # Get the output from the Keras model
            # Reshape the input to have shape (batch_size, 1, 4)
            print("Shape of encoded_state_np:", encoded_state_np.shape)

            reshaped_state = np.reshape(encoded_state_np, (-1, 25, 5))
            # Get the output from the Keras model
            output = self.target_model(reshaped_state)
            act_values = tf.reduce_sum(output, axis=0)
            action = np.argmax(act_values[0])
            return action, 1

    @introspectTrace()
    def action(self, state):
        action, policy = self.get_action(state)
        if not self.is_discrete:
            action = [self.actions[action]]
        return action, policy

    @introspectTrace()
    def calc_target_f(self, exp):
        state, action, reward, next_state, done = exp
        np_state = np.array(state, dtype=self.dtype_observation).reshape(1, 1, len(state))
        np_next_state = np.array(next_state, dtype=self.dtype_observation).reshape(1, 1, len(next_state))
        expectedQ = 0
        if not done:
            with tf.device(self.device):
                expectedQ = self.gamma * np.amax(self.target_model.predict(np_next_state)[0])
        target = reward + expectedQ
        with tf.device(self.device):
            target_f = self.target_model.predict(np_state)
        # For handling continuous to discrete actions
        action_idx = action if self.is_discrete else np.where(self.actions == action)[1]
        target_f[0][action_idx] = target
        return target_f[0]

    def has_data(self):
        return (self.replay_buffer.get_buffer_length() >= self.batch_size)

    @introspectTrace()
    def generate_data(self):
        if not self.has_data():
            batch_states = np.zeros((self.batch_size, 1, self.env.observation_space.shape[0]), dtype=self.dtype_observation)
            batch_target = np.zeros((self.batch_size, self.env.action_space.n), dtype=self.dtype_action)
            indices = -1 * np.ones(self.batch_size)
            importance = np.ones(self.batch_size)
        else:
            minibatch, importance, indices = self.replay_buffer.sample(self.batch_size, priority_scale=self.priority_scale)
            batch_target = list(map(self.calc_target_f, minibatch))
            batch_states = [np.array(exp[0], dtype=self.dtype_observation).reshape(1, 1, len(exp[0]))[0] for exp in minibatch]
            batch_states = np.reshape(batch_states, [len(minibatch), 1, len(minibatch[0][0])])
            batch_target = np.reshape(batch_target, [len(minibatch), self.env.action_space.n])

        if self.priority_scale > 0:
            yield batch_states, batch_target, indices, importance
        else:
            yield batch_states, batch_target

    @introspectTrace()
    def train(self, batch):
        ret = None
        if self.is_learner:
            start_time = time.time()
            with tf.device(self.device):
                if self.priority_scale > 0:
                    if multiLearner:
                        loss = self.training_step(batch)
                    else:
                        loss = LossHistory()
                        sample_weight = batch[3] ** (1 - self.epsilon)
                        self.model.fit(batch[0], batch[1], epochs=1, batch_size=1, verbose=0, callbacks=loss, sample_weight=sample_weight)
                        loss = loss.loss
                    ret = batch[2], loss
                else:
                    if multiLearner:
                        loss = self.training_step(batch)
                    else:
                        self.model.fit(batch[0], batch[1], epochs=1, verbose=0)
            end_time = time.time()
            self.training_time += (end_time - start_time)
            self.ntraining_time += 1
            logger.info('Agent[{}]- Training: {} '.format(self.rank, (end_time - start_time)))
            start_time_episode = time.time()
            logger.info('Agent[%s] - Target update time: %s ' % (str(self.rank), str(time.time() - start_time_episode)))
        else:
            logger.warning('Training will not be done because this instance is not set to learn.')
        return ret

    @tf.function
    def training_step(self, batch):
        with tf.GradientTape() as tape:
            probs = self.model(batch[0], training=True)
            if len(batch) > 2:
                sample_weight = batch[3] * (1 - self.epsilon)
            else:
                sample_weight = np.ones(len(batch[0]))
            loss_value = self.loss_fn(batch[1], probs, sample_weight=sample_weight)
        tape = hvd.DistributedGradientTape(tape)
        grads = tape.gradient(loss_value, self.model.trainable_variables)
        self.opt.apply_gradients(zip(grads, self.model.trainable_variables))

        if self.first_batch:
            hvd.broadcast_variables(self.model.variables, root_rank=0)
            hvd.broadcast_variables(self.opt.variables(), root_rank=0)
            self.first_batch = 0
        return loss_value

    def set_priorities(self, indices, loss):
        self.replay_buffer.set_priorities(indices, loss)

    def get_weights(self):
        logger.debug("Agent[%s] - get target weight." % str(self.rank))
        return self.target_model.get_weights()

    def set_weights(self, weights):
        logger.info("Agent[%s] - set target weight." % str(self.rank))
        logger.debug("Agent[%s] - set target weight: %s" % (str(self.rank), weights))
        with tf.device(self.device):
            self.target_model.set_weights(weights)

    @introspectTrace()
    def target_train(self):
        if self.is_learner:
            logger.info("Agent[%s] - update target weights." % str(self.rank))
            with tf.device(self.device):
                model_weights = self.model.get_weights()
                target_weights = self.target_model.get_weights()
            for i in range(len(target_weights)):
                target_weights[i] = (
                    self.tau * model_weights[i] + (1 - self.tau) * target_weights[i]
                )
            self.set_weights(target_weights)
        else:
            logger.warning(
                "Weights will not be updated because this instance is not set to learn."
            )

    def epsilon_adj(self):
        if self.epsilon > self.epsilon_min:
            self.epsilon *= self.epsilon_decay

    def load(self, filename):
        layers = self.target_model.layers
        with open(filename, 'rb') as f:
            pickle_list = pickle.load(f)

        for layerId in range(len(layers)):
            layers[layerId].set_weights(pickle_list[layerId][1])

    def save(self, filename):
        layers = self.target_model.layers
        pickle_list = []
        for layerId in range(len(layers)):
            weigths = layers[layerId].get_weights()
            pickle_list.append([layers[layerId].name, weigths])

        with open(filename, 'wb') as f:
            pickle.dump(pickle_list, f, -1)

    def update(self):
        logger.info("Implement update method in dqn.py")

    def monitor(self):
        logger.info("Implement monitor method in dqn.py")
