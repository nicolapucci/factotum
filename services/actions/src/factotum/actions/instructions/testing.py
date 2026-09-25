"""Stand-ins for what skills in instructions mode reach, so that their tests need no network and no
container: a web server on this machine that answers what it is told to, and a runner that runs the
files of a job with the interpreter of the tests, in a folder of its own.

The program that runs a plan is the real one in every test that uses them: what is replaced is the
world it reaches and the container it runs in."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from factotum.contracts.runner import Job, JobResult

_PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


def step_http(name: str, method: str, url: str, **fields: Any) -> dict[str, Any]:
    """A request of a plan, as a model would write it."""
    return {"id": name, "kind": "http", "method": method, "url": url, **fields}


def step_command(name: str, command: str, **args: Any) -> dict[str, Any]:
    """A command of a plan, as a model would write it."""
    return {"id": name, "kind": "command", "name": command, "args": args}


def plan_dict(
    *steps: dict[str, Any], say: str | None = None, reason: str = "as told"
) -> dict[str, Any]:
    """A plan, as a model would write it."""
    return {"reason": reason, "steps": list(steps), "say": say}


@dataclass
class Seen:
    """What a web server was asked."""

    method: str
    target: str
    """What the request line said: a path, or a whole address when it came through a proxy."""

    headers: dict[str, str]
    """By their names in lower case."""

    body: bytes

    @property
    def path(self) -> str:
        return urlsplit(self.target).path

    @property
    def query(self) -> str:
        return urlsplit(self.target).query

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class FakeWeb:
    """A web server on this machine: it keeps what it is asked and answers what it is told to (and
    404 to the rest). It is every host a plan is made to reach."""

    def __init__(self) -> None:
        self.seen: list[Seen] = []
        self.replies: dict[tuple[str, str], Reply] = {}
        self.default = Reply(404)
        self.delay_s = 0.0
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def answer(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        json_body: Any = None,
        text: str | None = None,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if raw is not None:
            body = raw
        elif json_body is not None:
            body = json.dumps(json_body).encode()
        else:
            body = (text or "").encode()
        kind = "application/json" if json_body is not None else "text/plain; charset=utf-8"
        self.replies[(method, path)] = Reply(
            status, body, {"Content-Type": kind, **(headers or {})}
        )

    @property
    def port(self) -> int:
        assert self._server is not None  # noqa: S101 - a server that is not started has no port
        return int(self._server.server_address[1])

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def __enter__(self) -> Self:
        web = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                web.seen.append(
                    Seen(
                        self.command,
                        self.path,
                        {name.lower(): value for name, value in self.headers.items()},
                        body,
                    )
                )
                if web.delay_s:
                    time.sleep(web.delay_s)
                reply = web.replies.get((self.command, urlsplit(self.path).path), web.default)
                self.send_response(reply.status)
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                self.wfile.write(reply.body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:
                return None

        self._server = _Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def clean_environment(**given: str) -> dict[str, str]:
    """The environment of a run in these tests: what Python needs to start, and what the run is
    given, and nothing of the developer's (a proxy, a token)."""
    kept = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in {"SYSTEMROOT", "PATH", "TEMP", "TMP", "LANG", "LC_ALL"}
        and name.lower() not in _PROXY_VARIABLES
    }
    return {**kept, "PYTHONDONTWRITEBYTECODE": "1", **given}


class LocalRunner:
    """The runner service, in this process: it runs the files of a job with the interpreter of the
    tests, in a folder of its own, with what the job gives it and nothing else, and answers what
    the runner answers. It reaches what the machine reaches: there is no sandbox here."""

    def __init__(self, *, proxy: str | None = None) -> None:
        self.jobs: list[Job] = []
        self.results: list[JobResult] = []
        self._proxy = proxy

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        job = Job.model_validate_json(request.content)
        self.jobs.append(job)
        result = self.run(job)
        self.results.append(result)
        return httpx.Response(200, text=result.model_dump_json())

    def run(self, job: Job) -> JobResult:
        began = time.monotonic()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for file in job.files:
                target = root / file.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(file.data)
            environment = clean_environment(**job.env)
            if job.hosts and self._proxy is not None:
                environment["http_proxy"] = environment["https_proxy"] = self._proxy
            try:
                done = subprocess.run(  # noqa: S603 - the interpreter of the tests, and files a test wrote
                    [sys.executable, "-E", "-s", str(root / job.entrypoint)],
                    input=job.stdin,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    cwd=root,
                    env=environment,
                    timeout=job.timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return JobResult(timed_out=True, duration_ms=int((time.monotonic() - began) * 1000))
        return JobResult(
            exit_code=done.returncode,
            stdout=done.stdout[:65_536],
            stderr=done.stderr[:65_536],
            duration_ms=int((time.monotonic() - began) * 1000),
        )
