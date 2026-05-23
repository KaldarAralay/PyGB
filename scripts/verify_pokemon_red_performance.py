from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
RANGE_RE = re.compile(r"^(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)$")
RATIO_RE = re.compile(r"^(-?\d+(?:\.\d+)?)/(-?\d+(?:\.\d+)?)$")


@dataclass(frozen=True)
class BenchmarkScenario:
    name: str
    script: Path
    prefix: str
    extra_args: tuple[str, ...]
    default_min_fps: float
    expected_frames: int
    expected_cpu_instr: int
    expected_cpu_cycles: int
    audio_enabled: bool = False


SCENARIOS = {
    "text": BenchmarkScenario(
        name="text",
        script=ROOT / "scripts" / "benchmark_pokemon_red_text.py",
        prefix="pokemon-red-text-profile",
        extra_args=(),
        default_min_fps=50.0,
        expected_frames=240,
        expected_cpu_instr=1_564_703,
        expected_cpu_cycles=16_853_764,
    ),
    "sprites": BenchmarkScenario(
        name="sprites",
        script=ROOT / "scripts" / "benchmark_pokemon_red_sprites.py",
        prefix="pokemon-red-sprites-profile",
        extra_args=(),
        default_min_fps=50.0,
        expected_frames=600,
        expected_cpu_instr=2_717_563,
        expected_cpu_cycles=42_134_400,
    ),
    "sprites-audio": BenchmarkScenario(
        name="sprites-audio",
        script=ROOT / "scripts" / "benchmark_pokemon_red_sprites.py",
        prefix="pokemon-red-sprites-profile",
        extra_args=("--audio-output",),
        default_min_fps=45.0,
        expected_frames=600,
        expected_cpu_instr=2_717_563,
        expected_cpu_cycles=42_134_400,
        audio_enabled=True,
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Pokemon Red performance profiles as an automated regression gate."
    )
    parser.add_argument("--rom", type=Path, default=ROOT / "roms" / "PRed.gb")
    parser.add_argument(
        "--save-file",
        type=Path,
        default=ROOT / "saves" / "pokemon-red-test.sav",
        help="Save file used by sprite-heavy scenarios.",
    )
    parser.add_argument(
        "--scenario",
        choices=tuple(SCENARIOS),
        action="append",
        help="Scenario to run. Defaults to all headless scenarios.",
    )
    parser.add_argument("--min-fps-text", type=float, default=SCENARIOS["text"].default_min_fps)
    parser.add_argument(
        "--min-fps-sprites",
        type=float,
        default=SCENARIOS["sprites"].default_min_fps,
    )
    parser.add_argument(
        "--min-fps-sprites-audio",
        type=float,
        default=SCENARIOS["sprites-audio"].default_min_fps,
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
        help="Optional captured window-profile log to validate host/live metrics.",
    )
    parser.add_argument("--window-min-fps", type=float, default=50.0)
    parser.add_argument("--window-min-audio-queue-ms", type=float, default=80.0)
    parser.add_argument(
        "--window-ignore-initial",
        type=int,
        default=1,
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


def parse_profile_line(line: str, expected_prefix: str | None = None) -> dict[str, Any]:
    parts = line.strip().split()
    if not parts:
        raise ValueError("profile line is empty")
    prefix = parts[0]
    if expected_prefix is not None and prefix != expected_prefix:
        raise ValueError(f"expected prefix {expected_prefix!r}, got {prefix!r}")
    metrics: dict[str, Any] = {"profile": prefix}
    for token in parts[1:]:
        if "=" not in token:
            continue
        key, raw_value = token.split("=", 1)
        metrics[key] = parse_metric_value(raw_value)
    return metrics


def parse_metric_value(value: str) -> Any:
    range_match = RANGE_RE.match(value)
    if range_match:
        return tuple(parse_number(part) for part in range_match.groups())
    ratio_match = RATIO_RE.match(value)
    if ratio_match:
        return tuple(parse_number(part) for part in ratio_match.groups())
    if NUMBER_RE.match(value):
        return parse_number(value)
    return value


def parse_number(value: str) -> int | float:
    if "." in value:
        return float(value)
    return int(value)


def metric_number(metrics: dict[str, Any], key: str) -> int | float:
    value = metrics.get(key)
    if isinstance(value, (int, float)):
        return value
    raise ValueError(f"metric {key!r} is missing or non-numeric: {value!r}")


def metric_range(metrics: dict[str, Any], key: str) -> tuple[int | float, int | float]:
    value = metrics.get(key)
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(part, (int, float)) for part in value)
    ):
        return value
    raise ValueError(f"metric {key!r} is missing or not a range: {value!r}")


def run_scenario(
    scenario: BenchmarkScenario,
    *,
    rom: Path,
    save_file: Path,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-B",
        str(scenario.script),
        "--rom",
        str(rom),
    ]
    if scenario.name.startswith("sprites"):
        command.extend(["--save-file", str(save_file)])
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


