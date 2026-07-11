"""Deterministic policy and Codex CLI execution for specialist routing."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


CODING = re.compile(r"\b(code|repo(?:sitory)?|bug|fix|test|lint|type.?check|implement|refactor|migration|deploy|pr|diff|function|class|api|database)\b", re.I)
HIGH_RISK = re.compile(r"\b(critical|urgent|production|security|auth(?:entication|orization)?|permission|concurren|migration|data.?integrity|architect|major refactor|multi[- ]file|state management)\b", re.I)
DISCOVERY = re.compile(r"\b(inspect|locate|find|trace|review|reproduce|run tests?|lint|type.?check|regression test|small|isolated|low.?risk)\b", re.I)
FAILURE = re.compile(r"\b(uncertain|incomplete|cannot reproduce|could not reproduce|tests? fail|failed|error|blocked)\b", re.I)


@dataclass(frozen=True)
class RouterConfig:
    coordinator_model: str = "openai/gpt-5.6"
    spark_model: str = "gpt-5.3-codex-spark"
    sol_model: str = "gpt-5.6-sol"
    reserve_percent: float = 20.0
    quota_cache_seconds: int = 120
    timeout_seconds: int = 1800
    codex_home: Path = Path.home() / ".codex"
    state_path: Path = Path.home() / ".hermes" / "specialist-router-state.json"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RouterConfig":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        data = {k: v for k, v in value.items() if k in allowed}
        for key in ("codex_home", "state_path"):
            if key in data:
                data[key] = Path(data[key]).expanduser()
        return cls(**data)


@dataclass(frozen=True)
class Decision:
    route: str
    reason: str
    discovery_first: bool = False


@dataclass
class PoolQuota:
    model: str
    five_hour_remaining: float | None = None
    weekly_remaining: float | None = None
    five_hour_resets_at: int | None = None
    weekly_resets_at: int | None = None
    observed_at: float | None = None

    @property
    def available(self) -> bool:
        return self.weekly_remaining is None or self.weekly_remaining > 0


class Router:
    def __init__(self, config: RouterConfig, *, runner=subprocess.run, clock=time.time):
        self.config = config
        self._runner = runner
        self._clock = clock
        self._quota_cache: tuple[float, dict[str, PoolQuota]] | None = None

    def classify(self, goal: str, risk: str = "auto") -> Decision:
        if not CODING.search(goal):
            return Decision("coordinator", "conversation, planning, research, or status")
        if risk in {"high", "critical"} or HIGH_RISK.search(goal):
            return Decision("sol", "complexity or risk requires substantial implementation", bool(DISCOVERY.search(goal)))
        if DISCOVERY.search(goal):
            return Decision("spark", "bounded discovery, test, review, or low-risk change")
        return Decision("spark", "bounded coding task; Spark receives one focused attempt")

    def quotas(self, force: bool = False) -> dict[str, PoolQuota]:
        now = self._clock()
        if not force and self._quota_cache and now - self._quota_cache[0] < self.config.quota_cache_seconds:
            return self._quota_cache[1]
        pools = {"spark": PoolQuota(self.config.spark_model), "sol": PoolQuota(self.config.sol_model)}
        known_sessions: dict[str, str] = {}
        try:
            state = json.loads(self.config.state_path.read_text())
            known_sessions = {
                str(a["session_id"]): str(a["pool"])
                for a in state.get("attempts", [])
                if a.get("session_id") and a.get("pool") in pools
            }
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        sessions = self.config.codex_home / "sessions"
        if sessions.exists():
            files = sorted(sessions.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in files[:250]:
                model = None
                session_id = None
                latest = None
                try:
                    for line in path.read_text(errors="replace").splitlines():
                        obj = json.loads(line)
                        payload = obj.get("payload") or {}
                        if obj.get("type") == "session_meta":
                            session_id = payload.get("session_id") or payload.get("id")
                            model = payload.get("model") or (payload.get("model_config") or {}).get("model")
                        if obj.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied":
                            model = (payload.get("thread_settings") or {}).get("model") or model
                        rate = payload.get("rate_limits")
                        if rate:
                            latest = rate
                except (OSError, json.JSONDecodeError):
                    continue
                key = known_sessions.get(str(session_id))
                if key is None:
                    key = "spark" if model == self.config.spark_model else "sol" if model == self.config.sol_model else None
                if key and latest and pools[key].observed_at is None:
                    primary, secondary = latest.get("primary") or {}, latest.get("secondary") or {}
                    pools[key] = PoolQuota(
                        model=model,
                        five_hour_remaining=_remaining(primary), weekly_remaining=_remaining(secondary),
                        five_hour_resets_at=primary.get("resets_at"), weekly_resets_at=secondary.get("resets_at"),
                        observed_at=path.stat().st_mtime,
                    )
                if all(p.observed_at is not None for p in pools.values()):
                    break
        self._quota_cache = (now, pools)
        return pools

    def reserve_active(self, quotas: dict[str, PoolQuota] | None = None) -> bool:
        sol = (quotas or self.quotas())["sol"]
        return sol.weekly_remaining is not None and sol.weekly_remaining <= self.config.reserve_percent

    def route_directive(self, goal: str, decision: Decision) -> str:
        quotas = self.quotas()
        route = "GPT-5.6 → Spark" if decision.route == "spark" else "GPT-5.6 → GPT-5.6-sol"
        return (
            f"{goal}\n\n<specialist-route>\nMODEL ROUTE\nTask: {goal[:120]}\nRoute: {route}\n"
            f"Reason: {decision.reason}\nQuota: sol {_q(quotas['sol'])}; Spark {_q(quotas['spark'])}\n"
            "Status: inspecting\nCall route_specialist_task exactly once with the original goal and repository. "
            "Report meaningful route transitions and finish from the coordinator model.\n</specialist-route>"
        )

    def execute(self, goal: str, repository: str, risk: str = "auto", simulate_spark_failure: bool = False) -> dict[str, Any]:
        repo = Path(repository).expanduser().resolve()
        if not goal.strip():
            raise ValueError("goal is required")
        if not repo.is_dir():
            raise ValueError(f"repository does not exist: {repo}")
        decision = self.classify(goal, risk)
        quotas = self.quotas()
        reserve = self.reserve_active(quotas)
        route = decision.route
        if route == "sol" and reserve and risk != "critical":
            route = "spark"
        attempts: list[dict[str, Any]] = []
        handoff: dict[str, Any] | None = None

        if route == "sol" and decision.discovery_first:
            discovery = self._invoke("spark", _discovery_prompt(goal), repo)
            attempts.append(discovery)
            handoff = self._handoff(goal, repo, discovery)
        elif route == "spark":
            spark = self._invoke("spark", goal, repo, simulate=simulate_spark_failure)
            attempts.append(spark)
            if not spark["ok"] or FAILURE.search(spark.get("message", "")):
                handoff = self._handoff(goal, repo, spark)
                route = "sol"

        if route == "sol":
            prompt = goal if handoff is None else _handoff_prompt(handoff)
            sol = self._invoke("sol", prompt, repo)
            attempts.append(sol)
            if sol["ok"]:
                review = self._invoke("spark", _review_prompt(goal, sol), repo)
                attempts.append(review)

        final = attempts[-1] if attempts else {"ok": True, "message": "coordinator-only"}
        state = {
            "coordinator_model": self.config.coordinator_model,
            "active_specialist": None,
            "task": goal[:200], "repository": str(repo), "routing_reason": decision.reason,
            "route": [a["pool"] for a in attempts], "reserve_active": reserve,
            "attempts": attempts, "ok": bool(final.get("ok")), "updated_at": int(self._clock()),
        }
        self._save_state(state)
        return state

    def _invoke(self, pool: str, prompt: str, repo: Path, simulate: bool = False) -> dict[str, Any]:
        model = self.config.spark_model if pool == "spark" else self.config.sol_model
        if simulate:
            return {"pool": pool, "model": model, "ok": False, "message": "simulated Spark failure", "session_id": None}
        cmd = ["codex", "--ask-for-approval", "never", "exec", "--json", "--sandbox", "workspace-write", "-m", model, "-C", str(repo), prompt]
        proc = self._runner(cmd, text=True, capture_output=True, timeout=self.config.timeout_seconds)
        message, session_id = _parse_codex_jsonl(proc.stdout)
        return {"pool": pool, "model": model, "ok": proc.returncode == 0 and bool(message), "message": message or proc.stderr[-2000:], "session_id": session_id}

    def _handoff(self, goal: str, repo: Path, attempt: dict[str, Any]) -> dict[str, Any]:
        return {"original_goal": goal, "repository": str(repo), "branch": _git_branch(repo), "relevant_files": [], "findings": attempt.get("message", "")[-4000:], "attempted_changes": "See working tree", "failing_tests_and_commands": attempt.get("message", "")[-2000:], "constraints": "Implement fully, test, preserve existing changes", "acceptance_criteria": goal}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self.config.state_path)

    def format_status(self) -> str:
        quotas = self.quotas()
        state = {}
        try:
            state = json.loads(self.config.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        return "\n".join(["MODEL ROUTE STATUS", f"Coordinator: {self.config.coordinator_model}", f"Active specialist: {state.get('active_specialist') or 'none'}", f"Task/repository: {state.get('task') or 'none'} / {state.get('repository') or 'none'}", f"Reason: {state.get('routing_reason') or 'none'}", f"sol: {_q(quotas['sol'])}", f"Spark: {_q(quotas['spark'])}", f"sol reserve active: {'yes' if self.reserve_active(quotas) else 'no'}"])


def _remaining(window: Mapping[str, Any]) -> float | None:
    used = window.get("used_percent")
    return None if used is None else max(0.0, 100.0 - float(used))


def _q(pool: PoolQuota) -> str:
    five = "unknown" if pool.five_hour_remaining is None else f"{pool.five_hour_remaining:.0f}%/5h"
    week = "unknown" if pool.weekly_remaining is None else f"{pool.weekly_remaining:.0f}%/week"
    return f"{five}, {week}"


def _parse_codex_jsonl(text: str) -> tuple[str, str | None]:
    messages, session = [], None
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "thread.started":
            session = obj.get("thread_id")
        item = obj.get("item") or {}
        if item.get("type") == "agent_message" and item.get("text"):
            messages.append(item["text"])
    return "\n".join(messages), session


def _git_branch(repo: Path) -> str:
    try:
        return subprocess.run(["git", "branch", "--show-current"], cwd=repo, text=True, capture_output=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def _discovery_prompt(goal: str) -> str:
    return f"Inspect only; do not edit. Locate relevant files and execution paths, reproduce if possible, and report focused tests for this goal:\n{goal}"


def _handoff_prompt(bundle: dict[str, Any]) -> str:
    return "Implement and test the original goal. Compact Spark handoff follows:\n" + json.dumps(bundle, ensure_ascii=False)


def _review_prompt(goal: str, sol: dict[str, Any]) -> str:
    return f"Independently review and validate the completed implementation for this goal. Inspect the diff and run focused tests; do not redo the implementation.\nGoal: {goal}\nImplementer report: {sol.get('message', '')[-3000:]}"
