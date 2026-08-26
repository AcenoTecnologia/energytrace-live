import os
import sys
import time
import signal
import platform
import subprocess
import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime


# =========================================================
# CONFIGURATION
# =========================================================

IS_WINDOWS = platform.system().lower().startswith("win")

# Folder where this run_energytrace_live.py file is located
SCRIPT_DIR = Path(__file__).resolve().parent

# Project folder
BASE_DIR = SCRIPT_DIR

# Live plot script located in the same folder as this runner
PLOT_SCRIPT = SCRIPT_DIR / "plot_energytrace_live.py"

# ---------------------------------------------------------
# CCS / STUNE paths
# ---------------------------------------------------------

if IS_WINDOWS:
    DEFAULT_CCS = Path(r"C:\ti\ccs2051")
    DEFAULT_CONFIG = Path(
        r"C:\ti\workspace_ccstheia\zed_sw_CC1352P_2_LAUNCHXL_tirtos7_ticlang\targetConfigs\CC1352P1F3.ccxml"
    )
    STUNE_EXE_NAME = "stune.exe"
else:
    DEFAULT_CCS = Path.home() / "ti" / "ccs2051"
    DEFAULT_CONFIG = (
        Path.home()
        / "ti"
        / "workspace_ccstheia"
        / "zed_sw_CC1352P_2_LAUNCHXL_tirtos7_ticlang"
        / "targetConfigs"
        / "CC1352P1F3.ccxml"
    )
    STUNE_EXE_NAME = "stune"

DEFAULT_STUNE_DIR = DEFAULT_CCS / "ccs_base" / "emulation" / "analysis" / "bin" / "stune"
STUNE_EXE = DEFAULT_STUNE_DIR / STUNE_EXE_NAME

# ---------------------------------------------------------
# EnergyTrace settings
# ---------------------------------------------------------

# Device settings
DEFAULT_DEVICE = "CC1352"
DEFAULT_VOLTAGE = 3.3
DEFAULT_BATTERY_CAPACITY_MAH = 220.0
DEFAULT_NO_PLOT = 0

# Duration in ms. Use 0 for indefinite capture.
DEFAULT_DURATION_MS = 0.0

# Python live plot settings
DEFAULT_DOWNSAMPLE_MS = 0.01
DEFAULT_REFRESH_MS = 10
DEFAULT_WINDOW_S = 0.0
DEFAULT_MAX_POINTS = 2000

# EnergyTrace mode
DEFAULT_ET_MODE = "et"

DEFAULT_SETTINGS = {
    "ccs": str(DEFAULT_CCS),
    "target_config": str(DEFAULT_CONFIG),
    "device": DEFAULT_DEVICE,
    "voltage": DEFAULT_VOLTAGE,
    "battery_capacity": DEFAULT_BATTERY_CAPACITY_MAH,
    "no_plot": DEFAULT_NO_PLOT,
    "duration": DEFAULT_DURATION_MS,
    "downsample": DEFAULT_DOWNSAMPLE_MS,
    "refresh": DEFAULT_REFRESH_MS,
    "window": DEFAULT_WINDOW_S,
    "max_points": DEFAULT_MAX_POINTS,
    "et_mode": DEFAULT_ET_MODE,
}

# =========================================================
# HELPERS
# =========================================================

def make_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def resolve_path(path_value: str) -> Path:
    return Path(path_value).expanduser().resolve()

SETTING_TYPES = {
    "ccs": str,
    "target_config": str,
    "device": str,
    "voltage": float,
    "battery_capacity": float,
    "no_plot": int,
    "duration": float,
    "downsample": float,
    "refresh": int,
    "window": float,
    "max_points": int,
    "et_mode": str,
}


def normalize_key(key: str) -> str:
    return key.strip().lower().replace("-", "_")


