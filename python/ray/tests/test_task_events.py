from collections import defaultdict
from typing import Dict
import pytest
import sys
import threading
import time
from ray._private.state_api_test_utils import verify_failed_task
from ray.exceptions import RuntimeEnvSetupError
from ray.runtime_env import RuntimeEnv
from ray._private import ray_constants

import ray
from ray.experimental.state.common import ListApiOptions, StateResource
from ray._private.test_utils import (
    raw_metrics,
    run_string_as_driver,
    run_string_as_driver_nonblocking,
    wait_for_condition,
)
from ray.experimental.state.api import StateApiClient, list_actors, list_tasks

from ray._private.worker import RayContext

_SYSTEM_CONFIG = {
    "task_events_report_interval_ms": 100,
    "metrics_report_interval_ms": 200,
    "enable_timeline": False,
    "gcs_mark_task_failed_on_job_done_delay_ms": 1000,
}


def aggregate_task_event_metric(info: RayContext) -> Dict:
    """
    Aggregate metrics of task events into:
        {
            "REPORTED": ray_gcs_task_manager_task_events_reported,
            "STORED": ray_gcs_task_manager_task_events_stored,
            "DROPPED_PROFILE_EVENT":
                ray_gcs_task_manager_task_events_dropped PROFILE_EVENT,
            "DROPPED_STATUS_EVENT":
                ray_gcs_task_manager_task_events_dropped STATUS_EVENT,
        }
    """
    res = raw_metrics(info)
    task_events_info = defaultdict(int)
    if "ray_gcs_task_manager_task_events_dropped" in res:
        for sample in res["ray_gcs_task_manager_task_events_dropped"]:
            if "Type" in sample.labels and sample.labels["Type"] != "":
                task_events_info["DROPPED_" + sample.labels["Type"]] += sample.value

    if "ray_gcs_task_manager_task_events_stored" in res:
        for sample in res["ray_gcs_task_manager_task_events_stored"]:
            task_events_info["STORED"] += sample.value

    if "ray_gcs_task_manager_task_events_reported" in res:
        for sample in res["ray_gcs_task_manager_task_events_reported"]:
            task_events_info["REPORTED"] += sample.value

    return task_events_info


def test_status_task_events_metrics(shutdown_only):
    info = ray.init(num_cpus=1, _system_config=_SYSTEM_CONFIG)

    # Start a task
    @ray.remote
    def f():
        pass

    for _ in range(10):
        ray.get(f.remote())

    def verify():
        metric = aggregate_task_event_metric(info)
        assert metric["REPORTED"] >= 10, (
            "At least 10 tasks events should be reported. "
            "Could be more than 10 with multiple flush."
        )
        assert metric["STORED"] == 11, "10 task + 1 driver's events should be stored."

        return True

    wait_for_condition(
        verify,
        timeout=20,
        retry_interval_ms=100,
    )


def test_failed_task_error(shutdown_only):
    ray.init(_system_config=_SYSTEM_CONFIG)

    # Test failed task with TASK_EXECUTION_EXCEPTION
    error_msg_str = "fail is expected to fail"

    @ray.remote
    def fail(x=None):
        if x is not None:
            time.sleep(x)
        raise ValueError(error_msg_str)

    with pytest.raises(ray.exceptions.RayTaskError):
        ray.get(fail.options(name="fail").remote())

    wait_for_condition(
        verify_failed_task,
        name="fail",
        error_type="TASK_EXECUTION_EXCEPTION",
        error_message=error_msg_str,
    )

    # Test canceled tasks with TASK_CANCELLED
    @ray.remote
    def not_running():
        raise ValueError("should not be run")

    with pytest.raises(ray.exceptions.TaskCancelledError):
        t = not_running.options(name="cancel-before-running").remote()
        ray.cancel(t)
        ray.get(t)

    # Cancel task doesn't have additional error message
    wait_for_condition(
        verify_failed_task,
        name="cancel-before-running",
        error_type="TASK_CANCELLED",
        error_message="",
    )

    # Test task failed when worker killed :WORKER_DIED
    @ray.remote(max_retries=0)
    def die():
        exit(27)

    with pytest.raises(ray.exceptions.WorkerCrashedError):
        ray.get(die.options(name="die-worker").remote())

    wait_for_condition(
        verify_failed_task,
        name="die-worker",
        error_type="WORKER_DIED",
        error_message="Worker exits with an exit code 27",
    )

    # Test actor task failed with actor dead: ACTOR_DIED
    @ray.remote
    class Actor:
        def f(self):
            time.sleep(999)

    a = Actor.remote()
    with pytest.raises(ray.exceptions.RayActorError):
        ray.kill(a)
        ray.get(a.f.options(name="actor-killed").remote())

    wait_for_condition(
        verify_failed_task,
        name="actor-killed",
        error_type="ACTOR_DIED",
        error_message="The actor is dead because it was killed by `ray.kill`",
    )


