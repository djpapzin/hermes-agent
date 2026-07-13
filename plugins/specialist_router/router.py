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

FOLLOW_UP = re.compile(r"^(?:fix|repair|continue|resume|same issue|this issue|that issue|the issue|it)\b", re.I)


CODING = re.compile(r"\b(code|repo(?:sitory)?|bug|fix|test|lint|type.?check|implement|refactor|migration|deploy|pr|diff|function|class|api|database)\b", re.I)
HIGH_RISK = re.compile(r"\b(critical|urgent|production|security|auth(?:entication|orization)?|permission|concurren|migration|data.?integrity|architect|major refactor|multi[- ]file|state management)\b", re.I)
DISCOVERY = re.compile(r"\b(inspect|locate|find|trace|review|reproduce|run tests?|lint|type.?check|regression test|small|isolated|low.?risk)\b", re.I)
FAILURE = re.compile(r"\b(uncertain|incomplete|cannot reproduce|could not reproduce|tests? fail|failed|error|blocked)\b", re.I)


@dataclass(frozen=True)
class RouterConfig:
    coordinator_model: str = "openai-api/gpt-5.6"
    spark_model: str = "gpt-5.3-codex-spark"
    sol_model: str = "gpt-5.6-sol"
    reserve_percent: float = 20.0
    max_concurrent_editing: int = 2
    banked_reset_available: str = "unknown"
    banked_reset_expires_at: int | None = None
    auto_review_enabled: str = "unknown"
    quota_cache_seconds: int = 120
    timeout_seconds: int = 1800
    codex_binary: str = "/home/ubuntu/.npm-global/bin/codex"
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

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.config.state_path.read_text())
            return state if isinstance(state, dict) else {}
        except (OSError, json.JSONDecodeError, TypeError):
            return {}

    def _looks_like_follow_up(self, goal: str) -> bool:
        text = goal.strip()
        return bool(text) and (len(text) <= 40 or bool(FOLLOW_UP.match(text)))

    def _resolve_goal_context(self, goal: str) -> tuple[str, dict[str, Any] | None]:
        state = self._load_state()
        prior_goal = str(state.get("task") or state.get("original_task") or "").strip()
        if not prior_goal or not self._looks_like_follow_up(goal):
            return goal, None
        attempts = [a for a in (state.get("attempts") or []) if isinstance(a, dict)]
        failures = [a for a in attempts if not a.get("ok")]
        if not failures and not state.get("routing_reason"):
            return goal, None
        failure_lines = []
        for attempt in failures[-3:]:
            pool = attempt.get("pool") or "specialist"
            model = attempt.get("model") or "unknown-model"
            message = str(attempt.get("message") or "").strip()
            if message:
                failure_lines.append(f"- {pool} ({model}): {message}")
        if not failure_lines and state.get("routing_reason"):
            failure_lines.append(f"- previous route reason: {state['routing_reason']}")
        bundle_lines = [
            "Original task:",
            prior_goal,
            "",
            "Follow-up request:",
            goal.strip(),
        ]
        if state.get("repository"):
            bundle_lines.extend(["", f"Repository: {state['repository']}"])
        if failure_lines:
            bundle_lines.extend(["", "Previous specialist failure:"])
            bundle_lines.extend(failure_lines)
        return "\n".join(bundle_lines), state

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
        resolved_goal, _state = self._resolve_goal_context(goal)
        route = "GPT-5.6 → Spark" if decision.route == "spark" else "GPT-5.6 → GPT-5.6-sol"
        return (
            f"{resolved_goal}\n\n<specialist-route>\nMODEL ROUTE\nTask: {resolved_goal[:120]}\nRoute: {route}\n"
            f"Reason: {decision.reason}\nQuota: sol {_q(quotas['sol'])}; Spark {_q(quotas['spark'])}\n"
            "Status: inspecting\nCall route_specialist_task exactly once with the original goal and repository. "
            "Report meaningful route transitions and finish from the coordinator model.\n</specialist-route>"
        )

    def execute(self, goal: str, repository: str, risk: str = "auto", simulate_spark_failure: bool = False, resume_session_id: str | None = None) -> dict[str, Any]:
        repo = Path(repository).expanduser().resolve()
        if not goal.strip():
            raise ValueError("goal is required")
        if not repo.is_dir():
            raise ValueError(f"repository does not exist: {repo}")
        resolved_goal, prior_state = self._resolve_goal_context(goal)
        decision = self.classify(resolved_goal, risk)
        quotas = self.quotas()
        reserve = self.reserve_active(quotas)
        route = decision.route
        if route == "sol" and reserve and risk != "critical":
            route = "spark"
        attempts: list[dict[str, Any]] = []
        handoff: dict[str, Any] | None = None

        if route == "sol" and decision.discovery_first:
            discovery = self._invoke("spark", _discovery_prompt(resolved_goal), repo, resume_session_id=resume_session_id)
            attempts.append(discovery)
            handoff = self._handoff(resolved_goal, repo, discovery)
        elif route == "spark":
            spark = self._invoke("spark", resolved_goal, repo, simulate=simulate_spark_failure, resume_session_id=resume_session_id)
            attempts.append(spark)
            if not spark["ok"] or FAILURE.search(spark.get("message", "")):
                handoff = self._handoff(resolved_goal, repo, spark)
                route = "sol"

        specialist_ok = False
        if route == "sol":
            prompt = resolved_goal if handoff is None else _handoff_prompt(handoff)
            sol = self._invoke("sol", prompt, repo, resume_session_id=resume_session_id if not attempts else None)
            attempts.append(sol)
            specialist_ok = bool(sol["ok"])
            if specialist_ok:
                review = self._invoke("spark", _review_prompt(resolved_goal, sol), repo)
                attempts.append(review)
        elif attempts:
            specialist_ok = bool(attempts[-1].get("ok"))

        fallback_used = not specialist_ok
        if fallback_used:
            attempts.append(self._fallback_attempt(resolved_goal, repo, attempts, prior_state))

        final = attempts[-1] if attempts else {"ok": True, "message": "coordinator-only"}
        state = {
            "coordinator_model": self.config.coordinator_model,
            "active_specialist": None,
            "task": resolved_goal[:200],
            "original_task": goal[:200],
            "repository": str(repo),
            "routing_reason": decision.reason,
            "route": [a["pool"] for a in attempts],
            "reserve_active": reserve,
            "attempts": attempts,
            "specialist_ok": specialist_ok,
            "fallback_used": fallback_used,
            "ok": bool(final.get("ok")),
            "updated_at": int(self._clock()),
        }
        self._save_state(state)
        return state

    def _invoke(self, pool: str, prompt: str, repo: Path, simulate: bool = False, resume_session_id: str | None = None) -> dict[str, Any]:
        model = self.config.spark_model if pool == "spark" else self.config.sol_model
        if simulate:
            return {"pool": pool, "model": model, "ok": False, "message": "simulated Spark failure", "session_id": None}
        if resume_session_id:
            cmd = [self.config.codex_binary, "--ask-for-approval", "never", "exec", "resume", "--skip-git-repo-check", "--json", "-m", model, resume_session_id, "-"]
        else:
            cmd = [self.config.codex_binary, "--ask-for-approval", "never", "exec", "--skip-git-repo-check", "--json", "--sandbox", "workspace-write", "-m", model, "-C", str(repo), "-"]
        proc = self._runner(
            cmd,
            cwd=repo,
            stdin=subprocess.PIPE,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=self.config.timeout_seconds,
        )
        message, session_id = _parse_codex_jsonl(proc.stdout)
        return {"pool": pool, "model": model, "ok": proc.returncode == 0 and bool(message), "message": message or proc.stderr[-2000:], "session_id": session_id}

    def _fallback_attempt(self, goal: str, repo: Path, attempts: list[dict[str, Any]], prior_state: dict[str, Any] | None) -> dict[str, Any]:
        lines = [
            "Specialist routing failed; continue directly in the coordinator/manual repo path.",
            f"Goal: {goal}",
            f"Repository: {repo}",
        ]
        if prior_state and prior_state.get("task"):
            lines.append(f"Previous task: {prior_state.get('task')}")
        if attempts:
            lines.append("Failed specialist attempts:")
            for attempt in attempts:
                lines.append(f"- {attempt.get('pool')} ({attempt.get('model')}): {str(attempt.get('message') or '').strip()}")
        lines.append("Do not ask the user to repeat the task; continue from this context.")
        return {
            "pool": "coordinator",
            "model": self.config.coordinator_model,
            "ok": True,
            "fallback_used": True,
            "message": "\n".join(lines),
            "session_id": None,
        }

    def _handoff(self, goal: str, repo: Path, attempt: dict[str, Any]) -> dict[str, Any]:
        return {
            "original_goal": goal,
            "repository": str(repo),
            "branch": _git_branch(repo),
            "relevant_files": [],
            "findings": attempt.get("message", "")[-4000:],
            "attempted_changes": "See working tree",
            "failing_tests_and_commands": attempt.get("message", "")[-2000:],
            "constraints": "Implement fully, test, preserve existing changes",
            "acceptance_criteria": goal,
        }

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
        sol = quotas["sol"]
        reserve = self.reserve_active(quotas)
        burst = _burst_state(sol, reserve)
        active = state.get("active_specialist_sessions", 0)
        return "\n".join([
            "MODEL ROUTE STATUS",
            f"Coordinator: {self.config.coordinator_model}",
            f"Routine route: {self.config.spark_model}",
            f"Complex route: {self.config.sol_model}",
            f"Active specialist sessions: {active} / {self.config.max_concurrent_editing}",
            f"Task/repository: {state.get('task') or 'none'} / {state.get('repository') or 'none'}",
            f"Reason: {state.get('routing_reason') or 'none'}",
            f"Sol five-hour remaining: {_pct(sol.five_hour_remaining)}",
            f"Sol weekly remaining: {_pct(sol.weekly_remaining)}",
            f"Spark quota: {_q(quotas['spark'])}",
            f"Sol reserve remaining: {self.config.reserve_percent:.0f}% weekly minimum",
            f"Sol reserve active: {'yes' if reserve else 'no'}",
            f"Banked reset availability: {self.config.banked_reset_available}",
            f"Banked reset expiry: {_timestamp(self.config.banked_reset_expires_at)}",
            "Banked reset redemption: manual recommendation only; never automatic",
            f"Codex auto-review enabled: {self.config.auto_review_enabled}",
            "Fresh exact-head GitHub Codex review required: yes",
            f"Recommended burst state: {burst}",
        ])


