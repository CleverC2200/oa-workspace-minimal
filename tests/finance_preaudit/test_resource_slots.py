from __future__ import annotations

import multiprocessing
from pathlib import Path
import tempfile
import unittest

from apps.oa_approval import resource_slots


def _hold_slot(root: str, ready: object, release: object) -> None:
    with resource_slots.acquire(
        "recorded-download",
        capacity=1,
        timeout_sec=1,
        root=Path(root),
    ):
        ready.set()
        release.wait(5)


class ResourceSlotsTest(unittest.TestCase):
    def test_slot_capacity_is_shared_across_processes(self) -> None:
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        with tempfile.TemporaryDirectory() as tmp:
            process = context.Process(
                target=_hold_slot,
                args=(tmp, ready, release),
            )
            process.start()
            try:
                self.assertTrue(ready.wait(2))
                with self.assertRaises(resource_slots.ResourceSlotTimeout):
                    with resource_slots.acquire(
                        "recorded-download",
                        capacity=1,
                        timeout_sec=0.1,
                        root=Path(tmp),
                    ):
                        pass
            finally:
                release.set()
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join(2)

            self.assertEqual(process.exitcode, 0)
            with resource_slots.acquire(
                "recorded-download",
                capacity=1,
                timeout_sec=1,
                root=Path(tmp),
            ):
                pass

    def test_different_resource_names_do_not_block_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with resource_slots.acquire(
                "direct-download",
                capacity=1,
                timeout_sec=1,
                root=root,
            ):
                with resource_slots.acquire(
                    "visible-browser",
                    capacity=1,
                    timeout_sec=1,
                    root=root,
                ):
                    pass


if __name__ == "__main__":
    unittest.main()
