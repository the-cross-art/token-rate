# token-rate

A working rig for learning **what a token actually costs** when you serve it yourself.

Quantized Qwen3-8B runs locally on Apple silicon through four different decoding
strategies. Every request passes through a metering proxy that times it, counts
it, prices it two ways, writes it to a ledger, exports it to Prometheus, and
pushes it to a Stripe meter. A LangGraph swarm drives realistic, uneven load
across it.

The question the whole thing exists to answer: **does speculative decoding make
tokens cheaper, and by how much, and for whom?**

---

## The idea that makes this interesting

A hosted API charges you **per token**. Your own machine charges you **per
second**.

That difference is the entire project. Hosted cost is linear in token count and
completely indifferent to how fast you generate. Local cost is linear in
*wall-clock time*, so it falls as throughput rises:

```
local $/token  =  (hardware amortization + marginal power) / tokens_per_second
```

Double your decode speed and you halve your cost per token, for free, with the
same model and the same weights. That is the lever speculative decoding pulls,
and this rig measures exactly how far it moves.

Both numbers are computed for every single request and shown side by side.

---

## What's in here

| Piece | What it is |
|---|---|
| `proxy/` | OpenAI-compatible FastAPI proxy — times, counts, prices, meters, bills |
| `swarm/` | LangGraph research pipeline: planner → 3 researchers → critic → synthesizer |
| `bench/` | A/B harness that cycles every decoding arm over identical prompts |
| `monitoring/` | Prometheus + Grafana configs, provisioned from files (run natively, no Docker) |
| `scripts/serve.sh` | Brings up one inference arm at a time |
| `scripts/stripe_bootstrap.py` | Creates the Stripe test-mode meter, price and customers |
| `config/profiles.yaml` | **Hardware profiles** — models, context, and the local cost basis per machine |
| `config/rates.yaml` | The hosted-API rate card |
| `scripts/bootstrap.sh` | One-command setup on a fresh machine |

### The four arms

All four serve the **same Qwen3-8B Q4_K_M weights**. Only token production differs.

| Alias | Port | Strategy | Extra memory |
|---|---|---|---|
| `spec-qwen3-8b` | 8081 | draft-model speculation, Qwen3-0.6B Q8 drafter | ~0.7 GB |
| `ngram-qwen3-8b` | 8082 | n-gram speculation, no draft model | none |
| `plain-qwen3-8b` | 8083 | no speculation — the control | none |
| `ollama-qwen3-8b` | 11434 | Ollama, no speculation — a second control | — |

**Ollama cannot do speculative decoding.** It has no draft-model option at all.
That is why it appears here as a *control*, not as a peer: comparing it to the
llama.cpp plain arm isolates engine overhead, so the speculation gain measured
between the llama.cpp arms isn't secretly an engine difference.

**One arm runs at a time.** 16 GB of unified memory cannot hold two 8B servers,
and even if it could, concurrent arms would contend for the same GPU and you'd
be measuring scheduler noise instead of decoding strategy.

---

## Setup

On a fresh machine:

```bash
git clone <this repo> && cd token-rate
./scripts/bootstrap.sh        # installs deps, detects hardware, downloads models
```

Then start it in four shells:

```bash
make monitoring     # Prometheus :9090, Grafana :3001 — native, ~420MB
make proxy          # metering proxy :8080   (leave running)
make spec           # speculative arm :8081  (leave running)
make swarm          # run the pipeline, print the cost table
```

Grafana: **http://127.0.0.1:3001/d/token-rate-main** (anonymous viewer;
`admin`/`admin` to edit).

> Grafana is on 3001, not the usual 3000, because a local dev server commonly
> owns 3000. Everything binds to `127.0.0.1` only — nothing is exposed to your
> network.

`make down` stops everything. `make help` lists the rest.

### Hardware profiles

Every machine-specific value lives in `config/profiles.yaml` — which model,
what context length, and **what the machine costs to run**. Nothing else in the
repo hardcodes hardware, so the same code produces correct numbers on different
machines.

| Profile | RAM | Target | Draft | Context |
|---|---|---|---|---|
| `air-m4-16gb` | 16 GB | Qwen3-8B Q4_K_M | Qwen3-0.6B Q8 | 8192 |
| `m5pro-48gb` | 48 GB | Qwen3-32B Q4_K_M | Qwen3-1.7B Q8 | 32768 |

The profile is auto-detected from installed RAM, or forced:

```bash
make profile                              # show the active one
TOKENRATE_PROFILE=m5pro-48gb make spec    # force one
```

