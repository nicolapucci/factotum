# The runner and the egress proxy

The script of a skill is somebody's code, and it runs on the machine that holds the household's
documents, next to a database and services with keys. Two services make that safe enough for code
that a household wrote or trusts ([ADR 0015](decisions/0015-the-runner-a-container-for-each-run-and-a-proxy-that-checks-a-grant.md),
which refines [ADR 0006](decisions/0006-skills-and-sandboxed-execution.md)):

* the **runner** runs a script in **a container of its own, made for the run and thrown away after
  it**, and says what the script wrote;
* the **egress proxy** is **the only way out** of that container, and lets a run reach the hosts its
  skill was granted and nothing else.

Neither knows what a skill, a tenant, a device or a model is. The runner is given a *job* (files to
run, what to feed them, secrets, hosts, a time limit); the proxy is given a *grant* (which hosts,
until when) that only the runner can make. Everything about *whose* skill it is, *which* secrets it
may have and *what* to do with what it printed is the actions service's, on its side of the wire
([actions.md](actions.md)).

*Implemented* (slice 5b): both services, their images and compose files, the actions service
running scripts through the one and giving devices commands through the edge, and secrets for
skills (a file, for development). *Since* (slice 6): a skill that is only instructions is run by
this same runner with `executor.py`, the program that does what a plan says, as the one file of the
job ([`skills.md`](skills.md)). *Not yet*: linked accounts (the hub, slice 7), any language but Python,
a stronger sandbox for a cloud (gVisor or Kata, ADR 0009).

## A run

```mermaid
sequenceDiagram
    participant A as Actions service
    participant R as Runner
    participant D as Container engine
    participant S as Container of the run
    participant E as Egress proxy
    participant W as A host it was granted
    A->>A: files of the enabled version, secrets of the owner, hosts of the grant
    A->>R: POST /v1/jobs (the job)
    R->>D: create (as the runner decides), put the files, start
    D->>S: python -E -s shim.py /skill/scripts/run.py
    S->>E: request for a listed host, with the grant as the password
    E->>E: signature, time, host listed, every address it names
    E->>W: connects to the address it checked
    W-->>S: what it answers (through the proxy)
    S-->>D: JSON on standard output
    R->>D: wait, read what was written, remove the container
    R-->>A: exit code, stdout, stderr (secrets taken out)
    A->>A: read it as {message, data, commands}; check; give commands to a device
```

## What a job can say, and what it cannot

A job (`factotum.contracts.runner.Job`) has:

| Field | |
| --- | --- |
| `label` | what to call it in a log (the name of the skill) |
| `files` | the folder of the skill: up to 50 files, 1 MB each, 5 MB in all; paths of letters, digits, `.`, `-` and `_`, never a way out of the folder; base64 |
| `entrypoint` | the script to run: one of the files, and a Python one (`.py`) |
| `stdin` | what the script reads on standard input (the JSON of its parameters), up to 64 KB |
| `env` | the secrets it was granted, by name (capitals; not a name the runner sets or that changes how a program starts: `PATH`, `HOME`, `HTTPS_PROXY`, anything that begins with `PYTHON`, `LD_` or `FACTOTUM_`), up to 20 |
| `hosts` | the hosts it may reach through the proxy: a name, a name with `*.` in front, or an address; up to 20; none means no network |
| `timeout_s` | 1 to 120 seconds |

**It cannot say what the container is like.** There is no field for an image, a mount, a
capability, a network, a limit, a user, a command or a device. That is the point: whoever can ask the
runner to run a job can run a script, and nothing else.

What comes back (`JobResult`): the exit code (nothing when it was stopped), what it wrote to
standard output and standard error (each up to 64 KB, the start of it when it wrote more), whether it
timed out, whether the engine killed it for memory, and how long it took. **All of it is what a skill
said, and none of it is trusted.**

## What a container is like

Decided in one place (`factotum.runner.sandbox.container_body`) from the runner's settings, so that
what a run is like is what the runner is set to and not what a job asks for:

