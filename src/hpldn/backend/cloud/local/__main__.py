"""
Run the local stack: production's nginx, guard daemon and gunicorn on localhost.

    uv run python -m hpldn.backend.cloud.local up [--fake-google] [--build] [--foreground]
    uv run python -m hpldn.backend.cloud.local status
    uv run python -m hpldn.backend.cloud.local unban <address> | --all [--forget]
    uv run python -m hpldn.backend.cloud.local down

``up`` renders the configuration (`render`), checks it with ``nginx -t``,
builds the frontend when asked or when ``build/`` is missing (``npm run
build:prod``: the map-free bundle production ships), then starts:

    api          gunicorn on tmp/local-stack/run/api.sock, 2 hardened uvicorn
                 workers (hpldn-api.service and .socket); HPLDN_API_DOCS=1, so
                 its openapi.json is there for the Playwright setup
    guard        python -m hpldn.backend.guard run (hpldn-guard.service)
    nginx        127.0.0.1:8080, production's hpldn.conf
    watcher      reloads nginx when the guard rewrites banned.conf or
                 crawlers.conf (hpldn-nginx-reload.path)
    fake-google  with --fake-google: siteverify on 127.0.0.1:18999 (`fake_google`)

and waits until ``GET /api/session/config`` answers through nginx.

The whole stack runs on a machine the size of the production host
(`HOST_CPUS`, `STACK_MEMORY_MAX`): ``up`` re-executes itself in a systemd user
scope with ``AllowedCPUs`` and ``MemoryMax``, and every process it starts
inherits that scope. Nothing is tuned inside it: as on the VPS, numpy's
OpenBLAS, CatBoost and nginx's ``worker_processes auto`` size themselves to the
CPUs they can see. Without the scope each gunicorn worker would start one
OpenBLAS thread per CPU of the workstation, and two workers building the local
price index at once would spin every core for minutes. Without
``--foreground`` it then returns and ``down`` stops the stack; with it, Ctrl-C
(or SIGTERM, as Playwright sends) stops everything. Logs are in
``tmp/local-stack/logs/``, pid files in ``tmp/local-stack/run/``.

It refuses to start with ``HPLDN_CAPTCHA_DISABLED=1`` or without the reCAPTCHA
keys: the point is to meet the gate production runs.
"""

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from hpldn.backend.api.config import CAPTCHA_DISABLED_ENV, DOCS_ENV, VERIFY_URL_ENV
from hpldn.backend.cloud.local import fake_google
from hpldn.backend.cloud.local.render import (
    LISTEN,
    RenderError,
    Stack,
    fetch_nginx,
    find_nginx,
    nginx_command,
    render,
)

EDGE_URL = f"http://{LISTEN}"
FAKE_GOOGLE_URL = f"http://{fake_google.HOST}:{fake_google.PORT}{fake_google.PATH}"
READY_PATH = "/api/session/config"
# The first start of a worker loads the model; give it time.
READY_TIMEOUT_S = 600
STOP_TIMEOUT_S = 45
WATCH_INTERVAL_S = 1.0
REQUIRED_KEYS = ("RECAPTCHA_SITE_KEY", "RECAPTCHA_SECRET_KEY", "HPLDN_PASS_SECRET", "HPLDN_API_KEY")
USER_AGENT = "hpldn-local-stack"

# The production host: VM.Standard.A1.Flex, 4 OCPU (cloud/vps/README.md).
HOST_CPUS = 4
# The whole stack's memory ceiling. On the host hpldn-api.service has
# MemoryMax=14G of 24 GB; its two workers use ~6.5 GB once warm.
STACK_MEMORY_MAX = "10G"
# The task ceiling of hpldn-api.service (512) plus room for nginx and the guard.
STACK_TASKS_MAX = 1024
# Set inside the scope, so `up` does not wrap itself twice.
SCOPE_ENV = "HPLDN_LOCAL_STACK_SCOPE"


@dataclass(frozen=True)
class Service:
    """One process of the stack: its name and a word its command line must contain."""

    name: str
    marker: str


# Start order; stopped in reverse.
SERVICES = (
    Service("api", "gunicorn"),
    Service("guard", "hpldn.backend.guard"),
    Service("nginx", "nginx"),
    Service("watcher", "hpldn.backend.cloud.local"),
    Service("fake-google", "fake_google"),
)


