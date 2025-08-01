# Copyright (c) 2024, NVIDIA CORPORATION. All rights reserved.

"""Input/output checkpointing."""

import contextlib
import os
import random
import shutil
import sys
import threading
from argparse import Namespace
from enum import Enum, auto
from logging import getLogger
from pathlib import Path

import numpy as np
from time import time

import torch

from megatron.core import mpu, tensor_parallel, dist_checkpointing
from megatron.core.dist_checkpointing.mapping import ShardedObject
from megatron.core.dist_checkpointing.serialization import get_default_load_sharded_strategy
from megatron.core.dist_checkpointing.strategies.fully_parallel import \
    FullyParallelSaveStrategyWrapper, FullyParallelLoadStrategyWrapper
from megatron.core.num_microbatches_calculator import update_num_microbatches
from megatron.core.fp8_utils import is_float8tensor, dequantize_fp8_tensor
from megatron.core.rerun_state_machine import get_rerun_state_machine
from .async_utils import schedule_async_save, is_empty_async_queue
from .global_vars import get_args
from .utils import unwrap_model, print_rank_0, append_to_progress_log, is_last_rank
from ..core.dist_checkpointing.serialization import \
    get_default_save_sharded_strategy
from .one_logger_utils import on_save_checkpoint_start, on_save_checkpoint_success
from . import wandb_utils

from . import ft_integration

from megatron.core.msc_utils import MultiStorageClientFeature, open_file


# [ModelOpt]: Import
try:
    from modelopt.torch.opt.plugins import (
        save_modelopt_state,
        save_sharded_modelopt_state,
        restore_modelopt_state,
        restore_sharded_modelopt_state,
    )
    has_nvidia_modelopt = True
except Exception:
    has_nvidia_modelopt = False

_CHECKPOINT_VERSION = None

logger = getLogger(__name__)
_NON_PERSISTENT_CKPT_SUBDIR = 'non_persistent'

def set_checkpoint_version(value):
    global _CHECKPOINT_VERSION
    if _CHECKPOINT_VERSION is not None:
        assert _CHECKPOINT_VERSION == value, \
            "checkpoint versions do not match"
    _CHECKPOINT_VERSION = value


def get_checkpoint_version():
    global _CHECKPOINT_VERSION
    return _CHECKPOINT_VERSION


def check_checkpoint_args(checkpoint_args):
    """Ensure fixed arguments for a model are the same for the input
    arguments and the one retrieved from checkpoint."""
    args = get_args()

    def _compare(arg_name, old_arg_name=None, default=None):
        if old_arg_name is not None:
            ckpt_arg_name = old_arg_name
        else:
            ckpt_arg_name = arg_name
        if default is not None:
            checkpoint_value = getattr(checkpoint_args, ckpt_arg_name, default)
        else:
            checkpoint_value = getattr(checkpoint_args, ckpt_arg_name)
        args_value = getattr(args, arg_name)
        error_message = '{} value from checkpoint ({}) is not equal to the ' \
                        'input argument value ({}).'.format(
                            arg_name, checkpoint_value, args_value)
        assert checkpoint_value == args_value, error_message

    _compare('num_layers')
    _compare('hidden_size')
    _compare('num_attention_heads')
    _compare('add_position_embedding', default=True)
    if args.vocab_file:
        _compare('max_position_embeddings')
        _compare('make_vocab_size_divisible_by')
        if not args.use_dist_ckpt:
            _compare('padded_vocab_size')
        _compare('tokenizer_type')
    if args.data_parallel_random_init:
        _compare('data_parallel_random_init')
    if get_checkpoint_version() < 3.0:
        _compare('tensor_model_parallel_size',
                 old_arg_name='model_parallel_size')
    if get_checkpoint_version() >= 3.0 and not args.use_dist_ckpt:
        _compare('tensor_model_parallel_size')
        _compare('pipeline_model_parallel_size')


def isfile(filename) -> bool:
    if MultiStorageClientFeature.is_enabled():
        msc = MultiStorageClientFeature.import_package()
        return msc.os.path.isfile(filename)
    else:
        return os.path.isfile(filename)


def ensure_directory_exists(filename, check_parent=True):
    """Build filename's path if it does not already exists."""
    dirname = os.path.dirname(filename) if check_parent else filename
    if MultiStorageClientFeature.is_enabled():
        msc = MultiStorageClientFeature.import_package()
        msc.os.makedirs(dirname, exist_ok=True)
    else:
        os.makedirs(dirname, exist_ok=True)


def get_checkpoint_name(checkpoints_path, iteration, release=False,
                        pipeline_parallel=None,
                        tensor_rank=None, pipeline_rank=None,
                        expert_parallel=None, expert_rank=None,
                        return_base_dir=False, basename="model_optim_rng.pt"):
    """Determine the directory name for this rank's checkpoint."""
    if release:
        directory = 'release'
    else:
        directory = 'iter_{:07d}'.format(iteration)
    if return_base_dir:
        common_path = os.path.join(checkpoints_path, directory)
        return common_path

    # Use both the tensor and pipeline MP rank.
    if pipeline_parallel is None:
        pipeline_parallel = (mpu.get_pipeline_model_parallel_world_size() > 1)
    if tensor_rank is None:
        tensor_rank = mpu.get_tensor_model_parallel_rank()
    if pipeline_rank is None:
        pipeline_rank = mpu.get_pipeline_model_parallel_rank()
    if expert_parallel is None:
        expert_parallel = (mpu.get_expert_model_parallel_world_size() > 1)
    if expert_rank is None:
        expert_rank = mpu.get_expert_model_parallel_rank()

    # Use both the tensor and pipeline MP rank. If using the distributed
    # optimizer, then the optimizer's path must additionally include the
    # data parallel rank.
    if not pipeline_parallel:
        common_path = os.path.join(checkpoints_path, directory,
                            f'mp_rank_{tensor_rank:02d}')
    else:
        common_path = os.path.join(checkpoints_path, directory,
                f'mp_rank_{tensor_rank:02d}_{pipeline_rank:03d}')

    if expert_parallel:
        common_path = common_path + f'_{expert_rank:03d}'

    return os.path.join(common_path, basename)


def get_distributed_optimizer_checkpoint_name(model_checkpoint_name):
    return os.path.join(os.path.dirname(model_checkpoint_name),
                        "distrib_optim.pt")


def find_checkpoint_rank_0(checkpoints_path, iteration, release=False):
    """Finds the checkpoint for rank 0 without knowing if we are using
    pipeline parallelism/expert parallelism or not.

    Since the checkpoint naming scheme changes if pipeline or expert
    parallelism is present, we need to look for both naming schemes if
    we don't know if the checkpoint has pipeline or expert parallelism.
    """

    # Look for checkpoint with no pipelining and no expert parallelism
    filename = get_checkpoint_name(checkpoints_path, iteration, release,
                                   pipeline_parallel=False,
                                   tensor_rank=0, pipeline_rank=0,
                                   expert_parallel=False, expert_rank=0)
    if isfile(filename):
        return filename

    # Look for checkpoint with no pipelining and expert parallelism
    filename = get_checkpoint_name(checkpoints_path, iteration, release,
                                   pipeline_parallel=False,
                                   tensor_rank=0, pipeline_rank=0,
                                   expert_parallel=True, expert_rank=0)
    if isfile(filename):
        return filename

    # Look for checkpoint with pipelining and no expert parallelism
    filename = get_checkpoint_name(checkpoints_path, iteration, release,
                                   pipeline_parallel=True,
                                   tensor_rank=0, pipeline_rank=0,
                                   expert_parallel=False, expert_rank=0)
    if isfile(filename):
        return filename

    # Look for checkpoint with pipelining and expert parallelism
    filename = get_checkpoint_name(checkpoints_path, iteration, release,
                                   pipeline_parallel=True,
                                   tensor_rank=0, pipeline_rank=0,
                                   expert_parallel=True, expert_rank=0)
    if isfile(filename):
        return filename

    # Look for a distributed checkpoint
    filename = get_checkpoint_name(checkpoints_path, iteration, release,
                                   pipeline_parallel=True,
                                   return_base_dir=True)
    if dist_checkpointing.check_is_distributed_checkpoint(filename):
        return filename

    return None


def get_checkpoint_tracker_filename(checkpoints_path):

    """Tracker file rescords the latest chckpoint during
    training to restart from."""
    return os.path.join(checkpoints_path, 'latest_checkpointed_iteration.txt')


def checkpoint_exists(checkpoints_path):
    if checkpoints_path is None:
        return False
    path = get_checkpoint_tracker_filename(checkpoints_path)
    return isfile(path)