def evaluate_scenario_metrics(
    scenario: BenchmarkScenario,
    metrics: dict[str, Any],
    *,
    min_fps: float,
    instruction_tolerance: int,
    cycle_tolerance: int,
) -> list[str]:
    failures: list[str] = []
    fps = float(metric_number(metrics, "run_fps"))
    if fps < min_fps:
        failures.append(f"{scenario.name}: run_fps {fps:.2f} < {min_fps:.2f}")

    frames = int(metric_number(metrics, "frames"))
    if frames != scenario.expected_frames:
        failures.append(
            f"{scenario.name}: frames {frames} != {scenario.expected_frames}"
        )

    cpu_instr = int(metric_number(metrics, "cpu_instr"))
    if abs(cpu_instr - scenario.expected_cpu_instr) > instruction_tolerance:
        failures.append(
            f"{scenario.name}: cpu_instr {cpu_instr} drifted from "
            f"{scenario.expected_cpu_instr} by more than {instruction_tolerance}"
        )

    cpu_cycles = int(metric_number(metrics, "cpu_cycles"))
    if abs(cpu_cycles - scenario.expected_cpu_cycles) > cycle_tolerance:
        failures.append(
            f"{scenario.name}: cpu_cycles {cpu_cycles} drifted from "
            f"{scenario.expected_cpu_cycles} by more than {cycle_tolerance}"
        )

    audio_output = int(metric_number(metrics, "audio_output"))
    if audio_output != int(scenario.audio_enabled):
        failures.append(
            f"{scenario.name}: audio_output {audio_output} != "
            f"{int(scenario.audio_enabled)}"
        )
    if scenario.audio_enabled:
        if int(metric_number(metrics, "apu_samples")) <= 0:
            failures.append(f"{scenario.name}: audio enabled but no APU samples produced")
        for key in ("apu_dropped_samples", "audio_underruns", "audio_dropped"):
            if key in metrics and int(metric_number(metrics, key)) != 0:
                failures.append(f"{scenario.name}: {key}={metrics[key]}")
    return failures


def parse_window_profile_log(path: Path) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("window-profile "):
            profiles.append(parse_profile_line(line, "window-profile"))
    return profiles


def evaluate_window_profiles(
    profiles: list[dict[str, Any]],
    *,
    min_fps: float,
    min_audio_queue_ms: float,
    ignore_initial: int,
) -> list[str]:
    failures: list[str] = []
    if not profiles:
        return ["window-profile: no profile lines found"]
    for index, metrics in enumerate(profiles):
        for key in ("audio_underruns", "audio_dropped", "apu_dropped_samples"):
            if key in metrics and int(metric_number(metrics, key)) != 0:
                failures.append(f"window-profile[{index}]: {key}={metrics[key]}")
        if index < ignore_initial:
            continue
        fps = float(metric_number(metrics, "wall_fps"))
        if fps < min_fps:
            failures.append(f"window-profile[{index}]: wall_fps {fps:.2f} < {min_fps:.2f}")
        queue_min = window_audio_queue_min(metrics)
        if queue_min is not None and queue_min < min_audio_queue_ms:
            failures.append(
                f"window-profile[{index}]: audio_queue_min_ms "
                f"{queue_min:.1f} < {min_audio_queue_ms:.1f}"
            )
    return failures


def window_audio_queue_min(metrics: dict[str, Any]) -> float | None:
    if "audio_queue_range_ms" in metrics:
        low, _ = metric_range(metrics, "audio_queue_range_ms")
        return float(low)
    if "audio_queue_ms" in metrics:
        return float(metric_number(metrics, "audio_queue_ms"))
    return None


def min_fps_for_scenario(args: argparse.Namespace, scenario_name: str) -> float:
    if scenario_name == "text":
        return args.min_fps_text
    if scenario_name == "sprites":
        return args.min_fps_sprites
    if scenario_name == "sprites-audio":
        return args.min_fps_sprites_audio
    raise ValueError(f"unknown scenario: {scenario_name}")


def json_ready(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [json_ready(part) for part in value]
    if isinstance(value, list):
        return [json_ready(part) for part in value]
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    return value


def main() -> int:
    args = parse_args()
    if args.instruction_tolerance < 0:
        raise SystemExit("--instruction-tolerance must be non-negative")
    if args.cycle_tolerance < 0:
        raise SystemExit("--cycle-tolerance must be non-negative")
    if args.window_ignore_initial < 0:
        raise SystemExit("--window-ignore-initial must be non-negative")

    scenario_names = args.scenario or list(SCENARIOS)
    all_failures: list[str] = []
    results: dict[str, Any] = {"headless": {}, "window": None, "failures": all_failures}

    for scenario_name in scenario_names:
        scenario = SCENARIOS[scenario_name]
        print(f"== Pokemon Red performance scenario: {scenario.name} ==")
        try:
            metrics = run_scenario(scenario, rom=args.rom, save_file=args.save_file)
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
        print("== Pokemon Red window-profile log ==")
        profiles = parse_window_profile_log(args.window_profile_log)
        failures = evaluate_window_profiles(
            profiles,
            min_fps=args.window_min_fps,
            min_audio_queue_ms=args.window_min_audio_queue_ms,
            ignore_initial=args.window_ignore_initial,
        )
        results["window"] = {
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

    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(json_ready(results), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.print_json:
        print(json.dumps(json_ready(results), indent=2, sort_keys=True))

    if all_failures:
        print(f"FAIL Pokemon Red performance gate: {len(all_failures)} failure(s)")
        return 1
    print("PASS Pokemon Red performance gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
