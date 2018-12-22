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
import random
from six.moves import queue
from threading import Thread

from rlgraph import get_distributed_backend
from rlgraph.agents import Agent
from rlgraph.execution.ray import RayValueWorker
from rlgraph.execution.ray.apex.ray_memory_actor import RayMemoryActor
from rlgraph.execution.ray.ray_executor import RayExecutor
from rlgraph.execution.ray.ray_util import create_colocated_ray_actors, RayTaskPool

if get_distributed_backend() == "ray":
    import ray


class SyncBatchExecutor(RayExecutor):
    """
    Implements distributed synchronous execution.
    """
    def __init__(self, environment_spec, agent_config):
        """
        Args:
            environment_spec (dict): Environment spec. Each worker in the cluster will instantiate
                an environment using this spec.
            agent_config (dict): Config dict containing agent and execution specs.
        """
        ray_spec = agent_config["execution_spec"].pop("ray_spec")
        self.worker_spec = ray_spec.pop("worker_spec")
        super(SyncBatchExecutor, self).__init__(executor_spec=ray_spec.pop("executor_spec"),
                                           environment_spec=environment_spec,
                                           worker_spec=self.worker_spec)

        # Must specify an agent type.
        assert "type" in agent_config
        self.agent_config = agent_config
        self.local_agent = self.build_agent_from_config(self.agent_config)

        # Create remote sample workers based on ray cluster spec.
        self.num_sample_workers = self.executor_spec["num_sample_workers"]

        # These are the tasks actually interacting with the environment.
        self.env_sample_tasks = RayTaskPool()
        self.env_interaction_task_depth = self.executor_spec["env_interaction_task_depth"]
        self.worker_sample_size = self.executor_spec["num_worker_samples"] + self.worker_spec["n_step_adjustment"] - 1

        assert not ray_spec, "ERROR: ray_spec still contains items: {}".format(ray_spec)
        self.logger.info("Setting up execution for Apex executor.")
        self.setup_execution()

    def setup_execution(self):
        # Start Ray cluster and connect to it.
        self.ray_init()

        # Create local worker agent according to spec.
        # Extract states and actions space.
        environment = RayExecutor.build_env_from_config(self.environment_spec)
        self.agent_config["state_space"] = environment.state_space
        self.agent_config["action_space"] = environment.action_space

        # Create remote workers for data collection.
        self.worker_spec["worker_sample_size"] = self.worker_sample_size
        self.logger.info("Initializing {} remote data collection agents, sample size: {}".format(
            self.num_sample_workers, self.worker_spec["worker_sample_size"]))
        self.ray_env_sample_workers = self.create_remote_workers(
            RayValueWorker, self.num_sample_workers, self.agent_config,
            # *args
            self.worker_spec, self.environment_spec, self.worker_frameskip
        )

    def test_worker_init(self):
        """
        Tests every worker for successful constructor call (which may otherwise fail silently.
        """
        for ray_worker in self.ray_env_sample_workers:
            self.logger.info("Testing worker for successful init: {}".format(self.worker_ids[ray_worker]))
            task = ray_worker.get_constructor_success.remote()
            result = ray.get(task)
            assert result is True, "ERROR: constructor failed, attribute returned: {}" \
                                   "instead of True".format(result)

    def _execute_step(self):
        """
        Executes a workload on Ray. The main loop performs the following
        steps until the specified number of steps or episodes is finished:

        - Sync weights to policy workers.
        - Schedule a set of samples
        - Wait until all sample tasks are complete
        - Perform local update(s)
        """
        # Env steps done during this rollout.
        env_steps = 0
        update_steps = 0

        # 1. Fetch results from RayWorkers.
        completed_sample_tasks = list(self.env_sample_tasks.get_completed())
        sample_batch_sizes = ray.get([task[1][1] for task in completed_sample_tasks])
        for i, (ray_worker, (env_sample_obj_id, sample_size)) in enumerate(completed_sample_tasks):
            # Randomly add env sample to a local replay actor.
            sample_steps = sample_batch_sizes[i]
            env_steps += sample_steps

            # TODO merge

            # TODO update logic

            self.env_sample_tasks.add_task(ray_worker, ray_worker.execute_and_get_with_count.remote())

        return env_steps, update_steps, 0, 0


