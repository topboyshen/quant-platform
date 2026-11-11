from __future__ import annotations

import pytest

from radar.runtime_lock import SingleInstanceLock, SingleInstanceLockError


def test_single_instance_lock_blocks_duplicate(tmp_path):
    first = SingleInstanceLock("radar-monitor-v3", lock_dir=tmp_path).acquire()
    try:
        with pytest.raises(SingleInstanceLockError):
            SingleInstanceLock("radar-monitor-v3", lock_dir=tmp_path).acquire()
    finally:
        first.release()


def test_single_instance_lock_can_reacquire_after_release(tmp_path):
    lock = SingleInstanceLock("radar-dashboard", lock_dir=tmp_path).acquire()
    lock.release()

    second = SingleInstanceLock("radar-dashboard", lock_dir=tmp_path).acquire()
    second.release()
