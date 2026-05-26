from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.benchmark_pokemon_crystal_overworld import (  # noqa: E402
    DEFAULT_BUTTON_SCRIPT,
    DEFAULT_PROFILE_FRAMES,
    DEFAULT_SAVE_FILE,
    DEFAULT_WINDOW_SIZE,
    DEFAULT_WARMUP_FRAMES,
)
from scripts.verify_pokemon_red_performance import (  # noqa: E402
    evaluate_window_profiles,
    json_ready,
    metric_number,
    parse_profile_line,
    parse_window_profile_log,
)


@dataclass(frozen=True)
class CrystalScenario:
    name: str
    extra_args: tuple[str, ...]
    default_min_fps: float
    expected_frames: int
    expected_ppu_frames: int | None
    expected_cpu_instr: int | None
    expected_cpu_cycles: int | None
    audio_enabled: bool = False


SCENARIOS = {
    "overworld": CrystalScenario(
        name="overworld",
        extra_args=(),
        default_min_fps=55.0,
        expected_frames=DEFAULT_PROFILE_FRAMES,
        expected_ppu_frames=600,
        expected_cpu_instr=1_624_904,
        expected_cpu_cycles=DEFAULT_PROFILE_FRAMES * 456 * 154,
    ),
    "overworld-audio": CrystalScenario(
        name="overworld-audio",
        extra_args=("--audio-output",),
        default_min_fps=49.0,
        expected_frames=DEFAULT_PROFILE_FRAMES,
        expected_ppu_frames=600,
        expected_cpu_instr=1_624_904,
        expected_cpu_cycles=DEFAULT_PROFILE_FRAMES * 456 * 154,
        audio_enabled=True,
    ),
}


