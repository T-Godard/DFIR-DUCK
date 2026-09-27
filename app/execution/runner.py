"""Exécution d'un sous-processus d'analyse, avec journalisation exhaustive et
suivi de performance.

`run_process()` :

* fusionne stdout+stderr et les écrit, horodatés, dans `log_path` (le
  `run.log` du run) tout en les relayant au logger applicatif en DEBUG ;
* isole le processus dans sa propre session (`setsid`) pour pouvoir tuer tout
  l'arbre proprement ;
* applique des limites optionnelles : RAM (RLIMIT_AS), temps CPU total
  (RLIMIT_CPU), nombre de cœurs (affinité CPU), politesse d'ordonnancement ;
* impose un timeout ;
* réagit à un `stop_event` (arrêt propre demandé, ex. shutdown du service) ;
* échantillonne périodiquement CPU % et RAM (process + enfants), écrit une
  série dans `data/runs/<id>/metrics.jsonl` et pousse chaque échantillon via
  `on_sample` (mise à jour live de l'UI).

Les limites ressources et le kill de groupe sont POSIX-only : hors Linux (dev
Windows) le processus tourne quand même, sans ces garde-fous (avertissement
loggé). Le suivi CPU/RAM, lui, fonctionne partout (psutil).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import shlex
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)

_POSIX = os.name == "posix"
_READ_CHUNK = 64 * 1024
_MAX_LINE = 1024 * 1024  # au-delà, on coupe : garde-fou contre un flux sans \n

OnSample = Callable[[dict], Awaitable[None]]


@dataclass
class ProcessResult:
    exit_code: int | None
    duration_seconds: float
    timed_out: bool
    cancelled: bool
    peak_rss_bytes: int | None = None
    last_rss_bytes: int | None = None
    last_cpu_percent: float | None = None
    metrics_samples: int = 0


def _ts() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _build_preexec(
    memory_limit_mb: int, cpu_seconds: int, cpu_cores: int, nice: int | None
):
    if not _POSIX:
        return None

    def _preexec() -> None:  # exécuté dans le processus enfant, après le fork
        os.setsid()

        import resource

        if memory_limit_mb and memory_limit_mb > 0:
            limit = memory_limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        if cpu_seconds and cpu_seconds > 0:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        if cpu_cores and cpu_cores > 0 and hasattr(os, "sched_setaffinity"):
            try:
                available = sorted(os.sched_getaffinity(0))
                chosen = set(available[:cpu_cores])
                if chosen:
                    os.sched_setaffinity(0, chosen)
            except OSError:
                pass
        if nice is not None:
            try:
                os.nice(nice)
            except OSError:
                pass

    return _preexec


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        if _POSIX:
            os.killpg(os.getpgid(proc.pid), sig)
        else:  # pragma: no cover - dev Windows
            proc.send_signal(sig)
    except (ProcessLookupError, PermissionError):
        pass


async def _terminate(proc: asyncio.subprocess.Process, log) -> None:
    """SIGTERM au groupe -> délai de grâce -> SIGKILL."""
    if proc.returncode is not None:
        return
    log.write(f"{_ts()}  [runner] SIGTERM\n")
    _signal_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=10)
        return
    except TimeoutError:
        pass
    log.write(f"{_ts()}  [runner] SIGKILL\n")
    _signal_group(proc, signal.SIGKILL)
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=10)
    except TimeoutError:  # pragma: no cover
        logger.error("runner: processus pid=%s insensible à SIGKILL", proc.pid)


async def _monitor(
    pid: int,
    interval: float,
    metrics_path: Path | None,
    on_sample: OnSample | None,
    state: dict,
) -> None:
    """Échantillonne CPU %/RAM de l'arbre de processus toutes les `interval` s."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    cache: dict[int, psutil.Process] = {}

    def _tree() -> list[psutil.Process]:
        live: dict[int, psutil.Process] = {}
        try:
            for p in [root, *root.children(recursive=True)]:
                known = cache.get(p.pid)
                if known is not None:
                    live[p.pid] = known
                else:
                    live[p.pid] = p
                    try:
                        p.cpu_percent(None)  # amorce (1er appel = 0.0)
                    except psutil.Error:
                        pass
        except psutil.NoSuchProcess:
            pass
        cache.clear()
        cache.update(live)
        return list(live.values())

    _tree()  # amorce l'arbre initial
    mf = metrics_path.open("a", encoding="utf-8", buffering=1) if metrics_path else None
    try:
        while True:
            await asyncio.sleep(interval)
            rss_total = 0
            cpu_total = 0.0
            alive = 0
            for p in _tree():
                try:
                    with p.oneshot():
                        rss_total += p.memory_info().rss
                        cpu_total += p.cpu_percent(None)
                    alive += 1
                except psutil.Error:
                    continue
            if alive == 0:
                break
            sample = {
                "ts": _ts(),
                "rss_bytes": rss_total,
                "cpu_percent": round(cpu_total, 1),
                "procs": alive,
            }
            state["last_rss"] = rss_total
            state["last_cpu"] = sample["cpu_percent"]
            state["peak_rss"] = max(state.get("peak_rss") or 0, rss_total)
            state["samples"] = state.get("samples", 0) + 1
            if mf:
                mf.write(json.dumps(sample) + "\n")
            if on_sample is not None:
                try:
                    await on_sample(sample)
                except Exception:  # noqa: BLE001
                    logger.exception("monitor: on_sample a levé une exception")
    except asyncio.CancelledError:
        pass
    finally:
        if mf:
            mf.close()


