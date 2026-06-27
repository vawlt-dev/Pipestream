# =============================================================================
# test_run_concurrent.py — the generous chain pool primitive
# =============================================================================
# Fast, no LLM/network — exercises core.run_concurrent() in isolation.
# =============================================================================

import contextvars
import time

import pytest

from core import run_concurrent


def test_result_order_matches_input_order_regardless_of_completion_order():
    # Task 0 sleeps longest, task 2 finishes first -- if order were
    # determined by completion rather than input position, this would fail.
    delays = [0.15, 0.05, 0.0]

    def make_task(i):
        def task():
            time.sleep(delays[i])
            return i
        return task

    tasks = [make_task(i) for i in range(len(delays))]
    assert run_concurrent(tasks) == [0, 1, 2]


def test_single_task_skips_pool_but_still_runs():
    assert run_concurrent([lambda: 42]) == [42]


def test_empty_list_returns_empty():
    assert run_concurrent([]) == []


def test_exception_in_one_task_propagates():
    def boom():
        raise ValueError("deliberate failure")

    with pytest.raises(ValueError, match="deliberate failure"):
        run_concurrent([lambda: 1, boom, lambda: 3])


def test_contextvar_propagates_into_worker_threads():
    """
    The concrete regression test for the tracing-under-threads fix: without
    contextvars.copy_context() propagation, each worker thread would see the
    ContextVar's default instead of the value set in the calling thread.
    """
    var = contextvars.ContextVar("test_var", default="default")
    var.set("set-in-caller")

    def read_var():
        return var.get()

    results = run_concurrent([read_var, read_var, read_var])
    assert results == ["set-in-caller"] * 3