PROFILE_PREFIX = "pokemon-crystal-overworld-profile"
WINDOW_PREFIX = "pokemon-crystal-overworld-window"
DEFAULT_EXPECTED_CYCLES = object()
DEFAULT_EXPECTED_PPU_FRAMES = object()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Pokemon Crystal saved-game CGB overworld performance as a regression gate."
    )
    parser.add_argument("--rom", type=Path, default=ROOT / "roms" / "crystal.gbc")
    parser.add_argument("--save-file", type=Path, default=DEFAULT_SAVE_FILE)
    parser.add_argument("--button-script", default=DEFAULT_BUTTON_SCRIPT)
    parser.add_argument("--warmup-frames", type=int, default=DEFAULT_WARMUP_FRAMES)
    parser.add_argument("--profile-frames", type=int, default=DEFAULT_PROFILE_FRAMES)
    parser.add_argument("--window-size", type=int, default=DEFAULT_WINDOW_SIZE)
    parser.add_argument(
        "--scenario",
        choices=tuple(SCENARIOS),
        action="append",
        help="Headless scenario to run. Defaults to all headless scenarios.",
    )
    parser.add_argument("--min-fps-overworld", type=float, default=SCENARIOS["overworld"].default_min_fps)
    parser.add_argument(
        "--min-fps-overworld-audio",
        type=float,
        default=SCENARIOS["overworld-audio"].default_min_fps,
    )
    parser.add_argument(
        "--min-window-fps",
        type=float,
        default=48.0,
        help="Minimum 60-frame headless metric window FPS.",
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
        "--expected-overworld-cpu-instr",
        type=int,
        help="Expected deterministic CPU instruction total for the overworld scenario.",
    )
    parser.add_argument(
        "--expected-overworld-audio-cpu-instr",
        type=int,
        help="Expected deterministic CPU instruction total for the overworld-audio scenario.",
    )
    parser.add_argument(
        "--expected-ppu-frames",
        type=int,
        help="Expected deterministic PPU frame delta for both Crystal headless scenarios.",
    )
    parser.add_argument(
        "--window-profile-log",
        type=Path,
        help="Optional captured Crystal window-profile log to validate host/live metrics.",
    )
    parser.add_argument(
        "--run-live-window",
        action="store_true",
        help="Open a live Tkinter window, run the scripted Crystal saved-game path, and validate profiles.",
    )
    parser.add_argument(
        "--skip-headless",
        action="store_true",
        help="Skip headless benchmark scenarios; useful when validating only a captured or live window profile.",
    )
    parser.add_argument(
        "--window-python",
        default=sys.executable,
        help="Python executable used for --run-live-window. Use system Python for Tkinter.",
    )
    parser.add_argument(
        "--live-warmup-frames",
        type=int,
        default=DEFAULT_WARMUP_FRAMES,
        help="Live frames to run before the measured live-window profile slice.",
    )
    parser.add_argument("--live-frames", type=int, default=DEFAULT_PROFILE_FRAMES)
    parser.add_argument("--live-profile-interval", type=int, default=60)
    parser.add_argument("--window-min-fps", type=float, default=45.0)
    parser.add_argument("--window-min-audio-queue-ms", type=float, default=30.0)
    parser.add_argument(
        "--window-ignore-initial",
        type=int,
        help=(
            "Ignore this many initial live/window-profile windows for FPS and queue checks. "
            "Defaults to the live warmup window count plus two startup windows."
        ),
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


def run_scenario(
    scenario: CrystalScenario,
    *,
    rom: Path,
    save_file: Path,
    button_script: str,
    warmup_frames: int,
    profile_frames: int,
    window_size: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-B",
        str(ROOT / "scripts" / "benchmark_pokemon_crystal_overworld.py"),
        "--rom",
        str(rom),
        "--save-file",
        str(save_file),
        "--button-script",
        button_script,
        "--warmup-frames",
        str(warmup_frames),
        "--profile-frames",
        str(profile_frames),
        "--window-size",
        str(window_size),
    ]
    command.extend(scenario.extra_args)

    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    profile_lines = [line for line in output.splitlines() if line.startswith(PROFILE_PREFIX + " ")]
    if result.returncode or not profile_lines:
        raise RuntimeError(
            f"{scenario.name} profile failed with return code {result.returncode}\n{output}"
        )
    metrics = parse_profile_line(profile_lines[-1], PROFILE_PREFIX)
    metrics["scenario"] = scenario.name
    metrics["command"] = command
    metrics["windows"] = [
        parse_profile_line(line, WINDOW_PREFIX)
        for line in output.splitlines()
        if line.startswith(WINDOW_PREFIX + " ")
    ]
    return metrics


def expected_cpu_instr_for_scenario(args: argparse.Namespace, scenario_name: str) -> int | None:
    if scenario_name == "overworld":
        return args.expected_overworld_cpu_instr
    if scenario_name == "overworld-audio":
        return args.expected_overworld_audio_cpu_instr
    raise ValueError(f"unknown scenario: {scenario_name}")


def min_fps_for_scenario(args: argparse.Namespace, scenario_name: str) -> float:
    if scenario_name == "overworld":
        return args.min_fps_overworld
    if scenario_name == "overworld-audio":
        return args.min_fps_overworld_audio
    raise ValueError(f"unknown scenario: {scenario_name}")


def evaluate_crystal_scenario_metrics(
    scenario: CrystalScenario,
    metrics: dict[str, Any],
    *,
    min_fps: float,
    min_window_fps: float,
    instruction_tolerance: int,
    cycle_tolerance: int,
    expected_cpu_instr: int | None,
    expected_ppu_frames: int | None | object = DEFAULT_EXPECTED_PPU_FRAMES,
    expected_frames: int | None = None,
    expected_cpu_cycles: int | None | object = DEFAULT_EXPECTED_CYCLES,
) -> list[str]:
    failures: list[str] = []
    fps = float(metric_number(metrics, "run_fps"))
    if fps < min_fps:
        failures.append(f"{scenario.name}: run_fps {fps:.2f} < {min_fps:.2f}")

    frames = int(metric_number(metrics, "frames"))
    if expected_frames is None:
        expected_frames = scenario.expected_frames
    if frames != expected_frames:
        failures.append(f"{scenario.name}: frames {frames} != {expected_frames}")

    if expected_ppu_frames is DEFAULT_EXPECTED_PPU_FRAMES:
        expected_ppu_frames = scenario.expected_ppu_frames
    if expected_ppu_frames is not None:
        ppu_frames = int(metric_number(metrics, "ppu_frames"))
        if ppu_frames != expected_ppu_frames:
            failures.append(f"{scenario.name}: ppu_frames {ppu_frames} != {expected_ppu_frames}")

    if expected_cpu_instr is not None:
        cpu_instr = int(metric_number(metrics, "cpu_instr"))
        if abs(cpu_instr - expected_cpu_instr) > instruction_tolerance:
            failures.append(
                f"{scenario.name}: cpu_instr {cpu_instr} drifted from "
                f"{expected_cpu_instr} by more than {instruction_tolerance}"
            )

    if expected_cpu_cycles is DEFAULT_EXPECTED_CYCLES:
        expected_cpu_cycles = scenario.expected_cpu_cycles
    if expected_cpu_cycles is not None:
        cpu_cycles = int(metric_number(metrics, "cpu_cycles"))
        if abs(cpu_cycles - expected_cpu_cycles) > cycle_tolerance:
            failures.append(
                f"{scenario.name}: cpu_cycles {cpu_cycles} drifted from "
                f"{expected_cpu_cycles} by more than {cycle_tolerance}"
            )

    audio_output = int(metric_number(metrics, "audio_output"))
    if audio_output != int(scenario.audio_enabled):
        failures.append(f"{scenario.name}: audio_output {audio_output} != {int(scenario.audio_enabled)}")
    if int(metric_number(metrics, "rtc_halted")) != 1:
        failures.append(f"{scenario.name}: rtc_halted={metrics['rtc_halted']}")
    if scenario.audio_enabled:
        if int(metric_number(metrics, "apu_samples")) <= 0:
            failures.append(f"{scenario.name}: audio enabled but no APU samples produced")
        for key in ("apu_dropped_samples", "audio_underruns", "audio_dropped"):
            if key in metrics and int(metric_number(metrics, key)) != 0:
                failures.append(f"{scenario.name}: {key}={metrics[key]}")
    for index, window in enumerate(metrics.get("windows", [])):
        window_fps = float(metric_number(window, "run_fps"))
        if window_fps < min_window_fps:
            failures.append(
                f"{scenario.name}: window[{index}] run_fps {window_fps:.2f} < {min_window_fps:.2f}"
            )
    if not metrics.get("windows"):
        failures.append(f"{scenario.name}: no {WINDOW_PREFIX} lines found")
    return failures


def parse_window_profile_text(output: str) -> list[dict[str, Any]]:
    return [
        parse_profile_line(line, "window-profile")
        for line in output.splitlines()
        if line.startswith("window-profile ")
    ]


def live_window_ignore_initial(args: argparse.Namespace) -> int:
    if args.window_ignore_initial is not None:
        return args.window_ignore_initial
    if args.live_profile_interval <= 0:
        return 2
    warmup_windows = args.live_warmup_frames // args.live_profile_interval
    return warmup_windows + 2


def run_live_window_profiles(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[str], str]:
    total_live_frames = args.live_warmup_frames + args.live_frames
    with tempfile.TemporaryDirectory(prefix="gbemu-crystal-live-") as temp_dir:
        live_save_file = Path(temp_dir) / args.save_file.name
        shutil.copy2(args.save_file, live_save_file)
        rtc_file = Path(f"{args.save_file}.rtc")
        if rtc_file.exists():
            shutil.copy2(rtc_file, Path(f"{live_save_file}.rtc"))
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
            str(total_live_frames),
            "--profile-window",
            "--profile-window-interval",
            str(args.live_profile_interval),
            "--save-file",
            str(live_save_file),
            "--button-script",
            args.button_script,
        ]
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    profiles = parse_window_profile_text(output)
    failures: list[str] = []
    if result.returncode:
        failures.append(f"live-window: command failed with return code {result.returncode}")
    if not profiles:
        failures.append("live-window: no window-profile lines found")
    return profiles, failures, output