async def run_process(
    *,
    argv: list[str],
    cwd: Path | str,
    log_path: Path | str,
    env: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
    memory_limit_mb: int = 0,
    cpu_seconds: int = 0,
    cpu_cores: int = 0,
    nice: int | None = None,
    stop_event: asyncio.Event | None = None,
    metrics_path: Path | str | None = None,
    metrics_interval: float = 0.0,
    on_sample: OnSample | None = None,
) -> ProcessResult:
    cwd = Path(cwd)
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path = Path(metrics_path) if metrics_path else None

    cmdline = shlex.join(argv)
    started = time.monotonic()

    with open(log_path, "a", encoding="utf-8", buffering=1) as log:
        log.write(f"\n{'=' * 72}\n")
        log.write(f"{_ts()}  [runner] démarrage\n")
        log.write(f"{_ts()}  [runner] argv : {cmdline}\n")
        log.write(f"{_ts()}  [runner] cwd  : {cwd}\n")
        if env:
            log.write(f"{_ts()}  [runner] env (clés) : {sorted(env)}\n")
        log.write(
            f"{_ts()}  [runner] limites : timeout={timeout_seconds}s "
            f"mem={memory_limit_mb or 0}Mo cpu_s={cpu_seconds or 0} "
            f"cpu_cores={cpu_cores or 'tous'} nice={nice}\n"
        )
        if not _POSIX:
            log.write(
                f"{_ts()}  [runner] AVERTISSEMENT : hors POSIX — pas de limite "
                f"ressources ni de kill de groupe\n"
            )
            logger.warning("runner: hors POSIX — limites ressources / kill de groupe désactivés")
        log.write(f"{'=' * 72}\n")

        logger.info("process.start argv=%s cwd=%s", cmdline, cwd)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                preexec_fn=_build_preexec(memory_limit_mb, cpu_seconds, cpu_cores, nice),
            )
        except OSError as exc:
            log.write(f"{_ts()}  [runner] ÉCHEC du lancement : {exc}\n")
            logger.exception("process.spawn_failed argv=%s", cmdline)
            raise

        log.write(f"{_ts()}  [runner] pid={proc.pid}\n")
        logger.info("process.running pid=%s", proc.pid)

        async def _pump() -> None:
            assert proc.stdout is not None
            buf = b""
            while True:
                chunk = await proc.stdout.read(_READ_CHUNK)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    _emit(log, raw)
                if len(buf) > _MAX_LINE:
                    _emit(log, buf)
                    buf = b""
            if buf:
                _emit(log, buf)

        pump_task = asyncio.create_task(_pump())
        wait_task = asyncio.create_task(proc.wait())
        stop_task = asyncio.create_task(stop_event.wait()) if stop_event else None

        metrics_state: dict = {"last_rss": None, "last_cpu": None, "peak_rss": 0, "samples": 0}
        monitor_task: asyncio.Task | None = None
        if metrics_interval and metrics_interval > 0:
            monitor_task = asyncio.create_task(
                _monitor(proc.pid, metrics_interval, metrics_path, on_sample, metrics_state)
            )

        timed_out = False
        cancelled = False
        effective_timeout = timeout_seconds if (timeout_seconds and timeout_seconds > 0) else None
        try:
            waited_for = {wait_task}
            if stop_task:
                waited_for.add(stop_task)
            done, _pending = await asyncio.wait(
                waited_for, timeout=effective_timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if wait_task in done:
                pass
            elif stop_task and stop_task in done:
                cancelled = True
                log.write(f"{_ts()}  [runner] arrêt demandé (stop_event)\n")
                logger.warning("process.stop_requested argv=%s", cmdline)
                await _terminate(proc, log)
            else:
                timed_out = True
                log.write(f"{_ts()}  [runner] TIMEOUT après {timeout_seconds}s\n")
                logger.warning("process.timeout after=%ss argv=%s", timeout_seconds, cmdline)
                await _terminate(proc, log)
        except asyncio.CancelledError:
            log.write(f"{_ts()}  [runner] tâche annulée — arrêt du processus\n")
            logger.warning("process.task_cancelled argv=%s", cmdline)
            await _terminate(proc, log)
            raise
        finally:
            if stop_task:
                stop_task.cancel()
            if not wait_task.done():
                wait_task.cancel()
            if monitor_task:
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass
            try:
                await asyncio.wait_for(pump_task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                pump_task.cancel()
            duration = time.monotonic() - started
            log.write(
                f"{_ts()}  [runner] fin : exit={proc.returncode} "
                f"durée={duration:.1f}s timeout={timed_out} annulé={cancelled} "
                f"pic_RAM={_human(metrics_state['peak_rss'])}\n"
            )

    logger.info(
        "process.end exit=%s dur=%.1fs timed_out=%s cancelled=%s peak_rss=%s",
        proc.returncode,
        duration,
        timed_out,
        cancelled,
        metrics_state["peak_rss"],
    )
    return ProcessResult(
        exit_code=proc.returncode,
        duration_seconds=duration,
        timed_out=timed_out,
        cancelled=cancelled,
        peak_rss_bytes=metrics_state["peak_rss"] or None,
        last_rss_bytes=metrics_state["last_rss"],
        last_cpu_percent=metrics_state["last_cpu"],
        metrics_samples=metrics_state["samples"],
    )


def _emit(log, raw: bytes) -> None:
    line = raw.decode("utf-8", errors="replace").rstrip("\r")
    log.write(f"{_ts()}  {line}\n")
    logger.debug("process.out %s", line)


def _human(n: int | None) -> str:
    if not n:
        return "?"
    val = float(n)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if val < 1024:
            return f"{val:.0f} {unit}" if unit == "o" else f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} Po"
