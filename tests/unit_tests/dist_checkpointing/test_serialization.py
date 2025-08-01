# Copyright (c) 2023, NVIDIA CORPORATION. All rights reserved.

import io
import logging
import os

import numpy as np
import pytest
import torch
from torch.distributed.checkpoint import CheckpointException as PyTCheckpointingException
from torch.distributed.checkpoint import FileSystemReader

try:
    from torch.distributed import DeviceMesh
    from torch.distributed._tensor import DTensor

    HAVE_DTENSOR = True
except ImportError:
    HAVE_DTENSOR = False

from megatron.core import parallel_state
from megatron.core.dist_checkpointing import (
    ShardedTensor,
    load,
    load_content_metadata,
    remove_sharded_tensors,
    save,
)
from megatron.core.dist_checkpointing.core import CheckpointingException, maybe_load_config
from megatron.core.dist_checkpointing.dict_utils import diff
from megatron.core.dist_checkpointing.mapping import ShardedObject, ShardedTensorFactory
from megatron.core.dist_checkpointing.serialization import (
    load_sharded_metadata,
    load_tensors_metadata,
)
from megatron.core.dist_checkpointing.strategies.base import StrategyAction, get_default_strategy
from megatron.core.dist_checkpointing.strategies.torch import TorchDistSaveShardedStrategy
from megatron.core.dist_checkpointing.validation import StrictHandling
from megatron.core.utils import is_torch_min_version
from tests.unit_tests.dist_checkpointing import TempNamedDir
from tests.unit_tests.test_utilities import Utils


