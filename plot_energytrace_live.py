import argparse
import io
import os
import re
import select
import sys
import time
from pathlib import Path

import matplotlib
from matplotlib.backend_bases import MouseButton
from typing import Any

if os.name == "nt":
    import msvcrt

NO_PLOT_REQUESTED = any(
    argument == "--no-plot=1"
    or (
        argument == "--no-plot"
        and index + 1 < len(sys.argv)
        and sys.argv[index + 1] == "1"
    )
    for index, argument in enumerate(sys.argv)
)
matplotlib.use("Agg" if NO_PLOT_REQUESTED else "QtAgg")

# Remove os atalhos padrao do Matplotlib que fecham a janela com q
matplotlib.rcParams["keymap.quit"] = []
matplotlib.rcParams["keymap.quit_all"] = []

import matplotlib.pyplot as plt

import numpy as np
import pandas as pd

from matplotlib.widgets import SpanSelector

def np_trapezoid(y, x=None, dx=1.0, axis=-1):
    trapezoid = getattr(np, "trapezoid", None)

    if trapezoid is not None:
        return trapezoid(y, x=x, dx=dx, axis=axis)

    return np.trapz(y, x=x, dx=dx, axis=axis)

# =========================================================
# FUNÇÕES AUXILIARES
# =========================================================

# Configura a janela do matplotlib para ficar centralizada na tela e com tamanho definido
def configure_window(fig, width=1200, height=650):
    try:
        manager: Any = plt.get_current_fig_manager()
        window = manager.window

        screen = window.screen().availableGeometry()

        # Garante que a janela não seja maior que a área útil da tela
        width = min(width, screen.width() - 100)
        height = min(height, screen.height() - 100)

        # Centraliza a janela
        x = screen.x() + (screen.width() - width) // 2
        y = screen.y() + (screen.height() - height) // 2

        window.resize(width, height)
        window.move(x, y)

        window.raise_()
        window.activateWindow()

    except Exception:
        pass

# Limita o número de pontos plotados, preservando os picos da corrente.
def limit_plot_points_peak_preserving(x, current, max_points=10000):
    if max_points < 2:
        raise ValueError("max_points must be at least 2.")

    n = len(x)

    if n <= max_points:
        return x, current

    x_arr = np.asarray(x)
    current_arr = np.asarray(current)

    # Cada bloco guarda até 2 pontos: mínimo e máximo da corrente.
    n_bins = max_points // 2

    indices = np.arange(n)
    chunks = np.array_split(indices, n_bins)

    keep_indices = []

    for chunk in chunks:
        if len(chunk) == 0:
            continue

        local_current = current_arr[chunk]

        idx_min = chunk[np.argmin(local_current)]
        idx_max = chunk[np.argmax(local_current)]

        keep_indices.append(idx_min)
        keep_indices.append(idx_max)

    keep_indices = np.array(sorted(set(keep_indices)))

    return (
        x_arr[keep_indices],
        current_arr[keep_indices],
    )

# Função para ordenar os arquivos CSV
def file_order_key(path, prefix):
    stem = path.stem

    if stem == prefix:
        return -1

    pattern = re.escape(prefix) + r"_(\d+)$"
    match = re.match(pattern, stem)

    if match:
        return int(match.group(1))

    return 10**12

