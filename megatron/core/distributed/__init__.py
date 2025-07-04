# Copyright (c) 2024, NVIDIA CORPORATION. All rights reserved.

try:
    from packaging.version import Version
except ImportError:
    pass

from .distributed_data_parallel import DistributedDataParallel
from .distributed_data_parallel_config import DistributedDataParallelConfig
from .finalize_model_grads import finalize_model_grads
from .torch_fully_sharded_data_parallel import TorchFullyShardedDataParallel
from .torch_fully_sharded_data_parallel_config import TorchFullyShardedDataParallelConfig