def main() -> int:
    args = parse_args()
    if args.instruction_tolerance < 0:
        raise SystemExit("--instruction-tolerance must be non-negative")
    if args.cycle_tolerance < 0:
        raise SystemExit("--cycle-tolerance must be non-negative")
    if args.warmup_frames < 0:
        raise SystemExit("--warmup-frames must be non-negative")
    if args.profile_frames < 1:
        raise SystemExit("--profile-frames must be positive")
    if args.window_size < 1:
        raise SystemExit("--window-size must be positive")
    if args.window_ignore_initial is not None and args.window_ignore_initial < 0:
        raise SystemExit("--window-ignore-initial must be non-negative")
    if args.live_warmup_frames < 0:
        raise SystemExit("--live-warmup-frames must be non-negative")
    if args.live_frames < 1:
        raise SystemExit("--live-frames must be positive")
    if args.live_profile_interval < 1:
        raise SystemExit("--live-profile-interval must be positive")

    scenario_names = args.scenario or list(SCENARIOS)
    all_failures: list[str] = []
    results: dict[str, Any] = {
        "headless": {},
        "window_log": None,
        "live_window": None,
        "failures": all_failures,
    }

    if not args.skip_headless:
        for scenario_name in scenario_names:
            scenario = SCENARIOS[scenario_name]
            print(f"== Pokemon Crystal performance scenario: {scenario.name} ==")
            try:
                fixed_default_slice = (
                    args.warmup_frames == DEFAULT_WARMUP_FRAMES
                    and args.profile_frames == scenario.expected_frames
                )
                expected_cpu_instr = expected_cpu_instr_for_scenario(args, scenario.name)
                if expected_cpu_instr is None and fixed_default_slice:
                    expected_cpu_instr = scenario.expected_cpu_instr
                expected_ppu_frames = args.expected_ppu_frames
                if expected_ppu_frames is None and fixed_default_slice:
                    expected_ppu_frames = scenario.expected_ppu_frames
                metrics = run_scenario(
                    scenario,
                    rom=args.rom,
                    save_file=args.save_file,
                    button_script=args.button_script,
                    warmup_frames=args.warmup_frames,
                    profile_frames=args.profile_frames,
                    window_size=args.window_size,
                )
                failures = evaluate_crystal_scenario_metrics(
                    scenario,
                    metrics,
                    min_fps=min_fps_for_scenario(args, scenario.name),
                    min_window_fps=args.min_window_fps,
                    expected_frames=args.profile_frames,
                    expected_cpu_cycles=(
                        scenario.expected_cpu_cycles
                        if fixed_default_slice
                        else None
                    ),
                    instruction_tolerance=args.instruction_tolerance,
                    cycle_tolerance=args.cycle_tolerance,
                    expected_cpu_instr=expected_cpu_instr,
                    expected_ppu_frames=expected_ppu_frames,
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
                    f"worst_window_fps={metrics['worst_window_fps']}",
                    f"frames={metrics['frames']}",
                    f"cpu_instr={metrics['cpu_instr']}",
                    f"cpu_cycles={metrics['cpu_cycles']}",
                )

    if args.window_profile_log is not None:
        print("== Pokemon Crystal window-profile log ==")
        profiles = parse_window_profile_log(args.window_profile_log)
        ignore_initial = live_window_ignore_initial(args)
        failures = evaluate_window_profiles(
            profiles,
            min_fps=args.window_min_fps,
            min_audio_queue_ms=args.window_min_audio_queue_ms,
            ignore_initial=ignore_initial,
        )
        results["window_log"] = {
            "log": str(args.window_profile_log),
            "profiles": profiles,
            "ignore_initial": ignore_initial,
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
        print("== Pokemon Crystal live window-profile capture ==")
        profiles, command_failures, output = run_live_window_profiles(args)
        profile_failures = []
        if profiles:
            ignore_initial = live_window_ignore_initial(args)
            profile_failures = evaluate_window_profiles(
                profiles,
                min_fps=args.window_min_fps,
                min_audio_queue_ms=args.window_min_audio_queue_ms,
                ignore_initial=ignore_initial,
            )
        failures = command_failures + profile_failures
        results["live_window"] = {
            "profiles": profiles,
            "ignore_initial": live_window_ignore_initial(args),
            "warmup_frames": args.live_warmup_frames,
            "measured_frames": args.live_frames,
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
        print(f"FAIL Pokemon Crystal performance gate: {len(all_failures)} failure(s)")
        return 1
    print("PASS Pokemon Crystal performance gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