def read_metadata(tracker_filename):
    # Read the tracker file and either set the iteration or
    # mark it as a release checkpoint.
    iteration = 0
    release = False

    with open_file(tracker_filename, 'r') as f:
        metastring = f.read().strip()
        try:
            iteration = int(metastring)
        except ValueError:
            release = metastring == 'release'
            if not release:
                print_rank_0('ERROR: Invalid metadata file {}. Exiting'.format(
                    tracker_filename))
                sys.exit()
    assert iteration > 0 or release, 'error parsing metadata file {}'.format(
        tracker_filename)

    # Get the max iteration retrieved across the ranks.
    if torch.distributed.is_initialized():
        iters_cuda = torch.tensor([iteration], dtype=torch.long, device='cuda')
        torch.distributed.all_reduce(iters_cuda, op=torch.distributed.ReduceOp.MAX)
        max_iter = iters_cuda[0].item()

        # We should now have all the same iteration.
        # If not, print a warning and chose the maximum
        # iteration across all ranks.
        if iteration != max_iter:
            rank = torch.distributed.get_rank()
            print('WARNING: on rank {} found iteration {} in the '
                  'metadata while max iteration across the ranks '
                  'is {}, replacing it with max iteration.'.format(
                      rank, iteration, max_iter), flush=True)
    else:
        # When loading a checkpoint outside of training (for example,
        # when editing it), we might not have torch distributed
        # initialized, in this case, just assume we have the latest
        max_iter = iteration
    return max_iter, release


def get_rng_state(ckpt_format: str):
    """Collect rng state across data parallel ranks."""
    args = get_args()
    rng_state = {
        'random_rng_state': random.getstate(),
        'np_rng_state': np.random.get_state(),
        'torch_rng_state': torch.get_rng_state(),
        'cuda_rng_state': torch.cuda.get_rng_state(),
        'rng_tracker_states': tensor_parallel.get_cuda_rng_tracker().get_states()}

    rng_state_list = None
    if args.data_parallel_random_init and torch.distributed.is_initialized() and \
            mpu.get_data_parallel_world_size() > 1:
        rng_state_list = \
            [None for i in range(mpu.get_data_parallel_world_size())]
        torch.distributed.all_gather_object(
            rng_state_list,
            rng_state,
            group=mpu.get_data_parallel_group())
    else:
        rng_state_list = [rng_state]

    if ckpt_format == "torch_dist":
        pp_rank = mpu.get_pipeline_model_parallel_rank()
        pp_size = mpu.get_pipeline_model_parallel_world_size()
        tp_rank = mpu.get_tensor_model_parallel_rank()
        tp_size = mpu.get_tensor_model_parallel_world_size()
        rng_state_list = ShardedObject('rng_state', rng_state_list, (pp_size, tp_size), (pp_rank, tp_rank),
                                       replica_id=mpu.get_data_parallel_rank(with_context_parallel=True))

    return rng_state_list

class CheckpointType(Enum):
    LEGACY = auto()
    LOCAL = auto()
    GLOBAL = auto()
    TORCH_DCP = auto()

def _build_sharded_state_dict_metadata(args: Namespace) -> dict:
    """Builds metadata used for sharded_state_dict versioning.

    The whole content metadata is passed to ``shared_state_dict`` model and optimizer methods
    and therefore affects only the logic behind sharded_state_dict creation.
    The content metadata should be minimalistic, ideally flat (or with a single nesting level)
    and with semantically meaningful flag names (e.g. `distrib_optim_sharding_type`).
    In particular, a simple integer (or SemVer) versioning flag (e.g. `metadata['version'] = 3.4`)
    is discouraged, because the metadata serves for all models and optimizers and it's practically
    impossible to enforce a linearly increasing versioning for this whole space.
    """
    metadata = {}
    if args.use_distributed_optimizer:
        if args.ckpt_fully_parallel_save:
            metadata['distrib_optim_sharding_type'] = 'fully_sharded_model_space'
        else:
            metadata['distrib_optim_sharding_type'] = 'dp_zero_gather_scatter'
    metadata['chained_optim_avoid_prefix'] = True
    return metadata

