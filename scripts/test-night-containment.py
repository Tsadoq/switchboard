import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from agent_board.runner.night_containment import RunPlan, unit_argv, verified_empty


def show(unit: str) -> dict[str, str]:
    result = subprocess.run(
        (
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=Id,LoadState,ActiveState,ControlGroup,InvocationID,Description",
        ),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except FileNotFoundError:
        return False


def exercise(delay: float, refused: bool = False) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="switchboard-night-test-") as directory:
        root = Path(directory)
        for name in ("night", "ordinary", "night-home", "ordinary-home"):
            (root / name).mkdir()
        output = root / "children.json"
        child = (
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "signal.signal(signal.SIGHUP, signal.SIG_IGN); time.sleep(90)"
        )
        body = (
            "import json,os,signal,subprocess,sys,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"children=[subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True) for _ in range(2)]; "
            f"Path({str(output)!r}).write_text(json.dumps([os.getpid(),*[p.pid for p in children]])); "
            "time.sleep(90)"
        )
        run = RunPlan.create(
            run_id=str(uuid4()),
            fence="a" * 43,
            principal="pi@dev",
            seconds=12,
            request_elapsed=0,
            monotonic_now=time.monotonic(),
            boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            uid=os.getuid(),
            home=root / "night-home",
            ordinary_home=root / "ordinary-home",
            workdir=root / "night",
            ordinary_workdir=root / "ordinary",
            command=(sys.executable, "-c", body),
        )
        journal = root / "run.json"
        run.save(journal)
        ordinary = subprocess.Popen((sys.executable, "-c", "import time; time.sleep(90)"))
        try:
            time.sleep(delay)
            started = subprocess.run(
                unit_argv(run, journal, sys.executable),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert started.returncode == 0, started.stderr
            until = time.monotonic() + (3 if refused else 5)
            while time.monotonic() < until:
                if output.exists() or (refused and show(run.unit).get("ActiveState") == "failed"):
                    break
                time.sleep(0.1)
            if refused:
                assert not output.exists() and not journal.with_suffix(".observed.json").exists()
                assert show(run.unit).get("ActiveState") == "failed"
                diagnostic = subprocess.run(
                    ("journalctl", "--user", "-u", run.unit, "--no-pager", "-n", "35"),
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                assert "ContainmentError: expired" in diagnostic.stdout, diagnostic.stdout
                assert ordinary.poll() is None
                return {"passed": True, "launch_delay_seconds": delay, "expired_start_refused": True}
            if not output.exists():
                details = subprocess.run(
                    ("journalctl", "--user", "-u", run.unit, "--no-pager", "-n", "35"),
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                raise AssertionError((show(run.unit), details.stdout, details.stderr))
            pids = json.loads(output.read_text())
            observation = json.loads(journal.with_suffix(".observed.json").read_text())
            assert not verified_empty(
                run,
                show(run.unit),
                run.boot_id,
                observation["invocation"],
                Path("/sys/fs/cgroup"),
            )
            until = run.deadline_monotonic + 3
            while any(alive(pid) for pid in pids) and time.monotonic() < until:
                time.sleep(0.1)
            assert not any(alive(pid) for pid in pids), (pids, show(run.unit))
            assert time.monotonic() <= run.deadline_monotonic, "containment exceeded the hard deadline"
            assert ordinary.poll() is None
            properties = show(run.unit)
            assert verified_empty(
                run,
                properties,
                run.boot_id,
                observation["invocation"],
                Path("/sys/fs/cgroup"),
            ), properties
            return {
                "passed": True,
                "unit": run.unit,
                "launch_delay_seconds": delay,
                "children": len(pids),
                "setsid_sigterm_resistant_children": 2,
                "ordinary_sentinel_survived": True,
                "stop_verified": True,
                "late_seconds": round(time.monotonic() - run.deadline_monotonic, 3),
                "properties": properties,
            }
        finally:
            deadline = run.unit.removesuffix(".service") + "-deadline"
            try:
                subprocess.run(
                    ("systemctl", "--user", "stop", run.unit, deadline + ".timer", deadline + ".service"),
                    capture_output=True,
                    timeout=8,
                    check=False,
                )
                subprocess.run(
                    (
                        "systemctl",
                        "--user",
                        "reset-failed",
                        run.unit,
                        deadline + ".timer",
                        deadline + ".service",
                    ),
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            finally:
                ordinary.terminate()
                ordinary.wait(timeout=5)


def main() -> None:
    evidence = [exercise(0), exercise(3), exercise(7, refused=True)]
    path = Path(os.environ.get("NIGHT_TEST_EVIDENCE", "/tmp/switchboard-night-containment.json"))
    path.write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence))


if __name__ == "__main__":
    main()
