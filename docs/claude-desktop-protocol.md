# Claude Desktop follower development adapter

This is a removable Linux copy of Desktop **3.6556.1** bound to one existing
Claude Code **2.1.295** transcript. Actual history and a live quota-failure path
were observed on 2026-10-26; successful native workflow acceptance is still pending.
It is a viewer; the separate native TUI owner adapter executes tasks.

## Preparation and rollback

`claude_desktop_view.py` checks the original package and three exact chunk hashes,
validates the source/session/version, then creates a new private directory. It
copies the executable, writes a new ASAR, copies the adapter scripts, and seals
the already-validated binding. Other native resources are linked. The installed
application and provider configuration remain unchanged. Preparation failures
remove only the new output directory.

Opening rechecks the original archive and all copied executable components. It
refuses while any `claude-desktop` process exists: the copy uses the existing
profile/login exclusively. It never stops the normal app. Close the copy and open
the installed app to return to normal use; remove the prepared directory when no
longer needed. Do not override signing/integrity checks if a future build refuses
the copy. A matching version string alone is insufficient.

## Native interface

The pinned `index.chunk-CTo_fOxG.js` factory is replaced before `Sw()` creates the
original LocalSessionManager. The replacement calls the read-only loader, not the
original manager. It supplies the native `LocalSessions` API and forwards actual
source messages using `dispatchOnEvent`. Navigation uses the native
`/epitaxy/local_<CLI UUID>` route and `dispatchNavigate`.

The `mainView.js` preload patch acknowledges actual navigation and event listener
registration/removal through a private IPC channel. Main-frame sender checks and
per-window subscription identities prevent another frame from releasing events.
Navigation waits for its listener; announcement waits independently for the native
session-event listener. Reading a session list is not a readiness acknowledgment.
These acknowledgments still do not prove transcript hydration or pixels rendered.

`claude_desktop_follower.js` has no process, filesystem or Electron access. Its API
exposes one exact session, native transcript snapshots/pages and real message
events. Mutations, adoption, arbitrary resume and unknown methods fail explicitly.
Native warm/focus methods have no execution effects. Port transcript delivery is
declined so the native API uses its inline fallback.

`claude_desktop_loader.js` launches only `claude_desktop_source.py`, never Claude.
The Python reader validates the entire source before its initial snapshot, then
follows the same descriptor. Batches include source identity, producer version,
exact session and contiguous sequence. Source replacement, truncation, consumed
history rewrites, changed message UUID contents and unsupported versions fail.
The reader rehashes consumed bytes on every poll; this development implementation
has not been optimized for large histories. A snapshot or batch over 16 MiB is
explicitly rejected. Snapshot chunking is not implemented.

EOF is waiting, not completion. Broken pipes close the reader without recursive
diagnostic writes. Stream EOF/error, child exit, invalid batches, delivery failure,
initialization timeout and window destruction settle initialization or make the
view unavailable and stop its reader. They never report the owner as completed.

## Remaining native acceptance

The source supports transcript replay plus optional exact native hook evidence
through `owner_state_dir`. Committed real turn events drive running/idle/failed/stopped;
unclaimed or inconsistent evidence yields unknown. Without that binding, the native schema requires an
`isRunning` boolean; this passive display uses `false`, omits `turnRunning`, and
identifies owner state as unknown. That UI field must not drive task state or
acceptance. Native history/event hydration races still require real validation
and, if necessary, a renderer-level acknowledgment. No readiness receipt is made.

On 2026-10-25 a copy was prepared from the actual installation. After permission
to quit idle sessions, the original client's quit dialog reported a background
command still running, despite its registry saying idle. Quit was cancelled; no
copy launch or new model request occurred. Original Desktop remained running.

On 2026-10-26 normal quit succeeded. The copy displayed real history and appended
a newly dispatched peer brief and its provider quota failure. A subsequent copy
bound to the real hook evidence rendered failure with its busy indicator cleared.
Only one native TUI executor existed. The copy closed and the original client
was restored. Successful live tool activity and active-turn hydration remain open.

Offline tests exercise the actual pure core, loader and source process, ASAR
rewriting, fixed binding and patched factory. The core's session views were also
checked with the installed native schema validator. These are component results,
not a substitute for launching and observing the real renderer.

Pinned source hashes:

```text
index.chunk-CTo_fOxG.js 3301b0b9d340c5ca4f6e69c83edfa9f984e2258d9709414571a0c6b544c63c36
mainView.js            f28f3650f4fe3eb123d45da864b02e79fa223f249e860e6c5bd196082c8a9cba
index.pre.js           3b7cb1557fd6dcffae9f47a19ae212650095ab34228efbeed0fabb13ff07434b
```