def test_failed_task_failed_due_to_node_failure(ray_start_cluster):
    cluster = ray_start_cluster
    cluster.add_node(num_cpus=1)
    ray.init(address=cluster.address)
    node = cluster.add_node(num_cpus=2)

    driver_script = """
import ray
ray.init("auto")

@ray.remote(num_cpus=2, max_retries=0)
def sleep():
    import time
    time.sleep(999)

x = sleep.options(name="node-killed").remote()
ray.get(x)
    """

    run_string_as_driver_nonblocking(driver_script)

    def driver_running():
        t = list_tasks(filters=[("name", "=", "node-killed")])
        return len(t) > 0

    wait_for_condition(driver_running)

    # Kill the node
    cluster.remove_node(node)

    wait_for_condition(
        verify_failed_task,
        name="node-killed",
        error_type="NODE_DIED",
        error_message="Task failed due to the node dying",
    )


def test_failed_task_unschedulable(shutdown_only):
    ray.init(num_cpus=1, _system_config=_SYSTEM_CONFIG)

    node_id = ray.get_runtime_context().get_node_id()
    policy = ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
        node_id=node_id,
        soft=False,
    )

    @ray.remote
    def task():
        pass

    task.options(
        scheduling_strategy=policy,
        name="task-unschedulable",
        num_cpus=2,
    ).remote()

    wait_for_condition(
        verify_failed_task,
        name="task-unschedulable",
        error_type="TASK_UNSCHEDULABLE_ERROR",
        error_message=(
            "The node specified via NodeAffinitySchedulingStrategy"
            " doesn't exist any more or is infeasible"
        ),
    )


# TODO(rickyx): Make this work.
# def test_failed_task_removed_placement_group(shutdown_only, monkeypatch):
#     ray.init(num_cpus=2, _system_config=_SYSTEM_CONFIG)
#     from ray.util.placement_group import placement_group, remove_placement_group
#     from ray.util.scheduling_strategies import PlacementGroupSchedulingStrategy
#
#     pg = placement_group([{"CPU": 2}])
#     ray.get(pg.ready())
#
#     @ray.remote(num_cpus=2)
#     def sleep():
#         time.sleep(999)
#
#     with monkeypatch.context() as m:
#         m.setenv(
#             "RAY_testing_asio_delay_us",
#             "NodeManagerService.grpc_server.RequestWorkerLease=3000000:3000000",
#         )
#
#         sleep.options(
#             scheduling_strategy=PlacementGroupSchedulingStrategy(placement_group=pg),
#             name="task-pg-removed",
#             max_retries=0,
#         ).remote()
#
#     remove_placement_group(pg)
#
#     wait_for_condition(
#         verify_failed_task,
#         name="task-pg-removed",
#         error_type="TASK_PLACEMENT_GROUP_REMOVED",
#     )


def test_failed_task_runtime_env_setup(shutdown_only):
    @ray.remote
    def f():
        pass

    bad_env = RuntimeEnv(conda={"dependencies": ["_this_does_not_exist"]})
    with pytest.raises(
        RuntimeEnvSetupError,
    ):
        ray.get(f.options(runtime_env=bad_env, name="task-runtime-env-failed").remote())

    wait_for_condition(
        verify_failed_task,
        name="task-runtime-env-failed",
        error_type="RUNTIME_ENV_SETUP_FAILED",
        error_message="ResolvePackageNotFound",
    )


