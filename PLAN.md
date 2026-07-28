# Multi-Device Upload Architecture — Master Plan

Status: proposal (nothing implemented yet). Companion document: [CODE_REVIEW.md](CODE_REVIEW.md) —
an 86-finding, adversarially verified review of the current codebase that this plan is grounded in.

Goal as stated: hyper-optimize the whole system; support ~20 devices pushing ~10 TB into an
Immich server in as short a time as possible, with media available immediately; auto-detect
server capability; link and manage multiple machines; Android/iOS apps; pick the right
languages (Rust/Go) and transport (gRPC/REST/GraphQL). Do an extreme code review first.

---

## 0. Reality check — where speed actually comes from

Before any architecture: this GUI does not upload anything. It builds a command line and
launches **immich-go** (already written in Go) in a terminal; immich-go talks to the
**Immich server**, which ingests, hashes, thumbnails, transcodes, and ML-indexes every asset.
So the throughput chain is:

```
device disk read  →  network link  →  server NIC  →  server disk write  →  Immich ingest jobs
```

Wire-time floor for 10 TB (payload only, ~94% efficiency, nothing else on the wire):

| Server link | Best case for 10 TB |
|---|---|
| 1 GbE | ≈ 24 hours |
| 2.5 GbE | ≈ 9.5 hours |
| 10 GbE | ≈ 2.4 hours (needs ~1.2 GB/s sustained server disk writes → NVMe) |

Twenty parallel devices do not make the server's NIC or disks faster — parallelism only helps
until the server link saturates, and 20 Wi-Fi clients contend for the same access-point spectrum.
"Very short time" is therefore bought with (a) a wired/fast server link, (b) NVMe-backed
storage, (c) orchestration that keeps the pipe full without overloading ingest — not with a
rewrite of the desktop GUI. The plan optimizes every layer we control and *measures* the rest.

**"Immediately available"** means Immich's ingest jobs (thumbnail generation especially) keep up.
The lever we control is job scheduling: pause heavy jobs (video transcode, ML/smart search,
face detection) during bulk ingest so thumbnails get the CPU, then resume them afterwards —
driven through the Immich job API and immich-go's job-pausing support.

### Language & transport decisions (proposed)

- **Keep the PySide6 GUI in Python** as the operator console. The review graded its `core/`
  a solid B; the GUI is not on the data path, so rewriting it in Rust/Go buys zero throughput.
  Its real problems (blocking main thread, god-object `app.py`) are fixed by refactoring, not
  by a language change.
- **Write all new distributed components in Go**: the fleet agent and the coordinator.
  Rationale: single static binary for every OS/arch (the same distribution model
  `BinaryManager` already handles for immich-go), same ecosystem as immich-go (upstream
  contributions stay in one language), goroutines fit a supervise-many-uploads workload.
  Rust would also work but adds build/contributor friction with no measurable win here —
  the hot loop is network I/O, not CPU.
- **gRPC over mTLS for the control plane** (coordinator ↔ agents): typed contracts,
  bidirectional streaming for live progress/logs, codegen for Go and Python (and Dart, if a
  mobile app happens). Not REST (no streaming, hand-rolled polling), not GraphQL (nothing
  here needs client-shaped queries). "Spark server" — Apache Spark is a distributed
  *analytics* engine; it is the wrong tool for upload orchestration. The coordinator is a
  small single-binary Go service instead.

---

## Phase 0 — Stabilize: fix what the review found (do this first)

The extreme review found the current app ships broken behavior that would poison any
multi-device work built on top. Full list in [CODE_REVIEW.md](CODE_REVIEW.md); the blockers:

1. **[critical]** macOS runs can never authenticate — env vars never reach Terminal.app
   (`core/terminal_launcher.py:176`). Every server-backed run on macOS fails today.
2. Cross-profile state bleed — new/loaded profiles inherit the previous profile's form data
   (`app.py:2253`).
3. Profile rename desyncs save/load targets and keyring identity; edits after rename are
   silently lost (`app.py:2170`, `core/profile_manager.py:296`).