def save_checkpoint(iteration, model, optimizer, opt_param_scheduler, num_floating_point_operations_so_far,
                    checkpointing_context=None, pipeline_rank=None, expert_rank=None, tensor_rank=None, pipeline_parallel=None, expert_parallel=None, non_persistent_ckpt=False,
                    train_data_iterator=None, preprocess_common_state_dict_fn = None):
    """Save a model, optimizer and optionally dataloader checkpoint.

    Checkpointing context is used to persist some checkpointing state
    throughout a single job. Must be initialized externally (not used if None).

    If non_persistent_ckpt is True,
    the checkpoint will be saved with special functionality for removing old checkpoints.
    There are several types of non-persistent checkpoints:
    "global" - Saved as a standard checkpoint (e.g., on Lustre) with old checkpoints being removed.
    "local" - Each rank saves a portion of the checkpoint locally (e.g., on SSD/ramdisk).

    Dataloader checkpoint is only saved if the dataloader supports it. Currently this applies only
    to the Megatron Energon dataloader (multimodal) and not the built-in Megatron dataloader (text-only).
    """
    start_ckpt = time()
    args = get_args()

    if args.async_save and not is_empty_async_queue():
        print_rank_0('WARNING: Starting a checkpoint save before previous has finished. Consider increasing the checkpoint interval.')

    # Prepare E2E metrics at start of save checkpoint
    productive_metrics = on_save_checkpoint_start(args.async_save)

    # Monitor for the checkpointing timeout (no-op if FT is not enabled)
    ft_integration.on_checkpointing_start()

    # Only rank zero of the data parallel writes to the disk.
    model = unwrap_model(model)

    # Handle non_persistent_ckpt flag. Besides overwriting `args.save` and
    # `args.use_dist_ckpt`, non-persistent global ckpt requires no additional logic
    ckpt_type = CheckpointType.GLOBAL if args.use_dist_ckpt else CheckpointType.LEGACY
    save_dir = args.save
    if non_persistent_ckpt:
        if args.non_persistent_ckpt_type == 'global':
            ckpt_type = CheckpointType.GLOBAL
            save_dir = (
                args.non_persistent_global_ckpt_dir
                if args.non_persistent_global_ckpt_dir
                else os.path.join(save_dir, _NON_PERSISTENT_CKPT_SUBDIR)
            )
            # TODO Can we ensure the previous checkpoint is saved? We don't want to allow two saves in parallel.
            cleanup_old_non_persistent_checkpoint(
                save_dir, leave_ckpt_num=1, do_async=args.async_save
            )
        elif args.non_persistent_ckpt_type == 'local':
            ckpt_type = CheckpointType.LOCAL
            save_dir = checkpointing_context['local_checkpoint_manager'].local_ckpt_dir
        else:
            raise NotImplementedError(f"Please use local or global non-persistent checkpoints (got: {args.non_persistent_ckpt_type})")

    ckpt_format = args.ckpt_format if ckpt_type == CheckpointType.GLOBAL else 'torch'
    print_rank_0('saving checkpoint at iteration {:7d} to {} in {} format'.format(
        iteration, save_dir, ckpt_format))

    # Collect rng state across data parallel ranks.
    rng_state = get_rng_state(args.ckpt_format)

    # Collect rerun state across all ranks
    rerun_state_machine = get_rerun_state_machine()
    rerun_state = rerun_state_machine.state_dict(
        data_iterator=train_data_iterator, ckpt_format=args.ckpt_format,
    )

    # Checkpoint name.
    return_base_dir = (ckpt_type != CheckpointType.LEGACY)
    checkpoint_name = get_checkpoint_name(save_dir, iteration, release=False, pipeline_parallel=pipeline_parallel,
        tensor_rank=tensor_rank, pipeline_rank=pipeline_rank, expert_parallel=expert_parallel, expert_rank=expert_rank, return_base_dir=return_base_dir)

    # Save dataloader state if the dataloader supports it (currently only Megatron Energon).
    maybe_save_dataloader_state(train_data_iterator, iteration, getattr(args, "dataloader_save", None))

    # Save distributed optimizer's custom parameter state.
    if (
        args.use_distributed_optimizer
        and not args.no_save_optim
        and optimizer is not None
        and ckpt_type == CheckpointType.LEGACY
    ):
        optim_checkpoint_name = \
            get_distributed_optimizer_checkpoint_name(checkpoint_name)
        ensure_directory_exists(optim_checkpoint_name)
        if not optimizer.is_stub_optimizer:
            optimizer.save_parameter_state(optim_checkpoint_name)

    async_save_request = None
    if args.async_save:
        if ckpt_type == CheckpointType.LEGACY:
            raise NotImplementedError('Async checkpoint save not implemented for legacy checkpoints')
        elif ckpt_type == CheckpointType.GLOBAL and args.ckpt_format != 'torch_dist':
            raise NotImplementedError(f'Async checkpoint save not implemented for {args.ckpt_format} distributed checkpoint format')

    rank = torch.distributed.get_rank() if torch.distributed.is_initialized() else 0

    # Collect args, model, RNG.
    if not torch.distributed.is_initialized() \
            or mpu.get_expert_data_parallel_rank() == 0 \
            or ckpt_type != CheckpointType.LEGACY:
        if ckpt_type != CheckpointType.LEGACY:
            sharded_sd_metadata = _build_sharded_state_dict_metadata(args)
            if args.use_distributed_optimizer:
                print_rank_0(f'Storing distributed optimizer sharded state of type'
                             f' {sharded_sd_metadata["distrib_optim_sharding_type"]}')
        else:
            sharded_sd_metadata = None
        state_dict = generate_state_dict(
            args,
            model,
            optimizer,
            opt_param_scheduler,
            rng_state,
            iteration=iteration,
            optim_sd_kwargs=dict(metadata=sharded_sd_metadata),
            model_sd_kwargs=dict(metadata=sharded_sd_metadata),
            rerun_state=rerun_state,
        )

        state_dict['num_floating_point_operations_so_far'] = num_floating_point_operations_so_far
        if ckpt_type == CheckpointType.GLOBAL and ckpt_format == "torch_dist":
            if not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0:
                # TODO Handle non-empty directories (e.g., after a crash during saving).
                ensure_directory_exists(checkpoint_name, check_parent=False)
            if checkpointing_context is not None and 'save_strategy' in checkpointing_context:
                save_strategy = checkpointing_context['save_strategy']
                # Already saved once before - don't need to rerun sharding validation
                validate_sharding_integrity = not args.ckpt_assume_constant_structure
            else:
                validate_sharding_integrity = True
                save_strategy = get_default_save_sharded_strategy(args.ckpt_format)
                if args.ckpt_assume_constant_structure and args.ckpt_format == 'torch_dist':
                    save_strategy.use_cached_ckpt_structure = args.ckpt_assume_constant_structure
                    if checkpointing_context is not None and 'load_strategy' in checkpointing_context:
                        cached_global_metadata = getattr(checkpointing_context['load_strategy'], 'cached_global_metadata', None)
                        if cached_global_metadata is not None:
                            logger.debug("Plugging in the read metadata from the load strategy...")
                            save_strategy.cached_global_metadata = cached_global_metadata
                        else:
                            logger.debug("Failed to plug in the read metadata from the load strategy...")

                if args.ckpt_fully_parallel_save:
                    save_strategy = FullyParallelSaveStrategyWrapper(save_strategy, mpu.get_data_parallel_group(with_context_parallel=True),
                                                                     args.ckpt_assume_constant_structure)
            # Store save strategy for future checkpoint saves
            if checkpointing_context is not None:
                checkpointing_context['save_strategy'] = save_strategy
            end_ckpt = time()
            logger.debug(f"rank: {rank}, takes {end_ckpt - start_ckpt} to prepare state dict for ckpt ")
            async_save_request = dist_checkpointing.save(state_dict, checkpoint_name, save_strategy,
                                                         async_sharded_save=args.async_save,
                                                         validate_access_integrity=validate_sharding_integrity,
                                                         preprocess_common_before_consistancy_check=preprocess_common_state_dict_fn,
                                                         content_metadata=sharded_sd_metadata)
            # [ModelOpt]: save sharded modelopt_state
            if has_nvidia_modelopt:
                save_sharded_modelopt_state(model, checkpoint_name, (args.ckpt_format, 1))
        elif ckpt_type == CheckpointType.GLOBAL and ckpt_format == "torch_dcp":
            if not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0:
                # TODO Handle non-empty directories (e.g., after a crash during saving).
                ensure_directory_exists(checkpoint_name, check_parent=False)

            fs_storage_writer = torch.distributed.checkpoint.FileSystemWriter(checkpoint_name)
            torch.distributed.checkpoint.save(
                state_dict=state_dict,
                storage_writer=fs_storage_writer,
            )
        else:
            # [ModelOpt]: Inject modelopt_state into state_dict
            if has_nvidia_modelopt:
                if ckpt_type == CheckpointType.LOCAL:
                    print_rank_0('WARNING: Local checkpointing does not support nvidia_modelopt.')
                else:
                    save_modelopt_state(model, state_dict)

            end_ckpt = time()
            logger.debug(f"rank: {rank}, takes {end_ckpt - start_ckpt} to prepare state dict for ckpt ")
            if ckpt_type == CheckpointType.LOCAL:
                try:
                    from megatron.core.dist_checkpointing.tensor_aware_state_dict import MCoreTensorAwareStateDict
                except ModuleNotFoundError:
                    raise RuntimeError("The 'nvidia_resiliency_ext' module is required for local "
                                       "checkpointing but was not found. Please ensure it is installed.")

                algo = args.non_persistent_local_ckpt_algo
                cached_metadata = None
                if args.ckpt_assume_constant_structure and 'local_checkpoint_cache' in checkpointing_context:
                    cached_metadata = checkpointing_context['local_checkpoint_cache']
                state_dict_for_save, cacheable_metadata = MCoreTensorAwareStateDict.from_state_dict(
                    state_dict, algo=algo, cached_metadata=cached_metadata,
                    parallelization_group=mpu.get_data_parallel_group(with_context_parallel=True)
                )
                async_save_request = checkpointing_context['local_checkpoint_manager'].save(
                    state_dict_for_save, iteration, is_async=bool(args.async_save)
                )
                checkpointing_context['local_checkpoint_cache'] = cacheable_metadata
            else:
                assert ckpt_type == CheckpointType.LEGACY
                # Save.
                ensure_directory_exists(checkpoint_name)
                torch.save(state_dict, checkpoint_name)
    start_misc = time()
    if ckpt_type != CheckpointType.LOCAL:
        if not args.async_save:
            assert async_save_request is None
            # Wait so everyone is done (necessary)
            if torch.distributed.is_initialized():
                torch.distributed.barrier()

    # And update the latest iteration
    if not torch.distributed.is_initialized() \
            or torch.distributed.get_rank() == 0:
        tracker_filename = get_checkpoint_tracker_filename(save_dir)

        if ckpt_type == CheckpointType.LOCAL:
            def iter_finalize_fn():
                print_rank_0('  successfully saved local checkpoint from iteration {:7d}'
                             .format(iteration))
                if args.log_progress and args.async_save:
                    append_to_progress_log(f'Saved async local checkpoint\tIteration: {iteration}',
                                           barrier=False)
        else:
            def iter_finalize_fn():
                prev_iteration = 0
                save_retain_interval = getattr(args, 'save_retain_interval', None)  # For backwards compatibility of tests.
                if save_retain_interval is not None:
                    if os.path.exists(tracker_filename):  # TODO: Make this work with MSC remote paths?
                        with open_file(tracker_filename, 'r') as f:
                            prev_iteration = int(f.read().strip())
                with open_file(tracker_filename, 'w') as f:
                    f.write(str(iteration))
                tensor_rank_to_print = (tensor_rank if tensor_rank is not None else mpu.get_tensor_model_parallel_rank()) + 1
                pipeline_rank_to_print = (pipeline_rank if pipeline_rank is not None else mpu.get_pipeline_model_parallel_rank()) + 1
                print_rank_0(f'  successfully saved checkpoint from iteration {int(iteration):7d} to {args.save} '
                             f'[ t {tensor_rank_to_print}/{mpu.get_tensor_model_parallel_world_size()}, '
                             f'p {pipeline_rank_to_print}/{mpu.get_pipeline_model_parallel_world_size()} ]')
                if args.log_progress and args.async_save:
                    append_to_progress_log(f'Saved async checkpoint\tIteration: {iteration}',
                                           barrier=False)

                def delete_checkpoint(args, iteration_to_delete):
                    checkpoint_name = get_checkpoint_name(args.save, iteration=iteration_to_delete,
                                                          return_base_dir=True)
                    try:
                        shutil.rmtree(checkpoint_name)  # TODO: Make this work with MSC remote paths?
                        print_rank_0(f'  successfully deleted checkpoint from iteration {iteration_to_delete:7d} '
                                     f'at {args.save}')
                        if args.log_progress:
                            append_to_progress_log(f'Deleted checkpoint\tIteration: {iteration_to_delete}', barrier=False)
                    except Exception as e:
                        print_rank_0(f'  encountered exception "{e}" when trying to delete checkpoint from '
                                     f'iteration {iteration_to_delete:7d} at {args.save}')
                        # Any exception encountered in checkpoint deletion can be ignored and is not fatal.
                        pass

                if save_retain_interval is not None:
                    if prev_iteration > 0 and prev_iteration != iteration and prev_iteration % save_retain_interval != 0:
                        checkpoint_name = get_checkpoint_name(args.save, iteration=prev_iteration,
                                                              return_base_dir=True)
                        # Don't delete if `checkpoint_name` is a symbolic link.
                        if os.path.islink(checkpoint_name):  # TODO: Make this work with MSC remote paths?
                            print_rank_0(f'  skipping deleting checkpoint from iteration {prev_iteration:7d} '
                                         f'at {args.save} since it is a symbolic link')
                        else:
                            # Asynchronous version of delete_checkpoint(args, iteration_to_delete=prev_iteration).
                            threading.Thread(target=delete_checkpoint, args=(args, prev_iteration,)).start()

        if args.async_save:
            assert async_save_request is not None
            async_save_request.add_finalize_fn(iter_finalize_fn)
        else:
            iter_finalize_fn()

    # Additional callback for one_logger (last rank)
    if not torch.distributed.is_initialized() \
       or is_last_rank():
        def onelogger_finalize_fn():
            on_save_checkpoint_success(productive_metrics, args.async_save)
        if args.async_save:
            assert async_save_request is not None
            async_save_request.add_finalize_fn(onelogger_finalize_fn)
        else:
            onelogger_finalize_fn()

    # Additional callback for wandb (last rank)
    if not torch.distributed.is_initialized() \
       or is_last_rank():
        def wandb_finalize_fn():
            wandb_utils.on_save_checkpoint_success(checkpoint_name, get_checkpoint_tracker_filename(save_dir), save_dir, iteration)
        if args.async_save:
            assert async_save_request is not None
            async_save_request.add_finalize_fn(wandb_finalize_fn)
        else:
            wandb_finalize_fn()

    if args.async_save:
        schedule_async_save(async_save_request)
        print_rank_0('  scheduled an async checkpoint save at iteration {:7d} to {}' \
                     .format(iteration, save_dir))

    # Wait so everyone is done (not necessary)
    if torch.distributed.is_initialized():
        torch.distributed.barrier()

    end_misc = time()
    logger.debug(f"rank: {rank}, takes {end_misc - start_misc} to finalize ckpt save ")

    ft_integration.on_checkpointing_end(is_async_finalization=False)

