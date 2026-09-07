#!/usr/bin/env python3
"""Measure synthetic USB-event-to-halt-request latency; never halt the Mac.

Uses real inventory probes and an injected inventory difference. Both shutdown
commands are replaced before the sourced engine runs. This excludes macOS's
device-publication delay and the time needed to physically power off.
"""

import argparse
import json
import os
from pathlib import Path
import select
import shlex
import statistics
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def sample(script, scenario):
    with tempfile.TemporaryDirectory(prefix="usb-watchdog-latency-") as directory:
        marker = Path(directory) / "changed"
        state = Path(directory) / "state"
        read_fd, write_fd = os.pipe()
        body = f"""
source {shlex.quote(str(script))}
# Safety: override both privileged sinks before running any engine function.
request_graceful_shutdown() {{ echo UNEXPECTED_GRACEFUL_REQUEST; exit 90; }}
request_forced_halt() {{ echo BENCHMARK_HALT_REQUEST; exit 0; }}
trap 'if declare -F stop_slow_probe >/dev/null; then stop_slow_probe; fi' EXIT
SHUTDOWN_POLICY=force-immediately
DRY_RUN=false
WAKE_GAP_SECONDS=999999
eval "$(declare -f get_usb_snapshot | /usr/bin/sed '1s/get_usb_snapshot/benchmark_usb_snapshot/')"
eval "$(declare -f get_slow_snapshot | /usr/bin/sed '1s/get_slow_snapshot/benchmark_slow_snapshot/')"
FAST_BASE=$(get_fast_snapshot) || exit 91
SLOW_BASE=$(get_slow_snapshot) || exit 92
get_usb_snapshot() {{
    benchmark_usb_snapshot || return 1
    if [[ -f {shlex.quote(str(marker))} ]]; then
        printf '\\nUSB:benchmark-injected-change\\n'
    fi
}}
get_slow_snapshot() {{
    echo BENCHMARK_SLOW_STARTED >&2
    benchmark_slow_snapshot
}}
STATE_FILE={shlex.quote(str(state))}
INSTANCE_TOKEN=0123456789abcdef0123456789abcdef
INSTANCE_STARTED=benchmark
FAST_HEALTHY=true
SLOW_HEALTHY=true
EVENT_MONITOR_ACTIVE=true
EVENT_MONITOR_HEALTHY=true
EVENT_MONITOR_PID=$$
EVENT_LAST_HEARTBEAT=$SECONDS
exec 9<&{read_fd}
SLOW_CYCLES={1 if scenario == "during_slow_probe" else 999999}
echo BENCHMARK_READY >&2
monitor_loop
"""
        process = subprocess.Popen(
            ["/bin/bash", "-c", body],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            pass_fds=(read_fd,),
            start_new_session=True,
        )
        os.close(read_fd)
        started = None
        buffer = b""
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                readable, _, _ = select.select([process.stdout], [], [], 0.5)
                if not readable:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Engine exited before the mock halt request")
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    trigger = (
                        line == b"BENCHMARK_SLOW_STARTED"
                        if scenario == "during_slow_probe"
                        else line == b"BENCHMARK_READY"
                    )
                    if trigger and started is None:
                        marker.touch()
                        started = time.perf_counter()
                        os.write(write_fd, b"usb-published\n")
                    if line == b"BENCHMARK_HALT_REQUEST" and started is not None:
                        return (time.perf_counter() - started) * 1000
            raise RuntimeError("Timed out waiting for the mock halt request")
        finally:
            os.close(write_fd)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                # Only this benchmark's new process group, never an armed app.
                os.killpg(process.pid, 9)
                process.wait(timeout=3)
            process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, help="Optional previous shell engine")
    parser.add_argument("--samples", type=int, default=10)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    scripts = {"current": ROOT / "usb_watchdog.sh"}
    if args.baseline:
        scripts = {"baseline": args.baseline.resolve(), **scripts}
    for label, script in scripts.items():
        for scenario in ("idle", "during_slow_probe"):
            values = [sample(script, scenario) for _ in range(args.samples)]
            print(json.dumps({
                "engine": label,
                "scenario": scenario,
                "samples": len(values),
                "median_ms": round(statistics.median(values), 1),
                "min_ms": round(min(values), 1),
                "max_ms": round(max(values), 1),
            }), flush=True)


if __name__ == "__main__":
    main()
