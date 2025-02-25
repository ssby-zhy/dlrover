import copy
import gc
import traceback

from torch.fx.graph_module import GraphModule

from atorch.auto.auto_accelerate_context import AutoAccelerateContext
from atorch.auto.opt_lib.optimization import DistributedGraphOptimization
from atorch.auto.opt_lib.shard_planners import BaseStagePlanner, BaseTensorParallelPlanner, MIPTensorParallelPlanner
from atorch.auto.opt_lib.utils import find_memory_factor, insert_split_point
from atorch.common.log_utils import default_logger as logger
from atorch.common.util_func import set_sync_bn_pg
from atorch.distributed.distributed import (
    create_parallel_group,
    destroy_parallel_group,
    get_ranks_in_same_group,
    parallel_group,
    parallel_group_and_ranks,
)
from atorch.modules.distributed_modules.compilers.pipe_compiler.distributed_pippy_compiler import _compile_to_pipe
from atorch.modules.distributed_modules.modules_registry import _SHARDABLE_OPERATORS, _register_custom_operators

from .parallel_mode_optimization import ParallelModeOptimization
from .pipeline_parallel_optimization import PipelineParallelOptimization
from .tensor_parallel_optimization import TensorParallelOptimization

try:
    from pippy.IR import MultiUseParameterConfig
except ImportError:
    MultiUseParameterConfig = None