def normalize_settings(raw_settings: dict) -> dict:
    settings = {}

    for key, value in raw_settings.items():
        key = normalize_key(key)

        if key not in SETTING_TYPES:
            print(f"Warning: unknown setting ignored: {key}")
            continue

        if value is None:
            continue

        value_type = SETTING_TYPES[key]

        try:
            settings[key] = value_type(value)
        except Exception:
            print(f"Warning: invalid value ignored for setting '{key}': {value}")

    return settings


def load_json_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"Could not read JSON settings file: {path}\n{exc}")

    if not isinstance(data, dict):
        raise SystemExit("JSON settings file must contain an object at the root.")

    return normalize_settings(data)


def load_xml_settings(path: Path) -> dict:
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception as exc:
        raise SystemExit(f"Could not read XML settings file: {path}\n{exc}")

    raw_settings = {}

    for child in root:
        key = child.tag
        value = child.text.strip() if child.text else ""
        raw_settings[key] = value

    return normalize_settings(raw_settings)


def load_settings_file(settings_file: str) -> dict:
    if not settings_file:
        return {}

    path = Path(settings_file).expanduser().resolve()

    if not path.exists():
        raise SystemExit(f"Settings file not found: {path}")

    suffix = path.suffix.lower()

    if suffix == ".json":
        return load_json_settings(path)

    if suffix == ".xml":
        return load_xml_settings(path)

    raise SystemExit("Unsupported settings file format. Use .json or .xml.")

def build_runtime_settings(args) -> dict:
    settings = DEFAULT_SETTINGS.copy()

    file_settings = load_settings_file(args.settings_file)
    settings.update(file_settings)

    cli_values = {
        "ccs": args.ccs,
        "target_config": args.target_config,
        "device": args.device,
        "voltage": args.voltage,
        "battery_capacity": args.battery_capacity,
        "no_plot": args.no_plot,
        "duration": args.duration,
        "downsample": args.downsample,
        "refresh": args.refresh,
        "window": args.window,
        "max_points": args.max_points,
        "et_mode": args.et_mode,
    }

    for key, value in cli_values.items():
        if value is not None:
            settings[key] = value

    return normalize_settings(settings)

def get_ccs_root(ccs_path: Path) -> Path:
    ccs_path = ccs_path.expanduser().resolve()

    if (ccs_path / "ccs_base").exists():
        return ccs_path

    if (ccs_path / "ccs" / "ccs_base").exists():
        return ccs_path / "ccs"

    return ccs_path


def get_stune_paths(ccs_path: Path):
    ccs_root = get_ccs_root(ccs_path)

    stune_dir = ccs_root / "ccs_base" / "emulation" / "analysis" / "bin" / "stune"

    if IS_WINDOWS:
        stune_exe = stune_dir / "stune.exe"
    else:
        stune_exe = stune_dir / "stune"

    return ccs_root, stune_dir, stune_exe


def format_duration(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))

    return str(value)


def stune_path(path: Path) -> str:
    path_str = str(path.resolve()).replace("\\", "/")

    if " " in path_str:
        return f'"{path_str}"'

    return path_str

def build_environment(ccs: Path, stune_dir: Path) -> dict:
    env = os.environ.copy()

    extra_paths = [
        stune_dir,
        ccs / "ccs_base" / "DebugServer" / "bin",
        ccs / "ccs_base" / "common" / "bin",
        ccs / "ccs_base" / "common" / "uscif",
        ccs / "ccs_base" / "common" / "uscif" / "xds110",
    ]

    existing_path = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(str(p) for p in extra_paths) + os.pathsep + existing_path

    if not IS_WINDOWS:
        extra_lib_paths = [
            stune_dir,
            ccs / "ccs_base" / "DebugServer" / "bin",
            ccs / "ccs_base" / "common" / "bin",
            ccs / "ccs_base" / "common" / "uscif",
            ccs / "ccs_base" / "common" / "uscif" / "xds110",
            ccs / "ccs_base" / "emulation" / "analysis" / "bin"
        ]

        existing_ld_library_path = env.get("LD_LIBRARY_PATH", "")

        if existing_ld_library_path:
            env["LD_LIBRARY_PATH"] = (
                os.pathsep.join(str(p) for p in extra_lib_paths)
                + os.pathsep
                + existing_ld_library_path
            )
        else:
            env["LD_LIBRARY_PATH"] = os.pathsep.join(str(p) for p in extra_lib_paths)

    return env


