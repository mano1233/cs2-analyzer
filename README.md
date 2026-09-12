# cs2-analyzer

Parses CS2 demos and tracks one player's aim, utility and team-role stats over time.

Runs as a nightly Kubernetes CronJob on the [midgard](https://github.com/mano1233/midgard)
cluster (module `terraform/modules/cs2-analyzer`), and the same code runs locally against a
folder of demos.

## What it measures

Beyond kills and damage, the things that actually explain lost rounds:

- **Aim** — accuracy, headshot rate, and the share of first shots fired *while moving*
  (a moving shot in CS2 goes wide regardless of crosshair placement).
- **Utility** — thrown per round vs bought, how many flashes blinded an enemy for at
  least a second, how many of those blinds became kills, utility still in the inventory
  at death, and how late the first T-side utility lands.
- **Duels** — who takes the round's first fight, who wins it, and whether deaths get
  traded inside 5 seconds.
- **Roles and pairings** — role assigned from measured behaviour (AWP share, T-side
  first-contact rate, utility volume), plus per-pair trade counts, flash-to-kill
  attribution, and average distance between teammates while both are alive.
- **Scopes** — every counter is available overall, per side (T/CT), and per half
  (MR12: rounds 1-12, 13-24, then overtime).

## Layout

| File | Role |
|---|---|
| `analyze.py` | The parser and all metrics. `analyze(dem)` returns one match; run directly for a per-match and pooled table. |
| `team.py` | Team report: individual performance, role signals, per-half tables, trade/flash/proximity matrices. |
| `cluster_run.py` | The CronJob entrypoint. Fetches new FACEIT demos, parses one at a time in scratch, writes results back to R2, publishes the report. |
| `report.py` | Renders `index.html` (headline metrics + per-match rows) and `team.html` (roles, per-half, trade/flash/distance matrices) from parsed results. |
| `Dockerfile` | Multi-arch image (`linux/amd64`, `linux/arm64`). |

## Running locally

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python analyze.py          # parse every *.dem here, write results.json
.venv/Scripts/python analyze.py --cached # reprint from results.json, no reparsing
.venv/Scripts/python team.py             # the team report
```

Demos are read from the working directory. FACEIT hands out `.dem.zst`; the parser needs
an unpacked `.dem`, so decompress first (`cluster_run.py` does this automatically).

## Running in the cluster

`cluster_run.py` expects:

| Variable | Meaning |
|---|---|
| `R2_BUCKET`, `R2_ENDPOINT` | S3-compatible target holding `demos/`, `results/`, `state/` |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | R2 credentials (Terraform mints these) |
| `FACEIT_API_KEY` | Server-side FACEIT Data API key. Empty is valid: it then only parses demos already in the bucket |
| `FACEIT_NICKNAME` | Whose matches to fetch and whose stats to track |
| `MAX_PER_RUN` | Cap on demos downloaded and parsed per run |
| `FACEIT_WINDOW_DAYS` | How far back to look for matches (default 21 - "the last three weeks", not a fixed match count) |
| `REPORT_DIR` | Where the HTML is rendered before publishing |
| `REPORT_CONFIGMAP` | ConfigMap to patch with the rendered pages; the web pod mounts it and kubelet re-syncs it, so nothing restarts |
| `SCRATCH` | Scratch dir for one demo at a time (an `emptyDir` in the CronJob) |

Output: `results/results.json` (full per-match, per-player counters) and
`results/trend.csv` (one row per match with the headline metrics).

## Notes

- Players are keyed by **name**, not SteamID: SteamID columns become float64 when
  NaN-padded and silently lose precision.
- `demoparser2` publishes `manylinux_2_17_aarch64` wheels, so the ARM image needs no
  Rust toolchain.
- A demo is ~200 MB compressed and ~285 MB unpacked; parsing peaks in the low gigabytes,
  which is why the CronJob is pinned to the 16 GB node.

## Versioning

The tag in `VERSION` is what the build publishes and what midgard's Terraform pins.
Bump it in the same commit as the change it ships.
