# 0015. The runner: a container for each run, and a proxy that checks a grant

Status: accepted, 2026-09-21. What it says a failure may say ("never what the script said") is refined
by [ADR 0028](0028-the-model-alone-does-nothing-a-request-is-satisfied-by-skills-or-declined.md): a script
may report a failure in words of its own, as it reports a message; what it printed without meaning to say
it still never reaches a person. Refines [ADR 0006](0006-skills-and-sandboxed-execution.md).

## Context

A skill in script mode is somebody's code (ADR 0006), and it runs on the machine that holds the
household's documents, beside a database and services that have keys. What could go wrong is
specific. The script may be buggy or hostile: it may use all the memory, fork without end, write
without end, or hold on for ever. It may try to reach what it was not granted: the database, another
service, the metadata endpoint of a cloud, a device in the house that the skill never named. It may
try to read secrets that are not its own, to leave something behind for the next run, or to take over
the service that runs it. And what it prints is what a script said: it must not be believed further
than it can be checked, or turned into an action that skips the checks.

ADR 0006 sketched the answer: a separate container, non-root, read-only, with limits, on an internal
network whose only exit is a proxy with an allowlist. This records how that is done, and where it
differs.

## Decision

**One disposable container per run, made by a service that does nothing else.** The `runner` is given
a *job* (files, an entrypoint, what to feed it, its secrets, the hosts it may reach, a time limit),
makes a container from a stock Python image through the engine's API, puts the files in, starts it,
waits, reads what it wrote, and removes it, whatever happened. **A job cannot say what the container
is like**: there is no field for an image, a mount, a capability, a network, a limit or a command; all
of that comes from the runner's settings, in one function. The container runs as an unprivileged user
with every capability dropped and no way to get one, with ceilings on memory (swap included), CPU
and processes, a small non-executable `/tmp` in memory as the only place to write, no shared memory,
a bounded log, and only the environment it was given. The runner never builds or pulls the image.

**The read-only root filesystem of ADR 0006 is not used, and what it was for is done otherwise.**
The engine refuses to put files into a container whose root is read-only. The files are put in owned
by root and the script is not root, so nothing outside `/tmp` can be written by it. Tests on a real
engine, from the inside, say so.

**No network unless it is granted, and then one exit.** A container has no network at all unless the
job lists hosts. With hosts it is on an internal network (no way out of its own) where the only thing
is the **egress proxy**, and it is told to use it (`HTTP_PROXY` and the like) with a **grant** as the
password. The grant is made by the runner and lists the hosts and a time, signed with a key that only
the runner and the proxy have. The proxy keeps no state: it checks the signature and the time, that
the host is listed, that **every** address the host resolves to is one a skill may reach (never this
machine, never link-local, where a cloud's metadata is; a **private** address only for a host that
the grant names whole and never for a domain, because a domain can answer with an address in the
house; an IPv4 address written as IPv6 is the address it wraps), and then connects **to the address it
checked**, so a name that answers differently a second time gets nothing. Tunnels go to a short list
of ports; what goes through is limited in time, bytes and silence; the log says what was decided and
why and never what was asked for. The proxy is on the sandbox network and on a network of its own to
get out, with nothing else on either, so a script cannot reach the database or another service
through it.

**Whose secrets a skill is given is settled before the job leaves.** The actions service gives a job
only the secrets that the version declares, that were granted when it was enabled, and that are
**its owner's own**: a skill a person wrote for themselves that asks for the household's token is not
given it, however it is granted. They are carried in the job, put in the environment of the container
and nowhere else, taken out of what the script wrote before it is answered, and named but never
shown in any error, log or record. Until the hub keeps them they are in a file for development, which
refuses to exist anywhere else.

**What a script prints is data.** It is read strictly as `{message, data, commands}`: a field that is
not one of those is a failure that names the field, a script cannot say that nothing was run, and
what a failure says is never what the script said. A command is data that the platform gives to a
device after `authorize()` and after checking that the device declared what it needs, so the sandbox
needs no way back into the platform. What loads can run: the entrypoint is a Python file, every file
has a name a container can be given, and a secret cannot be called what the runner sets or what
changes how a program starts.

**Python, and the standard library.** `python -E -s` in a stock image. Other languages are an image
and a command, and are not here. **Accounts** (linked services such as a music service) need the hub
(slice 7): a skill that declares one fails and says so.

**The socket is given to the runner and to nothing else.** The runner needs the socket of the
engine, which is as much as being root on the machine. It runs as a user that owns nothing, with no
capability, no new privileges and a read-only root, listens on loopback, is authenticated like every
service, and is the only service that mounts the socket (a deployment test says so). It does not
start outside development and tests until the hub issues credentials.

## Consequences

- **The isolation is at the level of a container**: namespaces, cgroups, the default seccomp profile,
  dropped capabilities, a kernel shared with the machine. It suits code that the household wrote or
  trusts. A multi-tenant cloud needs a stronger sandbox behind the same `SkillRunner` seam (gVisor,
  Kata, a microVM) or only instructions-mode skills, as ADR 0006 said.
- **The socket is the risk of the design.** A flaw in the runner that lets a caller steer the
  container body is a flaw that is root on the machine, and the design puts everything it can into
  making that hard (one function, no field, tests from the inside, nothing else with the socket). A
  rootless engine, or a proxy for the socket that only allows what the runner does, are what a
  deployment that wants less does.
- **A run costs a container**: about a second and a half where nothing else is going on, more when
  several start at once (the runner runs four at a time and tells the fifth to try again after five
  seconds). A pool of warm containers would take most of it and would break the rule that nothing
  survives a run, so it is not done.
- **Redaction is for accidents.** A script that has a secret can send it to a host it was granted or
  print it encoded. What a script may do with its secrets is what the review that enabled it decided.
- **The grant is readable by the script** (it is its password), which does no harm: it says what that
  script may reach, cannot be changed without the key, and stops being good when the run should have
  ended. The key must be long (32 characters or more), is never defaulted, and is given to the proxy
  and the runner and nothing else; both refuse to start without it.
- **Devices are reached by address**: the DNS of the engine usually cannot resolve `.local` names.
- **Windows hides what Linux shows.** A reset at the end of a tunnel is an error on Linux and an end
  on Windows; the tests of the proxy give it sides that fail as Linux's do.