def check_paths(ccs: Path, config: Path, stune_dir: Path, stune_exe: Path) -> None:
    errors = []

    if not ccs.exists():
        errors.append(f"CCS folder not found: {ccs}")

    if not stune_dir.exists():
        errors.append(f"stune folder not found: {stune_dir}")

    if not stune_exe.exists():
        errors.append(f"stune executable not found: {stune_exe}")

    if not config.exists():
        errors.append(f"target config not found: {config}")

    if not PLOT_SCRIPT.exists():
        errors.append(f"live plot script not found: {PLOT_SCRIPT}")

    if errors:
        print()
        print("=========================================================")
        print("CONFIGURATION ERROR")
        print("=========================================================")

        for error in errors:
            print(error)

        print("=========================================================")
        print()
        raise SystemExit(1)

def run_stune_commands(
    command_text: str,
    log_path: Path,
    env: dict,
    stune_exe: Path,
    work_dir: Path,
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with open(log_path, "w", encoding="utf-8", errors="ignore") as log_file:
        result = subprocess.run(
            [str(stune_exe)],
            input=command_text,
            text=True,
            cwd=str(work_dir),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            check=False,
        )

    return result.returncode


def start_stune_process(
    command_text: str,
    log_path: Path,
    env: dict,
    stune_exe: Path,
    work_dir: Path,
) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log_file = open(log_path, "w", encoding="utf-8", errors="ignore")

    creationflags = 0

    if IS_WINDOWS:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [str(stune_exe)],
        stdin=subprocess.PIPE,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(work_dir),
        env=env,
        text=True,
        creationflags=creationflags,
    )

    try:
        proc.stdin.write(command_text)
        proc.stdin.flush()
        proc.stdin.close()
    except Exception:
        pass

    proc._log_file = log_file

    return proc


def stop_process(proc: subprocess.Popen, name: str = "process") -> None:
    if proc is None:
        return

    if proc.poll() is not None:
        return

    print(f"Stopping {name}...")

    try:
        if IS_WINDOWS:
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception:
                proc.terminate()
        else:
            proc.send_signal(signal.SIGINT)

        proc.wait(timeout=5)

    except Exception:
        print(f"{name} did not stop gracefully. Killing...")
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

    log_file = getattr(proc, "_log_file", None)

    if log_file is not None:
        try:
            log_file.close()
        except Exception:
            pass

def log_has_no_connected_device(log_path: Path) -> bool:
    if not log_path.exists():
        return False

    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False

    text_lower = text.lower()

    if "failed to open debug probe handle" in text_lower:
        return True

    if "-260" in text_lower:
        return True

    return False

def print_no_device_error(log_path: Path) -> None:
    print()
    print("=========================================================")
    print("ERROR: NO DEVICE OR DEBUG PROBE CONNECTED")
    print("=========================================================")
    print()
    print("stune could not open the debug probe handle.")
    print()
    print("Possible causes:")
    print("- the LaunchPad/XDS110 is not connected;")
    print("- the USB cable is disconnected or damaged;")
    print("- another program is using the XDS110;")
    print("- CCS Debug Session is still active;")
    print("- the target configuration file is incorrect;")
    print("- the selected device does not match the connected board.")
    print()
    print("Suggested actions:")
    print("1. Connect the LaunchPad/XDS110.")
    print("2. Close CCS debug sessions.")
    print("3. Unplug and reconnect the USB cable.")
    print("4. Run the script again.")
    print()
    print(f"Log file: {log_path}")
    print("=========================================================")
    print()

def log_has_bad_data(log_path: Path) -> bool:
    if not log_path.exists():
        return False

    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False

    bad_patterns = [
        "BAD DATA DETECTED",
        "Bad energy trace data",
    ]

    text_lower = text.lower()

    return any(pattern.lower() in text_lower for pattern in bad_patterns)