def test_fault_tolerance_parent_failed(shutdown_only):
    ray.init(num_cpus=4, _system_config=_SYSTEM_CONFIG)

    # Each parent task spins off 2 child task, where each child spins off
    # 1 grand_child task.
    NUM_CHILD = 2

    @ray.remote
    def grand_child():
        time.sleep(999)

    @ray.remote
    def child():
        ray.get(grand_child.remote())

    @ray.remote
    def parent():
        for _ in range(NUM_CHILD):
            child.remote()
        # Sleep for a bit and kill itself.
        time.sleep(3)
        raise ValueError("parent task is expected to fail")

    parent.remote()

    def verify():
        tasks = list_tasks()
        assert len(tasks) == 5, (
            "Incorrect number of tasks are reported. "
            "Expected length: 1 parent + 2 child + 2 grand_child tasks"
        )
        print(tasks)
        for task in tasks:
            assert task["state"] == "FAILED"

        return True

    wait_for_condition(
        verify,
        timeout=10,
        retry_interval_ms=500,
    )


def test_parent_task_id_threaded_task(shutdown_only):
    ray.init(_system_config=_SYSTEM_CONFIG)

    # Task starts a thread
    @ray.remote
    def main_task():
        def thd_task():
            @ray.remote
            def thd_task():
                pass

            ray.get(thd_task.remote())

        thd = threading.Thread(target=thd_task)
        thd.start()
        thd.join()

    ray.get(main_task.remote())

    def verify():
        tasks = list_tasks()
        assert len(tasks) == 2
        expect_parent_task_id = None
        actual_parent_task_id = None
        for task in tasks:
            if task["name"] == "main_task":
                expect_parent_task_id = task["task_id"]
            elif task["name"] == "thd_task":
                actual_parent_task_id = task["parent_task_id"]
        assert actual_parent_task_id is not None
        assert expect_parent_task_id == actual_parent_task_id

        return True

    wait_for_condition(verify)


def test_parent_task_id_non_concurrent_actor(shutdown_only):
    ray.init(_system_config=_SYSTEM_CONFIG)

    def run_task_in_thread():
        def thd_task():
            @ray.remote
            def thd_task():
                pass

            ray.get(thd_task.remote())

        thd = threading.Thread(target=thd_task)
        thd.start()
        thd.join()

    @ray.remote
    class Actor:
        def main_task(self):
            run_task_in_thread()

    a = Actor.remote()
    ray.get(a.main_task.remote())

    def verify():
        tasks = list_tasks()
        expect_parent_task_id = None
        actual_parent_task_id = None
        for task in tasks:
            if "main_task" in task["name"]:
                expect_parent_task_id = task["task_id"]
            elif "thd_task" in task["name"]:
                actual_parent_task_id = task["parent_task_id"]
        print(tasks)
        assert actual_parent_task_id is not None
        assert expect_parent_task_id == actual_parent_task_id

        return True

    wait_for_condition(verify)


@pytest.mark.parametrize("actor_concurrency", [3, 10])
def test_parent_task_id_concurrent_actor(shutdown_only, actor_concurrency):
    # Test tasks runs in user started thread from actors have a parent_task_id
    # as the actor's creation task.
    ray.init(_system_config=_SYSTEM_CONFIG)

    def run_task_in_thread(name, i):
        def thd_task():
            @ray.remote
            def thd_task():
                pass

            ray.get(thd_task.options(name=f"{name}_{i}").remote())

        thd = threading.Thread(target=thd_task)
        thd.start()
        thd.join()

    @ray.remote
    class AsyncActor:
        async def main_task(self, i):
            run_task_in_thread("async_thd_task", i)

    @ray.remote
    class ThreadedActor:
        def main_task(self, i):
            run_task_in_thread("threaded_thd_task", i)

    def verify(actor_method_name, actor_class_name):
        tasks = list_tasks()
        print(tasks)
        expect_parent_task_id = None
        actual_parent_task_id = None
        for task in tasks:
            if f"{actor_class_name}.__init__" in task["name"]:
                expect_parent_task_id = task["task_id"]

        assert expect_parent_task_id is not None
        for task in tasks:
            if f"{actor_method_name}" in task["name"]:
                actual_parent_task_id = task["parent_task_id"]
                assert expect_parent_task_id == actual_parent_task_id, task

        return True

    async_actor = AsyncActor.options(max_concurrency=actor_concurrency).remote()
    ray.get([async_actor.main_task.remote(i) for i in range(20)])
    wait_for_condition(
        verify, actor_class_name="AsyncActor", actor_method_name="async_thd_task"
    )

    thd_actor = ThreadedActor.options(max_concurrency=actor_concurrency).remote()
    ray.get([thd_actor.main_task.remote(i) for i in range(20)])
    wait_for_condition(
        verify, actor_class_name="ThreadedActor", actor_method_name="threaded_thd_task"
    )


