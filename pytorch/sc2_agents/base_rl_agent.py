from pysc2.agents.base_agent import BaseAgent

from pytorch.utils.epsilon import Epsilon
import time
import math
import numpy as np
import copy
import os.path
from pytorch.utils.replay_memory import ReplayMemory, Transition
from collections import deque
import matplotlib.pyplot as plt
plt.ion()

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable

from pysc2.lib import actions
from pysc2.lib import features


_PLAYER_RELATIVE = features.SCREEN_FEATURES.player_relative.index
_PLAYER_FRIENDLY = 1
_PLAYER_NEUTRAL = 3  # beacon/minerals
_PLAYER_HOSTILE = 4
_NO_OP = actions.FUNCTIONS.no_op.id
_MOVE_SCREEN = actions.FUNCTIONS.Move_screen.id
_ATTACK_SCREEN = actions.FUNCTIONS.Attack_screen.id
_SELECT_ARMY = actions.FUNCTIONS.select_army.id
_NOT_QUEUED = [0]
_SELECT_ALL = [0]
_SELECT_POINT = actions.FUNCTIONS.select_point.id


# input is screen at 17x84x84
# output is screen at 2x84x84
#    1st dim = select point
#    2nd dim = move screen
class DQNCNN(nn.Module):
  def __init__(self, *args):
    super(DQNCNN, self).__init__(*args)
    self.conv1 = nn.Conv2d(1, 24, kernel_size=3, stride=1, padding=1)
    self.conv2 = nn.Conv2d(24, 24, kernel_size=3, stride=1, padding=2, dilation=2)
    self.conv3 = nn.Conv2d(24, 1, kernel_size=3, stride=1, padding=4, dilation=4)
    # self.conv4 = nn.Conv2d(24, 1, kernel_size=3, stride=1, padding=8, dilation=8)

  def forward(self, x):
    x = F.relu(self.conv1(x))
    x = F.relu(self.conv2(x))
    # x = F.relu(self.conv3(x))
    x = self.conv3(x)
    return x


