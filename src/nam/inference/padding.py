from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class BatchPlan:
    fixed_batch_size: int
    original_count: int

    def chunks(self, array: np.ndarray) -> list:
        chunks = []
        offset = 0
        while offset < self.original_count:
            end = min(offset + self.fixed_batch_size, self.original_count)
            chunk = array[offset:end]
            pad_rows = self.fixed_batch_size - chunk.shape[0]
            if pad_rows > 0:
                pad_shape = (pad_rows,) + chunk.shape[1:]
                chunk = np.concatenate([chunk, np.zeros(pad_shape, dtype=chunk.dtype)], axis=0)
            chunks.append((chunk, end - offset))
            offset = end
        return chunks

    def reassemble(self, chunk_outputs: list) -> list:
        num_outputs = len(chunk_outputs[0])
        reassembled = []
        for output_index in range(num_outputs):
            pieces = []
            for outputs, valid_rows in chunk_outputs:
                pieces.append(outputs[output_index][:valid_rows])
            reassembled.append(np.concatenate(pieces, axis=0))
        return reassembled


def plan_for(fixed_batch_size: int, array: np.ndarray) -> BatchPlan:
    return BatchPlan(fixed_batch_size=fixed_batch_size, original_count=array.shape[0])


def run_padded(infer_fn, fixed_batch_size: int, array: np.ndarray) -> list:
    plan = plan_for(fixed_batch_size, array)
    chunk_outputs = []
    for chunk, valid_rows in plan.chunks(array):
        outputs = infer_fn(chunk)
        chunk_outputs.append((outputs, valid_rows))
    return plan.reassemble(chunk_outputs)