def test_parent_task_id_tune_e2e(shutdown_only):
    # Test a tune e2e workload should not have any task with parent_task_id that's
    # not found.
    ray.init(_system_config=_SYSTEM_CONFIG)
    job_id = ray.get_runtime_context().get_job_id()
    script = """
import numpy as np
import ray
from ray import tune
import time

ray.init("auto")

@ray.remote
def train_step_1():
    time.sleep(0.5)
    return 1

def train_function(config):
    for i in range(5):
        loss = config["mean"] * np.random.randn() + ray.get(
            train_step_1.remote())
        tune.report(loss=loss, nodes=ray.nodes())


def tune_function():
    analysis = tune.run(
        train_function,
        metric="loss",
        mode="min",
        config={
            "mean": tune.grid_search([1, 2, 3, 4, 5]),
        },
        resources_per_trial=tune.PlacementGroupFactory([{
            'CPU': 1.0
        }] + [{
            'CPU': 1.0
        }] * 3),
    )
    return analysis.best_config


tune_function()
    """

    run_string_as_driver(script)
    client = StateApiClient()

    def list_tasks():
        return client.list(
            StateResource.TASKS,
            # Filter out this driver
            options=ListApiOptions(
                exclude_driver=False, filters=[("job_id", "!=", job_id)], limit=1000
            ),
            raise_on_missing_output=True,
        )

    def verify():
        tasks = list_tasks()

        task_id_map = {task["task_id"]: task for task in tasks}
        for task in tasks:
            if task["type"] == "DRIVER_TASK":
                continue
            assert task_id_map.get(task["parent_task_id"], None) is not None, task

        return True

    wait_for_condition(verify)


@ray.remote
class ActorOk:
    def ready(self):
        pass


@ray.remote
class ActorInitFailed:
    def __init__(self):
        raise ValueError("Actor init is expected to fail")

    def ready(self):
        pass


def test_actor_creation_task_ok(shutdown_only):
    ray.init(_system_config=_SYSTEM_CONFIG)
    a = ActorOk.remote()
    ray.get(a.ready.remote())

    def verify():
        tasks = list_tasks(filters=[("name", "=", "ActorOk.__init__")])
        actors = list_actors(filters=[("class_name", "=", "ActorOk")])

        assert len(tasks) == 1
        assert len(actors) == 1
        actor = actors[0]
        task = tasks[0]
        assert task["state"] == "FINISHED"
        assert task["actor_id"] == actor["actor_id"]
        return True

    wait_for_condition(verify)


def test_actor_creation_task_failed(shutdown_only):
    ray.init(_system_config=_SYSTEM_CONFIG)
    a = ActorInitFailed.remote()

    with pytest.raises(ray.exceptions.RayActorError):
        ray.get(a.ready.remote())

    def verify():
        tasks = list_tasks(filters=[("name", "=", "ActorInitFailed.__init__")])
        actors = list_actors(filters=[("class_name", "=", "ActorInitFailed")])

        assert len(tasks) == 1
        assert len(actors) == 1
        actor = actors[0]
        task = tasks[0]
        assert task["state"] == "FAILED"
        assert task["actor_id"] == actor["actor_id"]
        assert actor["state"] == "DEAD"
        return True

    wait_for_condition(verify)