**Update `machine_cost_usd` and `load_watts` for your machine, or every local
cost number will be wrong.** Measure power draw under sustained decode with
`sudo powermetrics --samplers cpu_power -i 1000` rather than guessing.

Why a *dense* 32B on the 48 GB profile rather than a same-size MoE: a MoE like
Qwen3-30B-A3B activates only ~3B parameters per token, so it is already fast
and speculation helps it far less. Dense models are memory-bandwidth bound,
which is exactly what speculative decoding attacks. Swap the profile's target
to see that contrast yourself — it is one of the more interesting things this
rig can show you.

### Why monitoring runs natively by default

Docker Desktop costs roughly **2 GB of RAM** to run a Linux VM whose only job is
hosting two Go binaries that run natively on macOS. On a 16 GB machine that 2 GB
is the difference between the model sitting in memory and paging to swap, and
that measured here as a **3.4x throughput swing** — a 3.4x swing in cost per
token, far larger than any decoding optimization.

Native Prometheus + Grafana cost about 420 MB between them and start in seven
seconds. On a 48 GB machine the 2 GB is noise and Docker is the more portable
choice, so both paths are supported:

```bash
make monitoring          # native (default)
make monitoring-docker   # Docker Desktop
```

Both read the same provisioning files, so the dashboards are identical.

### The benchmark

```bash
make bench          # cycles all four arms, ~10 minutes
```

It runs three deliberately different prompt types, because **a single prompt
type will tell you a flattering or damning story about either speculation
method**:

- **generative** — novel prose. The hard case; the drafter must genuinely predict.
- **structured** — JSON output. Highly predictable sequences; draft models shine.
- **extractive** — repeat-the-input. The n-gram arm's best case.

Expect draft-model speculation to win big on structured output and modestly on
prose, and n-gram speculation to be near-free on extractive work and near-useless
on novel prose.

---

## Metered billing (Stripe test mode — free, permanently)

Test mode never charges anything and needs no card. Test customers have no
payment method and invoices never settle. There is no paid Stripe plan involved
and no way for this to cost money.

### 1. Get a test key

1. Sign up at **https://dashboard.stripe.com/register** (email + password; no
   card, no business details needed to use test mode).
2. In the dashboard, confirm the **Test mode** toggle (top right) is ON.
3. Go to **Developers → API keys** and copy the **Secret key** — it starts with
   `sk_test_`.

You never need to activate the account or submit business details. An
unactivated account can use test mode indefinitely.

### 2. Put it in `.env`

```bash
STRIPE_API_KEY=sk_test_51Abc...        # your key
STRIPE_ENABLED=true
STRIPE_METER_EVENT_NAME=llm_tokens
```

The proxy **refuses to start** if `STRIPE_ENABLED=true` and the key is not
`sk_test_`. Live mode is not reachable from this codebase.

### 3. Create the billing objects

```bash
make stripe-setup
```

This is idempotent — run it as often as you like. It creates:

| Object | What it does |
|---|---|
| **Meter** `llm_tokens` | sums usage events, keyed to a customer by `stripe_customer_id` |
| **Product + metered Price** | $0.00054 per 1,000 tokens, driven by that meter |
| **7 Customers** | one per swarm agent — `planner`, `researcher-1..3`, `critic`, `synthesizer`, `bench` |
| **7 Subscriptions** | what turns meter events into invoice line items |

IDs are written to `data/stripe.json`. The swarm reads that file and sends each
node's `X-Customer` header automatically — no further wiring.

### 4. Restart the proxy and generate usage

```bash
# in the proxy's shell: Ctrl-C, then
make proxy      # startup log should now say "stripe: ENABLED (test mode)"
make swarm
```

### 5. See the usage

```bash
make stripe-usage      # reads it back out of Stripe, per agent
```

Or in the Stripe dashboard: **Billing → Meters → LLM tokens**. Aggregation is
asynchronous; allow ~a minute after a run.

To see an actual invoice, open a customer under **Customers**, find their
subscription, and view the upcoming invoice — the metered line item will show
the token count and the computed charge.

### How two rates fit on one meter

The meter sums a single value, but the rate card has separate prompt and
completion prices. Prompt tokens are therefore discounted to their
completion-token equivalent before being sent (`billable_units` in
`proxy/stripe_meter.py`), so one meter and one price reproduce the true
two-rate price. In production you would use two meters; folding them here keeps
the mechanics visible.

### Failure behaviour