def wait_for_log(log_path: Path, stune_proc: subprocess.Popen, timeout_s: float = 5.0) -> bool:
    start = time.monotonic()

    while time.monotonic() - start < timeout_s:
        if log_path.exists():
            return True

        if stune_proc.poll() is not None:
            return False

        time.sleep(0.2)

    return False


def stune_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


# =========================================================
# STUNE COMMAND BUILDERS
# =========================================================

def build_warmup_commands(ts: str, device: str, config: Path, et_mode: str) -> str:
    lines = [
        "disconnect",
        f"connect --config={stune_path(config)} --device={device} xds",
    ]

    warmup_csv_name = f"warmup_{ts}.csv"

    lines.append(f"energytrace --out={warmup_csv_name} --duration=10 {et_mode}")

    lines.extend([
        "disconnect",
        "quit",
        "",
    ])

    return "\n".join(lines)


def build_capture_commands(
    csv_name: str,
    device: str,
    config: Path,
    duration_ms: float,
    et_mode: str,
) -> str:
    lines = [
        "disconnect",
        f"connect --config={stune_path(config)} --device={device} xds",
        f"energytrace --out={csv_name} --duration={format_duration(duration_ms)} {et_mode}",
        "disconnect",
        "quit",
        "",
    ]

    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser(
        description="EnergyTrace LiveMode runner using TI stune."
    )

    parser.add_argument(
        "--settings",
        "--config-file",
        dest="settings_file",
        default="",
        help="Path to a JSON or XML settings file."
    )

    parser.add_argument(
        "--ccs",
        default=None,
        help="Path to the CCS installation folder."
    )

    parser.add_argument(
        "--config",
        "--target-config",
        dest="target_config",
        default=None,
        help="Path to the .ccxml target configuration file."
    )

    parser.add_argument(
        "--device",
        default=None,
        help="Device name passed to stune. Example: CC1352"
    )

    parser.add_argument(
        "--voltage",
        type=float,
        default=None,
        help="Fallback voltage used if the CSV does not contain voltage data."
    )

    parser.add_argument(
        "--battery-capacity",
        type=float,
        default=None,
        help="Battery capacity in mAh used to estimate battery lifetime."
    )

    parser.add_argument(
        "--no-plot",
        type=int,
        choices=[0, 1],
        default=None,
        help="Disable the live plot while keeping capture and analysis enabled. Use 1 to disable."
    )

    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Capture duration in milliseconds. Use 0 for indefinite capture."
    )

    parser.add_argument(
        "--downsample",
        type=float,
        default=None,
        help="Downsample window in milliseconds used by the live plot."
    )

    parser.add_argument(
        "--refresh",
        type=int,
        default=None,
        help="Live plot refresh time in milliseconds."
    )

    parser.add_argument(
        "--window",
        type=float,
        default=None,
        help="Visible plot window in seconds. Use 0 to show the full capture."
    )

    parser.add_argument(
        "--max-points",
        type=int,
        default=None,
        help="Maximum number of points displayed in the live plot."
    )

    parser.add_argument(
        "--et-mode",
        default=None,
        help="EnergyTrace mode passed to stune. Usually: et"
    )

    return parser.parse_args()

# =========================================================
# MAIN
# =========================================================

