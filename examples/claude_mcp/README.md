# Claude MCP

The MCP wrapper is an integration sketch, not a privileged core path.

Current intent:

- wrap the HTTP API with MCP tools
- keep `memory-hall` as the engine library
- use the same `MH_API_TOKEN` / `MH_ADMIN_TOKEN` HTTP auth story as other callers
- leave production-grade identity, ACL, and multi-tenant policy to `memory-gateway` or a future hardened mode
