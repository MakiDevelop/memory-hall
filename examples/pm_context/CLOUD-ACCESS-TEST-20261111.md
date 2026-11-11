# Cloud entrance test receipt (2026-11-11, synthetic data)

Route B from CLOUD-ACCESS.md, test only: Cloudflare Tunnel + Access (Service Auth) in front of a throwaway instance of this branch on a private machine (port 8799, empty synthetic store, namespace `project:synthetic`). The production instance was not touched. The one-day service token, tunnel and Access app are to be revoked after this test.

Client: a Claude Code cloud sandbox whose only egress is a managed HTTPS proxy. The three headers (`CF-Access-Client-Id`, `CF-Access-Client-Secret`, `Authorization: Bearer`) were stored as environment network secrets and injected by the proxy for the test host only; the sandbox never saw their values.

| Check (from the cloud sandbox) | Result |
| --- | --- |
| No credentials (edge) | 403 from Cloudflare Access |
| Access headers only, no app Bearer (checked on the private machine) | 401 from the app |
| Bearer only, no Access headers (private machine) | 403 |
| Both via injected secrets: initialize, tools/list (13 tools), boot, search, checkpoint (rev 1), precheck | 200, ~350 ms per call |
| Same schema error 3x, then reconnect | attempt 3 → `stop_retrying`; after reconnect attempt 4, still stopped |
| Valid call after stop | ok |
| Client sends its own wrong Bearer | 200: the proxy's injected header wins, so the sandbox cannot pick another identity |
| `mh pm boot` / `mh pm precheck` over the edge | failed before this fix, ok after |

Findings fixed in this PR:

1. `mh pm` ignored all proxies (`trust_env=False`), so it could not leave a proxy-only sandbox. Now opt-in with `MEMORY_HALL_31_USE_ENV_PROXY=1` (HTTPS only).
2. `mh pm` rebuilt each response with the original `Content-Encoding` after already decoding it, so any gzip/br response (Cloudflare compresses) failed as "connection failed". Encoding headers are now dropped from the rebuilt response; regression test added.
3. `mh pm` can now send Access service headers itself from `MEMORY_HALL_31_CF_ACCESS_CLIENT_ID/SECRET` (both or neither, HTTPS only), for clients without proxy injection.

Client support summary: any HTTP client works when the platform injects headers per host (as here). A native MCP connector that cannot set custom headers needs either platform injection or a single-layer alternative; not tested here.