def say(message: str) -> None:
    print(f"[local] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------
def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=value`` lines of `path`, parsed as `hpldn.backend.common.utils.load_env_vars` does."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def stack_env(stack: Stack, *, fake_google_on: bool = False) -> dict[str, str]:
    """
    The environment for the stack's processes: ours, then ``develop.env``, then the stack's paths.

    ``HPLDN_RECAPTCHA_VERIFY_URL`` is set to the fake Google with
    `fake_google_on`, and to empty otherwise, so a value in ``develop.env``
    (which the API loads again) cannot point a real-Google run at a fake one.
    """
    env = dict(os.environ)
    for key, value in read_env_file(stack.repo / "develop.env").items():
        env.setdefault(key, value)
    env.setdefault("HPLDN_DIR_FP", str(stack.repo))
    env.update(stack.guard_env())
    env[VERIFY_URL_ENV] = FAKE_GOOGLE_URL if fake_google_on else ""
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Local only: serve openapi.json for e2e/global-setup.ts (never set on the host).
    env[DOCS_ENV] = "1"
    return env


def check_parity(env: dict[str, str]) -> None:
    """Refuse a stack that would not meet production's gate."""
    if env.get(CAPTCHA_DISABLED_ENV) == "1":
        sys.exit(
            f"[local] {CAPTCHA_DISABLED_ENV}=1 is set: the local stack runs the reCAPTCHA gate as production "
            "does. Unset it (it is for the pytest suites only)."
        )
    missing = [key for key in REQUIRED_KEYS if not env.get(key)]
    if missing:
        sys.exit(f"[local] {', '.join(missing)} not set in the environment or develop.env.")


# ---------------------------------------------------------------------
# The host's shape
# ---------------------------------------------------------------------
def host_cpus() -> list[int]:
    """The CPUs the stack runs on: the first `HOST_CPUS` this process may use."""
    return sorted(os.sched_getaffinity(0))[:HOST_CPUS]


def _cpu_list(cpus: list[int]) -> str:
    return ",".join(str(cpu) for cpu in cpus)


def enter_host_scope(argv: list[str]) -> None:
    """
    Re-execute this command in a systemd user scope the size of the production host; returns only without one.

    The scope (``hpldn-local-stack-<pid>.scope``) has ``AllowedCPUs`` set to
    `HOST_CPUS` CPUs and ``MemoryMax`` to `STACK_MEMORY_MAX`; ``exec`` keeps
    the pid, so Ctrl-C and Playwright's SIGTERM still reach `cmd_up`. Without
    ``systemd-run --user`` the stack is only pinned to the CPUs, with a warning
    that memory is not capped.
    """
    if os.environ.get(SCOPE_ENV) == "1":
        return
    cpus = host_cpus()
    systemd_run = shutil.which("systemd-run")
    if systemd_run is not None:
        command = [
            systemd_run, "--user", "--scope", "--quiet", "--collect",
            f"--unit=hpldn-local-stack-{os.getpid()}",
            "--description=HPLDN local stack (the production host's size)",
            "-p", f"AllowedCPUs={_cpu_list(cpus)}",
            "-p", f"MemoryMax={STACK_MEMORY_MAX}",
            "-p", "MemorySwapMax=0",
            "-p", f"TasksMax={STACK_TASKS_MAX}",
        ]
        probe = subprocess.run([*command[:5], *command[7:], "--", "true"], capture_output=True, text=True)
        if probe.returncode == 0:
            os.environ[SCOPE_ENV] = "1"
            sys.stderr.flush()
            os.execv(systemd_run, [*command, "--", sys.executable, "-m", "hpldn.backend.cloud.local", *argv])
        say(f"systemd-run --user could not make the stack's scope ({probe.stderr.strip()})")
    os.sched_setaffinity(0, cpus)
    os.environ[SCOPE_ENV] = "1"
    say(f"pinned to CPUs {_cpu_list(cpus)} only; memory is NOT capped at {STACK_MEMORY_MAX}")


def cgroup_dir(pid: int) -> Path | None:
    """The cgroup v2 directory of `pid`, when it can be read."""
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    except OSError:
        pass
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return "?"


# ---------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------
def pid_file(stack: Stack, name: str) -> Path:
    return stack.run / f"{name}.pid"


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def running_pid(stack: Stack, service: Service) -> int | None:
    """The pid in the service's pid file, when that process is alive and still the service."""
    try:
        pid = int(pid_file(stack, service.name).read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    return pid if service.marker in _cmdline(pid) else None


def socket_in_use(path: Path) -> bool:
    """True when something accepts connections on the unix socket `path`."""
    if not path.exists():
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        try:
            sock.connect(str(path))
        except OSError:
            return False
    return True


def port_in_use(address: str) -> bool:
    host, port = address.rsplit(":", 1)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, int(port))) == 0


