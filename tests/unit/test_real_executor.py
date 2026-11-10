"""真 executor 的种子派生与池大小硬上界。"""

from __future__ import annotations

import pytest

from src.experiment.real import CALIBRATED_POOL_CEILING, identifier_for_seed, probe_executor


class TestIdentifier:
    def test_seed_uses_low_16_bits(self) -> None:
        assert identifier_for_seed(0x12345) == 0x2345

    @pytest.mark.parametrize("seed", [True, -1, 1.5])
    def test_invalid_seed_is_refused(self, seed) -> None:
        with pytest.raises((TypeError, ValueError)):
            identifier_for_seed(seed)


class TestPoolCeiling:
    def test_above_calibrated_ceiling_is_refused_before_socket_creation(self) -> None:
        with pytest.raises(ValueError, match="无丢包上界"):
            probe_executor(0, max_in_flight=CALIBRATED_POOL_CEILING + 1)

    def test_zero_is_refused(self) -> None:
        with pytest.raises(ValueError, match="至少为 1"):
            probe_executor(0, max_in_flight=0)
