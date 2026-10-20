# API endpoints and source references

Binding spec: [plans/zai-quota-implementation-plan.md](../../plans/zai-quota-implementation-plan.md)
§"Source references to follow". This file records what the implementation is
based on; nothing here was re-derived through paid calls.

## Endpoints (implemented in scripts/zai_api.py)

```text
GET  https://api.z.ai/api/monitor/usage/quota/limit
GET  https://api.z.ai/api/biz/customer-package-reset/list?targetType=PERSONAL
POST https://api.z.ai/api/biz/customer-package-reset/use
Authorization: Bearer ${ZAI_API_KEY}
```

- Quota: `data.limits[]` with `unit == 3` is the five-hour window;
  `percentage` is **used**; `nextResetTime` is epoch **milliseconds**.
- Cards: only `data.fiveHourResets` (`recordId`, `available`, `expireTime`);
  `weekResets` is ignored entirely.
- POST body: `{"targetType":"PERSONAL","resetType":"FIVE_HOUR","recordId":<int>,
  "requestId":"<uuid4>"}`; success only when `success` is true and `code` is
  0 or 200 (HTTP 200 alone is not success).

## Source projects consulted (read-only reference)

1. LogicIncZo/zai-usage — `zai-usage.ts`, `zai-usage.test.ts`, README:
   base URL, Bearer auth, minutes-until-reset, checkDecision shape.
   <https://github.com/LogicIncZo/zai-usage>
2. xxww0098/dsh-plugin-oauth-subs — `src/oauth/glm/index.ts`,
   `src/oauth/glm/quota.ts`, `test/glm.test.ts`: card URLs, POST body,
   envelope checks, mocked retry/request-id behavior (does NOT prove a live
   reset succeeded). <https://github.com/xxww0098/dsh-plugin-oauth-subs>
3. agent-next/zagent — `packages/driver/quota.mjs`: alternative
   ZCode-authenticated API and idempotency-key design; design reference only.
   <https://github.com/agent-next/zagent>

## Skill-discovery references

- Agent Skills specification:
  <https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx>
- OpenCode skills: <https://docs.opencode.ai/docs/skills/>
- Pi skills:
  <https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/skills.md>

## Unverified behaviors (do not rely on without dashboard confirmation)

- Server-side deduplication of retried `requestId` values is unproven for
  this account; the CLI therefore never retransmits an uncertain POST.
- The timezone of zone-less `expireTime` strings differs between source
  projects; raw strings are sorted chronologically and displayed as-is until
  the offset is confirmed against the dashboard.
- Live redemption on this account is unverified until the first
  owner-authorized low-quota use.