Billing is fire-and-forget on a background task. **A billing failure must never
add latency to, or fail, an inference request.** Every attempt is reconcilable
afterwards against the SQLite ledger, and `make stripe-usage` vs `make usage`
is the reconciliation: if those two disagree, events were dropped or
double-counted.

---

## What the dashboard shows

Three rows, in the order the questions actually arrive:

1. **Decoding speed** — throughput per arm, TTFT p95, draft acceptance rate.
   If the arms overlap on throughput, speculation isn't earning its memory.
2. **Unit economics** — local $/1k tokens per arm, hosted-equivalent vs actual
   cumulative spend, burn rate, margin.
3. **Swarm attribution** — spend per agent, token rate per agent, a per-agent
   ledger table.

### The number to watch: draft acceptance rate

Speculative decoding runs a small model ahead of the big one and has the big
model verify several tokens at once. Rejected drafts are wasted target-model
compute. Below roughly **0.6 acceptance**, the drafter costs more than it saves —
raise `--spec-draft-p-min` (demand more drafter confidence) or lower
`--spec-draft-n-max` (draft shorter runs). Both knobs are in `scripts/serve.sh`.

### The thing that will surprise you

In the research swarm, the **synthesizer** usually dominates spend while
producing the *fewest* tokens. It sees every prior output, so its prompt is
enormous. Watch the per-agent table's prompt-to-completion ratio: a high ratio
means a node is paying to re-read context rather than to think, and that's the
first thing to attack when trimming agent cost.

### The other thing that will surprise you

Your margin vs hosted will often be **negative**. Amortization accrues whether
or not you're generating tokens, so an idle machine is pure loss, and hosted
per-token prices for small models are genuinely very low. Self-hosting wins on
sustained high utilization, privacy and latency — not automatically on price.
This rig will show you the actual crossover point rather than letting you assume
one.

---

## Moving to another machine

Three commands:

```bash
git clone <this repo> && cd token-rate
./scripts/bootstrap.sh
# edit config/profiles.yaml: machine_cost_usd + load_watts for YOUR machine
```

The profile is picked from installed RAM automatically. On a 48 GB M5 Pro that
selects `m5pro-48gb` — Qwen3-32B Q4_K_M with a Qwen3-1.7B drafter at 32k
context — and the local cost model switches to that machine's basis. Proxy,
dashboards, ledger, swarm and billing are unchanged.

If your machine is not covered, add a profile rather than editing scripts. The
resolver **refuses to guess**: an unmatched machine is an error, not a silent
fallback, because picking the wrong cost basis would quietly corrupt every
number the project exists to produce.

With RAM to spare, `make monitoring-docker` runs the dashboards under Docker
Desktop instead of natively.

## Caveats worth knowing

- **The M4 Air is passively cooled.** Sustained benchmarks throttle. Run `bench`
  on a cool machine and don't trust a single run — the harness takes medians
  across repetitions for this reason.
- **`load_watts: 25.0` in `rates.yaml` is an estimate.** Measure yours with
  `sudo powermetrics --samplers cpu_power -i 1000` under load and replace it.
- **Free RAM is a throughput input on 16 GB.** Measured on this machine with
  Qwen3-8B Q4_K_M: **6.2 +/- 2.09 tok/s** with an IDE, browser and Docker open,
  **20.8 +/- 0.04 tok/s** with them closed. That is 3.4x throughput, 3.4x cost
  per token, and the difference between a measurement and noise. Close your
  desktop before benchmarking. The GPU reports 12.7 GB addressable.
- **Ollama and a llama.cpp arm running at once will both be slow.** `make stop`
  between arms; the bench harness does this automatically.

---

## A trap this rig caught on its first run

Qwen3 is a **hybrid-thinking model**. It emits reasoning tokens that are billed
exactly like visible output tokens but never reach the caller. The very first
smoke test through this proxy looked like this:

```
completion_tokens:    250
reasoning_estimated:  250
content:              ''
```

Full price, empty response. A naive meter counts 250 completion tokens and
reports healthy throughput; the user got nothing.

The proxy therefore estimates the reasoning/visible split from the character
ratio between the `reasoning` and `content` fields — neither llama.cpp nor
Ollama reports it separately — and exports it as
`tokenrate_reasoning_tokens_estimated_total`. The **"Hidden reasoning as a share
of billed output"** panel tracks it per agent.

Two mitigations, both in the swarm already: append `/no_think` to prompts that
don't need deliberation, and set `max_tokens` high enough that thinking cannot
consume the entire budget.
