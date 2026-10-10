#!/usr/bin/env python3
import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
SAFE_ENV = {"PATH": SAFE_PATH, "LC_ALL": "C", "LANG": "C"}

STATE_DIR = Path.home() / ".local/state/omarchy"
STATE_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = STATE_DIR / "battery_history.json"
HISTORY_NAME = "battery_history.json"
CACHE_NAME = "consumers_cache.json"
SNAP_NAME = "consumers_prev.json"
HISTORY_MAX_BYTES = 160 * 1024  # 1440 points (24h @1/min) ~ 90 KiB
HISTORY_MAX_POINTS = 1440

# Consumers churn slowly; re-forking ps on every panel tick was the single
# largest cost in this helper. The snapshot rides the same TTL: rows are the
# rate measured across the last refresh interval, not lifetime %CPU.
CONSUMERS_TTL_S = 30
CACHE_MAX_BYTES = 16 * 1024
SNAP_MAX_BYTES = 256 * 1024
# A snapshot older than this produces meaningless rates (suspend, long panel
# close) — report zeros instead of a garbage burst.
SNAP_STALE_S = 180

POWER_SUPPLY = "/sys/class/power_supply"
# Asahi/macSMC exposes macsmc-battery; generic ACPI uses BAT*/BATT*.
BATTERY_CANDIDATES = ("macsmc-battery", "BAT0", "BAT1", "BATT")

# Everything worth reading in one pass. Each name is a sysfs attribute on the
# battery device; _read_battery_attrs() slurps them with a single scan.
_ATTRS = (
    # raw charge accounting
    "capacity",
    "status",
    "cycle_count",
    "charge_now",
    "charge_full",
    "charge_full_design",
    "charge_counter",
    # energy accounting (uWh)
    "energy_now",
    "energy_full",
    "energy_full_design",
    # instantaneous electrical state
    "current_now",
    "power_now",
    "voltage_now",
    "voltage_min",
    "voltage_max",
    "voltage_min_design",
    "voltage_max_design",
    "temp",
    # kernel estimates (seconds)
    "time_to_empty_now",
    "time_to_full_now",
    # charge limiting
    "charge_control_start_threshold",
    "charge_control_end_threshold",
    "charge_behaviour",
    # descriptive
    "health",
    "model_name",
    "serial_number",
    "manufacture_year",
    "manufacture_month",
    "manufacture_day",
)