def test_actor_creation_nested_failure_from_actor(shutdown_only):
    ray.init(_system_config=_SYSTEM_CONFIG)

    @ray.remote
    class NestedActor:
        def ready(self):
            a = ActorInitFailed.remote()
            ray.get(a.ready.remote())

    a = NestedActor.remote()

    with pytest.raises(ray.exceptions.RayTaskError):
        ray.get(a.ready.remote())

    def verify():
        creation_tasks = list_tasks(filters=[("type", "=", "ACTOR_CREATION_TASK")])
        actors = list_actors()

        assert len(creation_tasks) == 2
        assert len(actors) == 2
        for actor in actors:
            if "NestedActor" in actor["class_name"]:
                assert actor["state"] == "ALIVE"
            else:
                assert "ActorInitFailed" in actor["class_name"]
                assert actor["state"] == "DEAD"

        for task in creation_tasks:
            if "ActorInitFailed" in task["name"]:
                assert task["state"] == "FAILED"
            else:
                assert task["name"] == "NestedActor.__init__"
                assert task["state"] == "FINISHED"
        return True

    wait_for_condition(verify)


def test_actor_creation_canceled(shutdown_only):
    ray.init(num_cpus=2, _system_config=_SYSTEM_CONFIG)

    # An actor not gonna be scheduled
    a = ActorOk.options(num_cpus=10).remote()

    # Kill it before it could be scheduled.
    ray.kill(a)

    def verify():
        tasks = list_tasks(filters=[("name", "=", "ActorOk.__init__")])
        actors = list_actors(filters=[("class_name", "=", "ActorOk")])

        assert len(tasks) == 1
        assert len(actors) == 1
        actor = actors[0]
        task = tasks[0]
        assert task["state"] == "FAILED"
        assert task["actor_id"] == actor["actor_id"]
        assert actor["state"] == "DEAD"
        return True

    wait_for_condition(verify)


def test_handle_driver_tasks(shutdown_only):
    ray.init(_system_config=_SYSTEM_CONFIG)

    job_id = ray.get_runtime_context().get_job_id()
    script = """
import ray
import time
ray.init("auto")

@ray.remote
def f():
    time.sleep(3)


ray.get(f.remote())
"""
    run_string_as_driver_nonblocking(script)

    client = StateApiClient()

    def list_tasks(exclude_driver):
        return client.list(
            StateResource.TASKS,
            # Filter out this driver
            options=ListApiOptions(
                exclude_driver=exclude_driver, filters=[("job_id", "!=", job_id)]
            ),
            raise_on_missing_output=True,
        )

    # Check driver running
    def verify():
        tasks_with_driver = list_tasks(exclude_driver=False)
        assert len(tasks_with_driver) == 2, tasks_with_driver
        task_types = {task["type"] for task in tasks_with_driver}
        assert task_types == {"NORMAL_TASK", "DRIVER_TASK"}

        for task in tasks_with_driver:
            if task["type"] == "DRIVER_TASK":
                assert task["state"] == "RUNNING", task

        return True

    wait_for_condition(verify, timeout=15, retry_interval_ms=1000)

    # Check driver finishes
    def verify():
        tasks_with_driver = list_tasks(exclude_driver=False)
        assert len(tasks_with_driver) == 2, tasks_with_driver
        for task in tasks_with_driver:
            if task["type"] == "DRIVER_TASK":
                assert task["state"] == "FINISHED", task

        tasks_no_driver = list_tasks(exclude_driver=True)
        assert len(tasks_no_driver) == 1, tasks_no_driver
        return True

    wait_for_condition(verify)


