# Local runtime and publication gates

`doctor` is read-only. It does not start clients, connect to conversation sockets,
run models, create receipts, or change providers and authentication.

- `runtime_ready` requires implemented capabilities, supported local clients and
  protocols, and full acceptance recorded on this exact computer. Default doctor
  exits 0 only when this gate passes; otherwise it exits 2.
- `release_ready` additionally requires native acceptance on Linux, Windows and
  macOS. `doctor --require-release` uses this gate. Windows/macOS remain explicitly
  unimplemented/unverified, so publication is blocked.
- `runtime_blockers` and `platform_blockers` describe the separate scopes. The
  combined `blockers` field remains for existing report consumers.

Both gates are currently false. The capability flags describe implemented code;
they are not configurable overrides. A receipt cannot enable a missing executor.

## Receipt binding

The optional local file is `native-acceptance.json` beside the retained worker
state, or the exact path in `CLAUDEX_ACCEPTANCE_EVIDENCE`. The doctor never creates
that directory or migrates old task state. See `inspect_acceptance()` in
`native_probe.py` for the schema and the report's `local_acceptance.binding` for
the expected current values.

Evidence is bound to the OS, hashed local host/user/home identity, current Skill
and executable Python/JavaScript/JSON files, all four client versions and Codex
IPC versions. Changing code, the Skill, host or client versions invalidates it.
Both initiating directions need exact Claude/Codex session UUIDs and all seven
workflow scenarios listed in `required_scenarios`. Every passed scenario needs a
relative artifact path and matching SHA-256, plus a run UUID and timestamp.

The receipt and artifacts must be bounded, nonempty, regular local files owned by
the current user and not writable by others. Missing, stale, changed, malformed
or escaping references do not pass. No local receipts, transcripts or credentials
belong in Git.

These checks validate binding and artifact integrity. They do not rerun the tests
or establish that an artifact's claims are true. The main reviewing agent must
inspect actual native behavior, diffs and test evidence before recording passage.
Never turn offline fixtures into a native acceptance receipt. Runtime readiness
also does not establish that current quota, permissions or connections are usable.