| | Why |
| --- | --- |
| a stock Python image (`python:3.13-slim`), which the runner **never pulls or builds** | nothing of ours or of the household's is in it but the skill's own files, so there is nothing in the image to find |
| **user `10002:10002`**, `no-new-privileges`, **every capability dropped**, not privileged, no ports published | a script is nobody in particular and cannot become somebody |
| **128 MB of memory, swap included** (`Memory` = `MemorySwap`), **0.5 CPU**, **64 processes**, 256 open files, no core files | a script that uses everything is killed (`oom_killed`), one that forks without end runs out of processes |
| `/tmp` is a **16 MB tmpfs, `noexec,nosuid,nodev`**, and it is the only place a script can write | a script cannot leave anything behind, and cannot run what it writes |
| `IpcMode none` | no shared memory with anything |
| **256 KB of log** kept by the engine | a script that writes without end cannot fill a disk |
| **no network** (`NetworkMode none`), unless the job has hosts and the runner has a proxy, and then only the sandbox network | see below |
| the environment is a few fixed variables, **the secrets the job carries** and, with hosts, the proxy and its grant | nothing of the runner's environment is in it |
| `python -E -s` | the interpreter ignores `PYTHON*` variables and the user's site directory |
| **always removed**, whatever happened (it ended, it was killed for being slow, the engine failed half way, the caller went away), and a sweeper removes what a runner that died left, by label | nothing accumulates |

**Why the root filesystem is not read-only.** ADR 0006 said the runner would have a read-only
filesystem. The engine refuses to put files into a container whose root is marked read-only (`PUT
/containers/{id}/archive` fails), and putting the skill's files into the container is what makes it
possible to keep them out of the image. What the flag is for is done otherwise: the files are put in
as owned by root and the script is not root, so `/skill`, `/opt/factotum`, `/etc` and `/usr` cannot
be written by it, and the only thing it can write is the tmpfs. A test on a real engine says so
(below).

**How a script gets its input.** The runner does not attach to the container to write to its standard
input (one channel less while a script runs), so a small shim (`/opt/factotum/shim.py`, put in with
the files) opens `/opt/factotum/input.json`, makes it the standard input and starts the script in its
place.

## The network