def test_fault_tolerance_job_failed(shutdown_only):
    ray.init(num_cpus=8, _system_config=_SYSTEM_CONFIG)
    script = """
import ray
import time

ray.init("auto")
NUM_CHILD = 2

@ray.remote
def grandchild():
    time.sleep(999)

@ray.remote
def child():
    ray.get(grandchild.remote())

@ray.remote
def finished_child():
    ray.put(1)
    return

@ray.remote
def parent():
    children = [child.remote() for _ in range(NUM_CHILD)]
    finished_children = ray.get([finished_child.remote() for _ in range(NUM_CHILD)])
    ray.get(children)

ray.get(parent.remote())

"""
    proc = run_string_as_driver_nonblocking(script)

    def verify():
        tasks = list_tasks()
        print(tasks)
        assert len(tasks) == 7, (
            "Incorrect number of tasks are reported. "
            "Expected length: 1 parent + 2 finished child +  2 failed child + "
            "2 failed grandchild tasks"
        )
        return True

    wait_for_condition(
        verify,
        timeout=10,
        retry_interval_ms=500,
    )
    time_sleep_s = 3
    # Sleep for a while to allow driver job runs async.
    time.sleep(time_sleep_s)

    proc.kill()

    def verify():
        tasks = list_tasks(detail=True)
        assert len(tasks) == 7, (
            "Incorrect number of tasks are reported. "
            "Expected length: 1 parent + 2 finished child +  2 failed child + "
            "2 failed grandchild tasks"
        )
        for task in tasks:
            if "finished" in task["func_or_class_name"]:
                assert (
                    task["state"] == "FINISHED"
                ), f"task {task['func_or_class_name']} has wrong state"
            else:
                assert (
                    task["state"] == "FAILED"
                ), f"task {task['func_or_class_name']} has wrong state"

                duration_ms = task["end_time_ms"] - task["start_time_ms"]
                assert (
                    # It takes time for the job to run
                    duration_ms > time_sleep_s / 2 * 1000
                    and duration_ms < 2 * time_sleep_s * 1000
                )

        return True

    wait_for_condition(
        verify,
        timeout=10,
        retry_interval_ms=500,
    )


@ray.remote
def task_finish_child():
    pass


@ray.remote
def task_sleep_child():
    time.sleep(999)


@ray.remote
class ChildActor:
    def children(self):
        ray.get(task_finish_child.remote())
        ray.get(task_sleep_child.remote())


@ray.remote
class Actor:
    def fail_parent(self):
        ray.get(task_finish_child.remote())
        task_sleep_child.remote()
        raise ValueError("expected to fail.")

    def child_actor(self):
        a = ChildActor.remote()
        try:
            ray.get(a.children.remote(), timeout=2)
        except ray.exceptions.GetTimeoutError:
            pass
        raise ValueError("expected to fail.")


def test_fault_tolerance_actor_tasks_failed(shutdown_only):
    ray.init(_system_config=_SYSTEM_CONFIG)
    # Test actor tasks
    with pytest.raises(ray.exceptions.RayTaskError):
        a = Actor.remote()
        ray.get(a.fail_parent.remote())

    def verify():
        tasks = list_tasks()
        assert (
            len(tasks) == 4
        ), "1 creation task + 1 actor tasks + 2 normal tasks run by the actor tasks"
        for task in tasks:
            if "finish" in task["name"] or "__init__" in task["name"]:
                assert task["state"] == "FINISHED", task
            else:
                assert task["state"] == "FAILED", task

        return True

    wait_for_condition(
        verify,
        timeout=10,
        retry_interval_ms=500,
    )


def test_fault_tolerance_nested_actors_failed(shutdown_only):
    ray.init(_system_config=_SYSTEM_CONFIG)

    # Test nested actor tasks
    with pytest.raises(ray.exceptions.RayTaskError):
        a = Actor.remote()
        ray.get(a.child_actor.remote())

    def verify():
        tasks = list_tasks()
        assert len(tasks) == 6, (
            "2 creation task + 1 parent actor task + 1 child actor task "
            " + 2 normal tasks run by child actor"
        )
        for task in tasks:
            if "finish" in task["name"] or "__init__" in task["name"]:
                assert task["state"] == "FINISHED", task
            else:
                assert task["state"] == "FAILED", task

        return True

    wait_for_condition(
        verify,
        timeout=10,
        retry_interval_ms=500,
    )


