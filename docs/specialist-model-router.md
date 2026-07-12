# Quota-aware specialist model router

The `specialist-router` plugin keeps ordinary Telegram conversation on the configured Hermes coordinator and delegates coding work to two independently metered Codex models.

## Policy

- Coordinator: `openai-api/gpt-5.6` for conversation, planning, summaries, status, and final reports.
- Spark: `gpt-5.3-codex-spark` for repository inspection, reproduction, focused tests, review, regression tests, and one bounded low-risk implementation attempt.
- Sol: `gpt-5.6-sol` immediately for high-risk or multi-file work, or after one failed/uncertain/incomplete Spark attempt.
- A successful sol implementation is independently reviewed by Spark.
- The router derives each pool's five-hour and weekly availability from Codex rollout telemetry and caches it for 120 seconds. At 20% weekly sol remaining, noncritical sol work stays on Spark; critical work may use the reserve.

The gateway hook leaves non-coding messages byte-for-byte unchanged. Coding messages receive an ephemeral route directive instructing the coordinator to invoke `route_specialist_task` once. The specialist runner feeds the complete prompt through Codex stdin (`-` + piped input) so multiline text survives intact, and it falls back to a coordinator/manual continuation when both specialist pools refuse to start. `/model-route-status` shows coordinator, active specialist, task/repository, reason, both quota pools, reserve state, and original task context.

## Configuration

Enable the bundled plugin and configure behavioral settings in `~/.hermes/config.yaml` (never `.env`):

```yaml
plugins:
  enabled: [specialist-router]
  entries:
    specialist-router:
      coordinator_model: openai-api/gpt-5.6
      spark_model: gpt-5.3-codex-spark
      sol_model: gpt-5.6-sol
      reserve_percent: 20
      quota_cache_seconds: 120
      codex_binary: /home/ubuntu/.npm-global/bin/codex
```

Authentication remains in the existing global VM/Codex credential stores. The plugin never copies credentials into a project.

The installed CLI was verified with `codex exec --model` and exposes `codex exec resume <session-id> --model ...` for session reuse. Each specialist result records its returned thread ID; pass it back as `resume_session_id` so a follow-up resumes the compact specialist context instead of rereading a repository.

## Rollback

Remove `specialist-router` from `plugins.enabled`, restore the prior `model` and `delegation` blocks in `~/.hermes/config.yaml`, and restart `hermes-gateway.service`. The only runtime state created by the plugin is `~/.hermes/specialist-router-state.json`, which may be left in place or removed after rollback.
