"""协议 v3 的记忆载荷与 M1/M2 的唯一自变量。"""

from __future__ import annotations

import random

import pytest

from src.adapter.memory import MemoryError, memory_to_wire, normalize_memory
from src.adapter.protocol import MemoryChannel, PROTOCOL_VERSION
from src.adapter.runner import BudgetLedger, SurfaceServer
from src.adapter.surface import ProbeSurface
from src.baselines.memory_bandit import run_memory_bandit

PREFIXES = ("2001:db8:5::/64", "2001:db8:5:1::/64")


class _Answer:
    __slots__ = ("status",)

    def __init__(self, status: str) -> None:
        self.status = status


class FakeSurface:
    def __init__(self, *, memory=None, statuses=None) -> None:
        self.prefixes = PREFIXES
        self.epoch_size = 2
        self._budget = 4
        self._memory = normalize_memory(memory, PREFIXES)
        self._statuses = list(statuses or ["received"] * 4)
        self.calls: list[tuple[str, int]] = []
        self.reports: list[tuple[str, bool]] = []
        self.remembered = None

    @property
    def budget_left(self) -> int:
        return self._budget

    @property
    def memory(self):
        return {
            prefix: {"pulls": entry["pulls"], "rewards": entry["rewards"]}
            for prefix, entry in self._memory.items()
        }

    def sample(self, prefix: str, n: int) -> list[_Answer]:
        self.calls.append((prefix, n))
        out = [_Answer(self._statuses.pop(0)) for _ in range(n)]
        return out

    def report(self, prefix: str, aliased: bool) -> None:
        self.reports.append((prefix, aliased))

    def remember(self, memory) -> None:
        self.remembered = normalize_memory(memory, PREFIXES)

    def rng(self) -> random.Random:
        return random.Random(7)


class TestMemorySchema:
    def test_missing_prefixes_are_filled_with_zero(self) -> None:
        assert normalize_memory({}, PREFIXES) == {
            PREFIXES[0]: {"pulls": 0, "rewards": 0},
            PREFIXES[1]: {"pulls": 0, "rewards": 0},
        }

    @pytest.mark.parametrize(
        "payload",
        [
            {"outside::/64": {"pulls": 1, "rewards": 1}},
            {PREFIXES[0]: {"pulls": -1, "rewards": 0}},
            {PREFIXES[0]: {"pulls": 1, "rewards": 2}},
            {PREFIXES[0]: {"pulls": 1, "rewards": 1, "aliased": True}},
            {PREFIXES[0]: {"pulls": True, "rewards": 0}},
        ],
    )
    def test_invalid_shapes_are_refused(self, payload) -> None:
        with pytest.raises(MemoryError):
            normalize_memory(payload, PREFIXES)

    def test_wire_order_follows_task_order(self) -> None:
        payload = {
            PREFIXES[1]: {"pulls": 3, "rewards": 1},
            PREFIXES[0]: {"pulls": 2, "rewards": 2},
        }
        assert list(memory_to_wire(payload, PREFIXES)) == list(PREFIXES)


class TestMemoryCrossesTheBoundary:
    def _server(self, *, memory=None) -> SurfaceServer:
        class Executor:
            def run(self, targets):
                raise AssertionError("本组不探测")

        return SurfaceServer(
            executor=Executor(),
            oracle=None,  # type: ignore[arg-type]
            ledger=BudgetLedger(10),
            sampler=lambda prefix, n: [],
            prefixes=PREFIXES,
            seed=9,
            epoch_size=2,
            memory=memory,
        )

    def test_hello_carries_current_protocol_epoch_and_memory(self) -> None:
        memory = {PREFIXES[0]: {"pulls": 4, "rewards": 3}}
        frame = self._server(memory=memory).handle({"id": 0, "verb": "hello", "args": {}})
        assert frame["result"]["protocol"] == PROTOCOL_VERSION == 4
        assert frame["result"]["epoch_size"] == 2
        assert frame["result"]["memory"][PREFIXES[0]] == {"pulls": 4, "rewards": 3}
        assert frame["result"]["memory"][PREFIXES[1]] == {"pulls": 0, "rewards": 0}

    def test_remember_updates_only_runner_side_memory(self) -> None:
        server = self._server()
        memory = {
            PREFIXES[0]: {"pulls": 7, "rewards": 7},
            PREFIXES[1]: {"pulls": 3, "rewards": 1},
        }
        frame = server.handle(
            {"id": 1, "verb": "remember", "args": {"memory": memory}}
        )
        assert frame["ok"] is True
        assert server.memory == memory

    def test_surface_memory_is_a_copy(self) -> None:
        memory = {PREFIXES[0]: {"pulls": 4, "rewards": 3}}
        surface = ProbeSurface(
            PREFIXES,
            MemoryChannel().client_side(),
            seed=9,
            epoch_size=2,
            memory=memory,
        )
        surface.memory[PREFIXES[0]]["pulls"] = 999
        assert surface.memory[PREFIXES[0]]["pulls"] == 4


class TestM1M2Difference:
    def test_empty_history_makes_the_first_epoch_identical(self) -> None:
        m1 = FakeSurface()
        m2 = FakeSurface()
        run_memory_bandit(m1, use_memory=False)
        run_memory_bandit(m2, use_memory=True)
        assert m1.calls == m2.calls
        assert m1.reports == m2.reports
        assert m1.remembered is None
        assert m2.remembered is not None

    def test_m1_ignores_history_and_m2_uses_it(self) -> None:
        history = {
            PREFIXES[0]: {"pulls": 20, "rewards": 20},
            PREFIXES[1]: {"pulls": 20, "rewards": 0},
        }
        m1 = FakeSurface(memory=history, statuses=["sent_no_reply"] * 4)
        m2 = FakeSurface(memory=history, statuses=["received"] * 4)
        run_memory_bandit(m1, use_memory=False)
        run_memory_bandit(m2, use_memory=True)
        assert m1.calls != m2.calls
        assert m1.remembered is None
        assert m2.remembered == {
            PREFIXES[0]: {"pulls": 24, "rewards": 24},
            PREFIXES[1]: {"pulls": 20, "rewards": 0},
        }
