#!/usr/bin/env python3
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
HISTORY_MAX_BYTES = 64 * 1024  # 240 points serialize to ~15 KiB


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
    return {"time": t, "cap": cap, "status": status if isinstance(status, str) else ""}


def _read_history(dirfd):
    """Bounded, no-follow read of the history file; [] on any anomaly."""
    try:
        fd = os.open(HISTORY_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
    except OSError:
        return []
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_size > HISTORY_MAX_BYTES:
            return []
        data = json.loads(os.read(fd, HISTORY_MAX_BYTES + 1).decode())
        if not isinstance(data, list):
            return []
        return [e for e in (_clean_entry(p) for p in data) if e is not None]
    except Exception:
        return []
    finally:
        os.close(fd)


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

def get_current_battery():
    cap = 50
    status = "Discharging"
    import glob
    # Asahi/macSMC exposes macsmc-battery; generic ACPI uses BAT*/BATT*
    sys_name = None
    for cand in ("macsmc-battery", "BAT0", "BAT1", "BATT"):
        if os.path.exists(f"/sys/class/power_supply/{cand}/capacity"):
            sys_name = cand
            break
    if sys_name:
        try:
            with open(f"/sys/class/power_supply/{sys_name}/capacity") as f:
                cap = int(f.read().strip())
        except Exception:
            pass
        try:
            with open(f"/sys/class/power_supply/{sys_name}/status") as f:
                status = f.read().strip()
        except Exception:
            pass
    # macsmc-ac online=1 means plugged in; at the 80% charge limit the SMC
    # reports "Not charging" — surface that as plugged-in instead.
    try:
        ac_online = int(open("/sys/class/power_supply/macsmc-ac/online").read().strip())
        if ac_online and status == "Not charging":
            status = "Plugged in (charge limit reached)"
    except Exception:
        pass
    return cap, status

def update_history(current_cap, status):
    history = []
    now = int(time.time())
    try:
        dirfd = _open_state_dir()
    except PermissionError:
        return history
    try:
        history = _read_history(dirfd)
        if not isinstance(history, list):
            history = []
    finally:
        os.close(dirfd)

    # Only append if last point is at least 60s ago or empty
    if not history or (now - history[-1].get("time", 0)) >= 60 or history[-1].get("cap") != current_cap:
        history.append({"time": now, "cap": current_cap, "status": status})

    # Keep ~4h at one point per minute.
    if len(history) > 240:
        history = history[-240:]

    try:
        dirfd = _open_state_dir()
    except PermissionError:
        return history
    try:
        _write_history(dirfd, history)
    except Exception:
        pass
    finally:
        os.close(dirfd)
    return history

GRAPH_WIDTH = 24

def make_ascii_graph(history, current_cap):
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

    chart = [
        spark,
        f"{min(caps)}–{max(caps)}% over {span_label} · now {current_cap}%",
    ]
    return "\n".join(chart), spark_abs

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


def get_top_consumers():
    consumers = []
    try:
        cmd = [_tool("ps"), "-eo", "comm,%cpu,%mem"]
        res = _run(cmd)
        if res is None:
            raise RuntimeError("ps failed")
        lines = res.strip().split("\n")
        totals = {}
        for line in lines[1:]:
            parts = line.split()
            if len(parts) >= 3:
                comm = parts[0]
                if comm in ["ps", "awk", "head", "grep", "cat", "python3"]:
                    continue
                try:
                    cpu = float(parts[1])
                    mem = float(parts[2])
                    if comm not in totals:
                        totals[comm] = {"cpu": 0.0, "mem": 0.0}
                    totals[comm]["cpu"] += cpu
                    totals[comm]["mem"] += mem
                except ValueError:
                    continue
        
        sorted_procs = sorted(totals.items(), key=lambda x: x[1]["cpu"], reverse=True)[:5]
        max_cpu = max((p[1]["cpu"] for p in sorted_procs), default=100.0)
        max_cpu = max(max_cpu, 10.0)
        
        for name, data in sorted_procs:
            cpu_val = data["cpu"]
            # 12-char bar
            bar_len = int(min(12, max(1, (cpu_val / max_cpu) * 12)))
            bar_str = "█" * bar_len + "░" * (12 - bar_len)
            consumers.append({
                "name": name[:12],
                "cpu": f"{cpu_val:.1f}%",
                "cpu_num": cpu_val,
                "mem": f"{data['mem']:.1f}%",
                "bar": bar_str
            })
    except Exception as e:
        consumers = [{"name": "unavailable", "cpu": "0%", "cpu_num": 0, "mem": "0%", "bar": "░░░░░░░░░░░░"}]
    return consumers

def main():
    cap, status = get_current_battery()
    history = update_history(cap, status)
    # --sample only records a point; the always-on timer in the panel calls it
    # so history accrues while the panel is closed too.
    if "--sample" in os.sys.argv[1:]:
        return
    ascii_graph, spark = make_ascii_graph(history, cap)
    top_consumers = get_top_consumers()
    
    out = {
        "capacity": cap,
        "status": status,
        "spark": spark,
        "ascii_graph": ascii_graph,
        "top_consumers": top_consumers
    }
    print(json.dumps(out))

if __name__ == "__main__":
    main()