def start(stack: Stack, service: Service, command: list[str], env: dict[str, str], *, detach: bool) -> subprocess.Popen:
    """Start `command`, logging to logs/<name>.log; `detach` puts it in its own session (background mode)."""
    log_path = stack.logs / f"{service.name}.log"
    with open(log_path, "ab") as log:
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(command)}\n".encode())
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=stack.repo,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=detach,
        )
    pid_file(stack, service.name).write_text(f"{process.pid}\n")
    say(f"{service.name:<12} pid {process.pid}  log {log_path.relative_to(stack.repo)}")
    return process


def stop(stack: Stack, service: Service) -> bool:
    """SIGTERM the service (and its process group when it leads one), SIGKILL after `STOP_TIMEOUT_S`."""
    pid = running_pid(stack, service)
    pid_file(stack, service.name).unlink(missing_ok=True)
    if pid is None:
        return False
    group = os.getpgid(pid) == pid

    def send(sig: int) -> None:
        try:
            os.killpg(pid, sig) if group else os.kill(pid, sig)
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        # A child of this process (foreground mode) must be reaped to disappear.
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                break
        except ChildProcessError:
            pass
        time.sleep(0.2)
    else:
        say(f"{service.name} did not stop in {STOP_TIMEOUT_S}s; killing it")
        send(signal.SIGKILL)
    say(f"stopped {service.name} (pid {pid})")
    return True


def stop_all(stack: Stack) -> int:
    return sum(stop(stack, service) for service in reversed(SERVICES))


def tail(path: Path, lines: int = 20) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def ready(timeout: float, processes: dict[str, subprocess.Popen], stack: Stack) -> bool:
    """Wait for ``GET /api/session/config`` through nginx; False when a process dies or time runs out."""
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(EDGE_URL + READY_PATH, headers={"User-Agent": USER_AGENT})
    while time.monotonic() < deadline:
        for name, process in processes.items():
            if process.poll() is not None:
                say(f"{name} exited with {process.returncode}; its log ends:\n{tail(stack.logs / f'{name}.log')}")
                return False
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    say(f"{EDGE_URL}{READY_PATH} did not answer 200 within {timeout:.0f}s")
    return False


# ---------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------
def nginx_binary(stack: Stack) -> Path:
    binary = find_nginx(stack)
    if binary is None:
        say("nginx is not installed; unpacking Ubuntu's package into the stack (fetch_nginx.sh)")
        binary = fetch_nginx(stack)
    return binary


