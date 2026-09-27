# DFIR-DUCK

Drop a DFIR-ORC collection in, get the artefacts extracted and Sigma detections
out.

DFIR-DUCK is a web front-end that chains together tools you already trust. It
takes the `.7z` archives produced by
**[DFIR-ORC](https://github.com/DFIR-ORC/dfir-orc)**, hands them to
**[DFIR-OGRE](https://github.com/ANSSI-FR/dfir-ogre)** for parsing and
normalisation, pulls the raw artefacts OGRE does not surface — event logs, MFT,
registry hives — and runs three Sigma engines over them. One page follows the
whole thing, from upload to output.

Both DFIR-ORC and DFIR-OGRE come from
**[ANSSI](https://github.com/ANSSI-FR)**, the French national cybersecurity
agency. ORC does the collection on the machine; OGRE turns that collection into
a normalised artefact timeline. DFIR-DUCK does neither — it orchestrates them,
alongside [Chainsaw](https://github.com/WithSecureLabs/chainsaw),
[Hayabusa](https://github.com/Yamato-Security/hayabusa) and
[Zircolite](https://github.com/wagga40/Zircolite), and gets out of the way. The
credit for the analysis belongs upstream.

Everything runs in a single Docker container on one host, and no data leaves
the machine.

The interface is in French.

---

## Requirements

A Linux host with Docker and the Compose plugin, 16 GB of RAM, and enough disk
for your collections — expect the extracted output to be several times the size
of the archives.

---

## Install

```bash
git clone https://github.com/T-Godard/DFIR-DUCK.git dfir-duck && cd dfir-duck
cp .env.example .env         # edit it, see below
./init.sh                    # build the image
docker compose up -d
curl localhost/healthz       # {"status":"ok",...}
```

The build fetches and compiles the analysis tools, so the first one takes 15 to
30 minutes. Later builds reuse the Docker cache.

Then open `http://<host>:<port>/` — port 80 by default. **There is no login.**
Put it behind your own reverse proxy and access controls if the host is
reachable by anyone else.

### `.env`

| Variable | Default | What it does |
|---|---|---|
| `DFIR_DUCK_DIRECTORY` | `/opt/dfir-duck` | Host directory holding all data |
| `DFIR_DUCK_PORT` | `80` | HTTP port on the host |
| `DFIR_DUCK_MEM_LIMIT` | `16g` | Container memory ceiling |
| `DFIR_DUCK_CPU_LIMIT` | `8` | Cores the container may use |

`.env.example` documents the rest, including how to split the heavy directories
across disks and how to read an intake pool owned by another service. Any key of
`app/config.yaml` can be overridden here in upper case. Tool versions are pinned
in the `Dockerfile`.

---

## Use it

**Feed it.** An *acquisition* is one `*_Outcome.json` plus the `.7z` archives it
references — usually one machine. Either drop the files in the **Acquisitions**
tab, or copy them into `<data>/uploads/_pending/` yourself. Files sort
themselves out, order does not matter, and processing starts on its own as soon
as an acquisition has all its archives.

**Watch it.** **Vue d'ensemble** shows one card per machine with two steps:
*Collecte* and *Extraction*. The **Extraction** tab lists the individual runs.
Every row has two buttons: **Log** opens the live output, **Forcer** runs the
tool again — nothing retries by itself, so this is how you recover from a
failure once you have fixed its cause. A failed tool never blocks the others.

**Read it.** Everything lands under `<data>/output/`, one directory per tool and
per machine, named after the machine so browsing by hand is readable. CSV and
JSON Lines — take it from there with whatever you use to analyse.

### What runs

| Tool | Produces |
|---|---|
| `dfir-ogre` | Artefact timeline and normalised raw data |
| `evtx-extract` | The `.evtx` logs pulled out of the archives |
| `evtx-chainsaw`, `evtx-hayabusa`, `evtx-zircolite` | Sigma detections, three engines on the same logs |
| `mft-extract` | The NTFS master file table |
| `mft-chainsaw` | Detections on the MFT, with Chainsaw's own MFT rules |
| `registry-extract` | The registry hives |
| `registry-chainsaw` | The shimcache, as an execution timeline |

DFIR-OGRE does not surface the raw MFT or the hives, hence the two `*-extract`
runs. Uncheck any tool in the **Paramètres** tab; switching off an extraction
also stops whatever depends on it.

> **On the shimcache.** An entry carries the file's modification date, not its
> execution time, so the raw list dates nothing. Chainsaw builds a timeline by
> anchoring the entries it *can* date: those matching a path pattern, and —
> when the Amcache was collected — those whose two timestamps sit close
> together. The patterns live in `app/tools/shimcache_patterns.txt`, one regular
> expression per line, and they decide how much of the timeline ends up dated.
> Adding patterns is not automatically an improvement: one that matches paths
> where *old* files live anchors the timeline on the wrong dates.

---

## Where things land

```
<data>/
  uploads/<machine>/          the collection as received
  output/<tool>/<machine>/    everything the tools produce
  runs/<run_id>/run.log       full log of every run
  logs/app.log                application log
  investigation.json          state of the investigation
  preferences.json            settings made in the interface
```

Deleting an acquisition's directories and its entry in `investigation.json`
removes it; there is no delete button yet.

---

## Operating it

```bash
docker compose logs -f              # application log
docker compose restart              # after changing app/config.yaml
./init.sh && docker compose up -d   # after changing the Dockerfile
curl localhost/healthz              # status, running jobs, resources
curl localhost/metrics              # Prometheus metrics
```

`app/config.yaml` holds concurrency, timeouts and per-run resource limits; every
key is commented there. When a run fails, its **Log** says why.

---

## Licence

DFIR-DUCK is Apache 2.0 — see [`LICENSE`](LICENSE).

The analysis tools are **not** part of this repository: the image downloads them
at build time and each keeps its own licence, several of them copyleft.
[`NOTICE.md`](NOTICE.md) lists every one of them with its licence and source.
Read it before publishing a prebuilt image anywhere — that would redistribute
them, with obligations this project cannot discharge for you. Build your own
image; sharing this source tree carries no such obligation.