def cleanup_old_non_persistent_checkpoint(save_dir, leave_ckpt_num=1, do_async=False):
    if torch.distributed.is_initialized() and torch.distributed.get_rank() != 0:
        return
    save_dir = Path(save_dir)

    iter_prefix = "iter_"
    iter_ckpts = save_dir.rglob(f'{iter_prefix}*')
    sorted_iter_ckpts = sorted(iter_ckpts, key=lambda ckpt_name: int(ckpt_name.name[len(iter_prefix):]))
    if not sorted_iter_ckpts:
        return
    rm_iter_ckpts = sorted_iter_ckpts[:-leave_ckpt_num]
    print_rank_0(f'Non-persistent checkpoints scheduled for removal: {rm_iter_ckpts}')
    print_rank_0(f'Non-persistent checkpoints to be kept: {sorted_iter_ckpts[-leave_ckpt_num:]}')

    def remove_iter_ckpts(_iter_ckpts):
        for ckpt in _iter_ckpts:
            shutil.rmtree(ckpt)
    if do_async:
        threading.Thread(target=remove_iter_ckpts, args=(rm_iter_ckpts,)).start()
    else:
        remove_iter_ckpts(rm_iter_ckpts)


def maybe_save_dataloader_state(train_iterator, iteration, dataloader_save_path):
    """Saves dataloader state if the dataloader supports it.

    Currently, this is only used by Megatron Energon dataloader (multimodal) to store its state at a
    specific iteration. The Megatron built-in dataloader (text-only) creates index files upfront
    to track its state.

    If the provided dataloader has `save_state` method, then it is called to save the state.
    Otherwise, no state is saved.

    Args:
        train_iterator (iterable): Train dataloader.
        iteration (int): Current iteration.
        dataloader_save_path (str): Path where the dataloader state is saved.
    """
    # If no dataloader or saving path is provided, exit early, otherwise, raise an error.
    if train_iterator is None or dataloader_save_path is None or dataloader_save_path == "":
        return

    # If dataloader doesn't support saving state, raise an error.
    if not hasattr(train_iterator.iterable, "save_state"):
        raise RuntimeError(f"Could not find a save_state for the train_iterator of type {type(train_iterator)}")

    # Save dataloader state for each data parallel rank only once.
    first_rank = mpu.is_pipeline_first_stage(ignore_virtual=True) and mpu.get_tensor_model_parallel_rank() == 0
    if not first_rank:
        return

    dp_rank = mpu.get_data_parallel_rank()
    print(f"saving dataloader checkpoint at iteration {iteration} to {dataloader_save_path}")
    train_dataloader_state_dict = train_iterator.iterable.save_state()
    data_state_save_path = get_checkpoint_name(
        dataloader_save_path, iteration,
        basename=f'train_dataloader_dprank{dp_rank:03d}.pt'
    )

    torch.distributed.barrier(group=mpu.get_data_parallel_group())

    if mpu.get_data_parallel_rank() == 0:
        ensure_directory_exists(data_state_save_path)

    torch.distributed.barrier(group=mpu.get_data_parallel_group())

    dataloader_save_dict = {}
    dataloader_save_dict['dataloader_state_dict'] = train_dataloader_state_dict
    torch.save(dataloader_save_dict, data_state_save_path)


def generate_state_dict(args, model, optimizer, opt_param_scheduler,
                        rng_state, iteration=None,
                        optim_sd_kwargs=None, model_sd_kwargs=None, rerun_state=None):
    """Generate a state dict from given model, optimizer, scheduler, rng state and others. """

    # Arguments, iteration, and model.
    state_dict = {}
    state_dict['args'] = args
    state_dict['checkpoint_version'] = 3.0
    if iteration is not None:
        state_dict['iteration'] = iteration

    for i in range(len(model)):
        key = "model"
        if len(model) > 1:
            key = f"model{i}"

        if args.ckpt_format == "torch_dist":
            model_sd = model[i].sharded_state_dict(**(model_sd_kwargs or {}))
        else:   # torch, torch_dcp
            model_sd = model[i].state_dict_for_save_checkpoint()

        state_dict[key] = model_sd

    # Optimizer stuff.
    if not args.no_save_optim:
        if optimizer is not None and not optimizer.is_stub_optimizer:
            optimizer_sd = None

            if args.ckpt_format == "torch_dist":
                optimizer_sd = optimizer.sharded_state_dict(state_dict, **(optim_sd_kwargs or {}))
            else:
                optimizer_sd = optimizer.state_dict()

            state_dict['optimizer'] = optimizer_sd

        if opt_param_scheduler is not None:
            state_dict['opt_param_scheduler'] = \
                opt_param_scheduler.state_dict()

    # Rerun state
    if rerun_state:
        state_dict['rerun_state_machine'] = rerun_state

    # RNG states.
    if not args.no_save_rng and rng_state:
        state_dict["rng_state"] = rng_state
    return state_dict


def _transpose_first_dim(t, num_splits, num_splits_first, model):
    input_shape = t.size()
    # We use a self_attention module but the values extracted aren't
    # specific to self attention so should work for cross attention as well
    while hasattr(model, 'module'):
        model = model.module
    attention_module = model.language_model.encoder.layers[0].self_attention
    hidden_size_per_attention_head = attention_module.hidden_size_per_attention_head
    num_attention_heads_per_partition = attention_module.num_attention_heads_per_partition
    if num_splits_first:
        """[num_splits * np * hn, h]
        -->(view) [num_splits, np, hn, h]
        -->(tranpose) [np, num_splits, hn, h]
        -->(view) [np * num_splits * hn, h] """

        intermediate_shape = \
            (num_splits, num_attention_heads_per_partition,
             hidden_size_per_attention_head) + input_shape[1:]

        t = t.view(*intermediate_shape)
        t = t.transpose(0, 1).contiguous()
    else:
        """[np * hn * num_splits, h]
        -->(view) [np, hn, num_splits, h]
        -->(tranpose) [np, num_splits, hn, h]
        -->(view) [np * num_splits * hn, h] """

        intermediate_shape = \
            (num_attention_heads_per_partition,
             hidden_size_per_attention_head, num_splits) +\
             input_shape[1:]

        t = t.view(*intermediate_shape)
        t = t.transpose(1, 2).contiguous()
    t = t.view(*input_shape)

    return t