def cmd_up(stack: Stack, args) -> int:
    env = stack_env(stack, fake_google_on=args.fake_google)
    check_parity(env)
    enter_host_scope(args.argv)
    env[SCOPE_ENV] = "1"

    alive = [service.name for service in SERVICES if running_pid(stack, service)]
    if alive:
        sys.exit(f"[local] already running ({', '.join(alive)}); `down` first.")
    busy = [a for a in (LISTEN,) + ((f"{fake_google.HOST}:{fake_google.PORT}",) if args.fake_google else ()) if port_in_use(a)]
    if socket_in_use(stack.api_socket):
        busy.append(str(stack.api_socket))
    if busy:
        sys.exit(f"[local] {', '.join(busy)} already in use by another process (ss -lp shows which); stop it first.")
    # A socket file left by a killed gunicorn would stop it binding.
    stack.api_socket.unlink(missing_ok=True)

    binary = nginx_binary(stack)
    try:
        render(stack, api_key=env.get("HPLDN_API_KEY", ""), nginx=binary)
    except RenderError as exc:
        sys.exit(f"[local] render failed: {exc}")
    test = subprocess.run(nginx_command(stack, binary, "-t", "-q"), capture_output=True, text=True)
    if test.returncode != 0:
        sys.exit(f"[local] nginx -t failed:\n{test.stderr}")
    say(f"rendered {stack.root.relative_to(stack.repo)}; nginx -t ok ({binary})")

    if args.build or not (stack.build / "index.html").is_file():
        say("building the frontend (npm run build:prod: no source maps)")
        build_env = {key: value for key, value in os.environ.items() if key != "REACT_APP_API_BASE"}
        build_env["GENERATE_SOURCEMAP"] = "false"
        subprocess.run(["npm", "run", "build:prod", "--silent"], cwd=stack.build.parent, env=build_env, check=True)

    detach = not args.foreground
    commands = {
        "api": [
            sys.executable, "-m", "gunicorn", "hpldn.backend.api.main:app",
            "-k", "hpldn.backend.api.worker.HardenedUvicornWorker",
            "--workers", "2",
            # The host's systemd socket is hpldn:www-data 0660; here gunicorn
            # binds the stand-in itself, owner-only.
            "--bind", f"unix:{stack.api_socket}",
            "--umask", "0o077",
            "--timeout", "360",
            "--graceful-timeout", "30",
            "--limit-request-line", "4094",
            "--limit-request-fields", "50",
            "--limit-request-field_size", "8190",
            # No --access-logfile: the API logs each request itself, with the
            # time and the real client (api/access_log.py).
        ],
        "guard": [sys.executable, "-m", "hpldn.backend.guard", "run"],
        "nginx": nginx_command(stack, binary, "-g", "daemon off;"),
        "watcher": [sys.executable, "-m", "hpldn.backend.cloud.local", "watch", "--nginx", str(binary)],
        "fake-google": [sys.executable, "-m", "hpldn.backend.cloud.local.fake_google"],
    }
    if args.fake_google:
        say(f"fake Google: the API checks tokens at {FAKE_GOOGLE_URL}")
    processes: dict[str, subprocess.Popen] = {}
    stopping = False

    def _stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    try:
        for service in SERVICES:
            if service.name == "fake-google" and not args.fake_google:
                continue
            processes[service.name] = start(stack, service, commands[service.name], env, detach=detach)
            if service.name == "guard":
                # nginx logs to the guard's socket from its first request.
                deadline = time.monotonic() + 10
                while not stack.socket.exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
        if not ready(READY_TIMEOUT_S, processes, stack):
            stop_all(stack)
            return 1
        say(f"up: {EDGE_URL} (site and /api), API {stack.api_socket}" + (", fake Google" if args.fake_google else ""))
        say(f"host shape: CPUs {_cpu_list(sorted(os.sched_getaffinity(0)))}, {scope_summary(os.getpid())}")
        if detach:
            say("stop it with: uv run python -m hpldn.backend.cloud.local down")
            return 0
        say("Ctrl-C stops it")
        while True:
            for name, process in processes.items():
                if process.poll() is not None:
                    say(f"{name} exited with {process.returncode}; stopping the stack. Its log ends:\n"
                        f"{tail(stack.logs / f'{name}.log')}")
                    stop_all(stack)
                    return 1
            time.sleep(1)
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        say("stopping")
        stop_all(stack)
        return 0 if stopping or not detach else 130


def cmd_down(stack: Stack, _args) -> int:
    if stop_all(stack) == 0:
        say("nothing was running")
    return 0


def active_bans(stack: Stack) -> list:
    from hpldn.backend.guard.store import Store

    database = stack.guard_state / "guard.db"
    if not database.is_file():
        return []
    store = Store(database)
    try:
        return store.active_bans(time.time())
    finally:
        store.close()


def scope_summary(pid: int) -> str:
    """The cgroup of `pid`: its name, memory in use and ceiling, and CPUs."""
    directory = cgroup_dir(pid)
    if directory is None or not (directory / "memory.current").exists():
        return "no cgroup limits"
    current = _read(directory / "memory.current")
    used = f"{int(current) / 2**30:.1f} GiB" if current.isdigit() else current
    ceiling = _read(directory / "memory.max")
    ceiling = f"{int(ceiling) / 2**30:.0f} GiB" if ceiling.isdigit() else ceiling
    return f"{directory.name}: memory {used} of {ceiling}, CPUs {_read(directory / 'cpuset.cpus.effective')}"