4. Two entirely dead features from signal-wiring bugs: manual binary path (`app.py:1076`)
   and ban-file rows (`app.py:691`).
5. Album options silently dropped on Upload From Folder / Picasa
   (`core/command_builder.py:535`, `app.py:1222`, `core/advanced_flags.py:744`).
6. Windows `.bat` launcher mangles `%` and non-ASCII paths (`core/terminal_launcher.py:81,93`).
7. Corrupt config/profile files silently replaced with defaults, then overwritten
   (`core/config_manager.py:199`, `core/profile_manager.py:97`).
8. macOS release artifact is arm64 mislabeled x86_64 (`.github/workflows/release.yml:92`).
9. **Test hermeticity**: the suite currently writes the developer's real config, real OS
   keyring, and real lock files (`tests/test_app.py:1575,1678`), with a session-scoped mutable
   GUI fixture creating order-dependent passes. Fix via an autouse conftest that redirects
   config/HOME/keyring to tmp, and a reset-per-test fixture. Nothing later in this plan is
   trustworthy until CI is hermetic.
10. The remaining ~25 quick wins from the review (glob escaping, https default, atomic
    writes, checksum verification of downloaded binaries, dead-code removal, version-string
    drift, etc.).

Exit criteria: all critical/high findings fixed with regression tests; suite hermetic and
green on Linux/macOS/Windows CI; a launch-script content test per platform.

## Phase 1 — Hyper-optimize and restructure the GUI

Make the existing app excellent at what it is, and carve the seams the fleet work needs.

- **Never block the Qt main thread**: move Test Connection, run-button preflight,
  update checks, compat probing, and cancel-waits onto worker threads (the pattern already
  exists in `InstallWorker`). Today these freeze the UI for 3–45 s.
- **Kill per-keystroke recursive glob scans** (`app.py:622`, `core/validation.py:189`):
  debounce `update_status`, expand globs only at confirm time. This is the single biggest
  perceived-performance fix.
- **Decompose the 3,471-line `app.py` god object**: `widgets.py`, per-tab page builders,
  `JobController`, `UpdateController`. Introduce a `FormState` model so widgets stop being
  the only source of truth (structurally fixes profile bleed; lets `core/` tests run without
  a display server).
- **Extend the declarative `FlagDef` registry to primary tab flags**, collapsing the three
  parallel elif chains so a new tab/flag is one registry entry instead of ~10 coordinated edits.
- **Extract the Runner seam** — the review's key architectural finding: a
  `Runner` interface consuming the already-serializable `CommandPlan`
  (`external-terminal` | `attached-subprocess` | later `remote-agent`), a jobs *collection*
  replacing the single lock file, and plan building split into control-side flag emission vs
  target-side path resolution (today `abspath`/glob expansion happens at build time, which
  would mangle remote paths).
- Startup-time pass (defer heavy imports, single theme application) and measured baselines:
  cold start, keystroke latency, memory during a long run.

Exit criteria: UI never freezes; app.py < ~800 lines of shell; `Runner` abstraction merged
with the existing terminal launcher as its first implementation; perf baselines recorded.

## Phase 2 — Parallel local jobs + server capability autodetection

- **Attached-subprocess Runner**: launch immich-go as a captured child (live log pane,
  exit-code propagation, real cancel) instead of fire-and-forget terminal windows. Keep the
  terminal option for people who want it.
- **Multiple concurrent jobs**: job dashboard (queue, progress, per-job logs), per-job locks
  (the lock format already carries identity), global concurrency limit.
- **Server capability probe** (the "auto recognize the server" feature):
  - Immich API: version, features, storage quota/free space, job queue depths.
  - Measured: upload micro-benchmark (push N MB, measure), latency, and — where the server
    admin key is provided — job throughput during a probe window.
  - Output: a capability profile (max useful parallel streams, suggested job-pause set,
    expected wall-clock for a given corpus) shown before a big run.
- **Auto-tuning**: derive per-job and global concurrency from the probe instead of guesses;
  pause/resume Immich background jobs around bulk ingests (thumbnails stay on for
  "immediately available"); resume + verify pass at the end.
