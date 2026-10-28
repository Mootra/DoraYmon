"""The offline gate reporter must report failures, not just pass."""

from __future__ import annotations

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts.preflight_offline import FAIL, PASS, SKIP, Gate, report, select_gates


def _gate(name: str, *, slow: bool = False) -> Gate:
    return Gate(name=name, args=(), proves="something", slow=slow)


class SelectGatesTest(unittest.TestCase):
    def test_quick_skips_the_slow_gates(self) -> None:
        gates = (_gate("fast"), _gate("slow", slow=True))

        selected = select_gates(quick=True, gates=gates)

        self.assertEqual([gate.name for gate in selected], ["fast"])

    def test_full_run_keeps_everything(self) -> None:
        gates = (_gate("fast"), _gate("slow", slow=True))

        selected = select_gates(quick=False, gates=gates)

        self.assertEqual([gate.name for gate in selected], ["fast", "slow"])


class ReportTest(unittest.TestCase):
    def _run_report(self, statuses: list[str], **kwargs) -> tuple[int, str]:
        gates = tuple(_gate(name) for name in "abc"[: len(statuses)])
        outcomes = iter(statuses)
        calls: list[str] = []

        def runner(gate):
            calls.append(gate.name)
            return next(outcomes), "detail", 0.0

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = report(gates=gates, runner=runner, **kwargs)
        return code, buffer.getvalue()

    def test_all_passing_exits_zero(self) -> None:
        code, output = self._run_report([PASS, PASS])

        self.assertEqual(code, 0)
        self.assertIn("全部离线门槛通过", output)

    def test_a_failure_exits_non_zero(self) -> None:
        """A reporter that cannot fail is worse than no reporter."""
        code, output = self._run_report([PASS, FAIL])

        self.assertEqual(code, 1)
        self.assertIn("未通过", output)

    def test_a_selected_but_skipped_gate_fails_the_run(self) -> None:
        code, output = self._run_report([PASS, SKIP])

        self.assertEqual(code, 1)
        self.assertIn("必需门槛未通过或未完成", output)
        self.assertNotIn("全部离线门槛通过", output)

    def test_what_each_gate_proves_is_printed(self) -> None:
        """The point of the report is the limit of each gate, not just its status."""
        gates = (_gate("fast"),)
        gates[0].proves = "PROVES_MARKER"
        gates[0].does_not_prove = "LIMIT_MARKER"

        with redirect_stdout((buffer := io.StringIO())):
            report(gates=gates, runner=lambda gate: (PASS, "", 0.0))

        output = buffer.getvalue()
        self.assertIn("PROVES_MARKER", output)
        self.assertIn("LIMIT_MARKER", output)

    def test_the_real_qq_caveat_is_always_printed(self) -> None:
        """Passing every offline gate still must not read as \"QQ works\"."""
        _, passing = self._run_report([PASS])
        _, failing = self._run_report([FAIL])

        for output in (passing, failing):
            self.assertIn("不能声称机器人已在真实 QQ 环境可用", output)

    def test_the_two_route_commands_are_printed(self) -> None:
        _, output = self._run_report([PASS])

        self.assertIn("preflight_official_bot", output)
        self.assertIn("verify_account_channel", output)

    def test_a_quick_run_reaches_only_the_fast_gates(self) -> None:
        gates = (_gate("fast"), _gate("slow", slow=True))
        ran: list[str] = []

        def runner(gate):
            ran.append(gate.name)
            return PASS, "", 0.0

        with redirect_stdout(io.StringIO()):
            report(quick=True, gates=gates, runner=runner)

        self.assertEqual(ran, ["fast"])

    def test_quick_exclusions_are_explicit_and_do_not_claim_a_full_pass(self):
        gates = (_gate("fast"), _gate("slow", slow=True))
        with redirect_stdout((buffer := io.StringIO())):
            code = report(quick=True, gates=gates, runner=lambda gate: (PASS, "", 0))
        self.assertEqual(code, 0)
        self.assertIn("跳过：slow（--quick 未选择）", buffer.getvalue())
        self.assertNotIn("全部离线门槛通过", buffer.getvalue())

    def test_quick_mode_does_not_excuse_a_selected_incomplete_gate(self):
        code, _ = self._run_report([SKIP], quick=True)
        self.assertEqual(code, 1)

    def test_an_unknown_status_cannot_pass_the_report(self):
        code, _ = self._run_report(["unexpected"])
        self.assertEqual(code, 1)

    def test_no_selected_gates_cannot_count_as_acceptance(self):
        code, output = self._run_report([])
        self.assertEqual(code, 1)
        self.assertIn("未选择任何验收门槛", output)


class GateExecutionTest(unittest.TestCase):
    @patch("scripts.preflight_offline.subprocess.run")
    def test_timeout_is_a_failure_and_propagates_to_the_report(self, run):
        from scripts.preflight_offline import _run
        gate = _gate("synthetic-timeout")
        run.side_effect = subprocess.TimeoutExpired("synthetic", 1)
        status, detail, _ = _run(gate)
        self.assertEqual(status, FAIL)
        self.assertIn("未完成", detail)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(report(gates=(gate,), runner=_run), 1)

    @patch("scripts.preflight_offline.subprocess.run")
    def test_unavailable_command_is_reported_and_fails(self, run):
        from scripts.preflight_offline import _run
        run.side_effect = FileNotFoundError("synthetic missing command")
        status, detail, _ = _run(_gate("synthetic-missing"))
        self.assertEqual(status, FAIL)
        self.assertIn("无法执行", detail)

    @patch("scripts.preflight_offline.subprocess.run")
    def test_child_uses_the_current_interpreter_and_nonzero_exit_fails(self, run):
        from scripts.preflight_offline import _run
        run.return_value = subprocess.CompletedProcess([], 2, stdout="synthetic failure", stderr="")
        status, detail, _ = _run(_gate("synthetic-failed"))
        self.assertEqual(run.call_args.args[0][0], sys.executable)
        self.assertEqual(status, FAIL)
        self.assertIn("synthetic failure", detail)


class RealGateListTest(unittest.TestCase):
    def test_every_gate_states_what_it_does_not_prove(self) -> None:
        """Each gate must be honest about its limit, or the report oversells."""
        from scripts.preflight_offline import GATES

        for gate in GATES:
            with self.subTest(gate=gate.name):
                self.assertTrue(gate.proves)
                self.assertTrue(gate.does_not_prove)

    def test_the_three_expected_gates_are_present(self) -> None:
        from scripts.preflight_offline import GATES

        names = " ".join(gate.name for gate in GATES)
        self.assertIn("单元测试", names)
        self.assertIn("回放", names)
        self.assertIn("RAG", names)

    def test_gate_commands_use_module_invocation(self) -> None:
        """They must run from the project root without a PYTHONPATH dance."""
        from scripts.preflight_offline import GATES

        for gate in GATES:
            with self.subTest(gate=gate.name):
                self.assertIn("-m", gate.args)


if __name__ == "__main__":
    unittest.main()