@pytest.mark.parametrize("death_list", [["A"], ["Abb", "C"], ["Abb", "Ca", "A"]])
def test_fault_tolerance_advanced_tree(shutdown_only, death_list):
    import asyncio

    # Some constants
    NORMAL_TASK = 0
    ACTOR_TASK = 1

    # Root should always be finish
    execution_graph = {
        "root": [
            (NORMAL_TASK, "A"),
            (ACTOR_TASK, "B"),
            (NORMAL_TASK, "C"),
            (ACTOR_TASK, "D"),
        ],
        "A": [(ACTOR_TASK, "Aa"), (NORMAL_TASK, "Ab")],
        "C": [(ACTOR_TASK, "Ca"), (NORMAL_TASK, "Cb")],
        "D": [
            (NORMAL_TASK, "Da"),
            (NORMAL_TASK, "Db"),
            (ACTOR_TASK, "Dc"),
            (ACTOR_TASK, "Dd"),
        ],
        "Aa": [],
        "Ab": [(ACTOR_TASK, "Aba"), (NORMAL_TASK, "Abb"), (NORMAL_TASK, "Abc")],
        "Ca": [(ACTOR_TASK, "Caa"), (NORMAL_TASK, "Cab")],
        "Abb": [(NORMAL_TASK, "Abba")],
        "Abc": [],
        "Abba": [(NORMAL_TASK, "Abbaa"), (ACTOR_TASK, "Abbab")],
        "Abbaa": [(NORMAL_TASK, "Abbaaa"), (ACTOR_TASK, "Abbaab")],
    }

    ray.init(_system_config=_SYSTEM_CONFIG)

    @ray.remote
    class Killer:
        def __init__(self, death_list, wait_time):
            self.idx_ = 0
            self.death_list_ = death_list
            self.wait_time_ = wait_time
            self.start_ = time.time()

        async def next_to_kill(self):
            now = time.time()
            if now - self.start_ < self.wait_time_:
                # Sleep until killing starts...
                time.sleep(self.wait_time_ - (now - self.start_))

            # if no more tasks to kill - simply sleep to keep all running tasks blocked.
            while self.idx_ >= len(self.death_list_):
                await asyncio.sleep(999)

            to_kill = self.death_list_[self.idx_]
            print(f"{to_kill} to be killed")
            return to_kill

        async def advance_next(self):
            self.idx_ += 1

    def run_children(my_name, killer, execution_graph):
        children = execution_graph.get(my_name, [])
        for task_type, child_name in children:
            if task_type == NORMAL_TASK:
                task.options(name=child_name).remote(
                    child_name, killer, execution_graph
                )
            else:
                a = Actor.remote()
                a.actor_task.options(name=child_name).remote(
                    child_name, killer, execution_graph
                )

        # Block until killed
        while True:
            to_fail = ray.get(killer.next_to_kill.remote())
            if to_fail == my_name:
                ray.get(killer.advance_next.remote())
                raise ValueError(f"{my_name} expected to fail")

    @ray.remote
    class Actor:
        def actor_task(self, my_name, killer, execution_graph):
            run_children(my_name, killer, execution_graph)

    @ray.remote
    def task(my_name, killer, execution_graph):
        run_children(my_name, killer, execution_graph)

    killer = Killer.remote(death_list, 5)

    task.options(name="root").remote("root", killer, execution_graph)

    def verify():
        tasks = list_tasks()
        target_tasks = filter(
            lambda task: "__init__" not in task["name"]
            and "Killer" not in task["name"],
            tasks,
        )

        # Calculate tasks that should have failed
        dead_tasks = set()

        def add_death_tasks_recur(task, execution_graph, dead_tasks):
            children = execution_graph.get(task, [])
            dead_tasks.add(task)

            for _, child in children:
                add_death_tasks_recur(child, execution_graph, dead_tasks)

        for task in death_list:
            add_death_tasks_recur(task, execution_graph, dead_tasks)

        for task in target_tasks:
            if task["name"] in dead_tasks:
                assert task["state"] == "FAILED", task["name"]
            else:
                assert task["state"] == "RUNNING", task["name"]

        return True

    wait_for_condition(
        verify,
        timeout=15,
        retry_interval_ms=500,
    )