def _open_state_dir():
    """Descriptor for STATE_DIR — no symlinks, must be ours."""
    fd = os.open(STATE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        os.close(fd)
        raise PermissionError("state dir is not a user-owned real directory")
    return fd


def _clean_entry(p):
    """Normalize one history entry to {time, cap, status}; None if malformed.

    Entries are only skipped here — a bad point must never take down the
    whole read, or one corrupt line would zero the JSON output until the
    file is deleted.
    """
    if not isinstance(p, dict):
        return None
    try:
        t = int(p["time"])
        cap = int(p["cap"])
    except (KeyError, TypeError, ValueError):
        return None
    status = p.get("status", "")
    entry = {"time": t, "cap": cap, "status": status if isinstance(status, str) else ""}
    # Watts are optional in history — points recorded before the field existed
    # carry no "w" key and are still valid.
    w = p.get("w")
    if isinstance(w, (int, float)):
        entry["w"] = round(w, 2)
    return entry


def _read_history_meta(dirfd):
    """Bounded, no-follow read. Returns (entries, dropped) for repair checks."""
    try:
        fd = os.open(HISTORY_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
    except OSError:
        return [], 0
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_size > HISTORY_MAX_BYTES:
            return [], 0
        data = json.loads(os.read(fd, HISTORY_MAX_BYTES + 1).decode())
        if not isinstance(data, list):
            return [], 0
        kept = [e for e in (_clean_entry(p) for p in data) if e is not None]
        return kept, len(data) - len(kept)
    except Exception:
        return [], 0
    finally:
        os.close(fd)


def _read_history(dirfd):
    """Bounded, no-follow read of the history file; [] on any anomaly."""
    return _read_history_meta(dirfd)[0]


def _write_history(dirfd, history):
    """Publish via exclusive same-dir temp file + atomic rename."""
    tmp = f".{HISTORY_NAME}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
    try:
        os.write(fd, json.dumps(history).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, HISTORY_NAME, src_dir_fd=dirfd, dst_dir_fd=dirfd)


def _open_history_lock(dirfd: int) -> int:
    lock_name = f"{HISTORY_NAME}.lock"
    fd = os.open(lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        os.close(fd)
        raise PermissionError("history lock is not a user-owned regular file")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _close_history_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------- sysfs battery telemetry ----------


def _read_battery_attrs():
    """Every interesting battery attribute in one pass; {} if no battery.

    Sign convention is preserved from the kernel: current_now and power_now
    are negative while discharging, so callers can report direction without
    re-deriving it from `status`.
    """
    for cand in BATTERY_CANDIDATES:
        base = f"{POWER_SUPPLY}/{cand}"
        if not os.path.exists(f"{base}/capacity"):
            continue
        out = {}
        for attr in _ATTRS:
            try:
                with open(f"{base}/{attr}") as fh:
                    val = fh.read().strip()
            except OSError:
                continue
            if val:
                out[attr] = val
        return out
    return {}


def _num(attrs, key, scale=1.0, default=None):
    """Read an int sysfs attribute and rescale to human units."""
    raw = attrs.get(key)
    if raw is None:
        return default
    try:
        return int(raw) / scale
    except (TypeError, ValueError):
        return default


def _round(value, places):
    return None if value is None else round(value, places)


def _count(attrs, key, scale=1.0):
    """Whole-number sysfs attribute. Cycle and threshold counts must not
    render as 269.0 — they are read by humans."""
    val = _num(attrs, key, scale)
    return None if val is None else int(round(val))


def get_current_battery():
    """Coarse (capacity, status) pair kept for history + compatibility."""
    attrs = _read_battery_attrs()
    if not attrs:
        return None, "Battery unavailable"
    cap = _num(attrs, "capacity")
    if cap is None:
        return None, "Battery capacity unavailable"
    status = attrs.get("status", "Status unavailable")
    # macsmc-ac online=1 means plugged in; at the charge limit the SMC
    # reports "Not charging" — surface that as plugged-in instead.
    try:
        with open(f"{POWER_SUPPLY}/macsmc-ac/online") as fh:
            ac_online = int(fh.read().strip())
        limit = _num(attrs, "charge_control_end_threshold")
        if ac_online and status == "Not charging":
            status = (
                f"Plugged in (charge limit {limit:.0f}%)"
                if limit
                else "Plugged in (charge limit reached)"
            )
    except (OSError, ValueError):
        pass
    return int(cap), status


def get_telemetry():
    """Precise battery telemetry, or {} when the battery is absent.

    Units are chosen for display: Wh, W, A, V, degrees C, seconds. Kernel
    micro-units are divided once here so no consumer has to remember that
    power_now is uW and voltage_now is uV.
    """
    attrs = _read_battery_attrs()
    if not attrs:
        return {}

    energy_now = _num(attrs, "energy_now", 1e6)
    energy_full = _num(attrs, "energy_full", 1e6)
    energy_design = _num(attrs, "energy_full_design", 1e6)
    # charge_* are uAh: /1e6 gives Ah, /1e3 gives mAh. The ratio is unit-free
    # so SoH below can use either; the reported fields are in mAh because a
    # laptop pack reads naturally in mAh (7407 mAh, not 7.4 Ah).
    charge_full = _num(attrs, "charge_full", 1e6)
    charge_design = _num(attrs, "charge_full_design", 1e6)

    # State of health. Prefer the energy pair; fall back to the charge-coulomb
    # pair, which the same fuel gauge publishes. A pack that reports >100% is
    # clamped — it means "new", not "better than new".
    health_pct = None
    if energy_full and energy_design:
        health_pct = energy_full / energy_design * 100.0
    elif charge_full and charge_design:
        health_pct = charge_full / charge_design * 100.0
    if health_pct is not None:
        health_pct = min(100.0, health_pct)

    power_w = _num(attrs, "power_now", 1e6)
    current_a = _num(attrs, "current_now", 1e6)
    voltage_v = _num(attrs, "voltage_now", 1e6)

    year = attrs.get("manufacture_year")
    month = attrs.get("manufacture_month")
    day = attrs.get("manufacture_day")
    manufactured = None
    if year and month and day:
        try:
            manufactured = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        except (TypeError, ValueError):
            manufactured = None

    # charge_behaviour looks like "[auto] inhibit-charge"; the bracketed token
    # is the power supply's own mode, the rest is the driver policy.
    behaviour = attrs.get("charge_behaviour", "")
    behaviour = behaviour.split("]")[-1].strip() if behaviour else ""

    telemetry = {
        "status": attrs.get("status", ""),
        "health_pct": _round(health_pct, 1),
        "health": attrs.get("health", ""),
        "cycle_count": _count(attrs, "cycle_count"),
        "model": attrs.get("model_name", ""),
        "manufactured": manufactured,
        "energy_wh": _round(energy_now, 2),
        "energy_full_wh": _round(energy_full, 2),
        "energy_design_wh": _round(energy_design, 2),
        "charge_now_mah": _count(attrs, "charge_now", 1e3),
        "charge_full_mah": _count(attrs, "charge_full", 1e3),
        "charge_design_mah": _count(attrs, "charge_full_design", 1e3),
        "charge_throughput_ah": _round(_num(attrs, "charge_counter", 1e6), 2),
        "current_a": _round(current_a, 3),
        "voltage_v": _round(voltage_v, 3),
        "voltage_min_v": _round(_num(attrs, "voltage_min", 1e6), 3),
        "voltage_max_v": _round(_num(attrs, "voltage_max", 1e6), 3),
        "temp_c": _round(_num(attrs, "temp", 10.0), 1),
        "time_to_empty_s": _count(attrs, "time_to_empty_now"),
        "time_to_full_s": _count(attrs, "time_to_full_now"),
        "charge_limit_pct": _count(attrs, "charge_control_end_threshold"),
        "charge_resume_pct": _count(attrs, "charge_control_start_threshold"),
        "charge_behaviour": behaviour,
        "adapter_limit_w": _round(_read_power_supply_attr("macsmc-ac", "input_power_limit", 1e6), 1),
        "temps_c": _read_hwmon_temps(),
        "peripherals": _read_peripheral_batteries(),
    }

    # Watts are only meaningful against a load; below this the gauge is
    # reporting noise and the UI would show a flickering decimal. 1.0 W, not
    # 0.5: a genuinely idle pack still dithers a few tenths of a watt, and a
    # "-0.6 W out" line under a "Charging" header reads as a fault when it is
    # just the gauge disagreeing with itself.
    if power_w is not None and abs(power_w) < 1.0:
        power_w = 0.0

    # Direction is derived *after* the snap, so a sub-threshold reading reads
    # as idle instead of claiming a direction the panel then contradicts by
    # printing 0.00 W. Falls back to the status string when there is no sign.
    if power_w is None or power_w == 0:
        status = telemetry["status"]
        if status == "Charging":
            telemetry["flow"] = "in"
        elif status == "Discharging":
            telemetry["flow"] = "out"
        else:
            telemetry["flow"] = "idle"
    else:
        telemetry["flow"] = "in" if power_w > 0 else "out"

    telemetry["power_w"] = _round(power_w, 2)
    return telemetry


def _read_power_supply_attr(name, attr, scale=1.0):
    """One sysfs attribute off a named power_supply node; None when absent."""
    try:
        with open(f"{POWER_SUPPLY}/{name}/{attr}") as fh:
            return float(fh.read().strip()) / scale
    except (OSError, ValueError):
        return None


def _read_hwmon_temps():
    """Named thermal sensors from the macsmc hwmon block, {label: degC}."""
    out = {}
    for h in Path("/sys/class/hwmon").iterdir():
        try:
            if (h / "name").read_text().strip() != "macsmc_hwmon":
                continue
        except OSError:
            continue
        for t in h.glob("temp*_input"):
            try:
                label = t.with_name(t.name.replace("_input", "_label")).read_text().strip()
                out[label] = round(int(t.read_text().strip()) / 1000.0, 1)
            except (OSError, ValueError):
                continue
    return out


def _read_peripheral_batteries():
    """hidpp_* power supplies (Logitech peripherals etc), {name: pct}."""
    out = {}
    for p in Path(POWER_SUPPLY).glob("hidpp_battery_*"):
        try:
            cap = int((p / "capacity").read_text().strip())
        except (OSError, ValueError):
            continue
        # model_name is e.g. "MX Keys for Mac" — friendlier than the sysfs name.
        try:
            name = (p / "model_name").read_text().strip()
        except OSError:
            name = p.name
        out[name or p.name] = cap
    return out


def update_history(current_cap, status, power_w=None):
    if not isinstance(current_cap, int) or not 0 <= current_cap <= 100:
        return []
    history = []
    now = int(time.time())
    try:
        dirfd = _open_state_dir()
    except PermissionError:
        return history
    lockfd = None
    try:
        lockfd = _open_history_lock(dirfd)
        history, dropped = _read_history_meta(dirfd)
        if not isinstance(history, list):
            history = []
            dropped = 0

        # Only append if last point is at least 60s ago or the charge moved.
        appended = (
            not history
            or (now - history[-1].get("time", 0)) >= 60
            or history[-1].get("cap") != current_cap
        )
        if appended:
            point = {"time": now, "cap": current_cap, "status": status}
            if isinstance(power_w, (int, float)):
                point["w"] = round(power_w, 2)
            history.append(point)

        # 24h at one point per minute.
        trimmed = len(history) > HISTORY_MAX_POINTS
        if trimmed:
            history = history[-HISTORY_MAX_POINTS:]

        # Write only when something actually changed. The panel refreshes every
        # few seconds, so rewriting (and fsyncing) an unchanged 17 KB file on
        # every tick was ~17k pointless writes a day. `dropped` also triggers a
        # rewrite so a corrupt point gets scrubbed even when no sample is due.
        if appended or trimmed or dropped:
            _write_history(dirfd, history)
    except (OSError, ValueError, TypeError):
        return history
    finally:
        if lockfd is not None:
            _close_history_lock(lockfd)
        os.close(dirfd)
    return history


GRAPH_WIDTH = 24


def analyze_history(history, current_cap, telemetry=None):
    """Drain rate and projected runtime derived from the real series.

    Uses least squares over the recent discharging window rather than
    endpoint subtraction, so a single noisy sample cannot swing the number.
    """
    out = {
        "drain_pct_per_h": None,
        "projected_runtime_min": None,
        "window_min": None,
    }
    points = [p for p in history if isinstance(p, dict) and "time" in p and "cap" in p]
    if len(points) < 2:
        return out

    span_s = points[-1]["time"] - points[0]["time"]
    if span_s <= 0:
        return out
    out["window_min"] = span_s / 60.0

    # Least-squares slope in %/hour over the whole retained window.
    n = len(points)
    mean_t = sum(p["time"] for p in points) / n
    mean_c = sum(p["cap"] for p in points) / n
    num = sum((p["time"] - mean_t) * (p["cap"] - mean_c) for p in points)
    den = sum((p["time"] - mean_t) ** 2 for p in points)
    if den > 0:
        out["drain_pct_per_h"] = _round(-(num / den) * 3600.0, 2)

    # Projected time to empty: prefer the gauge's own estimate, and fall back
    # to energy/power when it is absent or stale (it reads 0 at full charge).
    energy_wh = (telemetry or {}).get("energy_wh")
    power_w = (telemetry or {}).get("power_w")
    tte = (telemetry or {}).get("time_to_empty_s")
    if tte:
        out["projected_runtime_min"] = int(tte // 60)
    elif energy_wh and power_w and power_w > 0.5:
        out["projected_runtime_min"] = int((energy_wh / power_w) * 60)

    return out


def make_ascii_graph(history, current_cap, telemetry=None, insights=None):
    ticks = "▁▂▃▄▅▆▇█"
    if not history:
        history = [{"time": int(time.time()), "cap": current_cap}]

    # Decimate to the display width by taking the newest sample per bucket, so
    # the sparkline covers the whole retained window rather than just the tail.
    points = history
    if len(points) > GRAPH_WIDTH:
        step = len(points) / GRAPH_WIDTH
        points = [points[min(len(points) - 1, int((i + 1) * step) - 1)] for i in range(GRAPH_WIDTH)]

    caps = [p["cap"] for p in points]
    lo, hi = min(caps), max(caps)
    # Scale to the data's own range so drift is visible, but never narrower
    # than a 10-point band: a steady charge must not read as a zigzag.
    if hi - lo < 10:
        mid = (hi + lo) / 2
        lo = max(0, int(mid - 5))
        hi = min(100, lo + 10)
        lo = max(0, hi - 10)

    span = hi - lo or 1
    spark = "".join(ticks[round((c - lo) / span * 7)] for c in caps)

    # Absolute-scale sparkline for callers that want raw charge level.
    spark_abs = "".join(ticks[min(7, max(0, int(c / 100.0 * 7.99)))] for c in caps)

    span_s = max(0, points[-1].get("time", 0) - points[0].get("time", 0))
    if span_s >= 3600:
        span_label = f"{span_s // 3600}h {span_s % 3600 // 60}m"
    else:
        span_label = f"{max(1, span_s // 60)}m"

    # Deliberately no "now" value here. The panel header already carries the
    # current charge, and it reads UPower while this is our own sampled cap, so
    # printing both put two different percentages a few centimetres apart.
    caption = f"{min(caps)}–{max(caps)}% over {span_label}"
    drain = (insights or {}).get("drain_pct_per_h")
    if drain:
        # Signed least-squares slope: positive means the window is losing
        # charge. Spell the direction out instead of leaving a bare -3.1%/h.
        caption += f" · {abs(drain):.1f}%/h {'down' if drain > 0 else 'up'}"

    return "\n".join([spark, caption]), spark_abs


def _tool(name):
    """Absolute path for an external helper, resolved under SAFE_PATH only."""
    return shutil.which(name, path=SAFE_PATH)


def _kill_tree(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


def _run(argv, timeout=2.0, max_bytes=262144):
    """Run argv with minimal env, hard deadline, producer byte cap.

    Child runs in its own process group so TERM/KILL reaches the tree.
    Returns stdout text or None on failure/timeout/overflow.
    """
    if not argv or not argv[0]:
        return None
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=SAFE_ENV, start_new_session=True,
        )
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return None
    if len(out) > max_bytes:
        return None
    return out.decode("utf-8", "replace")


def _read_consumers_cache(dirfd):
    try:
        fd = os.open(CACHE_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_size > CACHE_MAX_BYTES:
            return None
        payload = json.loads(os.read(fd, CACHE_MAX_BYTES + 1).decode())
    except Exception:
        return None
    finally:
        os.close(fd)
    if not isinstance(payload, dict):
        return None
    stamp = payload.get("at")
    rows = payload.get("rows")
    if not isinstance(stamp, (int, float)) or not isinstance(rows, list):
        return None
    if time.time() - stamp > CONSUMERS_TTL_S:
        return None
    return rows


def _write_consumers_cache(dirfd, rows):
    tmp = f".{CACHE_NAME}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
    except OSError:
        return
    try:
        os.write(fd, json.dumps({"at": time.time(), "rows": rows}).encode())
    finally:
        os.close(fd)
    try:
        os.rename(tmp, CACHE_NAME, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except OSError:
        try:
            os.unlink(tmp, dir_fd=dirfd)
        except OSError:
            pass


def _read_json_state(dirfd, name, max_bytes):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_size > max_bytes:
            return None
        data = json.loads(os.read(fd, max_bytes + 1).decode())
        return data if isinstance(data, dict) else None
    except Exception:
        return None
    finally:
        os.close(fd)


def _write_json_state(dirfd, name, payload):
    tmp = f".{name}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
    except OSError:
        return
    try:
        os.write(fd, json.dumps(payload).encode())
    finally:
        os.close(fd)
    try:
        os.rename(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except OSError:
        try:
            os.unlink(tmp, dir_fd=dirfd)
        except OSError:
            pass


def _scan_jiffies():
    """pid -> (comm, utime+stime jiffies) from /proc, one pass."""
    snap = {}
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            raw = (p / "stat").read_text()
            # comm is parenthesised and may itself contain spaces/parens —
            # split on the final ')' so the field offsets hold.
            close = raw.rfind(")")
            comm = raw[raw.index("(") + 1:close]
            fields = raw[close + 2:].split()
            snap[int(p.name)] = (comm, int(fields[11]) + int(fields[12]))
        except (OSError, ValueError, IndexError):
            continue
    return snap


def _scan_top_consumers(dirfd, power_w=None):
    """Rate-based top consumers: jiffies burned per wall-second since the
    previous scan, aggregated by comm — 'what is drawing power right now',
    not lifetime %CPU which mostly measures process age.

    Falls back to zeros (and still records the snapshot) when there is no
    prior sample or it is too old to trust.
    """
    consumers = []
    try:
        now = time.time()
        cur = _scan_jiffies()
        clk = os.sysconf("SC_CLK_TCK")

        prev_snap = _read_json_state(dirfd, SNAP_NAME, SNAP_MAX_BYTES) or {}
        prev = prev_snap.get("jobs") or {}
        prev_at = prev_snap.get("at") or 0
        fresh = 0 < now - prev_at <= SNAP_STALE_S

        _write_json_state(dirfd, SNAP_NAME, {"at": now, "jobs": cur})

        rates = {}
        if fresh:
            span = now - prev_at
            for pid, (comm, j) in cur.items():
                pj = prev.get(str(pid))
                if pj is None:
                    continue
                d = j - pj[1]
                if d < 0:
                    continue
                name = pj[0]
                rates[name] = rates.get(name, 0.0) + (d / clk) / span * 100.0

        rows = sorted(rates.items(), key=lambda x: x[1], reverse=True)
        # Everything under ~2% of one core is measurement noise, not a burner.
        rows = [r for r in rows if r[1] >= 2.0][:5]
        max_cpu = max((r[1] for r in rows), default=10.0)
        max_cpu = max(max_cpu, 10.0)

        for name, cpu_val in rows:
            bar_len = int(min(12, max(1, (cpu_val / max_cpu) * 12)))
            row = {
                "name": name[:12],
                "cpu": f"{cpu_val:.1f}%",
                "cpu_num": cpu_val,
                "bar": "█" * bar_len + "░" * (12 - bar_len),
            }
            # Rough watts share: this process's slice of busy cores, applied
            # to the pack's current draw. Honest as an estimate — labelled "≈".
            if isinstance(power_w, (int, float)) and abs(power_w) >= 1.0:
                ncpu = os.cpu_count() or 8
                row["watts"] = round(abs(power_w) * min(1.0, cpu_val / 100.0 / ncpu), 1)
            consumers.append(row)
    except Exception:
        consumers = [{"name": "unavailable", "cpu": "0%", "cpu_num": 0, "bar": "░" * 12}]
    return consumers


def get_top_consumers(force=False, power_w=None):
    """Top consumers, cached for CONSUMERS_TTL_S so the jiffy snapshot always
    spans a meaningful interval. Each scan doubles as the next scan's baseline.
    """
    try:
        dirfd = _open_state_dir()
    except (PermissionError, OSError):
        return []

    lockfd = None
    try:
        lockfd = _open_history_lock(dirfd)
        if not force:
            cached = _read_consumers_cache(dirfd)
            if cached is not None:
                return cached
        rows = _scan_top_consumers(dirfd, power_w)
        _write_consumers_cache(dirfd, rows)
        return rows
    except (OSError, ValueError, TypeError):
        return []
    finally:
        if lockfd is not None:
            _close_history_lock(lockfd)
        os.close(dirfd)


def make_watts_graph(history):
    """Sparkline of signed watts over the retained window. Only points that
    carry a 'w' field participate — charge-only history renders empty."""
    ticks = "▁▂▃▄▅▆▇█"
    ws = [p["w"] for p in history if isinstance(p, dict) and isinstance(p.get("w"), (int, float))]
    if len(ws) < 4:
        return ""
    points = ws
    if len(points) > GRAPH_WIDTH:
        step = len(points) / GRAPH_WIDTH
        points = [points[min(len(points) - 1, int((i + 1) * step) - 1)] for i in range(GRAPH_WIDTH)]
    hi = max(abs(w) for w in points)
    # A flat line carries no information — suppress it below a few watts so
    # the sparkline only appears when flow is actually interesting.
    if hi < 3:
        return ""
    return "".join(ticks[min(7, int(abs(w) / hi * 7.99))] for w in points)


def main():
    args = os.sys.argv[1:]
    cap, status = get_current_battery()
    telemetry = get_telemetry()
    # --sample only records a point; the always-on timer in the panel calls it
    # so history accrues while the panel is closed too.
    history = update_history(cap, status, (telemetry or {}).get("power_w"))
    if "--sample" in args:
        return
    if cap is None:
        print(json.dumps({
            "capacity": None,
            "status": status,
            "spark": "",
            "ascii_graph": "",
            "watts_graph": "",
            "top_consumers": [],
            "telemetry": {},
            "insights": {},
        }))
        return

    insights = analyze_history(history, cap, telemetry)
    ascii_graph, spark = make_ascii_graph(history, cap, telemetry, insights)
    print(json.dumps({
        "capacity": cap,
        "status": status,
        "spark": spark,
        "ascii_graph": ascii_graph,
        "watts_graph": make_watts_graph(history),
        "top_consumers": get_top_consumers(power_w=(telemetry or {}).get("power_w")),
        "telemetry": telemetry,
        "insights": insights,
    }))


if __name__ == "__main__":
    main()
