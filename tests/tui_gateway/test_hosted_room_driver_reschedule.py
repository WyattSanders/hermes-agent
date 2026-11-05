"""Follow-up scheduling of hosted rooms whose worker runs overlap.

Regression for #149195: a finishing room that owed a follow-up called ``wakeup()``, which
re-marks every room still holding a worker slot. Rooms whose runs overlap (the service
serializes ``prepare_room`` behind its policy lock) then re-marked each other on every exit
and were rescheduled forever, pinning a CPU and the state database.

The test is the supervisor: it runs exactly the ``_worker_loop`` body (clear the wake event,
run one cycle) only while a wake is pending, and every room run blocks in ``prepare_room``
until released. That makes "which runs does a wake schedule" deterministic and bounded, with
no dependence on thread timing.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from pathlib import Path

from gateway import hosted_rooms
from tests.waiting import wait_until
from tui_gateway.hosted_room_driver import HostedRoomBinding, HostedRoomRuntime

ROOM_A = HostedRoomBinding(room_id="room-a", gateway_id="gateway-a", authority_epoch=1)
ROOM_B = HostedRoomBinding(room_id="room-b", gateway_id="gateway-a", authority_epoch=1)
MAX_RUNS = 12  # far above the 2 runs per room a single follow-up needs


class GatedRooms:
    """``prepare_room`` hook: every room run blocks until the test releases it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.runs: list[str] = []
        self.blocked: list[tuple[threading.Thread, threading.Event]] = []

    def __call__(self, binding: HostedRoomBinding) -> None:
        release = threading.Event()
        with self._lock:
            self.runs.append(binding.room_id)
            self.blocked.append((threading.current_thread(), release))
        assert release.wait(10.0), f"{binding.room_id} run was never released"

    def entered(self, thread: threading.Thread) -> bool:
        with self._lock:
            return any(t is thread for t, _ in self.blocked)

    def pop_oldest(self) -> tuple[threading.Thread, threading.Event] | None:
        with self._lock:
            return self.blocked.pop(0) if self.blocked else None


def _runtime(tmp_path: Path, rooms: list[HostedRoomBinding], gates: GatedRooms) -> HostedRoomRuntime:
    db = tmp_path / "state.db"
    for binding in rooms:
        hosted_rooms.create_room(
            db, room_id=binding.room_id, name=binding.room_id,
            members=[{"profile": "ops", "handle": "ops"}],
            authority_gateway_id=binding.gateway_id, now=time.time())
    runtime = HostedRoomRuntime(
        db_path=db, rooms=rooms, rpc=object(), turn_lock=lambda _profile: None,
        prepare_room=gates, poll_interval_seconds=3600.0)
    # The test thread acts as the supervisor so it alone decides when a cycle runs.
    runtime._thread = threading.current_thread()
    return runtime


def _cycle(runtime: HostedRoomRuntime, gates: GatedRooms) -> None:
    """One ``_worker_loop`` iteration; return once every spawned run reached its gate."""
    runtime._wake.clear()
    runtime._run_cycle()
    for thread in tuple(runtime._room_threads.values()):
        wait_until(lambda: gates.entered(thread) or not thread.is_alive(),
                   what=f"{thread.name} reaching prepare_room")


def _drain(runtime: HostedRoomRuntime, gates: GatedRooms) -> list[str]:
    """Release the oldest blocked run at a time, cycling only while a wake is pending."""
    while (entry := gates.pop_oldest()) is not None:
        thread, release = entry
        release.set()
        thread.join(10.0)
        assert not thread.is_alive(), f"{thread.name} did not finish"
        if runtime._wake.is_set():
            _cycle(runtime, gates)
        assert len(gates.runs) <= MAX_RUNS, f"rooms never settled: {gates.runs}"
    return gates.runs


def test_overlapping_rooms_each_get_one_follow_up_then_settle(tmp_path: Path):
    gates = GatedRooms()
    runtime = _runtime(tmp_path, [ROOM_A, ROOM_B], gates)
    _cycle(runtime, gates)
    assert Counter(gates.runs) == {"room-a": 1, "room-b": 1}

    # Work admitted while both rooms hold their slots must revisit both once they exit.
    runtime.wakeup()
    runs = _drain(runtime, gates)

    assert Counter(runs) == {"room-a": 2, "room-b": 2}
    assert not runtime._wake.is_set()
    assert not any(t.is_alive() for t in runtime._room_threads.values())


def test_single_room_admitted_mid_run_is_rescheduled_once(tmp_path: Path):
    gates = GatedRooms()
    runtime = _runtime(tmp_path, [ROOM_A], gates)
    _cycle(runtime, gates)

    runtime.wakeup()
    runs = _drain(runtime, gates)

    assert runs == ["room-a", "room-a"]
    assert not runtime._wake.is_set()


def test_idle_overlapping_rooms_are_not_rescheduled(tmp_path: Path):
    gates = GatedRooms()
    runtime = _runtime(tmp_path, [ROOM_A, ROOM_B], gates)
    _cycle(runtime, gates)

    runs = _drain(runtime, gates)

    assert Counter(runs) == {"room-a": 1, "room-b": 1}
    assert not runtime._wake.is_set()