- **Benchmark harness** in-repo: synthetic corpus generator + timed end-to-end runs against a
  disposable Immich (docker compose), so every optimization claim in later phases is measured.

## Phase 3 — Fleet: link and manage multiple machines

The 20-device answer for everything that isn't a phone (laptops, desktops, NAS boxes):

- **`immich-fleet-agent` (Go, single static binary)**: runs on each source machine. Speaks
  gRPC/mTLS to the coordinator; receives a `CommandPlan`-derived job spec; resolves paths
  locally; downloads/verifies its own immich-go via the existing `BinaryManager` os/arch seam
  (which already anticipates this); supervises the run; streams progress/logs; supports
  pause/resume/cancel; survives restarts (resumable — immich-go's dedup makes re-runs cheap).
- **Coordinator**: embedded in the GUI first (the GUI *is* the control plane — zero extra
  deployment), extractable later into a headless daemon (docker) with the GUI as a remote
  console. Schedules jobs across agents against the Phase 2 capability profile so the fleet
  saturates — but never floods — the server.
- **Pairing/security**: one-time token or QR pairing; mTLS certs issued by the coordinator;
  API keys delivered to agents via env at spawn time only (extending the existing
  env-only-secrets discipline); no secrets at rest on agents.
- **Fleet dashboard** in the GUI: all machines, all jobs, aggregate throughput, server ingest
  backlog, ETA.

## Phase 4 — Android & iOS

Honesty first: **Immich already ships official Android/iOS apps** with background photo
backup. Rebuilding phone→Immich upload would duplicate a mature app. Two credible scopes:

- **Option A (recommended default)**: phones use the official Immich app; our system owns
  desktop/NAS bulk ingest and fleet orchestration. We add a "phone lane" to the 10 TB
  playbook (Phase 5) that sequences official-app backups so they don't fight the bulk ingest.
- **Option B (companion app)**: a Flutter app (same stack as Immich's own; gRPC via Dart
  codegen) that is primarily a *fleet remote-control* — monitor jobs, approve runs, watch
  ETAs from your phone — and optionally a bulk first-migration uploader feeding the
  coordinator. Costs to be clear about: Apple Developer account ($99/yr), App Store /
  TestFlight review for iOS (no sideloading), Play Console for Android (or direct APK),
  iOS background-execution limits that make third-party bulk uploaders genuinely hard.

Decision needed before Phase 4 work starts; everything earlier is unaffected either way.

## Phase 5 — The 10 TB / 20-device playbook

Turn the fleet into a repeatable migration procedure, and validate it:

- Pre-flight: capability probe, storage check (10 TB + transcode headroom), network audit
  (flag Wi-Fi sources; recommend wired staging for the biggest corpora).
- Ingest sequence: pause transcode/ML jobs → staged waves of agents sized to the server's
  measured ceiling → thumbnails prioritized so the timeline is browsable during ingest
  ("immediately available") → resume heavy jobs → verification pass (counts, checksums,
  dedup report) → summary report.
- Published benchmarks from the Phase 2 harness: X TB from N agents into a reference server
  in Y hours, so expectations are set by measurement, not marketing.

---

## Sequencing & effort (relative)

| Phase | Size | Depends on |
|---|---|---|
| 0 — Stabilize | M | — |
| 1 — Optimize/refactor | M–L | 0 |
| 2 — Parallel jobs + probe | M | 1 |
| 3 — Fleet (Go agent + gRPC) | L | 1 (Runner seam), 2 (probe) |
| 4 — Mobile | M (Option B) / S (Option A) | 3 |
| 5 — Playbook + benchmarks | S–M | 2, 3 |

## Open decisions

1. **Mobile scope**: Option A (official Immich app + orchestration) or Option B (Flutter
   companion app)? Recommendation: A now, B's monitoring-only subset later if wanted.
2. **Coordinator shape**: embedded-in-GUI first (recommended), headless daemon later — agree?
3. **Go for agent/coordinator** (recommended over Rust for distribution + ecosystem fit) — agree?
4. Anything in Phase 0's fix list you'd rather not change (e.g. behavior someone depends on)?
