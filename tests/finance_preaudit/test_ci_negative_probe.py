import unittest


class NegativeCiProbe(unittest.TestCase):
    def test_quality_gate_blocks_a_test_failure(self) -> None:
        self.fail("intentional_ci_negative_probe")