# Funções auxiliares para encontrar colunas no CSV
def normalize_col(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def find_column(columns, required_words):
    for col in columns:
        norm = normalize_col(col)

        ok = True

        for word in required_words:
            if word not in norm:
                ok = False
                break

        if ok:
            return col

    return None


def format_battery_lifetime(lifetime_h):
    total_minutes = max(0, int(lifetime_h * 60))
    minutes_per_hour = 60
    hours_per_day = 24
    days_per_month = 30
    days_per_year = 365

    if total_minutes < minutes_per_hour:
        return f"{total_minutes}min"

    total_hours, minutes = divmod(total_minutes, minutes_per_hour)

    if total_hours < hours_per_day:
        return f"{total_hours}h {minutes}min"

    total_days, hours = divmod(total_hours, hours_per_day)

    if total_days < days_per_month:
        return f"{total_days}d {hours}h {minutes}min"

    if total_days < days_per_year:
        total_months, days = divmod(total_days, days_per_month)

        return f"{total_months}months {days}d {hours}h {minutes}min"

    years, remaining_days = divmod(total_days, days_per_year)
    months, days = divmod(remaining_days, days_per_month)

    return f"{years}y {months}months {days}d {hours}h {minutes}min"


def terminal_stop_requested():
    if os.name == "nt":
        while msvcrt.kbhit():
            if msvcrt.getwch() in ("q", "Q", "\x1b"):
                return True

        return False

    readable, _, _ = select.select([sys.stdin], [], [], 0)

    if readable:
        return sys.stdin.readline(1).lower() in ("q", "\x1b")

    return False

# Função para downsample dos dados para plotagem
def downsample_for_plot(df, downsample_ms):
    if df.empty:
        return df

    t = df["time_s"].to_numpy(dtype=np.float64)
    v = df["voltage_V"].to_numpy(dtype=np.float64)
    i = df["current_A"].to_numpy(dtype=np.float64)

    if downsample_ms <= 0:
        return pd.DataFrame({
            "time_s": t,
            "voltage_V": v,
            "current_A": i,
        })

    dt = downsample_ms / 1000.0

    bins = np.floor(t / dt).astype(np.int64)

    starts = np.r_[0, np.flatnonzero(np.diff(bins)) + 1]
    counts = np.diff(np.r_[starts, len(t)])

    time_ds = np.add.reduceat(t, starts) / counts
    voltage_ds = np.add.reduceat(v, starts) / counts
    current_ds = np.add.reduceat(i, starts) / counts

    return pd.DataFrame({
        "time_s": time_ds,
        "voltage_V": voltage_ds,
        "current_A": current_ds,
    })

# Classe para ler arquivos CSV em tempo real
class LiveCsvReader:
    def __init__(self, folder, prefix, voltage_fallback):
        self.folder = Path(folder)
        self.prefix = prefix
        self.voltage_fallback = voltage_fallback

        # Estado individual de cada arquivo CSV
        self.states = {}

    def list_files(self):
        files = list(self.folder.glob(f"{self.prefix}*.csv"))
        files = sorted(files, key=lambda p: file_order_key(p, self.prefix))
        return files

    def read_new_data_from_file(self, path):
        path = Path(path)

        if path not in self.states:
            self.states[path] = {
                "offset": 0,
                "header": None,
                "remainder": "",
                "last_size": 0,
            }

        state = self.states[path]

        try:
            file_size = path.stat().st_size
        except FileNotFoundError:
            return pd.DataFrame()

        # Detecta quando o stune reescreveu/truncou o arquivo.
        # Isso acontece quando o EnergyTrace passa do buffer de aproximadamente 64 s.
        if file_size < state["offset"]:
            state["offset"] = 0
            state["header"] = None
            state["remainder"] = ""

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(state["offset"])
                text = f.read()
                state["offset"] = f.tell()
        except PermissionError:
            return pd.DataFrame()
        except FileNotFoundError:
            return pd.DataFrame()

        if not text:
            return pd.DataFrame()

        text = state["remainder"] + text

        if not text.endswith("\n"):
            last_newline = text.rfind("\n")

            if last_newline == -1:
                state["remainder"] = text
                return pd.DataFrame()

            state["remainder"] = text[last_newline + 1:]
            text = text[:last_newline + 1]
        else:
            state["remainder"] = ""

        lines = text.splitlines()

        if not lines:
            return pd.DataFrame()

        data_lines = []

        if state["header"] is None:
            header_index = None

            for i, line in enumerate(lines):
                lower = line.lower()

                if "time" in lower and "current" in lower:
                    header_index = i
                    break

            if header_index is None:
                return pd.DataFrame()

            state["header"] = [x.strip() for x in lines[header_index].split(",")]
            data_lines = lines[header_index + 1:]
        else:
            data_lines = lines

        if not data_lines:
            return pd.DataFrame()

        csv_text = "\n".join(data_lines)

        try:
            df = pd.read_csv(
                io.StringIO(csv_text),
                names=state["header"],
                on_bad_lines="skip"
            )
        except Exception:
            return pd.DataFrame()

        time_col = find_column(df.columns, ["time"])
        current_col = find_column(df.columns, ["current"])
        voltage_col = find_column(df.columns, ["voltage"])

        if time_col is None or current_col is None:
            return pd.DataFrame()

        # IMPORTANTE:
        # Aqui usamos o tempo do próprio CSV como tempo global da captura.
        # Não fazemos mais time_raw - raw_start + base_time.
        time_s = pd.to_numeric(df[time_col], errors="coerce") / 1000.0
        current_A = pd.to_numeric(df[current_col], errors="coerce") * 1e-9

        if voltage_col is not None:
            voltage_V = pd.to_numeric(df[voltage_col], errors="coerce") / 1000.0
        else:
            voltage_V = pd.Series(self.voltage_fallback, index=df.index)

        temp = pd.DataFrame({
            "time_s": time_s,
            "voltage_V": voltage_V,
            "current_A": current_A,
        })

        temp = temp.dropna()

        if temp.empty:
            return pd.DataFrame()

        temp = temp.sort_values("time_s").reset_index(drop=True)

        return temp

    def read_new_data(self):
        frames = []

        for file in self.list_files():
            df = self.read_new_data_from_file(file)

            if not df.empty:
                frames.append(df)

        if not frames:
            return pd.DataFrame()

        data = pd.concat(frames, ignore_index=True)

        # Ordena pelo tempo real do CSV, não pelo nome do arquivo
        data = data.sort_values("time_s").reset_index(drop=True)

        return data


# =========================================================
# MAIN
# =========================================================

def main():
    # Configuração do argparse
    parser = argparse.ArgumentParser()

    parser.add_argument("--folder", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--voltage", type=float, default=3.3)
    parser.add_argument("--battery-capacity", type=float, default=220.0)
    parser.add_argument("--no-plot", type=int, choices=[0, 1], default=0)
    parser.add_argument("--refresh-ms", type=int, default=1000)
    parser.add_argument("--downsample-ms", type=float, default=10.0)
    parser.add_argument("--window-s", type=float, default=0.0)
    parser.add_argument("--plot-max-points", type=int, default=4000)
    parser.add_argument("--stop-file", default="")
    parser.add_argument("--duration-ms", type=float, default=0.0)

    args = parser.parse_args()

    if args.plot_max_points < 2:
        parser.error("--plot-max-points must be at least 2.")

    no_plot = args.no_plot == 1

    reader = LiveCsvReader(
        folder=args.folder,
        prefix=args.prefix,
        voltage_fallback=args.voltage
    )

    # Keep raw plot samples separate from the compressed data sent to Matplotlib.
    capture_time_data = []
    capture_current_data = []
    total_points = 0
    first_time_s = None
    current_integral_mA_s = 0.0
    last_current_mA = None

    last_energy_J = 0.0
    last_time_s = None
    last_power_W = None

    if not no_plot:
        plt.ion()

        fig, ax_current = plt.subplots(1, 1, figsize=(11, 4))

        line_current, = ax_current.plot([], [])

        ax_current.set_ylabel("Current (mA)")
        ax_current.set_xlabel("Time (s)")
        ax_current.grid(True)

        fig.suptitle("EnergyTrace LiveMode")
        fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.95))

        plt.show(block=False)
        configure_window(fig, width=1200, height=650)

    stop_file = Path(args.stop_file) if args.stop_file else None

    state = {
        "running": True,
        "final_total_printed": False
    }

    def load_capture_data():
        history_reader = LiveCsvReader(
            folder=reader.folder,
            prefix=reader.prefix,
            voltage_fallback=reader.voltage_fallback,
        )
        data = history_reader.read_new_data()

        if data.empty:
            return data

        return data.drop_duplicates(subset=["time_s"], keep="last").sort_values("time_s")

    def get_analysis_data():
        data = load_capture_data()

        if data.empty:
            return None

        t_arr = data["time_s"].to_numpy(dtype=np.float64)
        v_arr = data["voltage_V"].to_numpy(dtype=np.float64)
        i_arr = data["current_A"].to_numpy(dtype=np.float64) * 1000.0
        p_arr = v_arr * i_arr
        e_arr = np.zeros_like(t_arr)

        if len(t_arr) > 1:
            e_arr[1:] = np.cumsum(0.5 * (p_arr[1:] + p_arr[:-1]) * np.diff(t_arr))

        return t_arr, v_arr, i_arr, p_arr, e_arr

    def analyze_interval(t1, t2, label="INTERVAL"):
        analysis_data = get_analysis_data()

        if analysis_data is None or len(analysis_data[0]) < 2:
            print("\nThere is still not enough data to analyze the interval.")
            return

        if t2 < t1:
            t1, t2 = t2, t1

        t_arr, v_arr, i_arr, p_arr, e_arr = analysis_data

        t1 = max(t1, float(t_arr[0]))
        t2 = min(t2, float(t_arr[-1]))

        duration_s = t2 - t1

        if duration_s <= 0:
            print("\nInvalid interval.")
            return

        e1 = np.interp(t1, t_arr, e_arr)
        e2 = np.interp(t2, t_arr, e_arr)

        mask = (t_arr > t1) & (t_arr < t2)

        t_interval = np.concatenate((
            [t1],
            t_arr[mask],
            [t2]
        ))

        v_interval = np.concatenate((
            [np.interp(t1, t_arr, v_arr)],
            v_arr[mask],
            [np.interp(t2, t_arr, v_arr)]
        ))

        i_interval = np.concatenate((
            [np.interp(t1, t_arr, i_arr)],
            i_arr[mask],
            [np.interp(t2, t_arr, i_arr)]
        ))

        p_interval = np.concatenate((
            [np.interp(t1, t_arr, p_arr)],
            p_arr[mask],
            [np.interp(t2, t_arr, p_arr)]
        ))

        energy_interval_mJ = np_trapezoid(p_interval, t_interval)

        avg_voltage_V = np_trapezoid(v_interval, t_interval) / duration_s
        avg_current_mA = np_trapezoid(i_interval, t_interval) / duration_s
        avg_power_mW = energy_interval_mJ / duration_s

        min_current_mA = float(np.min(i_interval))
        max_current_mA = float(np.max(i_interval))

        min_power_mW = float(np.min(p_interval))
        max_power_mW = float(np.max(p_interval))

        print()
        print("=========================================================")
        print(label)
        print("=========================================================")
        print(f"Start:                      {t1:.6f} s")
        print(f"End:                        {t2:.6f} s")
        print(f"Duration:                   {duration_s:.6f} s")
        print()
        print("Voltage")
        print(f"  Mean:                     {avg_voltage_V:.6f} V")
        print()
        print("Current")
        print(f"  Mean:                     {avg_current_mA:.6f} mA")
        print(f"  Min:                      {min_current_mA:.6f} mA")
        print(f"  Max:                      {max_current_mA:.6f} mA")
        print()
        print("Power")
        print(f"  Mean:                     {avg_power_mW:.6f} mW")
        print(f"  Min:                      {min_power_mW:.6f} mW")
        print(f"  Max:                      {max_power_mW:.6f} mW")
        print()
        print("Energy")
        print(f"  Energy in Interval:       {energy_interval_mJ:.6f} mJ")
        print(f"  Energy Start:             {e1:.6f} mJ")
        print(f"  Energy End:               {e2:.6f} mJ")
        print()
        print("Battery Lifetime")

        if args.battery_capacity <= 0:
            print("  Estimate:                 not calculated")
        elif avg_current_mA <= 0:
            print("  Estimate:                 unavailable (average current must be positive)")
        else:
            lifetime_h = args.battery_capacity / avg_current_mA
            lifetime_text = format_battery_lifetime(lifetime_h)
            print(f"  Estimate:                 {lifetime_text}")

        print("=========================================================")


    def analyze_point(t_click):
        analysis_data = get_analysis_data()

        if analysis_data is None or len(analysis_data[0]) < 1:
            print("\nThere is still not enough data to analyze the point.")
            return

        t_arr, _v_arr, i_arr, p_arr, e_arr = analysis_data

        idx = int(np.argmin(np.abs(t_arr - t_click)))

        t = t_arr[idx]
        current_mA = i_arr[idx]
        power_mW = p_arr[idx]
        energy_mJ = e_arr[idx]

        print()
        print("=========================================================")
        print("POINT")
        print("=========================================================")
        print(f"Time:                      {t:.6f} s")
        print(f"Current:                   {current_mA:.6f} mA")
        print(f"Power:                     {power_mW:.6f} mW")
        print(f"Energy:                    {energy_mJ:.6f} mJ")
        print("=========================================================")


    def freeze_capture(reason="capture stopped", create_stop_file=False):
        if not state["running"]:
            print("\nThe capture is already stopped.")
            return

        state["running"] = False

        print()
        print()
        print("=========================================================")
        print(reason.upper())
        print("=========================================================")
        print("The capture has been interrupted/finished.")
        print("You can still select intervals or click on the plot.")
        print("=========================================================")

        if create_stop_file and stop_file is not None:
            stop_file.parent.mkdir(parents=True, exist_ok=True)
            stop_file.write_text("stop\n", encoding="utf-8")
            print()
            print(f"Stop file created: {stop_file}")

        if total_points >= 2 and first_time_s is not None and last_time_s is not None and not state["final_total_printed"]:
            analyze_interval(
                first_time_s,
                last_time_s,
                label="TOTAL UNTIL STOP",
            )
            state["final_total_printed"] = True

        if not no_plot:
            fig.suptitle(f"EnergyTrace LiveMode - {reason}")
            fig.canvas.draw_idle()


    def request_stop():
        freeze_capture(
            reason="Stop requested by user",
            create_stop_file=True
        )


    def on_key_press(event):
        if event.key in ["q", "escape"]:
            request_stop()

        elif event.key == "t":
            if total_points >= 2 and first_time_s is not None and last_time_s is not None:
                analyze_interval(
                    first_time_s,
                    last_time_s,
                    label="TOTAL",
                )
            else:
                print("\nThere is still not enough data to analyze the total interval.")


    right_click_state = {
        "x": None,
        "y": None,
        "xdata": None,
        "valid": False
    }


    def on_mouse_press(event):
        if event.button != 3:
            return

        if event.xdata is None:
            return

        if event.inaxes != ax_current:
            return

        right_click_state["x"] = event.x
        right_click_state["y"] = event.y
        right_click_state["xdata"] = event.xdata
        right_click_state["valid"] = True


    def on_mouse_release(event):
        if event.button != 3:
            return

        if not right_click_state["valid"]:
            return

        dx = event.x - right_click_state["x"]
        dy = event.y - right_click_state["y"]

        drag_px = (dx**2 + dy**2) ** 0.5

        if drag_px <= RIGHT_CLICK_MAX_DRAG_PX:
            analyze_point(right_click_state["xdata"])
        else:
            print()
            print(f"Right-click ignored: drag of {drag_px:.1f} px.")

        right_click_state["x"] = None
        right_click_state["y"] = None
        right_click_state["xdata"] = None
        right_click_state["valid"] = False


    MIN_INTERVAL_S = 0.02  # 20 ms
    RIGHT_CLICK_MAX_DRAG_PX = 6

    def on_select_interval(t_min, t_max):
        interval_s = abs(t_max - t_min)

        if interval_s < MIN_INTERVAL_S:
            print()
            print(f"Selection ignored: interval too small ({interval_s:.6f} s).")
            return

        analyze_interval(
            t_min,
            t_max,
            label="INTERVAL",
        )

    if not no_plot:
        span_selector = SpanSelector(
            ax_current,
            on_select_interval,
            "horizontal",
            useblit=True,
            interactive=True,
            button=MouseButton.LEFT,
            minspan=MIN_INTERVAL_S
        )

        fig.canvas.mpl_connect("key_press_event", on_key_press)
        fig.canvas.mpl_connect("button_press_event", on_mouse_press)
        fig.canvas.mpl_connect("button_release_event", on_mouse_release)

    last_status_len = 0

    fig_num = None if no_plot else plt.get_fignums()[0]

    # Loop principal de atualização do gráfico
    while (no_plot and state["running"]) or (
        not no_plot and plt.fignum_exists(fig_num)
    ):
        if no_plot and terminal_stop_requested():
            request_stop()
            break

        if not state["running"]:
            plt.pause(0.2)
            continue

        new_data = reader.read_new_data()

        if new_data.empty:
            plt.pause(args.refresh_ms / 1000.0)
            continue

        if last_time_s is not None:
            new_data = new_data[new_data["time_s"] > last_time_s]

        if new_data.empty:
            plt.pause(args.refresh_ms / 1000.0)
            continue

        t = new_data["time_s"].to_numpy(dtype=np.float64)
        v = new_data["voltage_V"].to_numpy(dtype=np.float64)
        i = new_data["current_A"].to_numpy(dtype=np.float64)

        p = v * i
        current_mA = i * 1000.0

        if len(t) == 0:
            plt.pause(args.refresh_ms / 1000.0)
            continue

        energy_block = np.empty_like(t)

        if last_time_s is None:
            first_time_s = float(t[0])
            energy_block[0] = last_energy_J
        else:
            dt0 = t[0] - last_time_s
            energy_block[0] = last_energy_J + 0.5 * (p[0] + last_power_W) * dt0
            current_integral_mA_s += 0.5 * (current_mA[0] + last_current_mA) * dt0

        if len(t) > 1:
            dt_vec = np.diff(t)
            e_inc = 0.5 * (p[1:] + p[:-1]) * dt_vec
            energy_block[1:] = energy_block[0] + np.cumsum(e_inc)
            current_integral_mA_s += float(
                np.sum(0.5 * (current_mA[1:] + current_mA[:-1]) * dt_vec)
            )

        total_points += len(t)

        last_time_s = float(t[-1])
        last_power_W = float(p[-1])
        last_energy_J = float(energy_block[-1])
        last_current_mA = float(current_mA[-1])

        if not no_plot:
            capture_time_data.extend(t.tolist())
            capture_current_data.extend(current_mA.tolist())

            if args.window_s > 0:
                tmin = last_time_s - args.window_s
                first_kept = next(
                    (idx for idx, value in enumerate(capture_time_data) if value >= tmin),
                    len(capture_time_data),
                )
                capture_time_data = capture_time_data[first_kept:]
                capture_current_data = capture_current_data[first_kept:]

            plot_df = pd.DataFrame({
                "time_s": np.asarray(capture_time_data, dtype=np.float64),
                "voltage_V": np.ones(len(capture_time_data), dtype=np.float64),
                "current_A": np.asarray(capture_current_data, dtype=np.float64) / 1000.0,
            })

            plot_df = downsample_for_plot(
                plot_df,
                args.downsample_ms
            )

            plot_time, plot_current = limit_plot_points_peak_preserving(
                plot_df["time_s"].to_numpy(dtype=np.float64),
                plot_df["current_A"].to_numpy(dtype=np.float64) * 1000.0,
                max_points=args.plot_max_points
            )

            line_current.set_data(plot_time, plot_current)

            ax_current.relim()

            if args.window_s > 0 and total_points > 1:
                tmax = last_time_s
                tmin = max(first_time_s, tmax - args.window_s)

                ax_current.set_xlim(tmin, tmax)
                ax_current.autoscale_view(scalex=False, scaley=True)

            elif args.duration_ms > 0 and total_points > 1:
                tmin = 0.0
                tmax = args.duration_ms / 1000.0

                ax_current.set_xlim(tmin, tmax)
                ax_current.autoscale_view(scalex=False, scaley=True)

            else:
                ax_current.autoscale_view(scalex=True, scaley=True)

            fig.canvas.draw()
            fig.canvas.flush_events()

        elapsed_s = last_time_s - first_time_s if total_points > 1 else 0.0

        if elapsed_s > 0:
            avg_current_mA = current_integral_mA_s / elapsed_s
            avg_power_mW = (last_energy_J * 1000.0) / elapsed_s
        else:
            avg_current_mA = 0.0
            avg_power_mW = 0.0

        status_msg = (
            f"\rPts={total_points} | "
            f"t={last_time_s:.2f}s | "
            f"I={last_current_mA:.3f}mA | "
            f"Iavg={avg_current_mA:.3f}mA | "
            f"E={last_energy_J * 1000.0:.3f}mJ | "
            f"Pavg={avg_power_mW:.3f}mW"
        )

        clear_len = max(last_status_len, len(status_msg))
        sys.stdout.write(status_msg.ljust(clear_len))
        sys.stdout.flush()

        last_status_len = len(status_msg)

        if args.duration_ms > 0 and last_time_s is not None:
            if last_time_s >= (args.duration_ms / 1000.0):
                freeze_capture(
                    reason="capture finished by duration",
                    create_stop_file=False
                )

        if no_plot:
            time.sleep(args.refresh_ms / 1000.0)
        else:
            plt.pause(args.refresh_ms / 1000.0)

    print("\n\nReal time plot finished.")

    if total_points >= 2 and first_time_s is not None and last_time_s is not None and not state["final_total_printed"]:
        analyze_interval(
            first_time_s,
            last_time_s,
            label="FINAL_TOTAL",
        )
    else:
        if total_points < 2:
            print("There is still not enough data to calculate the total consumption.")


if __name__ == "__main__":
    main()
