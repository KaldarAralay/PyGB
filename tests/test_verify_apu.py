from __future__ import annotations

from pathlib import Path
import unittest

from scripts.verify_apu import (
    ApuRomResult,
    classify_blargg_output,
    evaluate_results,
    read_blargg_memory_output,
)


class FakeBus:
    def __init__(self, values: dict[int, int]) -> None:
        self.values = values

    def read8(self, address: int) -> int:
        return self.values.get(address, 0)


class FakeEmulator:
    def __init__(self, values: dict[int, int]) -> None:
        self.bus = FakeBus(values)


class ApuVerifierTests(unittest.TestCase):
    def test_reads_blargg_memory_output_signature_and_text(self) -> None:
        values = {
            0xA000: 0x80,
            0xA001: 0xDE,
            0xA002: 0xB0,
            0xA003: 0x61,
        }
        for offset, value in enumerate(b"01-registers\nPassed\n\0"):
            values[0xA004 + offset] = value

        status, signature_present, output = read_blargg_memory_output(
            FakeEmulator(values)  # type: ignore[arg-type]
        )

        self.assertEqual(status, 0x80)
        self.assertTrue(signature_present)
        self.assertEqual(output, "01-registers\nPassed\n")

    def test_classifies_passed_text_even_before_status_byte_settles(self) -> None:
        passed, failed = classify_blargg_output(
            status_code=0x80,
            signature_present=True,
            output="03-trigger\n\nPassed\n",
            timed_out=False,
        )

        self.assertTrue(passed)
        self.assertFalse(failed)

    def test_classifies_nonzero_result_code_as_failure(self) -> None:
        passed, failed = classify_blargg_output(
            status_code=0x03,
            signature_present=True,
            output="04-sweep\n\n",
            timed_out=False,
        )

        self.assertFalse(passed)
        self.assertTrue(failed)

    def test_evaluate_results_tracks_expected_pass_xfail_and_xpass(self) -> None:
        expected_pass_failure = ApuRomResult(
            name="01-registers.gb",
            path=Path("01-registers.gb"),
            passed=False,
            failed=True,
            timed_out=False,
            status_code=1,
            signature_present=True,
            instructions=1,
            cycles=4,
            output="Failed",
        )
        known_failure = ApuRomResult(
            name="04-sweep.gb",
            path=Path("04-sweep.gb"),
            passed=False,
            failed=True,
            timed_out=False,
            status_code=3,
            signature_present=True,
            instructions=1,
            cycles=4,
            output="Failed #3",
        )
        unexpected_pass = ApuRomResult(
            name="05-sweep details.gb",
            path=Path("05-sweep details.gb"),
            passed=True,
            failed=False,
            timed_out=False,
            status_code=0,
            signature_present=True,
            instructions=1,
            cycles=4,
            output="Passed",
        )

        failures = evaluate_results(
            [expected_pass_failure, known_failure, unexpected_pass],
            strict=False,
            allow_xpass=False,
        )

        self.assertEqual(
            failures,
            [
                "01-registers.gb: expected PASS, got fail",
                "05-sweep details.gb: XPASS, update EXPECTED_PASS/known-failure list",
            ],
        )

    def test_allow_xpass_accepts_known_failure_improvement(self) -> None:
        unexpected_pass = ApuRomResult(
            name="05-sweep details.gb",
            path=Path("05-sweep details.gb"),
            passed=True,
            failed=False,
            timed_out=False,
            status_code=0,
            signature_present=True,
            instructions=1,
            cycles=4,
            output="Passed",
        )

        failures = evaluate_results([unexpected_pass], strict=False, allow_xpass=True)

        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