def _remaining(window: Mapping[str, Any]) -> float | None:
    used = window.get("used_percent")
    return None if used is None else max(0.0, 100.0 - float(used))


def _q(pool: PoolQuota) -> str:
    five = "unknown" if pool.five_hour_remaining is None else f"{pool.five_hour_remaining:.0f}%/5h"
    week = "unknown" if pool.weekly_remaining is None else f"{pool.weekly_remaining:.0f}%/week"
    return f"{five}, {week}"


def _pct(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.0f}%"


def _timestamp(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def _burst_state(sol: PoolQuota, reserve: bool) -> str:
    if sol.weekly_remaining is None:
        return "quota unknown; routine work stays on Spark and Sol is reserved for critical work"
    if reserve:
        return "reserve mode; routine scans/triage/docs on Spark, Sol only for critical recovery"
    if sol.five_hour_remaining is not None and sol.five_hour_remaining <= 0:
        return "Sol five-hour limit reached; use Spark for routine work and wait for reset"
    return "normal burst; Spark for routine work, Sol for complex coding and release audits"


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
        return subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def _discovery_prompt(goal: str) -> str:
    return f"Inspect only; do not edit. Locate relevant files and execution paths, reproduce if possible, and report focused tests for this goal:\n{goal}"


def _handoff_prompt(bundle: dict[str, Any]) -> str:
    return "Implement and test the original goal. Compact Spark handoff follows:\n" + json.dumps(bundle, ensure_ascii=False)


def _review_prompt(goal: str, sol: dict[str, Any]) -> str:
    return f"Independently review and validate the completed implementation for this goal. Inspect the diff and run focused tests; do not redo the implementation.\nGoal: {goal}\nImplementer report: {sol.get('message', '')[-3000:]}"
