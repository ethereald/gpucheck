#!/usr/bin/env python3
"""Show which processes occupy GPU memory (VRAM) and optionally kill them.

Windows (WDDM) nvidia-smi usually reports "N/A" per-process memory, so this reads
the Windows "GPU Process Memory" performance counters instead. No dependencies.

Usage: python gpucheck.py [--json] [--kill PID [PID ...]] [--no-interactive]
On Windows, the script requests administrator rights on launch.
"""
import argparse
import ctypes
import json
import os
import re
import subprocess
import sys

PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$d = @{}; $s = @{}
foreach ($c in (Get-Counter '\GPU Process Memory(*)\Dedicated Usage').CounterSamples) {
  if ($c.InstanceName -match '^pid_(\d+)_') { $d[$Matches[1]] += [double]$c.CookedValue }
}
foreach ($c in (Get-Counter '\GPU Process Memory(*)\Shared Usage').CounterSamples) {
  if ($c.InstanceName -match '^pid_(\d+)_') { $s[$Matches[1]] += [double]$c.CookedValue }
}
$out = foreach ($k in ($d.Keys + $s.Keys | Sort-Object -Unique)) {
  if (($d[$k] + $s[$k]) -le 0) { continue }
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$k"
  $nm = $p.Name; if (-not $nm) { $nm = (Get-Process -Id $k).ProcessName }
  [pscustomobject]@{ pid = [int]$k; name = $nm; cmd = $p.CommandLine
                     dedicated = [double]$d[$k]; shared = [double]$s[$k] }
}
@($out) | ConvertTo-Json -Compress
"""


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def ensure_admin():
    if os.name != "nt" or ctypes.windll.shell32.IsUserAnAdmin():
        return True

    args = list(sys.argv[1:])
    if not getattr(sys, "frozen", False):
        args.insert(0, os.path.abspath(sys.argv[0]))
    params = subprocess.list2cmdline(args)
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, os.getcwd(), 1
    )
    if result <= 32:
        print(f"Could not start as administrator (ShellExecuteW returned {result}).",
              file=sys.stderr)
        return False
    sys.exit(0)


def gpu_totals():
    out = run(["nvidia-smi.exe", "--query-gpu=name,memory.used,memory.total",
               "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            gpus.append({"name": parts[0], "used_mib": int(parts[1]), "total_mib": int(parts[2])})
    return gpus


def windows_procs():
    out = run(["powershell.exe", "-NoProfile", "-Command", PS_SCRIPT]).strip()
    if not out or out == "null":
        return []
    data = json.loads(out)
    if isinstance(data, dict):
        data = [data]
    procs = [{
        "pid": d["pid"], "name": d.get("name") or "?", "cmd": d.get("cmd") or "",
        "dedicated_mib": round(d["dedicated"] / 1048576, 1),
        "shared_mib": round(d["shared"] / 1048576, 1),
    } for d in data]
    return sorted(procs, key=lambda p: p["dedicated_mib"], reverse=True)


def smi_procs():
    """Fallback (non-Windows or counters unavailable): nvidia-smi compute apps."""
    out = run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
               "--format=csv,noheader,nounits"])
    procs = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3 and parts[0].isdigit():
            mem = float(parts[2]) if re.fullmatch(r"[\d.]+", parts[2]) else 0.0
            procs.append({"pid": int(parts[0]), "name": os.path.basename(parts[1]),
                          "cmd": "", "dedicated_mib": mem, "shared_mib": 0.0})
    return sorted(procs, key=lambda p: p["dedicated_mib"], reverse=True)


def get_procs(gpus=()):
    procs = windows_procs() if os.name == "nt" else []
    procs = procs or smi_procs()
    limit = max((g["total_mib"] for g in gpus), default=None)
    for p in procs:
        # Some drivers report impossible values (stale counters); don't trust them
        p["unreliable"] = bool(limit and p["dedicated_mib"] > limit)
    return sorted(procs, key=lambda p: (p["unreliable"], -p["dedicated_mib"]))


def kill(pid):
    if pid == os.getpid():
        return False
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  {r.stdout.strip() or r.stderr.strip()}")
        return r.returncode == 0
    try:
        os.kill(pid, 9)
        return True
    except OSError:
        return False


def print_table(procs, gpus):
    for g in gpus:
        free = g["total_mib"] - g["used_mib"]
        print(f"{g['name']}: {g['used_mib']} / {g['total_mib']} MiB used ({free} MiB free)")
    if not procs:
        print("No processes using GPU memory found.")
        return
    print(f"\n{'#':<4}{'PID':<8}{'NAME':<24}{'VRAM MiB':>10}{'SHARED MiB':>12}  COMMAND")
    for i, p in enumerate(procs, 1):
        vram = "n/a*" if p["unreliable"] else f"{p['dedicated_mib']:.1f}"
        print(f"{i:<4}{p['pid']:<8}{p['name'][:23]:<24}{vram:>10}"
              f"{p['shared_mib']:>12.1f}  {p['cmd'][:60]}")
    total = sum(p["dedicated_mib"] for p in procs if not p["unreliable"])
    if any(p["unreliable"] for p in procs):
        print("* driver reported an impossible value (larger than total VRAM); ignored")
    print(f"{'':<36}{total:>10.1f}  (sum of listed processes)")
    if gpus:
        gap = sum(g["used_mib"] for g in gpus) - total
        print(f"{'-':<4}{'-':<8}{'(hidden / unattributed)':<24}{gap:>10.1f}{'':>12}  "
              "driver reserve, protected processes, or ignored bogus counters; run as admin to reveal more")


def parse_selection(text, procs):
    pids = set()
    for tok in re.split(r"[,\s]+", text.strip()):
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", tok)
        if m:
            idx = range(int(m.group(1)), int(m.group(2)) + 1)
        elif tok.isdigit():
            idx = [int(tok)]
        else:
            raise ValueError(f"bad token: {tok}")
        for i in idx:
            if not 1 <= i <= len(procs):
                raise ValueError(f"no row {i}")
            pids.add(procs[i - 1]["pid"])
    return pids


def interactive(procs, gpus):
    while procs:
        print_table(procs, gpus)
        text = input("\nRows to kill (e.g. 1,3 or 2-4), 'r' refresh, 'q' quit: ").strip().lower()
        if text in ("q", "quit", ""):
            return
        if text != "r":
            try:
                pids = parse_selection(text, procs)
            except ValueError as e:
                print(f"Invalid selection ({e}).\n")
                continue
            names = ", ".join(f"{p['name']}({p['pid']})" for p in procs if p["pid"] in pids)
            if input(f"Kill {names}? [y/N]: ").strip().lower() == "y":
                for pid in sorted(pids):
                    print(f"{'Killed' if kill(pid) else 'Could not kill'} {pid}")
        gpus = gpu_totals(); procs = get_procs(gpus)
        print()
    print_table(procs, gpus)


def main():
    if not ensure_admin():
        return 1

    ap = argparse.ArgumentParser(description="Show per-process GPU memory usage.")
    ap.add_argument("--json", action="store_true", help="output JSON and exit")
    ap.add_argument("--kill", type=int, nargs="+", metavar="PID", help="kill these PIDs and exit")
    ap.add_argument("--no-interactive", action="store_true", help="just print the table")
    args = ap.parse_args()

    if args.kill:
        for pid in args.kill:
            print(f"{'Killed' if kill(pid) else 'Could not kill'} {pid}")
        return 0

    gpus = gpu_totals(); procs = get_procs(gpus)
    if args.json:
        print(json.dumps({"gpus": gpus, "processes": procs}, indent=2))
    elif args.no_interactive or not sys.stdin.isatty():
        print_table(procs, gpus)
    else:
        try:
            interactive(procs, gpus)
        except (EOFError, KeyboardInterrupt):
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
