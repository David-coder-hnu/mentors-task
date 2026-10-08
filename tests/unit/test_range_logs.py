"""`range_logs` 的解析与对账 —— 纯文本，不碰 Docker，**进 hosted CI**。

这些断言的价值在于它们钉住的是**上游日志的格式**。格式一旦变了（比如上游
修掉 `Recieved` 那个拼写），解析器会静默取到错的值，而错的方向恰好是
「把丢包算得更多」——看起来更保守，实际是数字错了。所以拼写那条要单独钉。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.adapter.range_logs import (
    RangeLedger,
    RoundInvalid,
    count_captured,
    find_latest_queue_log,
    parse_queue_log,
    read_ledger,
)

#: 真实格式（`punchingbag.cpp:254` / `:274`），含上游那个 `Recieved` 的拼写。
SAMPLE_LOG = """\
[2026-10-08 12:00:00]: Queue size: 120, Usage: 0.048%, Received packets: 5000, Libnet errors: 0
[2026-10-08 12:00:01]: Queue size: 30, Usage: 0.012%, Received packets: 8000, Libnet errors: 2
[2026-10-08 12:00:02] Logger shutting down.
Final queue size: 0
Recieved packets: 8123
Libnet errors: 2
"""

CLEAN_LOG = """\
[2026-10-08 12:00:00]: Queue size: 5, Usage: 0.002%, Received packets: 1000, Libnet errors: 0
[2026-10-08 12:00:01] Logger shutting down.
Final queue size: 0
Recieved packets: 1000
Libnet errors: 0
"""


class TestParsing:
    def test_samples_are_read(self) -> None:
        ledger = parse_queue_log(SAMPLE_LOG)
        assert [s.queue_size for s in ledger.samples] == [120, 30]
        assert [s.received for s in ledger.samples] == [5000, 8000]
        assert ledger.samples[0].usage_percent == pytest.approx(0.048)

    def test_summary_beats_the_last_live_line(self) -> None:
        """汇总终值 8123 才是权威；最后一条实时行还说 8000。

        按 `"Received packets"` 去找汇总会取到 8000 —— 那个值偏小，
        于是 `captured - enqueued` 变大，丢包被**算多**。
        """
        ledger = parse_queue_log(SAMPLE_LOG)
        assert ledger.enqueued == 8123
        assert ledger.has_summary

    def test_spelling_trap_is_explicitly_covered(self) -> None:
        """上游汇总行写的是 `Recieved`（少一个 i）。这条钉住我们没"顺手改正"。"""
        assert "Recieved packets:" in SAMPLE_LOG.splitlines()[-2]

    def test_missing_summary_means_not_cleanly_shut_down(self) -> None:
        """只有实时行 —— 靶场被 kill 了，终值不可信。"""
        partial = SAMPLE_LOG.split(" Logger shutting down.")[0]
        ledger = parse_queue_log(partial)
        assert not ledger.has_summary
        assert ledger.enqueued == 8000, "退而取最后一条实时行，但 has_summary 必须为 False"

    def test_libnet_errors_are_read(self) -> None:
        assert parse_queue_log(SAMPLE_LOG).libnet_errors == 2


class TestReconcile:
    def _ledger(self, *, captured: int, enqueued: int, errors: int = 0) -> RangeLedger:
        return RangeLedger(
            enqueued=enqueued,
            final_queue_size=0,
            libnet_errors=errors,
            captured=captured,
            has_summary=True,
        )

    def test_clean_round_passes(self) -> None:
        self._ledger(captured=1000, enqueued=1000).reconcile(sent=1000)

    def test_pcap_overflow_is_caught(self) -> None:
        """第一层丢失：靶场压根没看见。日志里不留痕，只有 sent 能暴露它。"""
        with pytest.raises(RoundInvalid, match="靶场从没看见"):
            self._ledger(captured=900, enqueued=900).reconcile(sent=1000)

    def test_queue_full_drop_is_caught(self) -> None:
        """第二层丢失：抓到了但没入队。"""
        with pytest.raises(RoundInvalid, match="队列满被丢"):
            self._ledger(captured=1000, enqueued=800).reconcile(sent=1000)

    def test_libnet_errors_are_caught(self) -> None:
        with pytest.raises(RoundInvalid, match="libnet_write"):
            self._ledger(captured=1000, enqueued=1000, errors=3).reconcile(sent=1000)

    def test_extra_traffic_breaks_the_premise(self) -> None:
        """靶场收到得比我们发的还多 —— 有别的发送方，对账前提不成立。"""
        with pytest.raises(RoundInvalid, match="有别的进程"):
            self._ledger(captured=1100, enqueued=1100).reconcile(sent=1000)

    def test_missing_summary_invalidates_the_round(self) -> None:
        ledger = RangeLedger(
            enqueued=1000, final_queue_size=0, libnet_errors=0, captured=1000, has_summary=False
        )
        with pytest.raises(RoundInvalid, match="不是正常退出"):
            ledger.reconcile(sent=1000)

    def test_all_failure_modes_are_reported_together(self) -> None:
        """一次报全，别让用户修一个再撞下一个。"""
        ledger = RangeLedger(
            enqueued=800, final_queue_size=0, libnet_errors=1, captured=900, has_summary=True
        )
        with pytest.raises(RoundInvalid) as excinfo:
            ledger.reconcile(sent=1000)
        message = str(excinfo.value)
        assert "靶场从没看见" in message
        assert "队列满被丢" in message
        assert "libnet_write" in message


class TestFiles:
    def test_count_captured_counts_nonblank_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "packet_log.txt"
        log.write_text("1,2001:db8::1\n2,2001:db8::2\n\n3,2001:db8::3\n", encoding="utf-8")
        assert count_captured(log) == 3

    def test_missing_packet_log_says_why(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="自己的 CWD"):
            count_captured(tmp_path / "nope.txt")

    def test_latest_queue_log_picked_by_name_not_mtime(self, tmp_path: Path) -> None:
        """文件名时间戳即顺序；mtime 会被挂载/拷贝改写，靠不住。"""
        old = tmp_path / "queue_log_2026-10-08_11-00-00.txt"
        new = tmp_path / "queue_log_2026-10-08_12-00-00.txt"
        new.write_text(CLEAN_LOG, encoding="utf-8")
        old.write_text(SAMPLE_LOG, encoding="utf-8")
        # 把新的 mtime 改老，验证选择不受它影响
        import os

        os.utime(new, (0, 0))
        assert find_latest_queue_log(tmp_path) == new

    def test_read_ledger_joins_both_logs(self, tmp_path: Path) -> None:
        (tmp_path / "queue_log_2026-10-08_12-00-00.txt").write_text(CLEAN_LOG, encoding="utf-8")
        (tmp_path / "packet_log.txt").write_text("1,a\n2,b\n3,c\n", encoding="utf-8")

        ledger = read_ledger(tmp_path)
        assert ledger.enqueued == 1000
        assert ledger.captured == 3

    def test_end_to_end_clean_round(self, tmp_path: Path) -> None:
        """整条链路：两个日志文件 → 账本 → 判定通过。"""
        (tmp_path / "queue_log_2026-10-08_12-00-00.txt").write_text(CLEAN_LOG, encoding="utf-8")
        (tmp_path / "packet_log.txt").write_text(
            "".join(f"{i},2001:db8::1\n" for i in range(1000)), encoding="utf-8"
        )
        read_ledger(tmp_path).reconcile(sent=1000)

    def test_end_to_end_queue_drop_is_rejected(self, tmp_path: Path) -> None:
        """抓到了 1000 个、只入队 800 个 —— 队列满丢了 200。"""
        dropped = CLEAN_LOG.replace("Recieved packets: 1000", "Recieved packets: 800")
        (tmp_path / "queue_log_2026-10-08_12-00-00.txt").write_text(dropped, encoding="utf-8")
        (tmp_path / "packet_log.txt").write_text(
            "".join(f"{i},2001:db8::1\n" for i in range(1000)), encoding="utf-8"
        )
        with pytest.raises(RoundInvalid, match="队列满被丢"):
            read_ledger(tmp_path).reconcile(sent=1000)

    def test_end_to_end_pcap_overflow_is_rejected(self, tmp_path: Path) -> None:
        """靶场只抓到 1000、我们发了 1200 —— 那 200 个在 pcap 层就没了。"""
        (tmp_path / "queue_log_2026-10-08_12-00-00.txt").write_text(CLEAN_LOG, encoding="utf-8")
        (tmp_path / "packet_log.txt").write_text(
            "".join(f"{i},2001:db8::1\n" for i in range(1000)), encoding="utf-8"
        )
        with pytest.raises(RoundInvalid, match="靶场从没看见"):
            read_ledger(tmp_path).reconcile(sent=1200)