def fix_query_key_value_ordering(model, checkpoint_version):
    """Fix up query/key/value matrix ordering if checkpoint
    version is smaller than 2.0
    """
    if checkpoint_version < 2.0:
        if isinstance(model, list):
            assert len(model)==1
            model = model[0]
        for name, param in model.named_parameters():
            if name.endswith(('.query_key_value.weight', '.query_key_value.bias')):
                if checkpoint_version == 0:
                    fixed_param = _transpose_first_dim(param.data, 3, True, model)
                elif checkpoint_version == 1.0:
                    fixed_param = _transpose_first_dim(param.data, 3, False, model)
                else:
                    print_rank_0(f"Invalid checkpoint version {checkpoint_version}.")
                    sys.exit()
                param.data.copy_(fixed_param)
            if name.endswith(('.key_value.weight', '.key_value.bias')):
                if checkpoint_version == 0:
                    fixed_param = _transpose_first_dim(param.data, 2, True, model)
                elif checkpoint_version == 1.0:
                    fixed_param = _transpose_first_dim(param.data, 2, False, model)
                else:
                    print_rank_0(f"Invalid checkpoint version {checkpoint_version}.")
                    sys.exit()
                param.data.copy_(fixed_param)
        print_rank_0(" successfully fixed query-key-values ordering for"
                     " checkpoint version {}".format(checkpoint_version))


def _get_non_persistent_iteration(non_persistent_global_dir, args, checkpointing_context=None):
    if args.non_persistent_ckpt_type is None:
        return -1
    elif args.non_persistent_ckpt_type == "global":
        tracker_filename = get_checkpoint_tracker_filename(non_persistent_global_dir)
        if isfile(tracker_filename):
            iteration, release = read_metadata(tracker_filename)
            if release:
                raise RuntimeError('Non-persistent checkpoint can\'t be a release checkpoint')
        else:
            iteration = -1
            print_rank_0('WARNING: could not find the metadata file {}'.format(tracker_filename))
            print_rank_0('    will not load any non-persistent checkpoint')
        return iteration
    elif args.non_persistent_ckpt_type == "local":
        return checkpointing_context['local_checkpoint_manager'].find_latest()
    else:
        assert False, 'Please use local or global non-persistent checkpoints' \
            f'(got: {args.non_persistent_ckpt_type})'


def _load_non_persistent_base_checkpoint(
    non_persistent_global_dir,
    args,
    rank0,
    sharded_state_dict,
    non_persistent_iteration,
    checkpointing_context=None,
):
    """ Load the base state_dict from a non-persistent distributed checkpoint.
    Depending on the non_persistent_ckpt_type, different logic may be required.
    """
    assert args.non_persistent_ckpt_type is not None
    if args.non_persistent_ckpt_type == "global":
        if not rank0:
            print_rank_0(
                f'Loading from a non-persistent checkpoint (non-persistent iter {non_persistent_iteration})'
            )
        return _load_global_dist_base_checkpoint(
            non_persistent_global_dir, args, rank0, sharded_state_dict, non_persistent_iteration, False,
            checkpointing_context=checkpointing_context
        )
    elif args.non_persistent_ckpt_type == "local":
        intermediate_state_dict, checkpoint_name = checkpointing_context[
            'local_checkpoint_manager'
        ].load()
        state_dict = intermediate_state_dict.to_state_dict(
            sharded_state_dict,
            algo=args.non_persistent_local_ckpt_algo,
            parallelization_group = mpu.get_data_parallel_group(with_context_parallel=True)
        )
        return state_dict, checkpoint_name, False, CheckpointType.LOCAL
    else:
        raise NotImplementedError(f"Please use local or global non-persistent checkpoints (got: {args.non_persistent_ckpt_type})")


def _load_global_dist_base_checkpoint(
    load_dir, args, rank0, sharded_state_dict, iteration, release, checkpointing_context=None
):
    """ Load the base state_dict from the given directory containing the global distributed checkpoint """
    if rank0:
        checkpoint_name = find_checkpoint_rank_0(load_dir, iteration, release)
        state_dict = dist_checkpointing.load_common_state_dict(checkpoint_name)
        return state_dict, checkpoint_name, release, CheckpointType.GLOBAL

    if sharded_state_dict is None:
        assert not args.auto_detect_ckpt_format and not args.use_dist_ckpt, (
            args.auto_detect_ckpt_format,
            args.use_dist_ckpt,
        )
        raise RuntimeError(
            'Detected load from a distributed checkpoint, but neither --use-dist-ckpt nor --auto-detect-ckpt-format is set.'
        )

    checkpoint_name = get_checkpoint_name(load_dir, iteration, release, return_base_dir=True)
    load_strategy = get_default_load_sharded_strategy(checkpoint_name)
    # NOTE: `args.ckpt_fully_parallel_load` applies to both persistent and non-persistent checkpoints.
    if args.ckpt_fully_parallel_load:
        load_strategy = FullyParallelLoadStrategyWrapper(
            load_strategy, mpu.get_data_parallel_group(with_context_parallel=True)
        )
    if checkpointing_context is not None:
        checkpointing_context["load_strategy"] = load_strategy
    state_dict = dist_checkpointing.load(sharded_state_dict, checkpoint_name, load_strategy, strict=args.dist_ckpt_strictness)
    return state_dict, checkpoint_name, release, CheckpointType.GLOBAL


def _get_checkpoint_format(checkpoint_name):
    """Get the format of an existing checkpoint."""
    if MultiStorageClientFeature.is_enabled():
        msc = MultiStorageClientFeature.import_package()
        checkpoint_dir = msc.Path(checkpoint_name)
        is_torch_ckpt = any([f.name.startswith("mp_rank_0") for f in checkpoint_dir.iterdir()])
        is_torch_dcp = checkpoint_dir.joinpath(".metadata").exists()
    else:
        is_torch_ckpt = any([f.startswith("mp_rank_0") for f in os.listdir(checkpoint_name)])
        is_torch_dcp = os.path.exists(os.path.join(checkpoint_name, ".metadata"))

    ckpt_format = None
    if dist_checkpointing.check_is_distributed_checkpoint(checkpoint_name):
        ckpt_format = "torch_dist"
    elif is_torch_ckpt:
        ckpt_format = "torch"
    elif is_torch_dcp:
        ckpt_format = "torch_dcp"
    else:
        raise NotImplementedError(f"unknown checkpoint format in {checkpoint_name}")

    return ckpt_format


def _load_base_checkpoint(
    load_dir,
    args,
    rank0=False,
    sharded_state_dict=None,
    checkpointing_context=None,
):
    """ Load the base state_dict from the given directory

    If rank0 is true, just loads rank 0 checkpoint, ignoring arguments.
    """
    # Try to load non-persistent checkpoint first
    non_persistent_global_dir = (
        args.non_persistent_global_ckpt_dir
        if args.non_persistent_global_ckpt_dir or load_dir is None
        else os.path.join(load_dir, _NON_PERSISTENT_CKPT_SUBDIR)
    )
    non_persistent_iteration = _get_non_persistent_iteration(
        non_persistent_global_dir, args, checkpointing_context
    )
    iteration, release = -1, False
    tracker_filename = 'because load directory is not defined'
    if load_dir is not None:
        tracker_filename = get_checkpoint_tracker_filename(load_dir)
        if isfile(tracker_filename):
            iteration, release = read_metadata(tracker_filename)

    # Allow user to specify the loaded iteration.
    if getattr(args, "ckpt_step", None):
        iteration = args.ckpt_step

    if non_persistent_iteration != -1:  # there is a non-persistent checkpoint
        if non_persistent_iteration >= iteration:
            return _load_non_persistent_base_checkpoint(
                non_persistent_global_dir,
                args,
                rank0,
                sharded_state_dict,
                non_persistent_iteration,
                checkpointing_context,
            )
        else:
            print_rank_0('WARNING: non-persistent checkpoints are older than persistent checkpoint')

    # Otherwise we are dealing with global checkpoints
    # If no tracker file, return nothing
    if iteration == -1:
        if not rank0:
            print_rank_0('WARNING: could not find the metadata file {}'.format(tracker_filename))
            print_rank_0('    will not load any checkpoints and will start from random')
        # Conditionally exit if checkpoint not found.
        if args.exit_on_missing_checkpoint:
            print_rank_0(">> '--exit-on-missing-checkpoint' set ... exiting. <<")
            if torch.distributed.is_initialized():
                torch.distributed.barrier()
            sys.exit()

        return None, "", False, None

    # Determine the type of the checkpoint on disk.
    checkpoint_name = get_checkpoint_name(load_dir, iteration, release, return_base_dir=True)
    ckpt_format = _get_checkpoint_format(checkpoint_name)

    if not rank0:
        dist_infix = "distributed " if ckpt_format == "torch_dist" else ""
        if release:
            print_rank_0(f' loading release {dist_infix}checkpoint from {load_dir}')
        else:
            print_rank_0(
                f' loading {dist_infix}checkpoint from {load_dir} at iteration {iteration}'
            )

    ckpt_type = None

    # Handle global distributed checkpoint
    if ckpt_format == "torch_dist":
        return _load_global_dist_base_checkpoint(
            load_dir, args, rank0, sharded_state_dict, iteration, release, checkpointing_context=checkpointing_context
        )
    elif ckpt_format == "torch":
        ckpt_type = CheckpointType.LEGACY
        # Handle global legacy checkpoint
        if rank0:
            checkpoint_name = find_checkpoint_rank_0(load_dir, iteration, release)
        else:
            checkpoint_name = get_checkpoint_name(load_dir, iteration, release, return_base_dir=False)
        try:
            state_dict = torch.load(checkpoint_name, map_location='cpu', weights_only=False)
        except ModuleNotFoundError:
            from megatron.legacy.fp16_deprecated import loss_scaler

            # For backward compatibility.
            if not rank0:
                print_rank_0(' > deserializing using the old code structure ...')
            sys.modules['fp16.loss_scaler'] = sys.modules['megatron.legacy.fp16_deprecated.loss_scaler']
            sys.modules['megatron.fp16.loss_scaler'] = sys.modules[
                'megatron.legacy.fp16_deprecated.loss_scaler'
            ]
            sys.modules['megatron.model'] = sys.modules['megatron.legacy.model']
            state_dict = torch.load(checkpoint_name, map_location='cpu', weights_only=False)
            sys.modules.pop('fp16.loss_scaler', None)
            sys.modules.pop('megatron.fp16.loss_scaler', None)
            sys.modules.pop('megatron.model', None)
        except Exception as e:
            print('could not load the checkpoint')
            print(e)
            sys.exit()
    elif ckpt_format == "torch_dcp":
        ckpt_type = CheckpointType.TORCH_DCP

        if rank0:
            # _load_base_checkpoint is called from load_args_from_checkpoint. torch.distributed is not initialized.
            # Load only metadata.
            state_dict = {"args": None, "iteration": None}
            torch.distributed.checkpoint.load(
                state_dict=state_dict,
                checkpoint_id=checkpoint_name,
            )
        else:
            # _load_base_checkpoint is called from load_checkpoint with a proper state dict.
            state_dict = sharded_state_dict

            fs_storage_reader = torch.distributed.checkpoint.FileSystemReader(checkpoint_name)

            torch.distributed.checkpoint.load_state_dict(
                state_dict=state_dict,
                storage_reader=fs_storage_reader,
            )
    else:
        raise NotImplementedError(f"checkpoint format {ckpt_format} not supported")

    return state_dict, checkpoint_name, release, ckpt_type