A container is on **no network at all** unless the job lists hosts. With hosts, it is on the
**sandbox network** (compose names it `factotum-sandbox` and makes it `internal`, so it has no way out
of its own), where there is one thing: the egress proxy. It is given `HTTP_PROXY`, `HTTPS_PROXY` (and
the lower-case ones) pointing at the proxy with a user name and, as the password, **the grant**.
Programs that honour those variables (Python's `urllib`, which is what the example skills use) go
through the proxy without knowing; a program that does not cannot get out.

### The grant

The runner makes it for each run: `v1.<payload>.<signature>`, where the payload is the base64 of
`{"r": run, "h": [hosts], "x": expires_at}` and the signature is HMAC-SHA-256 with a key that **only
the runner and the proxy have** (`FACTOTUM_EGRESS_KEY`). It is good until a little after the run
should have ended. The proxy keeps nothing: it needs no list of runs to be kept in step with the
runner, only the key. A script can read its own grant (it is in its environment); that does no harm,
because it says what that script was let reach, cannot be changed, and stops being good.

### What the proxy checks

For every request, in this order, and refuses (with a status and, in its log, the reason) at the first
that fails:

1. **The grant is good**: the signature, the version, the shape, the time. Otherwise `407`.
2. **The host is one the grant lists.** A name, a name with `*.` in front (`*.example.org` is any
   name below `example.org`, not `example.org` itself and not `evilexample.org`), or an address, which
   is only ever matched by itself. Otherwise `403`; the name is not even looked up.
3. **Every address the host resolves to is one a skill may reach**, not the first: a name that answers
   with a good address and a bad one gets neither. Whatever the grant says:

   | Address | |
   | --- | --- |
   | this machine (loopback, unspecified) | never |
   | link-local (where a cloud's metadata service is), multicast, reserved | never |
   | an IPv4 address written as IPv6 (mapped, 6to4, NAT64) | it is the IPv4 address it wraps |
   | **private** (the LAN: a lamp, a hub, a router) | **only for a host the grant names whole**, never for a domain (`*.example.org`), because a domain can answer with an address in the house |
   | public | any listed host |

4. **A tunnel (`CONNECT`) is only made to the ports of `FACTOTUM_EGRESS_CONNECT_PORTS`** (443 by
   default). A request in the clear (a home server on `:8123`) may go to any port of a host that was
   listed.
5. **It connects to the address it checked**, never to the name again, so a name that answers
   differently a second time gets nothing.

It speaks no more of HTTP than it must: a request in the clear is sent on with its connection closed
after it, nothing is kept alive and no chunked body is accepted (`501`); a tunnel is copied as it
comes and never read. Anything that is not plainly a request is refused (`400`, `431`, `408`, `413`).
There are limits on all of it: 64 connections at once (`503` and *try again*), 10 seconds to say what
a request is, 10 to connect, 30 of silence, 130 in all and 20 MB through a connection.

### What the log says

Every decision is one line, with the run, the target and the reason and **never what was asked for**
(a path, a header, a body): `egress allowed`, `egress refused ... "the grant does not list the host"`,
`"the port is not one for a tunnel"`, `"an address of it: it is this machine"`. A grant that is not
good is logged without saying which part was wrong.

## Secrets

The actions service resolves them and the runner carries them. **The actions service** gives a job
only the secrets that (a) the version declares, (b) were granted when it was enabled and (c) belong
to **the skill's owner**: a skill a person wrote for themselves that asks for `HA_TOKEN` is given
theirs, or does not run; it is never given the household's, however it is granted. **The runner**
puts them in the environment of the container and nowhere else, and takes their values (and the
grant) out of what the script wrote before it is answered, so a traceback that prints a URL with a
token in it does not leak it. They are in no log, reply or audit record; an error names a secret
(`the secret HA_TOKEN is not set`) and never says a value.

Until the hub keeps them (slice 7) they are in a file for development,
[`config/secrets.example.yaml`](../config/secrets.example.yaml) (copy it to `config/secrets.dev.yaml`,
which git ignores): a `tenant` section, for the skills of the household, and a `user` section, for the
development user's. The service refuses to read it outside development and tests. A skill that needs
a secret that is not there fails and says which.

## What a script prints

It prints **one JSON object** on standard output and exits with 0: `{"message": ..., "data": ...,
"commands": [...]}`, and anything it wants to log goes to standard error. That is read as a
`SkillOutput`, strictly:

* a `message` is spoken as it is (up to 2000 characters); `data` is for whoever asked, never spoken;
  `commands` (up to 20) are `factotum.contracts.commands` and nothing else. **A command is data**: the
  platform gives it to a device, after `authorize()` and after checking that the device declared what
  it needs, so the sandbox needs no way back into the platform.
* **A field that is not one of the three is a failure**, and the message names the field (`messsage:
  Extra inputs are not permitted`): a script that misspells a key would otherwise be silent. A script
  cannot say that nothing was run (`dry_run`), and cannot choose the device.
* Nothing at all on standard output is *nothing to say* (the reply is *Done.*).

What goes wrong, and what the person hears (the trail says which):

| What happened | Outcome | The person hears |
| --- | --- | --- |
| the runner cannot be reached, is busy (`503`), does not know the caller (`401`, `403`) | `runner_unavailable` | *I cannot run that right now.* |
| the script ended with a code that is not 0, was killed for memory, took too long | `skill_failed` | *It did not work.* |
| what it printed is not one JSON document, or not a `SkillOutput` | `skill_failed` | *It did not work.* |
| a secret is not set, a file is gone, the device has no host to reach | `skill_failed` | *It did not work.* |
| the skill needs a linked account | `skill_failed` (accounts come with the hub) | *It did not work.* |
| a command was not done (the device is not connected, it took too long, the edge is away) | `command_failed` | *I asked the device, and it did not do it.* |

## The runner's API

| Route | What |
| --- | --- |
| `POST /v1/jobs` | run a job (up to 8 MB) and answer what it did; a script that failed is an answer, what is an error is that nothing could run it (`503`) |
| `GET /healthz`, `GET /readyz` | liveness, and readiness: the engine answers and has the image |

The caller is authenticated like every service (development headers until the hub). The runner
**does not start outside development and tests** until the hub issues real credentials.

## Deploying it

`docker compose --profile services up -d --build --wait` brings up, with the others, `egress`,
`sandbox-image` (a one-shot that makes sure the image scripts run in is on the engine) and `runner`.
`.env` needs **`FACTOTUM_EGRESS_KEY`** (32 characters or more:
`python -c "import secrets; print(secrets.token_urlsafe(48))"`); it has no default, and without one
the proxy and the runner do not start and say why.

* **The socket.** The runner is the only service given the socket of the engine
  (`/var/run/docker.sock`), and **that is as much as being root on the machine**: whoever controls
  the runner controls the engine. Hence it does nothing but jobs, cannot be told what a container is
  like, listens on loopback, runs as a user that owns nothing, with no capability, no new privileges
  and a read-only root, and the deployment tests say that nothing else mounts the socket. It is let
  read the socket without being root by the group that owns it: `FACTOTUM_DOCKER_GID` (`stat -c %g
  /var/run/docker.sock` on Linux; `0`, the default, on Docker Desktop).
* **The networks.** `sandbox` (named `factotum-sandbox`, `internal`) has the egress proxy on it and
  nothing else. `egress` is what the proxy goes out through and has nothing else on it. The proxy is
  on both and on no other, so **a script cannot reach the database or another service through it**,
  and the runner is on neither. The proxy can reach the machine as `host.docker.internal` (a home
  server on the host), and a LAN address, when a skill names it.
* **Names.** The proxy resolves names with the DNS of the engine, which usually cannot resolve `.local`
  names (mDNS), so a device is best reached by its address, or by a name the DNS of the machine
  knows. A skill names the device by `${device.host}`, which is whatever its binding says.
* **Runs by hand** (`uv run python -m factotum.runner`, `-m factotum.egress`) take the settings below
  from the environment; the runner needs an engine it can reach (`FACTOTUM_RUNNER_DOCKER_HOST`, a
  socket or `tcp://host:2375`).

### Configuration

`FACTOTUM_RUNNER_*` and `FACTOTUM_EGRESS_*`, all in [`.env.example`](../.env.example).

| Variable | Default | |
| --- | --- | --- |
| `RUNNER_HOST`, `_PORT` | `127.0.0.1`, `8086` | loopback unless told otherwise |
| `RUNNER_DOCKER_HOST` | `unix:///var/run/docker.sock` | where the engine is |
| `RUNNER_IMAGE` | `python:3.13-slim` | the image scripts run in (it must be there) |
| `RUNNER_NETWORK` | `factotum-sandbox` | the network with nothing on it but the proxy |
| `RUNNER_EGRESS_URL`, `_EGRESS_KEY` | none | where the proxy is for a container, and the key of the grants; without them no script has a network |
| `RUNNER_UID` | `10002` | who a script runs as |
| `RUNNER_MEMORY_MB`, `_CPUS`, `_PIDS`, `_TMP_MB`, `_LOG_KB` | `128`, `0.5`, `64`, `16`, `256` | what a script may use |
| `RUNNER_MAX_CONCURRENCY`, `_QUEUE_WAIT_S`, `_GRACE_S` | `4`, `5`, `10` | scripts at once, how long the next waits for its turn (then `503`), what is allowed to make and clean a container beyond the script's time |
| `EGRESS_HOST`, `_PORT` | `127.0.0.1`, `3128` | (the image listens on all interfaces of the sandbox network) |
| `EGRESS_KEY` | none | the key of the grants (32 characters or more); without it the proxy does not start |
| `EGRESS_CONNECT_PORTS` | `443` | the ports a tunnel may be made to |
| `EGRESS_MAX_CONNECTIONS` | `64` | runs served at once |
| `EGRESS_HEADER_TIMEOUT_S`, `_CONNECT_TIMEOUT_S`, `_IDLE_TIMEOUT_S`, `_MAX_DURATION_S`, `_MAX_MB` | `10`, `10`, `30`, `130`, `20` | limits of a connection |

The actions service is pointed at the runner and the edge with `FACTOTUM_ACTIONS_RUNNER_URL` and
`FACTOTUM_ACTIONS_EDGE_URL`, and at the secrets with `FACTOTUM_ACTIONS_SECRETS_FILE`; what is not set
is not there, and the service says so when it starts.

## What this is not

* **It is isolation at the level of a container**: namespaces, cgroups, the engine's default
  seccomp profile, dropped capabilities. It suits **code that the household wrote or trusts**, and it
  shares a kernel with the machine. It does not suit code from strangers: a multi-tenant cloud needs
  a stronger sandbox behind the same `SkillRunner` seam (gVisor, Kata, a microVM), or only
  instructions-mode skills (ADR 0009).
* **The socket is the risk** of this design, said once more: a flaw in the runner that lets a caller
  choose what a container is like is a flaw that is root on the machine. The container body is built
  in one function from settings, a job has no field to steer it, and tests on a real engine check the
  result from the inside; a rootless engine, or a proxy for the socket that only allows what the
  runner does, are what to do for a deployment that wants less.
* **Python only, and the standard library.** `python -E -s` in a stock image: a script has `urllib`,
  `json` and the rest of the standard library, and nothing installed. A package the script needs is
  carried in its folder (pure Python), or the script does without. A script in another language is
  an image and a command, and it is not here.
* **A script keeps nothing between runs**, by design; there is no volume. A skill that needs state
  keeps it somewhere it is granted to reach.
* **Redaction is for accidents, not malice.** A script that has a secret can send it to a host it was
  granted, or print it encoded; what is taken out is the value as the environment held it. What a
  script may do with its secrets is decided by the review that enabled it.
* **A run costs a container**: about a second and a half where nothing else is going on (below). A
  pool of warm containers would take most of it and would break the rule that nothing is left between
  two runs, so it is not done.
* **Who may ask the runner is the authentication of the service, which is a development one for
  now**: anything that can reach it can run a job and *choose its hosts* (the runner signs the grant
  for whatever the job lists), which is why it listens on loopback, does not start outside
  development and tests, and waits for the hub to issue the credentials that say which service may
  ask (slice 7).
* **`restart-router` still cannot be run**: it needs an approval (ADR 0007), and there is nobody to
  ask until the hub.

## Tests

* **No engine, no network** (`uv run pytest`): the runner's tests run against a fake engine that
  records what it is asked (what the container is like, the order of the calls, that it is removed
  when anything fails, the redaction, the sweep, the queue); the proxy's run over real sockets on
  this machine against a server that stands for every host, with a resolver that says where names are
  (every rule of the table above, the limits, the log, that it connects to the address it checked);
  the grant, the contracts, the secrets, the clients in the actions service and the whole path
  through it are tested on their own and together. The tunnel's tests include sides that fail as
  Linux's sockets do (a reset, a broken pipe, an abort, a timeout), because the event loop of Windows
  reports a reset as an end and hides what Linux shows.
* **A real engine** (`services/runner/tests/test_runner_sandbox_engine.py`, marked `integration`, 17
  tests): a script that looks at itself from the inside (who it is, what it can write, what it can
  reach, what it is given), and scripts that misbehave (all the memory, all the processes, all the
  time, output without end, a full tmpfs), and that nothing is left behind. They skip without a socket
  or without the image, and on Windows the engine is not on a socket, so they run in a Linux container
  that is given one:

  ```bash
  git archive --format=tar HEAD | MSYS_NO_PATHCONV=1 docker run --rm -i \
      -v /var/run/docker.sock:/var/run/docker.sock python:3.13-slim bash -c '
      mkdir /w && cd /w && tar -x && pip install -q uv==0.12.17 &&
      uv sync --all-packages --frozen -q &&
      uv run pytest services/runner/tests/test_runner_sandbox_engine.py -q -rs'
  ```

  (`FACTOTUM_TEST_DOCKER_SOCKET` and `FACTOTUM_TEST_SANDBOX_IMAGE` say where the engine and the image
  are.) CI pulls the image and runs them. The program that runs the plans of skills that are only
  instructions has four such tests of its own (`services/actions/tests/test_actions_executor_sandbox.py`):
  that the job the actions service makes for a plan runs in the container as it is made (with a
  message in Italian, so that what it prints is UTF-8), that it has no network when no host was
  granted, that a secret in its environment is never in what it prints, and that what is not a plan
  is refused with an exit code of its own.
* **The deployment files** are tested too: only the runner mounts the socket, nothing but the proxy
  is on the sandbox networks, the sandbox network has no way out, the key goes to the proxy and the
  runner and to nothing else, and no service is privileged or shares the host.

## Measured

On the machine that runs everything else (Docker Desktop on Windows 11, an RTX 3060 Ti that was
shared with the desktop): read them as what to expect **at worst**, and as figures that a stronger
machine improves. One run of each.

| | |
| --- | --- |
| A container that does nothing, one after the other (15 runs) | median **1.35 s** (1.14 to 1.68 s, 90th percentile 1.62 s), from the request to the answer |
| The example skills, as the runner reports them | volume (no network) 0.94 s; lamp through the proxy to a home server 1.44 s; garage door 1.82 s; weather (two HTTPS requests to Open-Meteo) 1.65 s |
| Plans that a person wrote, run by `executor.py` in the runner (slice 6, no model) | two HTTPS requests to Open-Meteo 2.28 s; two POSTs with a token to a home server 1.80 s; two commands, no network, 1.44 s; from the request to the answer 1.9 to 2.7 s |
| Four scripts of 3 s at once (four at a time) | 5.7 to 6.0 s each |
| Eight at once | the first four in 5.3 to 5.9 s, the next four in 10.4 to 11.0 s |
| Twelve at once | eight answered (up to 11.3 s) and **four refused with `503` after 6.5 s**, as the wait of 5 s for a turn says |
| A *yes* that runs a garage door, from the message to the reply, through the edge (no model is asked) | **1.9 s** |
| A command said to a speaker, on the shared card | volume 5.0 s, lamp 5.8 s, weather 5.2 s: the model that reads the message is 3 to 4 s of it |
| A command reaching the device once the script ended | 15 ms |

With the whole stack in containers there were no errors in any log, no container left behind after
any run, and, for what a script tried, this is what it got: with no hosts, the only interface was
`lo`, and DNS, `postgres:5432`, `1.1.1.1:443` and the host's `:8123` all failed; with a home server
granted, the proxy let the request through and refused with `403` *example.com* (in the clear and as
a tunnel), *169.254.169.254*, the proxy's own loopback, *postgres*, the proxy itself by name, and a
tunnel to the granted host on a port that is not a tunnel port; the home server saw exactly the two
requests that were allowed, one with the token and one with a wrong one (which it refused itself).

The program that runs the plans of skills that are only instructions was run the same way, with plans
written by hand and sent to the runner as the actions service sends them (no model in it): the two
HTTPS requests to Open-Meteo went through the proxy and the sentence came back with what they
answered (*L'aria a Roma: indice 22*, in UTF-8); the two `POST`s with a token reached the stand-in for a
home server, authorized, with the bodies that the plan gave (`brightness_pct` as a number); a command
with an accent and an emoji came back as it was written; a request to a host that was not granted was
refused by the proxy and ended the run with `step out: the request did not get through (OSError)`;
and a plan that says a secret made its request and left the sentence unsaid (`a secret is not for
saying` on standard error), with the token nowhere in what it printed. The checks refuse both of
those before anything is run; these are the second line of defence.

### What the live run found

* **The proxy logged an error at the end of a tunnel** to a real host: the event loop of Linux
  raises `ConnectionResetError` from a read when a side breaks the connection off, and it came out of
  the task group of the tunnel as an exception group that nothing handled ("the proxy failed", and it
  would have written a `500` into a tunnel that was still open). Windows reports the same reset as an end, so
  tests over real sockets passed there whatever the code did. A side that goes away, in whatever way,
  is the end of the tunnel; the test that guards it gives the tunnel sides that fail as Linux's do.
* **A `.env` in the repository changed what unit tests found.** With the stack up there is a `.env`
  (the database password, the key of the proxy, the environment), and a test that starts the proxy
  from the environment, expecting it to refuse to run without a key, started it and never returned.
  Every test now runs from an empty directory.
* **The first message after a pause was answered as a question**, as in slice 5a: with the card shared
  and a chat model and an embedding model to load, the embedding and the plan both ran out of time
  (`a message could not be embedded`, `could not be planned`). That is the models' and the card's, not
  the runner's; how long each is waited for is a setting (`FACTOTUM_ACTIONS_MATCH_TIMEOUT_S`,
  `FACTOTUM_ACTIONS_PLANNER_TIMEOUT_S`), and a deployment with more power will not meet it.