def main() -> int:
    args = parse_args()
    settings = build_runtime_settings(args)

    ccs_input = resolve_path(settings["ccs"])
    config = resolve_path(settings["target_config"])

    ccs, stune_dir, stune_exe = get_stune_paths(ccs_input)

    device = settings["device"]
    voltage = settings["voltage"]
    battery_capacity_mAh = settings["battery_capacity"]
    no_plot = settings["no_plot"] == 1
    duration_ms = settings["duration"]
    downsample_ms = settings["downsample"]
    refresh_ms = settings["refresh"]
    window_s = settings["window"]
    max_points = settings["max_points"]
    et_mode = settings["et_mode"]

    check_paths(
        ccs=ccs,
        config=config,
        stune_dir=stune_dir,
        stune_exe=stune_exe,
    )

    env = build_environment(
        ccs=ccs,
        stune_dir=stune_dir,
    )

    ts = make_timestamp()

    out_dir = SCRIPT_DIR / "out" / ts
    csv_dir = out_dir / "csv"
    log_dir = out_dir / "log"
    cmd_dir = out_dir / "cmd"

    csv_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    cmd_dir.mkdir(parents=True, exist_ok=True)

    csv_path = csv_dir / f"energytrace_{ts}.csv"
    stop_file = cmd_dir / f"stop_live_{ts}.flag"

    warmup_log = log_dir / f"stune_warmup_log_{ts}.txt"
    capture_log = log_dir / f"stune_log_{ts}.txt"

    warmup_cmd_file = cmd_dir / f"stune_warmup_cmd_{ts}.txt"
    capture_cmd_file = cmd_dir / f"stune_cmd_{ts}.txt"

    if stop_file.exists():
        stop_file.unlink()

    print()
    print("=========================================================")
    print("EnergyTrace LiveMode Runner")
    print("=========================================================")
    print(f"OS:              {platform.system()}")
    print()
    print(f"CCS:             {ccs}")
    print(f"Config:          {config}")
    print()
    print(f"Device:          {device}")
    print(f"Voltage (V):     {voltage}")

    if battery_capacity_mAh > 0:
        print(f"Battery (mAh):   {battery_capacity_mAh}")
    else:
        print(f"Battery (mAh):   not set (0)")

    print(f"Plot:            {'disabled' if no_plot else 'enabled'}")

    if duration_ms > 0: print(f"Duration (ms):   {duration_ms}")
    else: print(f"Duration (ms):   indefinite (0)")

    print(f"Refresh (ms):    {refresh_ms}")

    if window_s > 0: print(f"Window (s):      {window_s}")
    else: print(f"Window (s):      full capture (0)")

    print(f"ET mode:         {et_mode}")
    print()
    print(f"CSV:             {csv_path}")
    print(f"Log:             {capture_log}")
    print("=========================================================")
    print()

    # -----------------------------------------------------
    # Warm-up
    # -----------------------------------------------------

    print("Running EnergyTrace warm-up...")

    warmup_commands = build_warmup_commands(
        ts=ts,
        device=device,
        config=config,
        et_mode=et_mode,
    )

    warmup_cmd_file.write_text(warmup_commands, encoding="utf-8")

    run_stune_commands(
        command_text=warmup_commands,
        log_path=warmup_log,
        env=env,
        stune_exe=stune_exe,
        work_dir=csv_dir,
    )

    if log_has_no_connected_device(warmup_log):
        print_no_device_error(warmup_log)
        return 1

    print("Warm-up finished.")
    print(f"Warm-up log: {warmup_log}")
    print()

    # BAD DATA in warm-up is intentionally ignored.
    time.sleep(2)

    # -----------------------------------------------------
    # Real capture
    # -----------------------------------------------------

    print("Starting real EnergyTrace capture...")

    capture_commands = build_capture_commands(
        csv_name=csv_path.name,
        device=device,
        config=config,
        duration_ms=duration_ms,
        et_mode=et_mode,
    )

    capture_cmd_file.write_text(capture_commands, encoding="utf-8")

    stune_proc = start_stune_process(
        command_text=capture_commands,
        log_path=capture_log,
        env=env,
        stune_exe=stune_exe,
        work_dir=csv_dir,
    )

    if not wait_for_log(capture_log, stune_proc, timeout_s=5.0):
        print()
        print("=========================================================")
        print("ERROR: stune log was not generated")
        print("=========================================================")
        print(f"Expected log: {capture_log}")
        print("=========================================================")
        print()
        stop_process(stune_proc, "stune")
        return 1

    time.sleep(0.5)

    if log_has_no_connected_device(capture_log):
        print_no_device_error(capture_log)
        stop_process(stune_proc, "stune")
        return 1

    if log_has_bad_data(capture_log):
        print()
        print("=========================================================")
        print("ERROR: BAD DATA DETECTED IN REAL CAPTURE")
        print("=========================================================")
        print(f"Capture log: {capture_log}")
        print(f"Warm-up log: {warmup_log}")
        print()
        print("Suggestions:")
        print("- Disconnect and reconnect the XDS110 debugger")
        print("- Close CCS debug sessions")
        print("- Check if another program is using the XDS110")
        print("=========================================================")
        print()
        stop_process(stune_proc, "stune")
        return 1

    # -----------------------------------------------------
    # Live plot
    # -----------------------------------------------------

    print("Starting Python LivePlot...")

    live_cmd = [
        sys.executable,
        str(PLOT_SCRIPT),
        "--folder",
        str(csv_dir),
        "--prefix",
        f"energytrace_{ts}",
        "--voltage",
        str(voltage),
        "--battery-capacity",
        str(battery_capacity_mAh),
        "--no-plot",
        str(int(no_plot)),
        "--refresh-ms",
        str(refresh_ms),
        "--downsample-ms",
        str(downsample_ms),
        "--window-s",
        str(window_s),
        "--plot-max-points",
        str(max_points),
        "--stop-file",
        str(stop_file),
        "--duration-ms",
        str(duration_ms),
    ]

    live_proc = subprocess.Popen(
        live_cmd,
        cwd=str(SCRIPT_DIR),
        env=env,
    )

    if no_plot:
        print("LivePlot disabled. Capture and analysis are still running.")
        print("Press q or Esc in this terminal to stop the capture.")
        print()
    else:
        print("LivePlot started.")
        print()
        print("LivePlot commands:")
        print("  q or Esc      -> stop capture")
        print("  t             -> total until current time")
        print("  left drag     -> interval analysis")
        print("  right click   -> point analysis")
        print()

    # -----------------------------------------------------
    # Monitor
    # -----------------------------------------------------

    try:
        while True:
            if log_has_no_connected_device(capture_log):
                print_no_device_error(capture_log)
                stop_process(stune_proc, "stune")
                break

            if log_has_bad_data(capture_log):
                print()
                print("=========================================================")
                print("ERROR: BAD DATA DETECTED DURING REAL CAPTURE")
                print("=========================================================")
                print(f"Capture log: {capture_log}")
                print("Try disconnecting and reconnecting the device.")
                print("=========================================================")
                print()
                stop_process(stune_proc, "stune")
                break

            if stop_file.exists():
                print()
                print("Stop requested by LivePlot.")
                stop_process(stune_proc, "stune")
                break

            if stune_proc.poll() is not None:
                print()
                print("stune process finished.")

                if no_plot and duration_ms > 0 and live_proc.poll() is None:
                    time.sleep(1)
                    continue

                break

            if not no_plot and live_proc.poll() is not None:
                print()
                print("LivePlot was closed.")
                stop_process(stune_proc, "stune")
                break

            time.sleep(1)

    except KeyboardInterrupt:
        print()
        print("Keyboard interrupt received.")
        stop_process(stune_proc, "stune")

        if live_proc.poll() is None:
            stop_process(live_proc, "LivePlot")

        return 130

    # -----------------------------------------------------
    # Keep plot open if it is still running
    # -----------------------------------------------------

    if no_plot and live_proc.poll() is None:
        stop_process(live_proc, "analysis")
    elif live_proc.poll() is None:
        print()
        print("Capture stopped/finished.")
        print("Close the LivePlot window to finish the runner.")
        print()

        try:
            live_proc.wait()
        except KeyboardInterrupt:
            stop_process(live_proc, "LivePlot")

    print()
    print("=========================================================")
    print("EnergyTrace LiveMode finished")
    print("=========================================================")
    print(f"CSV:         {csv_path}")
    print(f"Capture log: {capture_log}")
    print(f"Warm-up log: {warmup_log}")
    print("=========================================================")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())