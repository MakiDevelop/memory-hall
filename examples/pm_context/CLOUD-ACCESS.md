# Cloud PM access: decision proposal, no ingress change

The business requirement is for any authorized PM agent to read evidence and update progress while preserving source citations, namespace scope, revocation and delivery receipts. Model brand and network location are separate concerns. The same MCP/`mh pm` contract is available to Claude Code, Codex, Grok and Gemini; a Claude-specific bridge must not become the only write interface.

This comparison is documentation for a later decision. It does not select a route, issue tokens, change ACLs, enable Funnel, create DNS records or start a tunnel. The current private route stays the reference until a separately approved rollout.

## Options

| Route | How it works | Benefits | Constraints / risks |
| --- | --- | --- | --- |
| A. Local or controlled runner; Claude Remote Control where supported | Cloud coordinator asks a worker on the private machine to call the shared MCP/CLI, then return a compact card to its parent | No public Memory Hall ingress; reuses private connectivity; credentials remain on the worker | Worker must be online and authorized; extra routing/latency; a local post receipt is not main-thread receipt. Claude Remote Control is a Claude session transport, not a general four-vendor API |
| B. Cloudflare Tunnel + Access + app authentication | An outbound connector publishes only the approved MCP hostname/path through Access; the app separately verifies scoped bearer identity | Direct HTTPS for capable runners without opening an inbound router port; edge policies and per-runner revocation | Client must support Access service-auth headers or another selected Access flow; two credential layers; external control-plane dependency; application rate/path controls and a distinct cloud principal still needed |
| C. Tailscale Funnel + application gateway | Funnel routes public HTTPS to a narrowly configured local gateway; gateway enforces app auth, methods, body limits and rate limits | Reuses Tailscale tooling and HTTPS naming | Funnel is public internet access, unlike Serve. A private ACL does not authenticate arbitrary internet callers. Edge authentication/rate controls must be explicitly designed; do not expose the raw pilot as the entire security boundary |

Route A is feasible as a mediated workflow when a supported local session/runner is available; it does not make an unrelated cloud process a tailnet member. Claude's Remote Control operates a session on the local machine and uses its connection model. Supported environments, account requirements and lifecycle should be checked at rollout. See [Claude Code Remote Control](https://code.claude.com/docs/en/remote-control).

Cloudflare Tunnel uses outbound connector connections; Access service tokens can authenticate automated clients with `CF-Access-Client-Id` and `CF-Access-Client-Secret`. Keep those headers separate from the application's `Authorization: Bearer` header. Never assume all native MCP clients support arbitrary headers, interactive redirects or multiple auth layers: test each actual client. The batch-1 `mh pm` CLI only provides application bearer authentication; Cloudflare service-auth header support would be a selected-route follow-up. See [Tunnel overview](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/), [service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/) and [agent authentication](https://developers.cloudflare.com/cloudflare-one/access-controls/authenticate-agents/).

Tailscale documents Funnel as internet exposure and distinguishes it from tailnet-only Serve. A public Funnel route requires a separate gateway policy. Verify current product limits and platform support before selection. See [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel).

## Suggested decision sequence

Engineering proposal: first generalize route A with the shared `mh pm` interface and progress-writer capability on a controlled runner. Measure its routing overhead. If direct cloud MCP access remains necessary, compare B and C with actual client support and choose a dedicated narrow endpoint. This is a recommendation, not an authorization to open either route.

Use an independent cloud principal per runner, finite namespace scopes and separate read/progress-publish capabilities. Keep the existing single PM publisher until expanded live write identities are approved. A separate human decision confirmer is a proposal for the confirmation workflow; the batch-1 PM role does not imply that a human-confirmation identity has been provisioned.

Source credentials through an approved secret manager/keychain or runner store. Do not include token values in prompts, shared notes, command arguments, logs or repo files. A lost cloud credential must be revocable without disrupting the private PM runner. Edge credential removal and app principal removal are separate controls and need separate checks.

## Parent reporting contract for route A

The coordinator supplies task ID, bounded questions, allowed namespace, parent/thread identity and a correlation ID. The local worker calls the common PM tools using its own principal and reports:

- Correlation ID, task ID, checkpoint revision, source generation and operation ID for any authorized write.
- Each answer with source SHA and original path/line mapping, distinguishing excerpt lines from original lines.
- Unverified items, stale/history warnings and remaining context budget.
- Posting tool receipt and destination thread ID.

The parent must independently acknowledge the same correlation/message ID and revision. A successful post to the worker's own thread is not proof of parent receipt. Private workspace hooks and parent-routing tools require explicit, minimal configuration; do not generalize a K234-only allowlist into blanket tool access. A coordinator unable to see the parent reports delivery unconfirmed.

## Acceptance before a direct route goes live

1. Stage only synthetic documents and fresh test identities. Exercise real read/write, namespace/context denial and no-auth/wrong-auth refusal from each selected client.
2. Revoke a token and retry an already-open session within one minute. Verify scope/role changes invalidate old sessions. Verify edge and application identities separately where applicable.
3. Confirm TLS hostname, permitted MCP path/methods, body-size/rate/concurrency limits, safe logs and bounded errors. The current pilot has body/session limits; it does not implement a complete public-edge rate-limiting policy.
4. Interrupt each delivery destination, retry the same operation, verify all three readback hashes, and prove no partial version becomes visible as published.
5. Close only the newly selected public route and verify private MCP remains available. Keep a route-specific rollback plan; avoid a broad proxy/Serve reset that could disable unrelated private services.
6. For mediated reporting, retain both posting receipt and parent acknowledgment. For cloud folder synchronization, retain cloud-side readback SHA separately from local notes SHA.

Tests in this PR cover isolated loopback TCP and CLI behavior, not a public route or cloud receipt. Network selection, credential provisioning, deployment and real-cloud acceptance remain separately authorized work.
