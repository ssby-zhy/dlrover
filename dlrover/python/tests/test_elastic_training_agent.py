# Copyright 2022 The DLRover Authors. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest

from torch.distributed.elastic.agent.server.api import WorkerSpec, WorkerState
from torch.distributed.elastic.rendezvous import RendezvousParameters
from torch.distributed.launcher.api import LaunchConfig

from dlrover.python.common.constants import RendezvousName
from dlrover.python.elastic_agent.master_client import (
    GlobalMasterClient,
    build_master_client,
)
from dlrover.python.elastic_agent.torch.training import (
    ElasticTrainingAgent,
    MasterRendezvousHandler,
)
from dlrover.python.tests.test_utils import start_local_master


class ElasticTrainingAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._master, addr = start_local_master()
        GlobalMasterClient.MASTER_CLIENT = build_master_client(addr)
        self.config = LaunchConfig(
            min_nodes=2,
            max_nodes=2,
            nproc_per_node=8,
            run_id="test",
        )
        rdzv_parameters = RendezvousParameters(
            backend=self.config.rdzv_backend,
            endpoint=self.config.rdzv_endpoint,
            run_id=self.config.run_id,
            min_nodes=self.config.min_nodes,
            max_nodes=self.config.max_nodes,
            local_addr=self.config.local_addr,
            **self.config.rdzv_configs,
        )

        master_addr = "127.0.0.1"
        node_id = 0

        self.rdzv_handler = MasterRendezvousHandler(
            RendezvousName.ELASTIC_TRAINING,
            node_id,
            rdzv_parameters,
            local_world_size=self.config.nproc_per_node,
        )
        self.rdzv_handler.join_timeout = 5

        self.spec = WorkerSpec(
            role=self.config.role,
            local_world_size=self.config.nproc_per_node,
            entrypoint="echo",
            args=tuple([]),
            rdzv_handler=self.rdzv_handler,
            max_restarts=self.config.max_restarts,
            monitor_interval=self.config.monitor_interval,
            redirects=self.config.redirects,
            tee=self.config.tee,
            master_addr=master_addr,
            local_addr=self.config.local_addr,
        )

    def addCleanup(self):
        self._master.stop()

    def test_rank0_rendzevous(self):
        node_id = 0
        agent = ElasticTrainingAgent(
            rank_id=node_id,
            config=self.config,
            entrypoint="python",
            spec=self.spec,
            start_method=self.config.start_method,
            log_dir=self.config.log_dir,
        )
        self.rdzv_handler._client.join_rendezvous(
            1, 8, self.rdzv_handler._name
        )
        agent._rendezvous(agent._worker_group)
        worker_group = agent._worker_group
        self.assertEqual(len(worker_group.workers), 8)
        self.assertEqual(worker_group.group_rank, 0)
        self.assertEqual(worker_group.group_world_size, 2)
        worker = worker_group.workers[1]
        self.assertEqual(worker.local_rank, 1)
        self.assertEqual(worker.global_rank, 1)
        self.assertEqual(worker.world_size, 16)
        self.assertFalse(
            agent._membership_changed("default", self.rdzv_handler)
        )

    def test_rank1_rendzevous(self):
        node_id = 1
        agent = ElasticTrainingAgent(
            rank_id=node_id,
            config=self.config,
            entrypoint="python",
            spec=self.spec,
            start_method=self.config.start_method,
            log_dir=self.config.log_dir,
        )
        self.rdzv_handler._rank_id = node_id
        self.rdzv_handler._client.join_rendezvous(
            0, 8, self.rdzv_handler._name
        )
        store = self.rdzv_handler._get_store(round=0, group=1)
        store.set("MASTER_ADDR", "127.0.0.1".encode())
        store.set("MASTER_PORT", "12345".encode())
        agent._rendezvous(agent._worker_group)
        worker_group = agent._worker_group
        self.assertEqual(len(worker_group.workers), 8)
        self.assertEqual(worker_group.group_rank, 1)
        self.assertEqual(worker_group.group_world_size, 2)
        worker = worker_group.workers[1]
        self.assertEqual(worker.local_rank, 1)
        self.assertEqual(worker.global_rank, 9)
        self.assertEqual(worker.world_size, 16)


class ElasticTrainingAgentRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self._master, addr = start_local_master()
        GlobalMasterClient.MASTER_CLIENT = build_master_client(addr)
        self.config = LaunchConfig(
            min_nodes=1,
            max_nodes=1,
            nproc_per_node=2,
            run_id="test",
            monitor_interval=0.1,
        )
        rdzv_parameters = RendezvousParameters(
            backend=self.config.rdzv_backend,
            endpoint=self.config.rdzv_endpoint,
            run_id=self.config.run_id,
            min_nodes=self.config.min_nodes,
            max_nodes=self.config.max_nodes,
            local_addr=self.config.local_addr,
            **self.config.rdzv_configs,
        )

        master_addr = "127.0.0.1"
        node_id = 0

        self.rdzv_handler = MasterRendezvousHandler(
            RendezvousName.ELASTIC_TRAINING,
            node_id,
            rdzv_parameters,
            local_world_size=self.config.nproc_per_node,
        )
        self.rdzv_handler.join_timeout = 5

        self.spec = WorkerSpec(
            role=self.config.role,
            local_world_size=self.config.nproc_per_node,
            entrypoint="echo",
            args=tuple([]),
            rdzv_handler=self.rdzv_handler,
            max_restarts=self.config.max_restarts,
            monitor_interval=self.config.monitor_interval,
            redirects=self.config.redirects,
            tee=self.config.tee,
            master_addr=master_addr,
            local_addr=self.config.local_addr,
        )

    def addCleanup(self):
        self._master.stop()

    def test_monitor_workers(self):
        self.config.network_check = False
        agent = ElasticTrainingAgent(
            rank_id=0,
            config=self.config,
            entrypoint="echo",
            spec=self.spec,
            start_method=self.config.start_method,
            log_dir=self.config.log_dir,
        )
        agent._report_failure_to_master({})
        run_result = agent._invoke_run()
        self.assertDictEqual(run_result.failures, {})
        self.assertEqual(run_result.state, WorkerState.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