def load_args_from_checkpoint(
    args, load_arg='load', checkpointing_context=None
):
    """Set required arguments from the checkpoint specified in the
    arguments.

    Will overwrite arguments that have a non-None default value, but
    will leave any arguments that default to None as set.

    Returns the same args NameSpace with the new values added/updated.

    If no checkpoint is specified in args, or if the checkpoint is
    there but invalid, the arguments will not be modified

    """
    load_dir = getattr(args, load_arg)

    if load_dir is None:
        print_rank_0('No load directory specified, using provided arguments.')
        return args

    state_dict, checkpoint_name, release, ckpt_type = _load_base_checkpoint(
        load_dir,
        args,
        rank0=True,
        checkpointing_context=checkpointing_context,
    )

    # Args.
    if not state_dict:
        print_rank_0('Checkpoint not found to provide arguments, using provided arguments.')
        return args

    if 'args' not in state_dict:
        print_rank_0('Checkpoint provided does not have arguments saved, using provided arguments.')
        return args

    checkpoint_args = state_dict['args']
    checkpoint_version = state_dict.get('checkpoint_version', 0)
    args.iteration = state_dict['iteration']

    # One-off conversion for foundation models
    if hasattr(checkpoint_args, 'disable_bias_linear'):
        setattr(
            checkpoint_args, 'add_bias_linear', not getattr(checkpoint_args, 'disable_bias_linear')
        )

    def _set_arg(arg_name, old_arg_name=None, force=False):
        if not force and getattr(args, arg_name, None) is not None:
            return

        if old_arg_name is not None:
            checkpoint_value = getattr(checkpoint_args, old_arg_name, None)
        else:
            checkpoint_value = getattr(checkpoint_args, arg_name, None)

        if checkpoint_value is not None:
            print_rank_0(f"Setting {arg_name} to {checkpoint_value} from checkpoint")
            setattr(args, arg_name, checkpoint_value)
        else:
            print_rank_0(f"Checkpoint did not provide arguments {arg_name}")

    # Model args.
    _set_arg('num_layers')
    _set_arg('hidden_size')
    _set_arg('ffn_hidden_size')
    _set_arg('seq_length')
    _set_arg('num_attention_heads')
    _set_arg('num_query_groups', force=True)
    _set_arg('group_query_attention', force=True)
    _set_arg('kv_channels')
    _set_arg('max_position_embeddings')
    _set_arg('position_embedding_type', force=True)
    _set_arg('add_position_embedding', force=True)
    _set_arg('use_rotary_position_embeddings', force=True)
    _set_arg('rotary_base', force=True)
    _set_arg('rotary_percent', force=True)
    _set_arg('rotary_interleaved', force=True)
    _set_arg('add_bias_linear', force=True)
    _set_arg('add_qkv_bias', force=True)
    _set_arg('squared_relu', force=True)
    _set_arg('swiglu', force=True)
    _set_arg('untie_embeddings_and_output_weights', force=True)
    _set_arg('apply_layernorm_1p', force=True)
    _set_arg('normalization', force=True)
    _set_arg('apply_query_key_layer_scaling', force=True)
    _set_arg('attention_dropout', force=True)
    _set_arg('hidden_dropout', force=True)

    _set_arg('hybrid_override_pattern', force=True)
    _set_arg('spec', force=True)
    _set_arg('hybrid_attention_ratio', force=True)
    _set_arg('hybrid_mlp_ratio', force=True)

    _set_arg('num_experts', force=True)
    _set_arg('moe_layer_freq', force=True)
    if getattr(checkpoint_args, 'num_experts', None) is not None:
        _set_arg('moe_ffn_hidden_size', force=True)
    else:
        setattr(args, 'moe_ffn_hidden_size', None)
    _set_arg('moe_router_topk', force=True)
    _set_arg('moe_token_dispatcher_type', force=True)
    _set_arg('moe_router_pre_softmax', force=True)
    _set_arg('moe_grouped_gemm', force=True)
    _set_arg('moe_shared_expert_intermediate_size', force=True)

    # Mamba args.
    _set_arg('mamba_state_dim', force=True)
    _set_arg('mamba_head_dim', force=True)
    _set_arg('mamba_num_groups', force=True)
    _set_arg('mamba_num_heads', force=True)
    _set_arg('is_hybrid_model', force=True)

    # Heterogeneous args.
    _set_arg('heterogeneous_layers_config_path', force=True)
    _set_arg('heterogeneous_layers_config_encoded_json', force=True)

    # Tokenizer args.
    _set_arg('tokenizer_type', force=True)
    # Using checkpoint version might not always be safe (e.g., if running on different cluster).
    if args.use_tokenizer_model_from_checkpoint_args:
        _set_arg('tokenizer_model', force=True)
    _set_arg('tiktoken_pattern', force=True)
    _set_arg('padded_vocab_size')

    # Checkpoint args.
    _set_arg('ckpt_format')

    # Model parallelism args.
    if args.use_mp_args_from_checkpoint_args:
        if checkpoint_version < 3.0:
            _set_arg('tensor_model_parallel_size', 'model_parallel_size')
        else:
            _set_arg('tensor_model_parallel_size', force=True)
            _set_arg('pipeline_model_parallel_size', force=True)
            _set_arg('virtual_pipeline_model_parallel_size', force=True)
            _set_arg('num_layers_per_virtual_pipeline_stage')
            _set_arg('expert_model_parallel_size', force=True)

    return args, checkpoint_args