class MixedParallelOptimization(DistributedGraphOptimization):
    def __init__(self):
        """
        Args:
            tp_shard_planner: the shard planner to be used for tensor parallel, can be string,
                in which case
                    'base'/None -> BaseTensorParallelPlanner
                    'mip' -> MIPTensorParallelPlanner
            pp_shard_planner: the shard planner to be used for pipeline parallel, can be string,
                in which case
                    "equal_size" -> split_into_nstages_equal_size
        """
        name = "mixed_parallel"
        super().__init__(name)

    def tune(
        self,
        model_context,
        config=None,
        strategy=None,
        apply_transform=True,
    ):
        logger.warning(
            "Pipeline parallelism is more stable with Environment Variable: " "CUDA_DEVICE_MAX_CONNECTIONS=1"
        )
        config = config if config is not None else dict()

        parallel_mode = config.get("parallel_mode", None)
        pipe_config = config.get("pipe_config", dict())
        tensor_config = config.get("tensor_config", dict())

        # FIXME be more flexible after we have different planners
        self.pipe_shard_planner = BaseStagePlanner()

        if tensor_config.get("shard_planner", "mip") == "base":
            self.tensor_shard_planner = BaseTensorParallelPlanner()
        else:
            self.tensor_shard_planner = MIPTensorParallelPlanner(
                memory_bound=self.memory_bound,
                fp32_flops=self.fp32_flops,
                merge_nodes=True,
                solver="glpk",
                greedy_init=True,
                timelimit=10 * 60,
            )

        if parallel_mode is None:
            raise ValueError("Currently we support only semi-auto mixed parallel opt, with known parallel_mode")

        all_local_ranks = get_ranks_in_same_group(parallel_mode, 0)
        "tensor" in all_local_ranks and _register_custom_operators()
        if "pipe" in all_local_ranks or "tensor" in all_local_ranks:
            if "pipe" in all_local_ranks:
                train_mode = model_context.convert_to_loss_wrapper()
            else:
                train_mode = True

            data_size = len(all_local_ranks.get("data", []))

            # Do not set chunks as we want a more conservative estimate
            parallel_config = {"ddp_size": data_size}
            graph, sharding_specs, tensor_shapes = self._trace_and_propagate(
                model_context, config, strategy, parallel_config
            )
            model = model_context.model

            if "pipe" in all_local_ranks:
                # setting up pipeline related configs
                pipe_ranks = all_local_ranks["pipe"]
                nstages = pipe_config.get("nstages", len(pipe_ranks))
                chunks = pipe_config.get("chunks", len(pipe_ranks))
                pipe_topo = self.device_topo.get_physical_topology(pipe_ranks)
                pipe_schedule = pipe_config.get("pipe_schedule", "Interleaved1F1B")
                # This is a hack to specify split points.
                insert_before_modules = pipe_config.get("insert_before_modules", None)
                self.pipe_shard_planner = pipe_config.get("shard_planner", self.pipe_shard_planner)
                use_c10d = pipe_config.get("use_c10d", False)
                dynamic_shape = pipe_config.get("dynamic_shape", False)

                output_loss_value_spec = True if train_mode else None

                checkpoint_keys = dict()

                for name, mod in model.named_modules():
                    for param_name, param in mod.named_parameters():
                        checkpoint_keys[f"{name}.{param_name}"] = getattr(param, "checkpoint_name", None)
                    for buffer_name, mod_buffer in mod.named_buffers():
                        checkpoint_keys[f"{name}.{buffer_name}"] = getattr(mod_buffer, "checkpoint_name", None)

                if insert_before_modules is not None:
                    # match qualname with node name
                    insert_before_nodes = []
                    for node in graph.nodes:
                        if node.op == "call_module" and node.target in insert_before_modules:
                            insert_before_nodes.append(node.name)
                else:
                    insert_before_nodes = self.pipe_shard_planner.generate_sharding_plan(
                        model, graph, sharding_specs, tensor_shapes, device_topo=pipe_topo, nstages=nstages
                    )

                data_configs = {}
                # should not set down env configs as it is device specific
                env_configs = {}
                # we don't need to specify a ddp argument, the detection of ddp can be handled
                # in apply_wrapper
                pipe_compiler_configs = {
                    "insert_before_nodes": insert_before_nodes,
                    "multi_use_param_spec": MultiUseParameterConfig.REPLICATE,
                    "output_loss_value_spec": output_loss_value_spec,
                    "expected_num_stages": nstages,
                    "compile_with_dynamo": False,
                    "export_mode": True,
                    "chunks": chunks,
                    "pipe_schedule": pipe_schedule,
                    "checkpoint_keys": checkpoint_keys,
                    "use_c10d": use_c10d,
                    "train_mode": train_mode,
                    "dynamic_shape": dynamic_shape,
                }

                pipe_best_config = {
                    "data_configs": data_configs,
                    "env_configs": env_configs,
                    "compiler_configs": pipe_compiler_configs,
                }
                graph = insert_split_point(graph, insert_before_nodes, nstages)
            else:
                pipe_best_config = None

            if "tensor" in all_local_ranks:
                tp_ranks = all_local_ranks["tensor"]
                tp_topo = self.device_topo.get_physical_topology(tp_ranks)
                optimizer = model_context.create_optim()
                model = model_context.model

                if isinstance(self.tensor_shard_planner, MIPTensorParallelPlanner):
                    # adjust memory factor for mip planner
                    # FIXME not checking checkpointing_optimization now, since not sure how it affects GPURAM
                    forward_factor, grad_factor, param_factor, optimizer_factor = find_memory_factor(strategy)
                    self.tensor_shard_planner.set_mem_factors(
                        forward_factor, grad_factor, param_factor, optimizer_factor
                    )

                tp_best_config = self.tensor_shard_planner.generate_sharding_plan(
                    model, graph, sharding_specs, tensor_shapes, tp_topo, optimizer
                )

                del optimizer
                gc.collect()
            else:
                tp_best_config = None

        else:
            pipe_best_config, tp_best_config = None, None

        use_ddp = "data" in all_local_ranks and pipe_best_config is None

        best_config = {
            "pipe_config": pipe_best_config,
            "tp_config": tp_best_config,
            "ddp": use_ddp,
            "parallel_mode": parallel_mode,
        }
        return True, best_config, model_context

    def transform(self, model_context, config=None):
        model_context.add_wrapper("mp", MixedParallelOptimization.apply_wrapper, config, is_pre_wrapper=True)
        return model_context

    @staticmethod
    def apply_wrapper(model_context, wrapper_name, wrapper_config):
        """
        args:
            wrapper_config:
                pipe_config
                tp_config
                ddp
        return:
            A tensor parallel model
        """
        try:

            pipe_config = wrapper_config["pipe_config"]
            tp_config = wrapper_config["tp_config"]
            ddp = wrapper_config["ddp"]
            parallel_mode = wrapper_config["parallel_mode"]

            destroy_parallel_group(destroy_rpc=False)
            create_parallel_group(parallel_mode)

            # model -> loss_wrapper -> split_graph -> tp_compiler -> pipe_compiler
            if pipe_config is not None:
                # Default checkpoint to be true
                pipe_config.setdefault("checkpoint", True)
                amp_config = pipe_config["compiler_configs"].get("amp_config", None)
                use_c10d = pipe_config["compiler_configs"].get("use_c10d", False)
                model_context.convert_to_loss_wrapper(amp_config=amp_config)

                insert_before_nodes = pipe_config["compiler_configs"]["insert_before_nodes"]
                expected_num_stages = pipe_config["compiler_configs"]["expected_num_stages"]

                # get the graph and insert split point
                # at this point parallel groups are readily created
                # If we are using Pipes, we do not need to care about how nodes are sharded
                graph = model_context.capture_compute_graph(
                    backend="meta_fx", leaf_modules=list(_SHARDABLE_OPERATORS.values())
                )
                pipe_graph = insert_split_point(graph, insert_before_nodes, expected_num_stages)
                gm = GraphModule(model_context.model, pipe_graph)

                if use_c10d:
                    compile_with_dynamo = pipe_config["compiler_configs"].get("compile_with_dynamo", False)
                    multi_use_param_spec = pipe_config["compiler_configs"].get("multi_use_param_spec", None)
                    output_loss_value_spec = pipe_config["compiler_configs"].get("output_loss_value_spec", True)
                    _, pipe_ranks = parallel_group_and_ranks("pipe")
                    model_pipe, _ = _compile_to_pipe(
                        gm, pipe_ranks, compile_with_dynamo, multi_use_param_spec, output_loss_value_spec
                    )
                    fake_split_gm = copy.deepcopy(model_pipe.split_gm).to("meta")
                    counter = AutoAccelerateContext.counter
                    if not hasattr(AutoAccelerateContext, "fake_split_gm"):
                        AutoAccelerateContext.add_ac_attr("fake_split_gm", {counter: fake_split_gm})

                if tp_config is not None:
                    tp_config["gm"] = gm
                    tp_config["defer_init"] = True

            if tp_config is not None:
                # do tp_compiler first
                # FIXME will the amp ops persist?
                model_context.add_wrapper(
                    "tp", TensorParallelOptimization.apply_wrapper, tp_config, is_pre_wrapper=True
                )

            if pipe_config is not None:
                # then pipe_compiler
                model_context.add_wrapper(
                    "pipe", PipelineParallelOptimization.apply_wrapper, pipe_config, is_pre_wrapper=True
                )

            if ddp:
                set_sync_bn_pg(model_context.model, parallel_group("data"))
                model_context.add_wrapper("ddp", ParallelModeOptimization.apply_wrapper, "data", is_pre_wrapper=False)

            model_context.parallel_mode_config = parallel_mode
            logger.info("Successfully transformed the model into mixed parallel model")
        except Exception as e:
            traceback.print_exc()
            logger.warning(f"Failed to transform the model into a parallel model, with error: {e}")

    @staticmethod
    def reset(config):
        # this should remove the meta model offload dir at correct timing
        pass
