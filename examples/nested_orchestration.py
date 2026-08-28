# nested_orchestration.py
# 
# The proper way for an OrchestrationClient to call another OrchestrationClient.
# As can be observed, we do not roll our own batching, 

from __future__ import annotations

from nam import concurrency

concurrency.initialize()

from nam.concurrency import OrchestrationClient, OrchestrationPipeline, OrchestrationFrame
from nam.concurrency import executor as executors


# --- first pipeline / client -------------------------------------------

first_pipeline = OrchestrationPipeline(executors)


def _cb_first_stage(owner, task, batch):
    for frame in batch:
        frame.result = frame.userdata * 2
    return batch


first_pipeline.add_layer(handler=_cb_first_stage, role="cpu", max_batch=32)
first_pipeline.finish()


class FirstFrame(OrchestrationFrame):
    def __init__(self, index, userdata):
        super().__init__(userdata=userdata)
        self.result = None


class FirstClient(OrchestrationClient):
    def __init__(self):
        super().__init__(first_pipeline, max_window=32)

    def _create_frame(self, index, userdata):
        return FirstFrame(index, userdata)


# --- second pipeline / client -------------------------------------------
#
# The async layer below creates a FirstClient and feeds it, resolving its
# own task only once FirstClient's on_ready/on_discarded fires for the
# item it fed - matching DEVELOPMENT_MANUAL.md section 3's
# asynchronous=True contract (task.ready()/discard() called later, from
# any thread).
second_pipeline = OrchestrationPipeline(executors)


def _cb_second_stage(owner, task, batch):
    client = FirstClient() # create the instance to feed to
    pending = {} # we're using a pending dict so we can map the FirstClient's frames with SecondClient's

    def _on_dispatch(first_frame, outer_frame):
        pending[first_frame] = outer_frame
        return outer_frame.userdata # return what FirstClient understands - it would not understand outer_frame, it's not aware of that class or its semantics (SecondFrame)

    def _on_ready(first_frame, task=task):
        outer_frame = pending.pop(first_frame)
        outer_frame.result = first_frame.result
        task.ready(outer_frame) # the frame gets dispatched to next layer

    def _on_discarded(first_frame, task=task):
        outer_frame = pending.pop(first_frame)
        task.discard(outer_frame) # the frame gets discarded

    client.on_dispatch(_on_dispatch)
    client.on_ready(_on_ready)
    client.on_discarded(_on_discarded)
    client.feed(batch)


# When you're calling another OrchestratorClient from an OrchestratorClient, remember to mark the layer asynchronous=True, 
# because those frames will be arriving (ready/discard) asynchronously so by the time the handler returns, we won't have anything processed to 
# return.
second_pipeline.add_layer(handler=_cb_second_stage, role="cpu", max_batch=32, asynchronous=True)
second_pipeline.finish()


class SecondFrame(OrchestrationFrame):
    def __init__(self, index, userdata):
        super().__init__(userdata=userdata)
        self.result = None


class SecondClient(OrchestrationClient):
    def __init__(self):
        super().__init__(second_pipeline, max_window=32)

    def _create_frame(self, index, userdata):
        return SecondFrame(index, userdata)