class TestSerialization:
    def setup_method(self, method):
        pass

    def teardown_method(self, method):
        Utils.destroy_model_parallel()

    def test_single_process_save_load(self, tmp_path_dist_ckpt):
        Utils.initialize_model_parallel(1, 1)

        sharded_state_dict = {
            'sd_keyA': ShardedTensor.from_rank_offsets(
                'keyA', torch.ones(2, 4), replica_id=Utils.rank
            ),
            'sd_keyB': ShardedTensor.from_rank_offsets(
                'keyB', torch.ones(3, 5, 7), replica_id=Utils.rank
            ),
        }

        if HAVE_DTENSOR:
            mesh = DeviceMesh.from_group(
                parallel_state.get_data_parallel_group(with_context_parallel=True), "cuda"
            )
            sharded_state_dict['sd_keyD'] = ShardedTensor.from_rank_offsets(
                'keyD',
                DTensor.from_local(torch.ones(3, 5, 7), mesh)._local_tensor,
                replica_id=Utils.rank,
            )

        # sync=True to make sure other ranks wait for rank 0 to finish creating directory.
        with TempNamedDir(
            tmp_path_dist_ckpt / 'test_single_process_save_load', sync=True
        ) as ckpt_dir:
            save(sharded_state_dict, ckpt_dir)
            torch.distributed.barrier()

            saved_config = maybe_load_config(ckpt_dir)
            if saved_config.sharded_backend == 'zarr':
                assert (ckpt_dir / 'keyA').is_dir()
                assert (ckpt_dir / 'keyB').is_dir()
                assert not (ckpt_dir / 'keyC').exists()
                assert not (ckpt_dir / 'sd_keyA').is_dir()

                if HAVE_DTENSOR:
                    assert (ckpt_dir / 'keyD').is_dir()

            load_ssd = {
                'load_sd_keyA': ShardedTensor.from_rank_offsets(
                    'keyA', torch.ones(2, 4), replica_id=Utils.rank
                )
            }
            loaded_state_dict = load(load_ssd, ckpt_dir)

            assert set(loaded_state_dict.keys()) == {'load_sd_keyA'}
            assert isinstance(loaded_state_dict['load_sd_keyA'], torch.Tensor)
            assert loaded_state_dict['load_sd_keyA'].shape == (2, 4)

        Utils.destroy_model_parallel()

    def test_multi_process_save(self, tmp_path_dist_ckpt):
        Utils.initialize_model_parallel(2, 4)

        state_dict = {
            'sd_keyA': ShardedTensor.from_rank_offsets(
                'keyA', torch.ones(2, 4), (0, Utils.rank, Utils.world_size)
            ),
            'sd_keyB': ShardedTensor.from_rank_offsets(
                'keyB', torch.ones(3, 5, 7), (2, Utils.rank, Utils.world_size)
            ),
            'lr': 0.01,
            'rank': torch.distributed.get_rank(),
        }

        def preprocess_fn(x):
            del x['rank']
            return x

        # sync=True to make sure other ranks wait for rank 0 to finish creating directory.
        with TempNamedDir(tmp_path_dist_ckpt / 'test_multi_process_save', sync=True) as ckpt_dir:
            save(
                state_dict,
                ckpt_dir,
                validate_access_integrity=True,
                preprocess_common_before_consistancy_check=preprocess_fn,
            )

            saved_config = maybe_load_config(ckpt_dir)
            if saved_config.sharded_backend == 'zarr':
                assert (ckpt_dir / 'keyA').is_dir()
                assert (ckpt_dir / 'keyB').is_dir()
                assert not (ckpt_dir / 'keyC').exists()
                assert not (ckpt_dir / 'sd_keyA').is_dir()

        Utils.destroy_model_parallel()

    def test_multi_process_save_log_difference(self, tmp_path_dist_ckpt, caplog):
        Utils.initialize_model_parallel(2, 4)
        rank = Utils.rank
        world_size = Utils.world_size

        state_dict = {
            'sd_keyA': ShardedTensor.from_rank_offsets(
                'keyA', torch.ones(2, 4), (0, rank, world_size)
            ),
            'sd_keyB': ShardedTensor.from_rank_offsets(
                'keyB', torch.ones(3, 5, 7), (2, rank, world_size)
            ),
            'rank': rank,
        }

        def preprocess_fn(x):
            return x

        # sync=True to make sure other ranks wait for rank 0 to finish creating directory.
        with TempNamedDir(
            tmp_path_dist_ckpt / 'test_multi_process_save_log_difference', sync=True
        ) as ckpt_dir:
            with caplog.at_level(logging.WARNING):
                save(
                    state_dict,
                    ckpt_dir,
                    validate_access_integrity=True,
                    preprocess_common_before_consistancy_check=preprocess_fn,
                )

        if rank == 0:
            # Rank 0 should not log the warning related to common state dict difference
            assert not any(
                f"Rank {rank} common state dict differs from rank 0 common state dict."
                in record.message
                for record in caplog.records
            )
        else:
            found_detailed_match = False
            # Construct the expected full message string based on user request
            expected_full_message = (
                f"Rank {rank} common state dict differs from rank 0 common state dict. "
                f"Keys only on rank 0: [], "
                f"Keys only on {rank}: [], "
                f"Mismatched keys: [(('rank',), <class 'int'>, <class 'int'>)]"
            )

            for record in caplog.records:
                if record.message == expected_full_message:
                    found_detailed_match = True
                    break

            assert (
                found_detailed_match
            ), f"Did not find expected log message format for mismatch on rank {rank}. Expected: {expected_full_message}"

        Utils.destroy_model_parallel()

    def test_partition_change_save_load(self, tmp_path_dist_ckpt, strategy=None):
        Utils.initialize_model_parallel(2, 4)

        # ten_a: global shape (2, 4):
        ten_a_global = torch.tensor([[0, 1, 2, 3], [10, 11, 12, 13]])
        ten_a = (
            torch.zeros(1, 1)
            + 10 * parallel_state.get_tensor_model_parallel_rank()
            + parallel_state.get_pipeline_model_parallel_rank()
        )
        assert ten_a.shape == (1, 1)

        # ten_b: global shape (4, 5, 80), where (x, y, z) is (100x + z)
        ten_b = torch.zeros(4, 5, 10) + (torch.arange(10) + 10 * Utils.rank)
        ten_b += torch.arange(4).unsqueeze(-1).unsqueeze(-1) * 100
        assert ten_b.shape == (4, 5, 10)

        state_dict = {
            'sd_keyA': ShardedTensor.from_rank_offsets(
                'keyA',
                ten_a,
                (
                    0,
                    parallel_state.get_tensor_model_parallel_rank(),
                    parallel_state.get_tensor_model_parallel_world_size(),
                ),
                (
                    1,
                    parallel_state.get_pipeline_model_parallel_rank(),
                    parallel_state.get_pipeline_model_parallel_world_size(),
                ),
                replica_id=0,
            ),
            'sd_keyB': ShardedTensor.from_rank_offsets(
                'keyB', ten_b, (2, Utils.rank, Utils.world_size)
            ),
        }

        ten_a_global_shape = ten_a_global.shape
        ten_b_global_shape = (4, 5, 10 * 8)

        assert state_dict['sd_keyA'].local_shape == (1, 1)
        assert state_dict['sd_keyA'].global_shape == ten_a_global_shape
        assert state_dict['sd_keyB'].global_shape == ten_b_global_shape

        # sync=True to make sure other ranks wait for rank 0 to finish creating directory.
        with TempNamedDir(
            tmp_path_dist_ckpt / 'test_partition_change_save_load', sync=True
        ) as ckpt_dir:
            save(state_dict, ckpt_dir, strategy)

            del ten_a, ten_b

            # without changing TPxPP, load tensors without any sharding
            load_sd = {
                'sd_keyA': ShardedTensor.from_rank_offsets(
                    'keyA', torch.empty(ten_a_global_shape), replica_id=Utils.rank
                ),
                'sd_keyB': ShardedTensor.from_rank_offsets(
                    'keyB', torch.empty(ten_b_global_shape), replica_id=Utils.rank
                ),
            }
            loaded_state_dict = load(load_sd, ckpt_dir)

            ten_a = loaded_state_dict['sd_keyA']
            ten_b = loaded_state_dict['sd_keyB']
            assert isinstance(ten_a, torch.Tensor)
            assert ten_a.shape == ten_a_global_shape
            assert torch.all(ten_a == ten_a_global)

            assert isinstance(ten_b, torch.Tensor)
            assert ten_b.shape == ten_b_global_shape
            assert np.all(
                [
                    val == 100 * x + z
                    for x, x_row in enumerate(ten_b)
                    for y, y_row in enumerate(x_row)
                    for z, val in enumerate(y_row)
                ]
            )

            del ten_a, ten_b

            # change TPxPP
            Utils.destroy_model_parallel()
            Utils.initialize_model_parallel(1, 2)

            load_sd = {
                'sd_keyA': ShardedTensor.from_rank_offsets(
                    'keyA',
                    torch.empty(2, 1),
                    (
                        1,
                        parallel_state.get_data_parallel_rank(),
                        parallel_state.get_data_parallel_world_size(),
                    ),
                    replica_id=parallel_state.get_pipeline_model_parallel_rank(),
                ),
                'sd_keyB': ShardedTensor.from_rank_offsets(
                    'keyB',
                    torch.empty(5, 80),
                    (0, Utils.rank // 2, 4),
                    prepend_axis_num=1,
                    replica_id=Utils.rank % 2,
                ),
            }

            loaded_state_dict = load(load_sd, ckpt_dir)
            ten_a = loaded_state_dict['sd_keyA']
            ten_b = loaded_state_dict['sd_keyB']

            assert isinstance(ten_a, torch.Tensor)
            assert ten_a.shape == (2, 1)
            assert torch.all(
                ten_a[:, 0] == ten_a_global[:, parallel_state.get_data_parallel_rank()]
            )

            assert isinstance(ten_b, torch.Tensor)
            assert ten_b.shape == (5, 10 * 8)
            assert torch.all(
                ten_b == torch.arange(80).unsqueeze(0).expand(5, 80) + Utils.rank // 2 * 100
            )

    def test_load_tensors_metadata(self, tmp_path_dist_ckpt):
        Utils.initialize_model_parallel(2, 4)

        state_dict = {
            'sd_keyA': ShardedTensor.from_rank_offsets(
                'keyA', torch.arange(10) + Utils.rank * 10, (0, Utils.rank, Utils.world_size)
            ),
            'sd_keyB': ShardedTensor.from_rank_offsets(
                'keyB', torch.ones(3, 5, 7), (2, Utils.rank, Utils.world_size)
            ),
        }

        # sync=True to make sure other ranks wait for rank 0 to finish creating directory.
        with TempNamedDir(tmp_path_dist_ckpt / 'test_load_tensors_metadata', sync=True) as ckpt_dir:
            save(state_dict, ckpt_dir)

            del state_dict
            sharded_state_dict = load_tensors_metadata(ckpt_dir)
            # loaded dict keys are ShardedTensor keys!
            assert 'keyA' in sharded_state_dict
            assert 'sd_keyA' not in sharded_state_dict

            # Check metadata
            assert sharded_state_dict['keyA'].global_shape == (10 * Utils.world_size,)
            assert sharded_state_dict['keyB'].global_shape == (3, 5, 7 * Utils.world_size)
            assert sharded_state_dict['keyA'].local_shape == sharded_state_dict['keyA'].global_shape
            assert sharded_state_dict['keyB'].local_shape == sharded_state_dict['keyB'].global_shape
            assert sharded_state_dict['keyA'].global_offset == (0,)
            assert sharded_state_dict['keyB'].global_offset == (0, 0, 0)
            assert sharded_state_dict['keyA'].axis_fragmentations == (1,)
            assert sharded_state_dict['keyB'].axis_fragmentations == (1, 1, 1)
            assert sharded_state_dict['keyA'].replica_id == 0
            assert sharded_state_dict['keyB'].replica_id == 0

            # metadata dict can be loaded. We don't validate access because there are multiple replica_id=0
            state_dict = load(sharded_state_dict, ckpt_dir, validate_access_integrity=False)
            assert torch.all(state_dict['keyA'] == torch.arange(10 * Utils.world_size))

        Utils.destroy_model_parallel()

    def test_can_mix_sharded_tensors_and_factories(self, tmp_path_dist_ckpt):
        Utils.initialize_model_parallel(1, 1)

        def _build_fn(key, tensor, replica_id, flattened_range):
            assert flattened_range is None
            return [
                ShardedTensor.from_rank_offsets(key + 'part1', tensor, replica_id=replica_id),
                ShardedTensor.from_rank_offsets(key + 'part2', tensor, replica_id=replica_id),
                ShardedTensor.from_rank_offsets(key + 'part3', tensor, replica_id=replica_id),
            ]

        # state dict can be modified by dist_checkpointing.save, so two copies
        def get_sharded_state_dict(base=0):
            return {
                'all': [
                    ShardedTensor.from_rank_offsets(
                        'A', torch.arange(2) + base, replica_id=Utils.rank
                    ),
                    ShardedTensor.from_rank_offsets(
                        'B', torch.arange(3) + base, replica_id=Utils.rank
                    ),
                    ShardedTensor.from_rank_offsets(
                        'C', torch.arange(4) + base, replica_id=Utils.rank
                    ),
                    ShardedTensorFactory(
                        'D', torch.arange(5) + base, _build_fn, sum, replica_id=Utils.rank
                    ),
                ]
            }

        # sync=True to make sure other ranks wait for rank 0 to finish creating directory.
        with TempNamedDir(
            tmp_path_dist_ckpt / 'test_can_mix_sharded_tensors_and_factories', sync=True
        ) as ckpt_dir:
            save(get_sharded_state_dict(0), ckpt_dir)
            loaded_state_dict = load(get_sharded_state_dict(10), ckpt_dir)

        expected_sd = {
            'all': [
                torch.arange(2),
                torch.arange(3),
                torch.arange(4),
                torch.arange(5) * 3,  # sum of three parts, as specified in merge_fn
            ]
        }
        diffs = diff(loaded_state_dict, expected_sd)
        assert not any(map(bool, diffs)), diffs

        Utils.destroy_model_parallel()

    def test_load_error_msg(self, tmp_path_dist_ckpt):
        ckpt_dir_name = 'test_load_error_msg'
        Utils.initialize_model_parallel(1, 1)
        sh_ten = ShardedTensor.from_rank_offsets('keyA', torch.rand(10), replica_id=Utils.rank)
        state_dict = {'some_key': sh_ten}

        # Non-existent directory
        non_ex_path = f'/tmp/non-existent-path/{ckpt_dir_name}'
        with pytest.raises(CheckpointingException) as exc_info:
            load(state_dict, non_ex_path)
        assert f'directory {non_ex_path} does not exist' in str(exc_info.value)

        # sync=True to make sure other ranks wait for rank 0 to finish creating directory.
        with TempNamedDir(tmp_path_dist_ckpt / ckpt_dir_name, sync=True) as ckpt_dir:
            # Empty directory - not a distributed checkpoint
            with pytest.raises(CheckpointingException) as exc_info:
                load(state_dict, ckpt_dir)
            assert f'is not a distributed checkpoint' in str(exc_info.value)

            # Missing Zarr arrays
            torch.distributed.barrier()
            save(state_dict, ckpt_dir)
            sh_ten.key = 'different_key'
            with pytest.raises((CheckpointingException, PyTCheckpointingException)) as exc_info:
                load(state_dict, ckpt_dir)
            assert "different_key" in str(exc_info.value)

    def test_sharded_object_serialization(self, tmp_path_dist_ckpt):
        Utils.initialize_model_parallel(1, 1)
        # sync=True to make sure other ranks wait for rank 0 to finish creating directory.
        with TempNamedDir(tmp_path_dist_ckpt / 'test_sh_obj', sync=True) as ckpt_dir:
            state = {'some': 'dict'}
            state_serialized = io.BytesIO()
            torch.save(state, state_serialized)
            state_dict = {
                'some_key': ShardedObject(
                    'sh_obj_A', state_serialized, (1,), (0,), replica_id=Utils.rank
                )
            }

            save(state_dict, ckpt_dir)
            del state, state_serialized, state_dict
            other_state = {'other': 'dictionary'}
            other_serialized = io.BytesIO()
            torch.save(other_state, other_serialized)
            state_dict = {
                'other_key': ShardedObject(
                    'sh_obj_A', other_serialized, (1,), (0,), replica_id=Utils.rank
                )
            }
            load_state_dict = load(state_dict, ckpt_dir)
            assert 'other_key' in load_state_dict
            load_state_dict['other_key'].seek(0)
            loaded_state = torch.load(load_state_dict['other_key'])

            assert loaded_state == {'some': 'dict'}

        Utils.destroy_model_parallel()

    def test_tensor_shape_mismatch(self, tmp_path_dist_ckpt):
        Utils.initialize_model_parallel(2, 4)

        # Global tensor is just a range(32) repeated twice over the first dimension
        local_tensor = torch.arange(4).unsqueeze(0).expand(2, 4) + Utils.rank * 4

        state_dict = {
            'rigid': ShardedTensor.from_rank_offsets(
                'keyA', local_tensor, (1, Utils.rank, Utils.world_size)
            ),
            'flexible': ShardedTensor.from_rank_offsets(
                'keyB', local_tensor, (1, Utils.rank, Utils.world_size), allow_shape_mismatch=True
            ),
        }
        assert state_dict['rigid'].global_shape == (2, 32)
        assert state_dict['flexible'].global_shape == (2, 32)

        # sync=True to make sure other ranks wait for rank 0 to finish creating directory.
        with TempNamedDir(tmp_path_dist_ckpt / 'test_tensor_shape_mismatch', sync=True) as ckpt_dir:
            save(state_dict, ckpt_dir)

            pp_size = parallel_state.get_pipeline_model_parallel_world_size()
            pp_rank = parallel_state.get_pipeline_model_parallel_rank()
            tp_rank = parallel_state.get_tensor_model_parallel_rank()

            # Smaller coverage than expected (28 < 32)
            state_dict = {
                'rigid': ShardedTensor.from_rank_offsets(
                    'keyA', torch.ones(2, 7), (1, pp_rank, pp_size), replica_id=tp_rank
                )
            }
            with pytest.raises((CheckpointingException, PyTCheckpointingException)):
                load(state_dict, ckpt_dir)

            state_dict = {
                'flexible': ShardedTensor.from_rank_offsets(
                    'keyB',
                    torch.ones(2, 7),
                    (1, pp_rank, pp_size),
                    replica_id=tp_rank,
                    allow_shape_mismatch=True,
                )
            }
            loaded_state_dict = load(state_dict, ckpt_dir)
            assert torch.all(
                loaded_state_dict['flexible']
                == torch.arange(7).unsqueeze(0).expand(2, 7) + pp_rank * 7
            )

            # Larger coverage than expected (36 > 32)
            state_dict = {
                'rigid': ShardedTensor.from_rank_offsets(
                    'keyA', torch.ones(2, 9), (1, pp_rank, pp_size), replica_id=tp_rank
                )
            }
            with pytest.raises((CheckpointingException, PyTCheckpointingException)):
                load(state_dict, ckpt_dir)

            state_dict = {
                'flexible': ShardedTensor.from_rank_offsets(
                    'keyB',
                    torch.ones(2, 9),
                    (1, pp_rank, pp_size),
                    replica_id=tp_rank,
                    allow_shape_mismatch=True,
                )
            }
            loaded_state_dict = load(state_dict, ckpt_dir)
            expected_tensor = torch.arange(9).unsqueeze(0).expand(2, 9) + pp_rank * 9

            if pp_rank >= (32 // 9):
                assert pp_rank == 3, pp_rank
                expected_tensor[:, 5:] = 0  # padding with 0s
            assert torch.all(loaded_state_dict['flexible'] == expected_tensor)

        Utils.destroy_model_parallel()

    @pytest.mark.skipif(
        not is_torch_min_version("2.3.0"),
        reason="remove_sharded_tensors relies on Torch APIs introduced in v2.3.0",
    )
    @pytest.mark.flaky_in_dev
    def test_remove_sharded_tensors(self, tmp_path_dist_ckpt):
        Utils.initialize_model_parallel(2, 4)

        # Global tensor is just a range(32) repeated twice over the first dimension
        global_tensor = torch.arange(4).unsqueeze(0).expand(2, 4)
        state_dict = {
            'sd_keyA': ShardedTensor.from_rank_offsets(
                'keyA', torch.ones(2, 4), (0, Utils.rank, Utils.world_size)
            ),
            'sd_prefix_key_to_remove': ShardedTensor.from_rank_offsets(
                'prefix_key_to_remove', torch.ones(3, 5, 7), (2, Utils.rank, Utils.world_size)
            ),
        }

        prefix_name = "prefix"  ## we will drop all tensors whose keys begin with "prefix"

        # sync=True to make sure other ranks wait for rank 0 to finish creating directory.
        with TempNamedDir(
            tmp_path_dist_ckpt / 'test_remove_sharded_tensor_prefix', sync=True
        ) as ckpt_dir:
            save_strategy = TorchDistSaveShardedStrategy(
                "torch_dist", 1, separation_hint=prefix_name
            )
            save(state_dict, ckpt_dir, save_strategy)

            files = os.listdir(ckpt_dir)
            prefix_files = [f for f in files if f.startswith(prefix_name)]
            assert len(prefix_files) == torch.distributed.get_world_size()

            fs_reader = FileSystemReader(ckpt_dir)
            original_metadata = fs_reader.read_metadata()
            assert set(original_metadata.state_dict_metadata.keys()) == {
                'keyA',
                'prefix_key_to_remove',
            }

            if torch.distributed.get_rank() == 0:
                remove_sharded_tensors(ckpt_dir, key_prefix=prefix_name)
            torch.distributed.barrier()

            files = os.listdir(ckpt_dir)
            prefix_files = [f for f in files if f.startswith(prefix_name)]
            assert len(prefix_files) == 0

            new_metadata = fs_reader.read_metadata()
            assert set(new_metadata.state_dict_metadata.keys()) == {'keyA'}

        Utils.destroy_model_parallel()

    def test_empty_load(self, tmp_path_dist_ckpt):
        Utils.initialize_model_parallel(2, 4)

        if Utils.rank == 0:
            state_dict = {'common': 'common-value'}
        elif Utils.rank == 1:
            state_dict = {'a': 3}  # this is not saved at all (common saved by rank 0 only)
        elif Utils.rank == 2:
            state_dict = {'b': 3}  # this is not saved at all (common saved by rank 0 only)
        else:
            state_dict = {
                'a': ShardedTensor.from_rank_offsets(
                    'x', torch.ones((2,)) * Utils.rank, replica_id=Utils.rank - 3
                )
            }

        with TempNamedDir(tmp_path_dist_ckpt / 'test_empty_load', sync=True) as ckpt_dir:
            save(state_dict, ckpt_dir)
            torch.distributed.barrier()
            loaded_state_dict = load(state_dict, ckpt_dir)
            assert loaded_state_dict['common'] == 'common-value'

            if Utils.rank <= 2:
                assert loaded_state_dict.keys() == {'common'}
            else:
                assert loaded_state_dict.keys() == {'common', 'a'}
                loaded_state_dict['a'].cpu().numpy().tolist() == [
                    3,
                    3,
                ]  # rank 3 held the main replica so did the saving

        Utils.destroy_model_parallel()

    @pytest.mark.parametrize(
        'content_metadata', [{'a': 3}, {'nested': {'a': 3}, 'flat': (5, {6: None})}, {}]
    )
    def test_content_metadata_load_from_checkpoint(self, tmp_path_dist_ckpt, content_metadata):
        Utils.initialize_model_parallel(1, 1)
        state_dict = {'common': (3, 5, 7)}

        with TempNamedDir(
            tmp_path_dist_ckpt / 'test_content_metadata_load_from_checkpoint', sync=True
        ) as ckpt_dir:
            save(state_dict, ckpt_dir, content_metadata=content_metadata)
            torch.distributed.barrier()
            loaded_metadata = load_content_metadata(ckpt_dir)

        assert loaded_metadata == content_metadata

    @pytest.mark.parametrize(
        'content_metadata', [{'a': 3}, {'nested': {'a': 3}, 'flat': (5, {6: None})}, {}]
    )
    def test_content_metadata_load_from_state_dict(self, tmp_path_dist_ckpt, content_metadata):
        Utils.initialize_model_parallel(1, 1)
        state_dict = {'common': (3, 5, 7)}

        with TempNamedDir(
            tmp_path_dist_ckpt / 'test_content_metadata_load_from_state_dict', sync=True
        ) as ckpt_dir:
            save(state_dict, ckpt_dir, content_metadata=content_metadata)
            torch.distributed.barrier()
            loaded_state_dict = load(state_dict, ckpt_dir)
            loaded_metadata = load_content_metadata(preloaded_state_dict=loaded_state_dict)

        assert loaded_metadata == content_metadata


class TestNonStrictLoad:
    def setup_method(self, method):
        Utils.initialize_model_parallel(2, 4)  # doesn't matter for this test

    def teardown_method(self, method):
        Utils.destroy_model_parallel()

    def _get_base_state_dict(self):
        return {
            'TenA': ShardedTensor.from_rank_offsets('TenA', torch.arange(2), replica_id=Utils.rank),
            'TenB': ShardedTensor.from_rank_offsets(
                'TenB', torch.arange(3), (0, Utils.rank, Utils.world_size), replica_id=0
            ),
            'TenC': ShardedTensor.from_rank_offsets(
                'TenC', torch.arange(3), replica_id=Utils.world_size - Utils.rank - 1
            ),
            'ObjA': ShardedObject('ObjA', list(range(10)), (1,), (0,), replica_id=Utils.rank),
            'ObjB': ShardedObject(
                'ObjB', {Utils.rank + 7}, (1, Utils.world_size), (0, Utils.rank), replica_id=0
            ),
        }

    @pytest.mark.parametrize('save_format', ['zarr', 'torch_dist'])
    @pytest.mark.parametrize('validate_integrity', [True, False])
    def test_unexpected_keys_handling_during_validation(
        self, caplog, tmp_path_dist_ckpt, validate_integrity, save_format
    ):
        sharded_state_dict = self._get_base_state_dict()
        with TempNamedDir(
            tmp_path_dist_ckpt / 'test_unexpected_keys_raises_error_during_validation'
        ) as ckpt_dir:
            save_strategy = get_default_strategy(StrategyAction.SAVE_SHARDED, save_format, 1)
            save(sharded_state_dict, ckpt_dir, save_strategy)

            def load_with_flag(strict):
                sharded_state_dict = self._get_base_state_dict()
                sharded_state_dict['TenD'] = ShardedTensor.from_rank_offsets(
                    'UnexpectedTenD', torch.arange(3), replica_id=Utils.rank
                )
                sharded_state_dict['ObjD'] = ShardedObject(
                    'UnexpectedObjD', None, (1,), (0,), replica_id=Utils.rank
                )
                return load(
                    sharded_state_dict,
                    ckpt_dir,
                    validate_access_integrity=validate_integrity,
                    strict=strict,
                )

            def test_error(error_msg):
                assert 'Unexpected keys' in error_msg
                assert 'UnexpectedTenD' in error_msg
                assert 'UnexpectedObjD' in error_msg
                assert 'Missing keys' not in error_msg

            # ASSUME_OK_UNEXPECTED results in an exception raised by the underlying strategy
            with pytest.raises(
                PyTCheckpointingException if save_format == 'torch_dist' else CheckpointingException
            ) as exc_info:
                load_with_flag(StrictHandling.ASSUME_OK_UNEXPECTED)
            # Informative exceptions with `RAISE_*` options:
            with pytest.raises(CheckpointingException) as exc_info:
                load_with_flag(StrictHandling.RAISE_UNEXPECTED)
            test_error(str(exc_info.value))
            with pytest.raises(CheckpointingException) as exc_info:
                load_with_flag(StrictHandling.RAISE_ALL)
            test_error(str(exc_info.value))

            # Logged mismatches:
            with caplog.at_level(logging.WARNING):
                loaded_state_dict = load_with_flag(StrictHandling.LOG_UNEXPECTED)
            assert 'TenA' in loaded_state_dict
            test_error(caplog.text)
            with caplog.at_level(logging.WARNING):
                loaded_state_dict = load_with_flag(StrictHandling.LOG_ALL)
            assert 'TenA' in loaded_state_dict
            test_error(caplog.text)

            # Returned mismatches
            loaded_state_dict, missing_keys, unexpected_keys = load_with_flag(
                StrictHandling.RETURN_UNEXPECTED
            )
            assert 'TenA' in loaded_state_dict
            assert unexpected_keys == {'UnexpectedTenD', 'UnexpectedObjD'}
            assert missing_keys == set()
            loaded_state_dict, missing_keys, unexpected_keys = load_with_flag(
                StrictHandling.RETURN_ALL
            )
            assert 'TenA' in loaded_state_dict
            assert unexpected_keys == {'UnexpectedTenD', 'UnexpectedObjD'}
            assert missing_keys == set()

            # Ignore mismatch
            loaded_state_dict = load_with_flag(StrictHandling.IGNORE_ALL)
            assert 'TenA' in loaded_state_dict

    @pytest.mark.parametrize('save_format', ['zarr', 'torch_dist'])
    @pytest.mark.parametrize('validate_integrity', [True, False])
    def test_missing_keys_raises_error_during_validation(
        self, caplog, tmp_path_dist_ckpt, validate_integrity, save_format
    ):
        sharded_state_dict = self._get_base_state_dict()
        with TempNamedDir(
            tmp_path_dist_ckpt / 'test_missing_keys_raises_error_during_validation'
        ) as ckpt_dir:
            save_strategy = get_default_strategy(StrategyAction.SAVE_SHARDED, save_format, 1)
            save(sharded_state_dict, ckpt_dir, save_strategy)

            def load_with_flag(strict):
                sharded_state_dict = self._get_base_state_dict()
                del sharded_state_dict['TenA']
                del sharded_state_dict['ObjB']
                return load(
                    sharded_state_dict,
                    ckpt_dir,
                    validate_access_integrity=validate_integrity,
                    strict=strict,
                )

            def test_error(error_msg):
                assert 'Unexpected keys' not in error_msg
                assert 'TenA' in error_msg
                assert 'ObjB' in error_msg
                assert 'Missing keys' in error_msg

            # no mismatch for `*_UNEXPECTED` flag
            loaded_state_dict = load_with_flag(StrictHandling.ASSUME_OK_UNEXPECTED)
            assert 'TenB' in loaded_state_dict

            loaded_state_dict = load_with_flag(StrictHandling.RAISE_UNEXPECTED)
            assert 'TenB' in loaded_state_dict

            with caplog.at_level(logging.WARNING):
                loaded_state_dict = load_with_flag(StrictHandling.LOG_UNEXPECTED)
            assert (
                caplog.text == ''
                or '`zarr` distributed checkpoint backend is deprecated' in caplog.text
            )
            assert 'TenB' in loaded_state_dict

            loaded_state_dict, missing_keys, unexpected_keys = load_with_flag(
                StrictHandling.RETURN_UNEXPECTED
            )
            assert 'TenB' in loaded_state_dict
            assert missing_keys == set()
            assert unexpected_keys == set()

            loaded_state_dict = load_with_flag(StrictHandling.IGNORE_ALL)
            assert 'TenB' in loaded_state_dict

            # Informative exceptions with `RAISE_ALL` option:
            with pytest.raises(CheckpointingException) as exc_info:
                load_with_flag(StrictHandling.RAISE_ALL)
            test_error(str(exc_info.value))

            # Logged mismatches:
            with caplog.at_level(logging.WARNING):
                loaded_state_dict = load_with_flag(StrictHandling.LOG_ALL)
            assert 'TenB' in loaded_state_dict
            test_error(caplog.text)

            # Returned mismatches
            loaded_state_dict, missing_keys, unexpected_keys = load_with_flag(
                StrictHandling.RETURN_ALL
            )
            assert 'TenB' in loaded_state_dict
            assert unexpected_keys == set()
            assert missing_keys == {'TenA', 'ObjB'}

    @pytest.mark.parametrize('save_format', ['zarr', 'torch_dist'])
    @pytest.mark.parametrize('validate_integrity', [True, False])
    def test_exact_load_handling(self, caplog, tmp_path_dist_ckpt, validate_integrity, save_format):
        sharded_state_dict = self._get_base_state_dict()
        with TempNamedDir(tmp_path_dist_ckpt / 'test_exact_load_handling') as ckpt_dir:
            save_strategy = get_default_strategy(StrategyAction.SAVE_SHARDED, save_format, 1)
            save(sharded_state_dict, ckpt_dir, save_strategy)

            def load_with_flag(strict):
                sharded_state_dict = self._get_base_state_dict()
                return load(
                    sharded_state_dict,
                    ckpt_dir,
                    validate_access_integrity=validate_integrity,
                    strict=strict,
                )

            for strict in (
                StrictHandling.ASSUME_OK_UNEXPECTED,
                StrictHandling.LOG_UNEXPECTED,
                StrictHandling.LOG_ALL,
                StrictHandling.RAISE_UNEXPECTED,
                StrictHandling.RAISE_ALL,
                StrictHandling.IGNORE_ALL,
            ):
                with caplog.at_level(logging.WARNING):
                    loaded_state_dict = load_with_flag(strict)
                assert (
                    caplog.text == ''
                    or '`zarr` distributed checkpoint backend is deprecated' in caplog.text
                )
                assert 'TenB' in loaded_state_dict
                assert 'ObjB' in loaded_state_dict

            for strict in (StrictHandling.RETURN_UNEXPECTED, StrictHandling.RETURN_ALL):
                with caplog.at_level(logging.WARNING):
                    loaded_state_dict, missing_keys, unexpected_keys = load_with_flag(strict)
                assert (
                    caplog.text == ''
                    or '`zarr` distributed checkpoint backend is deprecated' in caplog.text
                )
                assert 'TenB' in loaded_state_dict
                assert 'ObjB' in loaded_state_dict
                assert missing_keys == set()
                assert unexpected_keys == set()

    @pytest.mark.parametrize('save_format', ['zarr', 'torch_dist'])
    def test_sharded_metadata(self, tmp_path_dist_ckpt, save_format):

        sharded_state_dict = self._get_base_state_dict()
        with TempNamedDir(tmp_path_dist_ckpt / 'test_exact_load_handling') as ckpt_dir:
            save_strategy = get_default_strategy(StrategyAction.SAVE_SHARDED, save_format, 1)
            save(sharded_state_dict, ckpt_dir, save_strategy)
            torch.distributed.barrier()
            sharded_metadata = load_sharded_metadata(ckpt_dir)
            assert set(sh_base.key for sh_base in sharded_metadata.values()) == {
                'TenA',
                'TenB',
                'TenC',
                'ObjA',
                'ObjB',
            }
            assert set(sharded_metadata.keys()) == {
                'TenA',
                'TenB',
                'TenC',
                'ObjA/shard_0_1',
                *(f'ObjB/shard_0.{i}_1.8' for i in range(8)),
            }

            loaded_state_dict = load(sharded_metadata, ckpt_dir, validate_access_integrity=False)

            assert loaded_state_dict['ObjA/shard_0_1'] == list(range(10))
            for shard_idx in range(8):
                assert loaded_state_dict[f'ObjB/shard_0.{shard_idx}_1.8'] == {shard_idx + 7}
            assert torch.all(loaded_state_dict['TenA'] == torch.arange(2))
            assert torch.all(loaded_state_dict['TenB'] == torch.arange(3).repeat(8))
            assert torch.all(loaded_state_dict['TenC'] == torch.arange(3))
