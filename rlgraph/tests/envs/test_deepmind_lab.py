# Copyright 2018 The RLgraph authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import unittest

from rlgraph.environments import DeepmindLabEnv
from rlgraph.tests.test_util import recursive_assert_almost_equal


class TestDeepmindLabEnv(unittest.TestCase):
    """
    Tests creation, resetting and stepping through an openAI Atari Env.
    """
    def test_deepmind_lab_env(self):
        frameskip = 4
        env = DeepmindLabEnv("seekavoid_arena_01", observations=["RGB_INTERLEAVED", "MAP_FRAME_NUMBER"], frameskip=4)

        # Assert action Space is IntBox(9). 9 default actions from IMPALA paper.
        self.assertTrue(env.action_space == IntBox(9))

        # Simple test runs with fixed actions.
        s = env.reset()
        # Assert we have pixels.
        self.assertGreaterEqual(np.mean(s["RGB_INTERLEAVED"]), 0)
        self.assertLessEqual(np.mean(s["RGB_INTERLEAVED"]), 255)
        accum_reward = 0.0
        for i in range(100):
            s, r, t, _ = env.step(env.action_space.sample())
            assert isinstance(r, float)
            self.assertGreaterEqual(np.mean(s["RGB_INTERLEAVED"]), 0)
            self.assertLessEqual(np.mean(s["RGB_INTERLEAVED"]), 255)
            # Assert the env-observed timestep counter.
            self.assertEqual(s["MAP_FRAME_NUMBER"], i * frameskip)
            accum_reward += r

        print("Accumulated Reward: ".format(accum_reward))
