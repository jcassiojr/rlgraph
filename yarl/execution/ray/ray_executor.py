# Copyright 2018 The YARL-Project, All Rights Reserved.
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

from copy import deepcopy

from six.moves import xrange as range_
import logging
import numpy as np
import time

from yarl import get_distributed_backend
from yarl.agents import Agent
from yarl.environments import Environment

if get_distributed_backend() == "ray":
    import ray


class RayExecutor(object):
    """
    Abstract distributed Ray executor.

    A Ray executor implements a specific distributed learning semantic by delegating
    distributed state management and execution to the Ray execution engine.

    """
    def __init__(self, cluster_spec):
        """

        Args:
            cluster_spec (dict): Contains all information necessary to set up and execute
                agents on a Ray cluster.
        """
        self.logger = logging.getLogger(__name__)

        # Ray workers for remote data collection.
        self.ray_env_sample_workers = None
        self.cluster_spec = cluster_spec

        # Global performance metrics.
        self.env_sample_iteration_throughputs = None
        self.update_iteration_throughputs = None

        # Map worker objects to host ids.
        self.worker_ids = dict()

    def ray_init(self):
        """
        Connects to a Ray cluster or starts one if none exists.
        """
        self.logger.info("Initializing Ray cluster with cluster spec:")
        for spec_key, value in self.cluster_spec.items():
            self.logger.info("{}: {}".format(spec_key, value))

        # Avoiding accidentally starting local redis clusters.
        if 'redis_host' not in self.cluster_spec:
            self.logger.warning("Warning: No redis address provided, starting local redis server.")
        ray.init(
            redis_address=self.cluster_spec.get('redis_address', None),
            num_cpus=self.cluster_spec.get('num_cpus', None),
            num_gpus=self.cluster_spec.get('num_gpus', None)
        )

    def create_remote_workers(self, cls, num_actors, agent_config, *args):
        """
        Creates Ray actors for remote execution.

        Args:
            cls (RayWorker): Actor class, must be an instance of RayWorker.
            num_actors (int): Num
            agent_config (dict): Agent config.
            *args (any): Arguments for RayWorker class.
        Returns:
            list: Remote Ray actors.
        """
        workers = []
        for i in range_(num_actors):
            worker = cls.remote(deepcopy(agent_config), *args)
            self.worker_ids[worker] = "worker_{}".format(i)
            workers.append(worker)
        return workers

    def setup_execution(self):
        """
        Creates and initializes all remote agents on the Ray cluster. Does not
        schedule any tasks yet.
        """
        raise NotImplementedError

    def init_tasks(self):
        """
        Initializes Remote ray worker tasks. Calling this method will result in
        actually scheduling tasks on Ray, as opposed to setup_execution which just
        creates the relevant remote actors.
        """
        pass

    def execute_workload(self, workload):
        """
        Executes a workload on Ray and measures worker statistics. Workload semantics
        are decided via the private implementer, _execute_step().
        Args:
            workload (dict): Workload parameters, primarily 'num_timesteps' and 'report_interval'
                to indicate how many steps to execute and how often to report results.
        """
        self.env_sample_iteration_throughputs = list()
        self.update_iteration_throughputs = list()
        self.init_tasks()

        # Init.
        self.env_sample_iteration_throughputs = list()
        self.update_iteration_throughputs = list()

        # Assume time step based initially.
        num_timesteps = workload['num_timesteps']

        # Performance reporting granularity.
        report_interval = workload['report_interval']
        timesteps_executed = 0
        iteration_times = []
        iteration_time_steps = []
        iteration_update_steps = []

        start = time.monotonic()
        # Call _execute_step as many times as required.
        while timesteps_executed < num_timesteps:
            iteration_step = 0
            iteration_updates = 0
            iteration_start = time.monotonic()

            # Record sampling and learning throughput every interval.
            while iteration_step < report_interval:
                worker_steps_executed, update_steps = self._execute_step()
                iteration_step += worker_steps_executed
                iteration_updates += update_steps

            iteration_end = time.monotonic() - iteration_start
            timesteps_executed += iteration_step

            # Append raw values, compute stats after experiment is done.
            iteration_times.append(iteration_end)
            iteration_update_steps.append(iteration_updates)
            iteration_time_steps.append(iteration_step)

            self.logger.info("Executed {} Ray worker steps, {} update steps, ({} of {} ({} %))".format(
                iteration_step, iteration_updates, timesteps_executed,
                num_timesteps, (100 * timesteps_executed / num_timesteps)
            ))

        # self.env_sample_throughputs.append(timesteps_executed)
        # self.update_throughputs.append(update_steps)
        total_time = (time.monotonic() - start) or 1e-10
        self.logger.info("Time steps executed: {} ({} ops/s)".
                         format(timesteps_executed, timesteps_executed / total_time))
        all_updates = np.sum(iteration_update_steps)
        self.logger.info("Updates executed: {}, ({} updates/s)".format(
            all_updates, all_updates / total_time
        ))
        for i in range_(len(iteration_times)):
            it_time = iteration_times[i]
            self.env_sample_iteration_throughputs.append(iteration_time_steps[i] / it_time)
            self.update_iteration_throughputs.append(iteration_update_steps[i] / it_time)

        worker_stats = self.get_aggregate_worker_results()
        self.logger.info("Retrieved worker stats for {} workers:".format(len(self.ray_env_sample_workers)))
        self.logger.info(worker_stats)

        return dict(
            # Overall stats.
            runtime=total_time,
            timesteps_executed=timesteps_executed,
            ops_per_second=(timesteps_executed / total_time),
            min_iteration_sample_throughput=np.min(self.env_sample_iteration_throughputs),
            max_iteration_sample_throughput=np.max(self.env_sample_iteration_throughputs),
            mean_iteration_sample_throughput=np.mean(self.env_sample_iteration_throughputs),
            min_iteration_update_throughput=np.min(self.update_iteration_throughputs),
            max_iteration_update_throughput=np.max(self.update_iteration_throughputs),
            mean_iteration_update_throughput=np.mean(self.update_iteration_throughputs),
            # Should be same as iteration throughput?
            # Worker stats.
            mean_worker_op_throughput=worker_stats["mean_worker_op_throughput"],
            max_worker_op_throughput=worker_stats["max_worker_op_throughput"],
            min_worker_op_throughput=worker_stats["min_worker_op_throughput"],
            mean_worker_reward=worker_stats["mean_reward"],
            max_worker_reward=worker_stats["max_reward"],
            min_worker_reward=worker_stats["min_reward"],
            # This is the mean final episode over all workers.
            final_reward=worker_stats["mean_final_reward"]
        )

    def sample_metrics(self):
        return self.env_sample_iteration_throughputs

    def update_metrics(self):
        return self.update_iteration_throughputs

    def _execute_step(self):
        """
        Actual private implementer of each step of the workload executed.
        """
        raise NotImplementedError

    @staticmethod
    def build_agent_from_config(agent_config):
        """
        Builds agent without using from_spec as Ray cannot handle kwargs correctly
        at the moment.

        Args:
            agent_config (dict): Agent config. Must contain 'type' field to lookup constructor.

        Returns:
            Agent: YARL agent object.
        """
        config = deepcopy(agent_config)
        # Pop type on a copy because this may be called by multiple classes/worker types.
        agent_cls = Agent.__lookup_classes__.get(config.pop('type'))

        return agent_cls(**config)

    @staticmethod
    def build_env_from_config(env_spec):
        """
        Builds environment without using from_spec as Ray cannot handle kwargs correctly
        at the moment.

        Args:
            env_spec (dict): Environment specification. Must contain 'type' field to lookup constructor.

        Returns:
            Environment: Env object.
        """
        env_cls = Environment.__lookup_classes__.get(env_spec['type'])
        return env_cls(env_spec['gym_env'])

    def result_by_worker(self, worker_id=None):
        """
        Retreives full episode-reward time series for a worker by id (or first worker in registry if None).

        Args:
            worker_id Optional[str]:

        Returns:
            dict: Full results for this worker.
        """
        if worker_id is not None:
            # Get first.
            assert worker_id in self.ray_env_sample_workers.keys(),\
                "Parameter worker_id: {} must be valid key. Fetch keys via 'get_sample_worker_ids'.".\
                format(worker_id)
            ray_worker = self.ray_env_sample_workers[worker_id]
        else:
            # Otherwise just pick  first.
            ray_worker = list(self.ray_env_sample_workers.values())[0]

        task = ray_worker.get_workload_statistics.remote()
        metrics = ray.get(task)

        # Return full reward series.
        return dict(
            episode_rewards=metrics["episode_rewards"],
            episode_timesteps=metrics["episode_timesteps"]
        )

    def get_sample_worker_ids(self):
        """
        Returns identifeirs of all sample workers.

        Returns:
            list: List of worker name strings in case individual analysis of one worker's results are required via
                'result_by_worker'.
        """
        return list(self.worker_ids.keys())

    def get_aggregate_worker_results(self):
        """
        Fetches execution statistics from remote workers and aggregates them.

        Returns:
            dict: Aggregate worker statistics.
        """
        min_rewards = []
        max_rewards = []
        mean_rewards = []
        final_rewards = []
        worker_op_throughputs = []
        worker_env_frame_throughputs = []
        episodes_executed = 0
        steps_executed = 0

        for ray_worker in self.ray_env_sample_workers:
            self.logger.info("Retrieving workload statistics for worker: {}".format(
                self.worker_ids[ray_worker])
            )
            task = ray_worker.get_workload_statistics.remote()
            metrics = ray.get(task)
            min_rewards.append(metrics["min_episode_reward"])
            max_rewards.append(metrics["max_episode_reward"])
            mean_rewards.append(metrics["mean_episode_reward"])
            episodes_executed += metrics["episodes_executed"]
            steps_executed += metrics["worker_steps"]
            final_rewards.append(metrics["final_episode_reward"])
            worker_op_throughputs.append(metrics["mean_worker_ops_per_second"])
            worker_env_frame_throughputs.append(metrics["mean_worker_env_frames_per_second"])

        return dict(
            min_reward=np.min(min_rewards),
            max_reward=np.max(max_rewards),
            mean_reward=np.mean(mean_rewards),
            mean_final_reward=np.mean(final_rewards),
            episodes_executed=episodes_executed,
            steps_executed=steps_executed,
            # Identify potential straggling workers.
            mean_worker_op_throughput=np.mean(worker_op_throughputs),
            min_worker_op_throughput=np.min(worker_op_throughputs),
            max_worker_op_throughput=np.max(worker_op_throughputs),
            mean_worker_env_frame_throughput=np.mean(worker_env_frame_throughputs)
        )
