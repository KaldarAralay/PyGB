from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.benchmark_super_mario_land_action import DEFAULT_BUTTON_SCRIPT  # noqa: E402
from scripts.verify_pokemon_red_performance import (  # noqa: E402
    BenchmarkScenario,
    evaluate_scenario_metrics,
    evaluate_window_profiles,
    json_ready,
    parse_profile_line,
    parse_window_profile_log,
)


SCENARIOS = {
    "action": BenchmarkScenario(
        name="action",
        script=ROOT / "scripts" / "benchmark_super_mario_land_action.py",
        prefix="super-mario-land-action-profile",
        extra_args=(),
        default_min_fps=50.0,
        expected_frames=600,
        expected_cpu_instr=1_343_492,
        expected_cpu_cycles=42_379_808,
    ),
    "action-audio": BenchmarkScenario(
        name="action-audio",
        script=ROOT / "scripts" / "benchmark_super_mario_land_action.py",
        prefix="super-mario-land-action-profile",
        extra_args=("--audio-output",),
        default_min_fps=45.0,
        expected_frames=600,
        expected_cpu_instr=1_343_492,
        expected_cpu_cycles=42_379_808,
        audio_enabled=True,
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Super Mario Land early-action performance profiles as a regression gate."
    )
    parser.add_argument("--rom", type=Path, default=ROOT / "roms" / "SML.gb")
    parser.add_argument(
        "--scenario",
        choices=tuple(SCENARIOS),
        action="append",
        help="Headless scenario to run. Defaults to all headless scenarios.",
    )
    parser.add_argument("--min-fps-action", type=float, default=SCENARIOS["action"].default_min_fps)
    parser.add_argument(
        "--min-fps-action-audio",
        type=float,
        default=SCENARIOS["action-audio"].default_min_fps,
    )
    parser.add_argument(
        "--instruction-tolerance",
        type=int,
        default=0,
        help="Allowed absolute drift from expected deterministic instruction totals.",
    )
    parser.add_argument(
        "--cycle-tolerance",
        type=int,
        default=0,
        help="Allowed absolute drift from expected deterministic CPU cycle totals.",
    )
    parser.add_argument(
        "--window-profile-log",
        type=Path,
        help="Optional captured Super Mario Land window-profile log to validate live metrics.",
    )
    parser.add_argument(
        "--run-live-window",
        action="store_true",
        help="Open a live Tkinter window, run the scripted action path, and validate profiles.",
    )
    parser.add_argument(
        "--window-python",
        default=sys.executable,
        help="Python executable used for --run-live-window. Use system Python for Tkinter.",
    )
    parser.add_argument("--live-frames", type=int, default=720)
    parser.add_argument("--live-profile-interval", type=int, default=60)
    parser.add_argument("--live-button-script", default=DEFAULT_BUTTON_SCRIPT)
    parser.add_argument("--window-min-fps", type=float, default=45.0)
    parser.add_argument("--window-min-audio-queue-ms", type=float, default=30.0)
    parser.add_argument(
        "--window-ignore-initial",
        type=int,
        default=2,
        help="Ignore this many initial 60-frame windows for FPS and queue checks.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Write parsed metrics and failures to a JSON file.",
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print parsed metrics as JSON after the text summary.",
    )
    return parser.parse_args()


def run_scenario(scenario: BenchmarkScenario, *, rom: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        "-B",
        str(scenario.script),
        "--rom",
        str(rom),
    ]
    command.extend(scenario.extra_args)

    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    profile_lines = [
        line for line in output.splitlines() if line.startswith(scenario.prefix + " ")
    ]
    if result.returncode or not profile_lines:
        raise RuntimeError(
            f"{scenario.name} profile failed with return code {result.returncode}\n{output}"
        )
    metrics = parse_profile_line(profile_lines[-1], scenario.prefix)
    metrics["scenario"] = scenario.name
    metrics["command"] = command
    return metrics


def parse_window_profile_text(output: str) -> list[dict[str, Any]]:
    return [
        parse_profile_line(line, "window-profile")
        for line in output.splitlines()
        if line.startswith("window-profile ")
    ]


def run_live_window_profiles(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[str], str]:
    command = [
        args.window_python,
        "-B",
        str(ROOT / "main.py"),
        str(args.rom),
        "--window",
        "--audio",
        "--max-instructions",
        "0",
        "--frames",
        str(args.live_frames),
        "--profile-window",
        "--profile-window-interval",
        str(args.live_profile_interval),
        "--button-script",
        args.live_button_script,
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    profiles = parse_window_profile_text(output)
    failures: list[str] = []
    if result.returncode:
        failures.append(
            f"live-window: command failed with return code {result.returncode}"
        )
    if not profiles:
        failures.append("live-window: no window-profile lines found")
    return profiles, failures, output


def min_fps_for_scenario(args: argparse.Namespace, scenario_name: str) -> float:
    if scenario_name == "action":
        return args.min_fps_action
    if scenario_name == "action-audio":
        return args.min_fps_action_audio
    raise ValueError(f"unknown scenario: {scenario_name}")


def main() -> int:
    args = parse_args()
    if args.instruction_tolerance < 0:
        raise SystemExit("--instruction-tolerance must be non-negative")
    if args.cycle_tolerance < 0:
        raise SystemExit("--cycle-tolerance must be non-negative")
    if args.live_frames < 1:
        raise SystemExit("--live-frames must be positive")
    if args.live_profile_interval < 1:
        raise SystemExit("--live-profile-interval must be positive")
    if args.window_ignore_initial < 0:
        raise SystemExit("--window-ignore-initial must be non-negative")

    scenario_names = args.scenario or list(SCENARIOS)
    all_failures: list[str] = []
    results: dict[str, Any] = {
        "headless": {},
        "window_log": None,
        "live_window": None,
        "failures": all_failures,
    }

    for scenario_name in scenario_names:
        scenario = SCENARIOS[scenario_name]
        print(f"== Super Mario Land performance scenario: {scenario.name} ==")
        try:
            metrics = run_scenario(scenario, rom=args.rom)
            failures = evaluate_scenario_metrics(
                scenario,
                metrics,
                min_fps=min_fps_for_scenario(args, scenario.name),
                instruction_tolerance=args.instruction_tolerance,
                cycle_tolerance=args.cycle_tolerance,
            )
        except Exception as exc:
            metrics = {"scenario": scenario.name}
            failures = [f"{scenario.name}: {exc}"]
        results["headless"][scenario.name] = metrics
        all_failures.extend(failures)
        if failures:
            for failure in failures:
                print(f"FAIL {failure}")
        else:
            print(
                "PASS",
                f"run_fps={metrics['run_fps']}",
                f"frames={metrics['frames']}",
                f"cpu_instr={metrics['cpu_instr']}",
                f"cpu_cycles={metrics['cpu_cycles']}",
            )

    if args.window_profile_log is not None:
        print("== Super Mario Land window-profile log ==")
        profiles = parse_window_profile_log(args.window_profile_log)
        failures = evaluate_window_profiles(
            profiles,
            min_fps=args.window_min_fps,
            min_audio_queue_ms=args.window_min_audio_queue_ms,
            ignore_initial=args.window_ignore_initial,
        )
        results["window_log"] = {
            "log": str(args.window_profile_log),
            "profiles": profiles,
            "ignore_initial": args.window_ignore_initial,
            "min_fps": args.window_min_fps,
            "min_audio_queue_ms": args.window_min_audio_queue_ms,
        }
        all_failures.extend(failures)
        if failures:
            for failure in failures:
                print(f"FAIL {failure}")
        else:
            print(f"PASS window_profiles={len(profiles)}")

    if args.run_live_window:
        print("== Super Mario Land live window-profile capture ==")
        profiles, command_failures, output = run_live_window_profiles(args)
        profile_failures = []
        if profiles:
            profile_failures = evaluate_window_profiles(
                profiles,
                min_fps=args.window_min_fps,
                min_audio_queue_ms=args.window_min_audio_queue_ms,
                ignore_initial=args.window_ignore_initial,
            )
        failures = command_failures + profile_failures
        results["live_window"] = {
            "profiles": profiles,
            "ignore_initial": args.window_ignore_initial,
            "min_fps": args.window_min_fps,
            "min_audio_queue_ms": args.window_min_audio_queue_ms,
            "output_tail": output[-2000:],
        }
        all_failures.extend(failures)
        if failures:
            for failure in failures:
                print(f"FAIL {failure}")
        else:
            print(f"PASS live_window_profiles={len(profiles)}")

    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(json_ready(results), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.print_json:
        print(json.dumps(json_ready(results), indent=2, sort_keys=True))

    if all_failures:
        print(f"FAIL Super Mario Land performance gate: {len(all_failures)} failure(s)")
        return 1
    print("PASS Super Mario Land performance gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