def check_file(type, task_name, expected_log, expect_no_end=False):
    """Check file of type = 'out'/'err'"""

    def _read_file(filepath, start, end):
        with open(filepath, "r") as f:
            f.seek(start, 0)
            if end is None:
                return f.read()
            return f.read(end - start)

    tasks = list_tasks(filters=[("name", "=", f"{task_name}")], detail=True)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["task_log_info"] is not None
    log_info = task["task_log_info"]

    file = log_info.get(f"std{type}_file", None)
    start_offset = log_info.get(f"std{type}_start", None)
    end_offset = log_info.get(f"std{type}_end", None)
    if not expect_no_end:
        assert end_offset >= start_offset
    else:
        assert end_offset is None
    assert start_offset > 0, "offsets should be > 0 with magical log prefix"
    actual_log = _read_file(file, start_offset, end_offset)
    assert actual_log == expected_log


def test_task_logs_info_basic(shutdown_only):
    """Test tasks (normal tasks/actor tasks) execution logging
    to files have the correct task log info
    """
    ray.init(num_cpus=1)

    def do_print(x):
        out_msg = ""
        err_msg = ""
        for j in range(3):
            out_msg += f"this is log line {j} to stdout from {x}\n"
        print(out_msg, end="", file=sys.stdout)

        for j in range(3):
            err_msg += f"this is log line {j} to stderr from {x}\n"
        print(err_msg, end="", file=sys.stderr)
        return out_msg, err_msg

    @ray.remote
    class Actor:
        def print(self, x):
            return do_print(x)

    @ray.remote
    def task_print(x):
        return do_print(x)

    a = Actor.remote()
    expected_logs = {}
    for j in range(3):
        exp_actor_out, exp_actor_err = ray.get(
            a.print.options(name=f"actor-task-{j}").remote(f"actor-task-{j}")
        )
        expected_logs[f"actor-task-{j}-out"] = exp_actor_out
        expected_logs[f"actor-task-{j}-err"] = exp_actor_err

    for j in range(3):
        exp_task_out, exp_task_err = ray.get(
            task_print.options(name=f"normal-task-{j}").remote(f"normal-task-{j}")
        )
        expected_logs[f"normal-task-{j}-out"] = exp_task_out
        expected_logs[f"normal-task-{j}-err"] = exp_task_err

    def verify():
        # verify logs
        for j in range(3):
            check_file("out", f"normal-task-{j}", expected_logs[f"normal-task-{j}-out"])
            check_file("err", f"normal-task-{j}", expected_logs[f"normal-task-{j}-err"])
            check_file("out", f"actor-task-{j}", expected_logs[f"actor-task-{j}-out"])
            check_file("err", f"actor-task-{j}", expected_logs[f"actor-task-{j}-err"])
            return True

    wait_for_condition(verify)


def test_task_logs_info_disabled(shutdown_only, monkeypatch):
    """Test when redirect disabled, no task log info is available
    due to missing log file
    """
    with monkeypatch.context() as m:
        m.setenv(ray_constants.LOGGING_REDIRECT_STDERR_ENVIRONMENT_VARIABLE, "1")

        ray.init(num_cpus=1)

        @ray.remote
        def f():
            print("hi")

        ray.get(f.remote())

        def verify():
            tasks = list_tasks()

            assert len(tasks) == 1
            assert tasks[0].get("task_log_info") is None
            return True

        wait_for_condition(verify)


def test_task_logs_info_running_task(shutdown_only):
    ray.init(num_cpus=1)

    @ray.remote
    def do_print_sleep(out_msg, err_msg):
        print(out_msg, end="", file=sys.stdout)
        print(err_msg, end="", file=sys.stderr)
        time.sleep(999)

    err_msg = "this is log line to stderr before sleeping\n"
    out_msg = "this is log line to stdout before sleeping\n"
    task_name = "log-running-task"
    do_print_sleep.options(name=task_name).remote(out_msg, err_msg)

    def verify():
        check_file("err", task_name, err_msg, expect_no_end=True)
        check_file("out", task_name, out_msg, expect_no_end=True)
        return True

    wait_for_condition(verify)


if __name__ == "__main__":
    import os

    if os.environ.get("PARALLEL_CI"):
        sys.exit(pytest.main(["-n", "auto", "--boxed", "-vs", __file__]))
    else:
        sys.exit(pytest.main(["-sv", __file__]))