def load_checkpoint(ddp_model, optimizer, opt_param_scheduler, load_arg='load', strict=True,
                    checkpointing_context=None, skip_load_to_model_and_opt=False):
    """Load a model checkpoint and return the iteration.
    strict (bool): whether to strictly enforce that the keys in
        :attr:`state_dict` of the checkpoint match the names of
        parameters and buffers in model.
    skip_load_to_model_and_opt (bool): whether to call `load_state_dict`
        for :attr:`model` and :attr:`optimizer`. In case of running FSDP2 with mcore distributed
        checkpointing, the tensors are already loaded in-place by `_load_base_checkpoint`.
    """
    args = get_args()
    load_dir = getattr(args, load_arg)

    # Check for model-opt format loading
    if hasattr(args, 'load_model_opt_format') and args.load_model_opt_format:
        print_rank_0(f'Loading checkpoint using ModelOpt format from {load_dir}')
        from megatron.post_training.checkpointing import load_modelopt_checkpoint

        # Call the ModelOpt checkpoint loading function
        load_modelopt_checkpoint(
            ddp_model,
            optimizer=optimizer,
            opt_param_scheduler=opt_param_scheduler,
            strict=strict,
            load_arg=load_arg
        )
        
        # Since load_modelopt_checkpoint doesn't return iteration count, we need to get it
        if torch.distributed.is_initialized():
            tracker_filename = get_checkpoint_tracker_filename(load_dir)
            if os.path.isfile(tracker_filename):
                iteration, release = read_metadata(tracker_filename)
                if release:
                    iteration = 0
            else:
                iteration = 0
        else:
            iteration = 0
        
        # We don't have a reliable way to get num_floating_point_operations_so_far from ModelOpt format
        return iteration, 0

    # Finetuning directories
    pretrained_dir = getattr(args, 'pretrained_checkpoint', None)
    if pretrained_dir is not None and not checkpoint_exists(load_dir):
        print_rank_0(
            f'Checkpoint file not found in load directory {load_dir} attempting to finetune with checkpoint in {pretrained_dir}'
        )
        load_dir = pretrained_dir
        if not checkpoint_exists(load_dir):
            raise FileNotFoundError("No checkpoint found in load directory or pretrained directory")
        args.finetune = True

    model = unwrap_model(ddp_model)

    ckpt_format = args.ckpt_format
    if args.auto_detect_ckpt_format or ckpt_format == "torch_dist":
        state_dict, checkpoint_name, release, ckpt_type = _load_base_checkpoint(
            load_dir,
            args,
            rank0=True,
            checkpointing_context=checkpointing_context,
        )

        ckpt_format = None
        if ckpt_type == CheckpointType.TORCH_DCP:
            ckpt_format = "torch_dcp"
        elif ckpt_type == CheckpointType.LEGACY:
            ckpt_format = "torch"
        elif ckpt_type in [CheckpointType.LOCAL, CheckpointType.GLOBAL]:
            ckpt_format = "torch_dist"
        elif ckpt_type == None:
            pass    # Not loaded.
        else:
            raise NotImplementedError(f"checkpoint format {ckpt_format} not supported")

    load_kwargs = {}
    ignore_rng_state = False
    ignore_rerun_state = True
    if ckpt_format == "torch_dist":
        ckpt_tp_pp = (
            state_dict['args'].tensor_model_parallel_size,
            state_dict['args'].pipeline_model_parallel_size,
        )
        run_tp_pp = (
            args.tensor_model_parallel_size,
            args.pipeline_model_parallel_size,
        )

        ckpt_world_size = getattr(state_dict['args'], 'world_size', 0)
        run_world_size = getattr(args, 'world_size', 0)
        ckpt_dp = getattr(state_dict['args'], 'data_parallel_size', 0)
        run_dp = getattr(args, 'data_parallel_size', 0)
        mismatch_msg = "(TP, PP) mismatch after resume ({} vs {} from checkpoint)".format(
            run_tp_pp, ckpt_tp_pp
        )

        # Determine if RNG state will be loaded
        if (ckpt_tp_pp == run_tp_pp and not release and not args.finetune and not args.no_load_rng
                and not getattr(state_dict['args'], 'no_save_rng', False)):
            gen_sd_rng_state = get_rng_state(args.ckpt_format)  # we can load the rng state
        else:
            ignore_rng_state = True
            gen_sd_rng_state = None
            if ckpt_tp_pp != run_tp_pp:
                print_rank_0("{}: RNG state will be ignored".format(mismatch_msg))

        sharded_sd_metadata = dist_checkpointing.load_content_metadata(preloaded_state_dict=state_dict)
        print_rank_0(f'sharded_state_dict metadata loaded from the checkpoint: {sharded_sd_metadata}')
        # Determine if optimizer state will be loaded
        if (not release and not args.finetune and not args.no_load_optim
                and not getattr(state_dict['args'], 'no_save_optim', False)):
            gen_sd_optim = optimizer
            gen_sd_opt_param_scheduler = opt_param_scheduler

            if args.use_distributed_optimizer:
                if sharded_sd_metadata is None:
                    # Backward-compatibility with old checkpoints which don't have content versioning
                    # Can be removed after ending support for MLM optimizer checkpoints with MCore < v0.13
                    # (for MCore v0.13+ checkpoints `sharded_sd_metadata is not None`)
                    sharded_sd_metadata = {
                        'distrib_optim_sharding_type': ('fully_sharded_model_space'
                                                        if getattr(state_dict['args'], 'ckpt_fully_parallel_save', False)
                                                        else 'dp_zero_gather_scatter'),
                    }
                if ckpt_tp_pp != run_tp_pp and sharded_sd_metadata['distrib_optim_sharding_type'] != 'fully_sharded_model_space':
                    raise RuntimeError(f"{mismatch_msg}: not supported for DistributedOptimizer with sharding type"
                                       f" {sharded_sd_metadata['distrib_optim_sharding_type']}."
                                       f" Please use `--ckpt-fully-parallel-save` flag during checkpoint saving.")
        else:
            gen_sd_optim = None
            gen_sd_opt_param_scheduler = None

        optim_sd_kwargs = dict(metadata=sharded_sd_metadata, is_loading=True)
        model_sd_kwargs = dict(metadata=sharded_sd_metadata)

        # Determine if rerun state will be loaded
        gen_sd_rerun_state = None
        if (
            ckpt_world_size == run_world_size
            and ckpt_tp_pp == run_tp_pp
            and ckpt_dp == run_dp
            and not release
            and not args.finetune
            and 'rerun_state_machine' in state_dict
        ):
            rerun_state_machine = get_rerun_state_machine()
            if rerun_state_machine.validate_state_dict(state_dict['rerun_state_machine']):
                gen_sd_rerun_state = rerun_state_machine.state_dict(
                    data_iterator=None, ckpt_format=ckpt_format, force=True,
                )
                ignore_rerun_state = False
        if (
            ckpt_world_size != run_world_size
            or ckpt_tp_pp != run_tp_pp
            or ckpt_dp != run_dp
        ):
            print_rank_0("Job sharding has changed: Rerun state will be ignored")

        # [ModelOpt]: IMPORTANT! Restoring modelopt_state (sharded or not) must be performed
        # after the model instance has been created and before _load_base_checkpoint is called.
        if has_nvidia_modelopt:
            if ckpt_type == CheckpointType.LOCAL:
                print_rank_0('WARNING: Local checkpointing does not support nvidia_modelopt.')
            elif ckpt_type == CheckpointType.GLOBAL:
                restore_modelopt_state(model, state_dict)
            else:
                restore_sharded_modelopt_state(model, checkpoint_name)

        # [ModelOpt]: Initial loading from non-resume sharded checkpoint to a Distillation Model
        # will result in key mismatch with loss modules potentially containing parameters, since
        # it requires generating a state_dict before loading. Here we hide those modules if present.
        with contextlib.ExitStack() as stack:  # Allows multiple context managers for each model shard
            if args.finetune and hasattr(model[0], "hide_loss_modules"):
                for m in model:
                    stack.enter_context(m.hide_loss_modules())
            load_kwargs['sharded_state_dict'] = generate_state_dict(
                args, model, gen_sd_optim, gen_sd_opt_param_scheduler, gen_sd_rng_state,
                optim_sd_kwargs=optim_sd_kwargs, model_sd_kwargs=model_sd_kwargs,
                rerun_state=gen_sd_rerun_state
            )
    elif args.ckpt_format == "torch_dcp":
        model_sd = model[0].state_dict()
        optimizer_sd = optimizer.state_dict(is_loading=True)
        sharded_state_dict = {
            "model": model_sd,
            "optimizer": optimizer_sd,
            "args": None,
            "iteration": 1,
            "rng_state": get_rng_state(args.ckpt_format),
            "checkpoint_version": None,
            "opt_param_scheduler": opt_param_scheduler.state_dict(),
            "num_floating_point_operations_so_far": 0,
        }
        load_kwargs["sharded_state_dict"] = sharded_state_dict

    state_dict, checkpoint_name, release, ckpt_type = _load_base_checkpoint(
        load_dir, args, rank0=False, checkpointing_context=checkpointing_context,
        **load_kwargs
    )

    # Checkpoint not loaded.
    if state_dict is None:
        # Iteration and num_floating_point_operations_so_far default to 0.
        return 0, 0

    # Set checkpoint version.
    set_checkpoint_version(state_dict.get('checkpoint_version', 0))

    # Convert to regular torch tensor to DTensor.
    if ckpt_type == CheckpointType.LEGACY and args.ckpt_format == "torch_dcp":
        dtensor_state_dict = _to_dtensor(ddp_model, state_dict["model"])
        state_dict["model"] = dtensor_state_dict

    # Set iteration.
    if args.finetune or release:
        iteration = 0
    else:
        try:
            iteration = state_dict['iteration']
        except KeyError:
            try:  # Backward compatible with older checkpoints
                iteration = state_dict['total_iters']
            except KeyError:
                print_rank_0('A metadata file exists but unable to load '
                             'iteration from checkpoint {}, exiting'.format(checkpoint_name))
                sys.exit()
    num_floating_point_operations_so_far = state_dict.get('num_floating_point_operations_so_far', 0)

    # Check arguments.
    assert args.consumed_train_samples == 0
    assert args.skipped_train_samples == 0
    assert args.consumed_valid_samples == 0
    if 'args' in state_dict and not args.finetune:
        checkpoint_args = state_dict['args']
        check_checkpoint_args(checkpoint_args)
        args.consumed_train_samples = getattr(checkpoint_args,
                                              'consumed_train_samples', 0)
        args.skipped_train_samples = getattr(checkpoint_args,
                                             'skipped_train_samples', 0)
        update_num_microbatches(consumed_samples=args.consumed_train_samples, verbose=True)
        args.consumed_valid_samples = getattr(checkpoint_args,
                                              'consumed_valid_samples', 0)
    else:
        print_rank_0('could not find arguments in the checkpoint ...')

    def load_model_state_dict(module, state_dict, strict: bool):
        """Helper function to load state dict with fallback for missing extra states."""
        try:
            module.load_state_dict(state_dict, strict=strict)
        except Exception as e:
            if strict:
                # Fallback support for backward compatibility breaking changes in TransformerEngine
                load_return = module.load_state_dict(state_dict, strict=False)
                print(f"load_return: {load_return}")
    # Model.
    strict = False if args.retro_add_retriever else strict
    if not skip_load_to_model_and_opt:
        if len(ddp_model) == 1:
            load_model_state_dict(ddp_model[0], state_dict['model'], strict)
        else:
            for i in range(len(ddp_model)):
                # If there is no corresponding model in the state_dict, it will be ignored.
                # It means that this is an empty stage.
                if 'model%d' % i not in state_dict:
                    continue
                load_model_state_dict(ddp_model[i], state_dict['model%d' % i], strict)
    # Fix up query/key/value matrix ordering if needed.
    checkpoint_version = get_checkpoint_version()
    print_rank_0(f' checkpoint version {checkpoint_version}')
    fix_query_key_value_ordering(model, checkpoint_version)

    # Optimizer.
    if not release and not args.finetune and not args.no_load_optim:
        try:
            # Load state dict.
            if not skip_load_to_model_and_opt and optimizer is not None and not optimizer.is_stub_optimizer:
                optimizer.load_state_dict(state_dict['optimizer'])

            # Load distributed optimizer's custom parameter state.
            # For distributed checkpoint it's already loaded in load_state_dict above
            is_torch_dist = ckpt_format == "torch_dist"
            if args.use_distributed_optimizer and not is_torch_dist:
                # NOTE: this is a manual read of the tracker file.
                # This code should not be reached when reading from a non_persistent checkpoint
                assert not is_torch_dist
                tracker_filename = get_checkpoint_tracker_filename(load_dir)
                iteration, release = read_metadata(tracker_filename)
                model_checkpoint_name = \
                    get_checkpoint_name(load_dir, iteration, release)
                optim_checkpoint_name = \
                    get_distributed_optimizer_checkpoint_name(
                        model_checkpoint_name)
                optimizer.load_parameter_state(optim_checkpoint_name,
                                               update_legacy_format=args.ckpt_convert_update_legacy_dist_opt_format)

            # Load scheduler.
            if opt_param_scheduler is not None:
                if 'lr_scheduler' in state_dict: # backward compatbility
                    opt_param_scheduler.load_state_dict(state_dict['lr_scheduler'])
                else:
                    opt_param_scheduler.load_state_dict(state_dict['opt_param_scheduler'])
        except KeyError as e:
            print_rank_0('Unable to load optimizer from checkpoint {}. '
                         'Specify --no-load-optim or --finetune to prevent '
                         'attempting to load the optimizer state, '
                         'exiting ...'.format(checkpoint_name))
            raise e
    else:
        if (args.fp16 or args.bf16) and optimizer is not None:
            if args.load_main_params_from_ckpt:
                optimizer.reload_model_params(state_dict=state_dict)
            else:
                optimizer.reload_model_params()

    # rerun state
    if not ignore_rerun_state:
        try:
            if 'rerun_state_machine' in state_dict:
                get_rerun_state_machine().load_state_dict(state_dict['rerun_state_machine'])
        except Exception as e:
            print(f"Unable to restore RerunMachine from checkpoint: {e}. Skipping.")

    # rng states.
    if not release and not args.finetune and not args.no_load_rng and not ignore_rng_state:
        try:
            if 'rng_state' in state_dict:
                # access rng_state for data parallel rank
                if args.data_parallel_random_init:
                    rng_state = state_dict['rng_state'][mpu.get_data_parallel_rank()]
                else:
                    rng_state = state_dict['rng_state'][0]
                random.setstate(rng_state['random_rng_state'])
                np.random.set_state(rng_state['np_rng_state'])
                torch.set_rng_state(rng_state['torch_rng_state'])
                torch.cuda.set_rng_state(rng_state['cuda_rng_state'])
                # Check for empty states array
                if not rng_state['rng_tracker_states']:
                    raise KeyError
                tensor_parallel.get_cuda_rng_tracker().set_states(
                    rng_state['rng_tracker_states'])
            else:  # backward compatability
                random.setstate(state_dict['random_rng_state'])
                np.random.set_state(state_dict['np_rng_state'])
                torch.set_rng_state(state_dict['torch_rng_state'])
                torch.cuda.set_rng_state(state_dict['cuda_rng_state'])
                # Check for empty states array
                if not state_dict['rng_tracker_states']:
                    raise KeyError
                tensor_parallel.get_cuda_rng_tracker().set_states(
                    state_dict['rng_tracker_states'])
        except KeyError:
            print_rank_0('Unable to load rng state from checkpoint {}. '
                         'Specify --no-load-rng or --finetune to prevent '
                         'attempting to load the rng state, '
                         'exiting ...'.format(checkpoint_name))
            sys.exit()

    # Some utilities want to load a checkpoint without distributed being initialized
    if torch.distributed.is_initialized():
        torch.distributed.barrier()

    print_rank_0(f'  successfully loaded checkpoint from {load_dir} '
                 f'[ t {mpu.get_tensor_model_parallel_rank() + 1}/{mpu.get_tensor_model_parallel_world_size()}, '
                 f'p {mpu.get_pipeline_model_parallel_rank() + 1}/{mpu.get_pipeline_model_parallel_world_size()} ] '
                 f'at iteration {iteration}')

    # Additional callback for wandb (last rank)
    if not torch.distributed.is_initialized() \
       or is_last_rank():
        wandb_utils.on_load_checkpoint_success(checkpoint_name, load_dir)

    torch.cuda.empty_cache()

    if iteration > 0:
        # Notify FT that a checkpoint was loaded.
        is_local_chkpt = (ckpt_type == CheckpointType.LOCAL)
        ft_integration.on_checkpoint_loaded(is_local_chkpt=is_local_chkpt)

    return iteration, num_floating_point_operations_so_far