class BaseRLAgent(BaseAgent):
  def __init__(self):
    super(BaseRLAgent, self).__init__()
    self.training = False
    self.max_frames = 10000000
    self._epsilon = Epsilon(start=1.0, end=0.1, update_increment=0.0001)
    self.gamma = 0.99
    self.train_q_per_step = 4
    self.train_q_batch_size = 256
    self.steps_before_training = 10000
    self.target_q_update_frequency = 10000

    self._Q_weights_path = "./data/SC2QAgent"
    self._Q = DQNCNN()
    if os.path.isfile(self._Q_weights_path):
      self._Q.load_state_dict(torch.load(self._Q_weights_path))
    self._Qt = copy.deepcopy(self._Q)
    # self._Q.cuda()
    # self._Qt.cuda()
    self._optimizer = optim.Adam(self._Q.parameters(), lr=1e-8)
    self._criterion = nn.MSELoss()
    self._memory = ReplayMemory(50000)

    self._loss = deque(maxlen=1000)
    self._max_q = deque(maxlen=1000)
    self._action = None
    self._screen = None
    self._fig = plt.figure()
    self._plot = [plt.subplot(2, 2, i+1) for i in range(4)]

    self._screen_size = 84

  def get_env_action(self, action, obs):
    #import ipdb; ipdb.set_trace()
    action = np.unravel_index(action, [1, self._screen_size, self._screen_size])
    target = [action[2], action[1]]
    command = _MOVE_SCREEN #action[0]   # removing unit selection out of the equation
    # if command == 0:
    #   command = _SELECT_POINT
    # else:
    #   command = _MOVE_SCREEN

    if command in obs.observation["available_actions"]:
      return actions.FunctionCall(command, [[0], target])
    else:
      return actions.FunctionCall(_NO_OP, [])


  '''
    :param
      s = obs.observation["screen"]
    :returns
      action = argmax action
  '''
  def get_action(self, s):
    # greedy
    if np.random.rand() > self._epsilon.value():
      # print("greedy action")
      # s = Variable(torch.from_numpy(s).cuda())
      s = Variable(torch.from_numpy(s))

      s = s.unsqueeze(0).float()
      self._action = self._Q(s).squeeze().cpu().data.numpy()
      return self._action.argmax()
    # explore
    else:
      # print("random choice")
      # action = np.random.choice([0, 1])
      action = 0
      target = np.random.randint(0, self._screen_size, size=2)
      return action * self._screen_size*self._screen_size + target[0] * self._screen_size + target[1]

  def select_friendly_action(self, obs):

    player_relative = obs.observation["feature_screen"][_PLAYER_RELATIVE]
    friendly_y, friendly_x = (player_relative == _PLAYER_FRIENDLY).nonzero()
    target = [int(friendly_x.mean()), int(friendly_y.mean())]
    return actions.FunctionCall(_SELECT_POINT, [[0], target])


  def train(self, env, training=True):
    self._epsilon.isTraining = training
    self.run_loop(env, self.max_frames)
    if self._epsilon.isTraining:
      torch.save(self._Q.state_dict(), self._Q_weights_path)

  def run_loop(self, env, max_frames=0):
    """A run loop to have agents and an environment interact."""
    total_frames = 0
    start_time = time.time()

    action_spec = env.action_spec()
    observation_spec = env.observation_spec()

    self.setup(observation_spec, action_spec)

    try:
      while True:
        obs = env.reset()[0]
        # remove unit selection from the equation by selecting the friendly on every new game.
        select_friendly = self.select_friendly_action(obs)
        obs = env.step([select_friendly])[0]
        # distance = self.get_reward(obs.observation["screen"])

        self.reset()

        while True:
          total_frames += 1

          self._screen = obs.observation["feature_screen"][5]
          s = np.expand_dims(obs.observation["feature_screen"][5], 0)
          # plt.imshow(s[5])
          # plt.pause(0.00001)
          if max_frames and total_frames >= max_frames:
            print("max frames reached")
            return
          if obs.last():
            print("total frames:", total_frames, "Epsilon:", self._epsilon.value())
            self._epsilon.increment()
            break

          action = self.get_action(s)
          env_actions = self.get_env_action(action, obs)
          obs = env.step([env_actions])[0]

          r = obs.reward
          s1 = np.expand_dims(obs.observation["feature_screen"][5], 0)
          done = r > 0
          if self._epsilon.isTraining:
            transition = Transition(s, action, s1, r, done)
            self._memory.push(transition)

          if total_frames % self.train_q_per_step == 0 and total_frames > self.steps_before_training and self._epsilon.isTraining:
            self.train_q()
            # pass

          if total_frames % self.target_q_update_frequency == 0 and total_frames > self.steps_before_training and self._epsilon.isTraining:
            self._Qt = copy.deepcopy(self._Q)
            #self.show_chart()
            # pass

          if not self._epsilon.isTraining and total_frames % 3 == 0:
            #self.show_chart()
            a = 1

    except KeyboardInterrupt:
      pass
    finally:
      print("finished")
      elapsed_time = time.time() - start_time
      print("Took %.3f seconds for %s steps: %.3f fps" % (
          elapsed_time, total_frames, total_frames / elapsed_time))

  def get_reward(self, s):
    player_relative = s[_PLAYER_RELATIVE]
    neutral_y, neutral_x = (player_relative == _PLAYER_NEUTRAL).nonzero()
    neutral_target = [int(neutral_x.mean()), int(neutral_y.mean())]
    friendly_y, friendly_x = (player_relative == _PLAYER_FRIENDLY).nonzero()
    if len(friendly_y) == 0 or len(friendly_x) == 0:   # this is shit
      return 0
    friendly_target = [int(friendly_x.mean()), int(friendly_y.mean())]

    distance_2 = (neutral_target[0]-friendly_target[0])**2 + (neutral_target[1]-friendly_target[1])**2
    distance = math.sqrt(distance_2)
    return -distance


  def show_chart(self):
    self._plot[0].clear()
    self._plot[0].set_xlabel('Last 1000 Training Cycles')
    self._plot[0].set_ylabel('Loss')
    self._plot[0].plot(list(self._loss))

    self._plot[1].clear()
    self._plot[1].set_xlabel('Last 1000 Training Cycles')
    self._plot[1].set_ylabel('Max Q')
    self._plot[1].plot(list(self._max_q))

    self._plot[2].clear()
    self._plot[2].set_title("screen")
    self._plot[2].imshow(self._screen)

    self._plot[3].clear()
    self._plot[3].set_title("action")
    self._plot[3].imshow(self._action)
    plt.pause(0.00001)

  def train_q(self):
    if self.train_q_batch_size >= len(self._memory):
      return

    s, a, s_1, r, done = self._memory.sample(self.train_q_batch_size)
    # s = Variable(torch.from_numpy(s).cuda()).float()
    s = Variable(torch.from_numpy(s)).float()

    #a = Variable(torch.from_numpy(a).cuda()).long().unsqueeze(1)
    a = Variable(torch.from_numpy(a)).long().unsqueeze(1)

    #s_1 = Variable(torch.from_numpy(s_1).cuda(), volatile=True).float()
    s_1 = Variable(torch.from_numpy(s_1), volatile=True).float()

    # r = Variable(torch.from_numpy(r).cuda()).float()
    r = Variable(torch.from_numpy(r)).float()

   # done = Variable(torch.from_numpy(1 - done).cuda()).float()
    done = Variable(torch.from_numpy(1 - done)).float()

    # Q_sa = r + gamma * max(Q_s'a')
    Q = self._Q(s)
    Q = Q.view(self.train_q_batch_size, -1)
    Q = Q.gather(1, a)

    Qt = self._Qt(s_1).view(self.train_q_batch_size, -1)

    # double Q
    best_action = self._Q(s_1).view(self.train_q_batch_size, -1).max(dim=1, keepdim=True)[1]
    y = r + done * self.gamma * Qt.gather(1, best_action)
    # Q
    # y = r + done * self.gamma * Qt.max(dim=1)[0].unsqueeze(1)

    y.volatile = False

    loss = self._criterion(Q, y)
    self._loss.append(loss.sum().cpu().data.numpy())
    self._max_q.append(Q.max().cpu().data.numpy()[0])
    self._optimizer.zero_grad()   # zero the gradient buffers
    loss.backward()
    self._optimizer.step()