def cmd_status(stack: Stack, _args) -> int:
    for service in SERVICES:
        pid = running_pid(stack, service)
        print(f"{service.name:<12} {'running, pid ' + str(pid) if pid else 'stopped'}")
    api = running_pid(stack, SERVICES[0])
    if api is not None:
        print(f"scope        {scope_summary(api)}")
    print(f"{LISTEN:<16} {'listening' if port_in_use(LISTEN) else 'closed'}")
    print(f"{'api.sock':<16} {'listening' if socket_in_use(stack.api_socket) else 'closed'}")
    bans = active_bans(stack)
    now = time.time()
    for ban in bans:
        print(f"ban {ban.key:<43} {ban.rule:<10} level {ban.level}  {ban.left(now)}")
    print(f"{len(bans)} active bans")
    return 0


def cmd_unban(stack: Stack, args) -> int:
    if bool(args.address) == bool(args.all):
        sys.exit("[local] unban takes an address or --all")
    keys = [ban.key for ban in active_bans(stack)] if args.all else [args.address]
    env = stack_env(stack)
    failed = 0
    for key in keys:
        command = [sys.executable, "-m", "hpldn.backend.guard", "unban", key] + (["--forget"] if args.forget else [])
        failed += subprocess.run(command, env=env, cwd=stack.repo).returncode != 0
    if args.all:
        say(f"{len(keys)} bans lifted" if keys else "no active bans")
    return 1 if failed else 0


def cmd_watch(stack: Stack, args) -> int:
    """
    The stand-in for hpldn-nginx-reload.path: reload nginx when the guard rewrites its includes.

    A file that fails ``nginx -t`` is not loaded, as on the host.
    """
    binary = Path(args.nginx)
    watched = (stack.banned_conf, stack.crawlers_conf)

    def mtimes() -> tuple:
        return tuple(path.stat().st_mtime_ns if path.exists() else None for path in watched)

    last = mtimes()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        while True:
            time.sleep(WATCH_INTERVAL_S)
            now = mtimes()
            if now == last:
                continue
            last = now
            test = subprocess.run(nginx_command(stack, binary, "-t", "-q"), capture_output=True, text=True)
            if test.returncode != 0:
                print(f"nginx -t failed; keeping the running configuration:\n{test.stderr}", flush=True)
                continue
            reload = subprocess.run(nginx_command(stack, binary, "-s", "reload"), capture_output=True, text=True)
            print(f"{time.strftime('%H:%M:%S')} reloaded nginx ({reload.returncode}) {reload.stderr.strip()}", flush=True)
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hpldn.backend.cloud.local", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up", help="render, then start nginx, the API, the guard and the reload watcher")
    up.add_argument("--fake-google", action="store_true", help=f"check reCAPTCHA tokens with a fake Google on {FAKE_GOOGLE_URL}")
    up.add_argument("--build", action="store_true", help="npm run build first (also done when build/ is missing)")
    up.add_argument("--foreground", action="store_true", help="stay attached; Ctrl-C or SIGTERM stops the stack")
    commands.add_parser("down", help="stop a stack started in the background")
    commands.add_parser("status", help="the stack's processes, ports and active bans")
    unban = commands.add_parser("unban", help="lift a ban (python -m hpldn.backend.guard unban, local paths)")
    unban.add_argument("address", nargs="?")
    unban.add_argument("--all", action="store_true", help="lift every active ban")
    unban.add_argument("--forget", action="store_true", help="also forget the strikes, so the next ban starts short")
    watch = commands.add_parser("watch", help=argparse.SUPPRESS)
    watch.add_argument("--nginx", required=True)
    args = parser.parse_args(argv)
    args.argv = sys.argv[1:] if argv is None else list(argv)

    stack = Stack.default()
    handler = {"up": cmd_up, "down": cmd_down, "status": cmd_status, "unban": cmd_unban, "watch": cmd_watch}[args.command]
    return handler(stack, args)


if __name__ == "__main__":
    sys.exit(main())