def _to_dtensor(wrapped_model, model_state_dict):
    device_mesh = wrapped_model[0].device_mesh

    new_model_sd = dict()
    for k, v in model_state_dict.items():
        # FP8 extra state cannot be converted to dtensor yet.
        if "_extra_state" in k:
            new_model_sd[k] = v
        else:
            new_model_sd[k] = torch.distributed.tensor.distribute_tensor(v, device_mesh)

    return new_model_sd


def load_biencoder_checkpoint(model, only_query_model=False,
                              only_context_model=False, custom_load_path=None):
    """
    selectively load retrieval models for indexing/retrieving
    from saved checkpoints
    """

    args = get_args()

    model = unwrap_model(model)

    load_path = custom_load_path if custom_load_path is not None else args.load

    tracker_filename = get_checkpoint_tracker_filename(load_path)

    with open_file(tracker_filename, 'r') as f:
        iteration = int(f.read().strip())

    checkpoint_name = get_checkpoint_name(load_path, iteration,
                                          args.use_distributed_optimizer,
                                          release=False)

    if mpu.get_data_parallel_rank() == 0:
        print('global rank {} is loading checkpoint {}'.format(
            torch.distributed.get_rank(), checkpoint_name))

    state_dict = torch.load(checkpoint_name, map_location='cpu', weights_only=False)
    ret_state_dict = state_dict['model']

    if only_query_model:
        ret_state_dict.pop('context_model')
    if only_context_model:
        ret_state_dict.pop('query_model')

    assert len(model) == 1
    model[0].load_state_dict(ret_state_dict)
    torch.distributed.barrier()

    if mpu.get_data_parallel_rank() == 0:
        print(' successfully loaded {}'.format(checkpoint_name))

    return model
