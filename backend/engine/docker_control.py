"""Docker Compose control for the worker fleet.

The supervisor's only power over workers is these four verbs: start, stop,
restart, and a whitelisted read-only command. Nothing else in the codebase
shells out to docker, so there is exactly one place to audit when a container
moved.

Two safety properties:
* `assert_container_allowed` gates every verb (see db.py). Under local + dry-run
  every verb is recorded and skipped instead of run, so a dry run exercises the
  real code path and still cannot touch a daemon.
* `container_exec` accepts a command from a fixed whitelist, never a string
  built by an LLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from .config import Config, get_config
from .db import ALLOWED_CONTAINER_CMDS, Database, SudoRequiredError, assert_container_allowed

log = logging.getLogger("engine.docker")

COMPOSE_PROJECT = "ghost-x2"
SERVICES = (
    "supervisor",
    "finance-gatekeeper",
    "telegram-bot",
    "worker-funnel",
    "worker-arbitrage",
    "worker-whitelabel",
)
# Map agent handle -> compose service name. Kept in code, not the DB, because a
# typo here is a deploy bug, not a runtime value someone might tune.
HANDLE_TO_SERVICE = {
    "supervisor": "supervisor",
    "finance": "finance-gatekeeper",
    "telegram": "telegram-bot",
    "worker:funnel": "worker-funnel",
    "worker:arbitrage": "worker-arbitrage",
    "worker:whitelabel": "worker-whitelabel",
}


class DockerError(RuntimeError):
    pass


@dataclass
class CmdResult:
    ok: bool
    stdout: str
    stderr: str
    code: int
    simulated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "stdout": self.stdout, "stderr": self.stderr,
            "code": self.code, "simulated": self.simulated,
        }


class DockerControl:
    def __init__(self, db: Database, cfg: Config | None = None) -> None:
        self.db = db
        self.cfg = cfg or get_config()
        self.audit = None  # set by the supervisor once audit exists

    # ------------------------------------------------------------------ core
    async def _run(self, args: list[str], timeout: float = 60) -> CmdResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise DockerError("the docker CLI is not installed on this host") from exc

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            return CmdResult(False, "", f"timed out after {timeout}s", -1)

        return CmdResult(
            ok=proc.returncode == 0,
            stdout=out.decode(errors="replace").strip(),
            stderr=err.decode(errors="replace").strip(),
            code=proc.returncode or 0,
        )

    def _compose_cmd(self, *verb: str) -> list[str]:
        return ["docker", "compose", "-p", COMPOSE_PROJECT, *verb]

    async def _guarded(self, action: str, args: list[str], timeout: float = 60) -> CmdResult:
        """Apply the runtime gate, and simulate instead of executing when armed
        but dry-running. Simulation is logged so a dry run still proves the path."""
        try:
            assert_container_allowed(self.cfg, action)
        except SudoRequiredError as exc:
            log.info("simulated %s: %s", action, exc)
            if self.audit:
                self.audit.info("docker.simulated", f"skipped: {action}", reason=str(exc))
            return CmdResult(True, "", f"simulated: {exc}", 0, simulated=True)

        result = await self._run(args, timeout=timeout)
        if self.audit:
            level_ok = result.ok
            writer = self.audit.info if level_ok else self.audit.error
            writer(
                "docker.exec",
                f"{action} -> exit {result.code}",
                command=" ".join(args),
                stderr=result.stderr[:500] or None,
            )
        return result

    # ----------------------------------------------------------------- verbs
    async def start(self, service: str) -> CmdResult:
        self._check_service(service)
        return await self._guarded(
            f"start {service}", self._compose_cmd("up", "-d", "--no-build", service)
        )

    async def stop(self, service: str) -> CmdResult:
        self._check_service(service)
        return await self._guarded(f"stop {service}", self._compose_cmd("stop", service))

    async def restart(self, service: str) -> CmdResult:
        self._check_service(service)
        return await self._guarded(f"restart {service}", self._compose_cmd("restart", service))

    async def pause(self, service: str) -> CmdResult:
        """Pause means SIGSTOP: state preserved, no CPU, still inspectable.
        Preferred over `stop` for a worker we expect to resume."""
        self._check_service(service)
        return await self._guarded("pause", [
            "docker", "container", "ls", "-q",
            "--filter", f"label=com.docker.compose.service={service}",
        ])

    async def container_exec(
        self, service: str, cmd: str, timeout: float = 30, as_root: bool = False
    ) -> CmdResult:
        """Run a whitelisted read-only command inside a worker container."""
        self._check_service(service)
        if cmd not in ALLOWED_CONTAINER_CMDS:
            raise DockerError(
                f"command not permitted: {cmd!r}. Add it to ALLOWED_CONTAINER_CMDS if it "
                "is genuinely read-only."
            )
        inner = ["sh", "-c", cmd]
        args = ["docker", "exec"]
        if not as_root:
            args += ["-u", "appuser"]
        # Resolve the container by compose label, not by name we guess.
        lookup = await self._run([
            "docker", "container", "ls", "-q", "--latest",
            "--filter", f"label=com.docker.compose.service={service}",
            "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
        ], timeout=15)
        container_id = lookup.stdout.split("\n")[0].strip()
        if not container_id:
            raise DockerError(f"no container found for service {service!r}")
        return await self._guarded(
            f"exec in {service}: {cmd}", ["docker", "exec", container_id, *inner], timeout=timeout
        )

    async def health(self, service: str | None = None) -> list[dict[str, Any]]:
        """Container state straight from the daemon, so the fleet grid can show
        'postgres says running, docker says exited' instead of trusting one source."""
        args = ["docker", "container", "ls", "-a", "--format", "json",
                "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}"]
        result = await self._run(args, timeout=15)
        if not result.ok:
            raise DockerError(f"docker ps failed: {result.stderr[:300]}")
        rows: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            svc = item.get("Labels", "")
            name = _label_value(svc, "com.docker.compose.service") or item.get("Name", "")
            if service and name != service:
                continue
            rows.append({
                "service": name,
                "container": item.get("Names") or item.get("Name"),
                "state": item.get("State"),
                "status": item.get("Status"),
                "health": _label_value(svc, "health") or "",
                "exit_code": item.get("ExitCode"),
            })
        return rows

    @staticmethod
    def _check_service(service: str) -> None:
        if service not in SERVICES:
            raise DockerError(
                f"unknown service {service!r}; known: {', '.join(SERVICES)}"
            )


def _label_value(labels: Any, key: str) -> str | None:
    """Pull one label out of docker's `--format json` Labels field.

    Older daemons emit a comma-joined 'k=v' string, newer ones emit a dict.
    Handling only one shape silently returns None on the other, which reads as
    'this container has no service label' and drops rows from the fleet grid.
    """
    if isinstance(labels, dict):
        return labels.get(key)
    if isinstance(labels, str):
        for part in labels.split(","):
            k, _, v = part.strip().partition("=")
            if k == key:
                return v
    return None
