"""Quota-aware specialist model router plugin."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .router import Router, RouterConfig

_router: Router | None = None


def _config() -> RouterConfig:
    try:
        from hermes_cli.config import load_config
        root = load_config() or {}
    except Exception:
        root = {}
    entry = (((root.get("plugins") or {}).get("entries") or {}).get("specialist-router") or {})
    return RouterConfig.from_mapping(entry)


def register(ctx) -> None:
    global _router
    _router = Router(_config())

    def pre_gateway_dispatch(*, event, **_kwargs):
        text = getattr(event, "text", "") or ""
        decision = _router.classify(text)
        if decision.route == "coordinator":
            return {"action": "allow"}
        directive = _router.route_directive(text, decision)
        return {"action": "rewrite", "text": directive}

    def status(_raw_args: str = "") -> str:
        return _router.format_status()

    def route_tool(args: dict, **_kwargs) -> str:
        result = _router.execute(
            goal=str(args.get("goal") or ""),
            repository=str(args.get("repository") or os.getcwd()),
            risk=str(args.get("risk") or "auto"),
            simulate_spark_failure=bool(args.get("simulate_spark_failure", False)),
        )
        return json.dumps(result, ensure_ascii=False)

    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_command(
        "model-route-status",
        handler=status,
        description="Show specialist routing, quota pools, and sol reserve state.",
    )
    ctx.register_tool(
        name="route_specialist_task",
        toolset="specialist-router",
        description="Route one coding goal to Spark or GPT-5.6-sol and return its verified result.",
        emoji="⇄",
        schema={
            "name": "route_specialist_task",
            "description": "Execute a coding task through the quota-aware Codex specialist router.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "repository": {"type": "string"},
                    "risk": {"type": "string", "enum": ["auto", "low", "high", "critical"]},
                    "simulate_spark_failure": {"type": "boolean"},
                },
                "required": ["goal", "repository"],
            },
        },
        handler=route_tool,
    )

