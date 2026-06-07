"""One-command local POC harness: launches the Lumos mock server, waits
for it to bind, then launches Streamlit with the two Lumos env vars wired
through. Streams both processes' stdout/stderr to this terminal with
provider-tagged line prefixes. Ctrl-C cleanly tears both down.

  python _tftool/run_with_lumos_mock.py

That single command is equivalent to running the mock server in one
terminal and `streamlit run app.py` in another with
`LUMOS_ACCESS_TOKEN=lsk_LOCAL_MOCK_TOKEN_FOR_DEV_ONLY` and
`LUMOS_SERVER_URL=http://127.0.0.1:8765` set, but with all output
multiplexed onto a single console.

Optional flags:
  --mock-port N        Override the mock server port (default 8765)
  --streamlit-port N   Override the Streamlit port (default 8501)
  --no-streamlit       Start the mock server only (handy when you want to
                       hit the API with curl / the LumosClient REPL
                       without spinning up the UI)

Why a Python wrapper instead of a shell oneliner: PowerShell pre-7 does
not support `&&` chaining (per `[[powershell-oneliner-commands]]`), and
launching two long-lived processes in one shell pipe with reliable
Ctrl-C teardown is awkward in any shell. The Python wrapper handles
readiness, env-var injection, signal forwarding, and prefixed log
streaming with no platform-specific gymnastics.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK_SCRIPT = REPO_ROOT / "_tftool" / "lumos_mock_server.py"
APP_SCRIPT = REPO_ROOT / "app.py"
DEFAULT_TOKEN = "lsk_LOCAL_MOCK_TOKEN_FOR_DEV_ONLY"


def _wait_for_http(url: str, timeout: float = 10.0) -> bool:
    """Poll an HTTP endpoint until it returns ANY response (incl. 401/404),
    which is enough to confirm the server is bound and parsing requests.

    Uses a real HTTP GET instead of a raw TCP probe so the mock's
    stdlib `http.server` doesn't log a malformed-request stack trace on
    every readiness check.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return True
        except urllib.error.HTTPError:
            # Any HTTP response (incl. 401 from the auth check) means the
            # server is up and parsing HTTP.
            return True
        except (urllib.error.URLError, ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


def _stream_output(proc: subprocess.Popen, prefix: str) -> None:
    """Tag each line from `proc.stdout` with `[prefix]` and forward to
    this process's stdout. Runs on a dedicated thread per child."""
    assert proc.stdout is not None
    for raw in proc.stdout:
        sys.stdout.write(f"[{prefix}] {raw}")
        sys.stdout.flush()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mock-port", type=int, default=8765)
    p.add_argument("--streamlit-port", type=int, default=8501)
    p.add_argument("--no-streamlit", action="store_true",
                   help="Start the mock server only; skip the Streamlit UI.")
    args = p.parse_args()

    if not MOCK_SCRIPT.exists():
        sys.stderr.write(f"missing: {MOCK_SCRIPT}\n")
        return 2
    if not args.no_streamlit and not APP_SCRIPT.exists():
        sys.stderr.write(f"missing: {APP_SCRIPT}\n")
        return 2

    procs: list[subprocess.Popen] = []
    threads: list[threading.Thread] = []

    def _spawn(cmd: list[str], prefix: str, env: dict | None = None) -> subprocess.Popen:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=str(REPO_ROOT),
        )
        t = threading.Thread(target=_stream_output, args=(proc, prefix), daemon=True)
        t.start()
        procs.append(proc)
        threads.append(t)
        return proc

    print(f"[harness] starting Lumos mock server on port {args.mock_port}")
    mock = _spawn(
        [sys.executable, str(MOCK_SCRIPT), str(args.mock_port)],
        prefix="mock",
    )

    if not _wait_for_http(f"http://127.0.0.1:{args.mock_port}/apps", timeout=10.0):
        sys.stderr.write(f"[harness] mock server did not bind on port {args.mock_port} within 10s\n")
        mock.terminate()
        return 1
    print(f"[harness] mock ready on http://127.0.0.1:{args.mock_port}")

    if args.no_streamlit:
        print("[harness] --no-streamlit set; press Ctrl-C to stop the mock")
        try:
            mock.wait()
        except KeyboardInterrupt:
            print("\n[harness] Ctrl-C received; stopping mock")
            mock.terminate()
        return 0

    env = os.environ.copy()
    env["LUMOS_ACCESS_TOKEN"] = DEFAULT_TOKEN
    env["LUMOS_SERVER_URL"] = f"http://127.0.0.1:{args.mock_port}"

    print(f"[harness] starting Streamlit on port {args.streamlit_port} with LUMOS_* env vars injected")
    streamlit = _spawn(
        [
            sys.executable, "-m", "streamlit", "run", str(APP_SCRIPT),
            "--server.port", str(args.streamlit_port),
            "--server.headless", "true",
        ],
        prefix="ui",
        env=env,
    )

    try:
        while True:
            if mock.poll() is not None:
                sys.stderr.write(f"[harness] mock exited unexpectedly (code {mock.returncode}); stopping Streamlit\n")
                streamlit.terminate()
                break
            if streamlit.poll() is not None:
                sys.stderr.write(f"[harness] Streamlit exited (code {streamlit.returncode}); stopping mock\n")
                mock.terminate()
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[harness] Ctrl-C received; stopping both processes")
        mock.terminate()
        streamlit.terminate()

    for proc in procs:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return 0


if __name__ == "__main__":
    sys.exit(main())
