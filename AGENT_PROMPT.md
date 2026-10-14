# Prompt for the agent on the new machine

Paste the block below to a coding agent (Claude Code, Cursor, etc.) running in
an empty directory on the M5 Pro.

---

```
Set up and run the token-rate project end to end on this machine.

Repo: https://github.com/the-cross-art/token-rate  (private — I'm authenticated via gh)

This is a rig for measuring what an LLM token actually costs when self-hosted.
It serves quantized Qwen3 locally through four different decoding strategies,
meters every request, prices it two ways (what a hosted API would charge vs
what it actually cost this machine), shows it in Grafana, and optionally pushes
usage to Stripe metered billing in test mode.

HARDWARE: MacBook Pro, M5 Pro, 48GB unified memory. Docker Desktop is installed
and I want to use it for monitoring here — 48GB has room for it.

Do all of this:

1. Clone the repo and run ./scripts/bootstrap.sh
   It auto-detects the hardware profile from installed RAM. On 48GB it must
   select `m5pro-48gb`, which uses Qwen3-32B-Q4_K_M (~20GB) with a
   Qwen3-1.7B-Q8_0 drafter (~1.8GB) at 32k context. Confirm this before the
   download starts — if it picks a different profile, stop and tell me.

2. Open config/profiles.yaml and update the `m5pro-48gb` profile's cost block:
   - machine_cost_usd: ask me what I paid for this machine
   - load_watts: MEASURE it, don't guess. Run a sustained generation and
     sample with: sudo powermetrics --samplers cpu_power -i 1000
   These two numbers drive the entire local cost model. If they're wrong,
   every cost figure the project produces is wrong.

3. Start it:
     make monitoring-docker     # Grafana :3001 + Prometheus :9090 under Docker
     make proxy                 # metering proxy :8080      (own shell)
     make spec                  # speculative arm :8081     (own shell)
   Verify with `make status` — it should show the profile and all ports UP.

4. Run `make bench`. This cycles all four decoding arms over three prompt
   types and prints medians. It takes ~15 minutes on a 32B. Report back:
   - decode tok/s per arm
   - draft acceptance rate for the speculative arm
   - local $/1k output tokens per arm
   IMPORTANT: quit other heavy apps first. On the 16GB machine, memory pressure
   caused a 3.4x throughput swing and made the measurement meaningless. Check
   `sysctl vm.swapusage` before and after — if swap is growing during the
   benchmark, the numbers are not trustworthy and you should tell me.

5. Run `make swarm`. This is a LangGraph pipeline (planner -> 3 parallel
   researchers -> critic -> synthesizer). It prints a per-agent cost table.
   Confirm the expected pattern holds: the synthesizer should have by far the
   largest prompt-token count and the highest cost, because it sees every
   prior output. Tell me the prompt:completion ratio for each agent.

6. Stripe (optional, test mode only, cannot charge money):
   - I'll give you a sk_test_ key to put in .env, and set STRIPE_ENABLED=true
   - run `make stripe-setup` then restart the proxy and run `make swarm`
   - then run BOTH `make stripe-usage` and `make usage` and compare them.
     Those two disagreeing means meter events were dropped or double-counted.
     Report whether they reconcile.

7. Open http://127.0.0.1:3001/d/token-rate-main and confirm every panel has
   data. Set the time range to "Last 6 hours". If any panel says "No data",
   diagnose it — don't just report it. Known gotcha: rate() cannot compute a
   value for a counter whose first observed sample is already its current
   value, which happens when an agent makes exactly one request per run. The
   economics panels already use cumulative ratios for this reason.

8. Finally, tell me how the 32B compares to the 8B numbers measured on the Air:
     Qwen3-8B Q4_K_M on M4 Air 16GB (idle machine):
       plain       20.8 tok/s    $0.00075 per 1k output tokens
       speculative 23.7 tok/s    $0.00067 per 1k, 84.8% draft acceptance
   A 32B is ~4x the weights, so expect roughly a quarter the tok/s — but the
   M5 Pro has far more memory bandwidth, so the real figure is the interesting
   part. I especially want to know whether speculative decoding helps MORE on
   the dense 32B than it did on the 8B (it should — dense models are
   bandwidth-bound, which is exactly what speculation attacks).

Read README.md first. Ask me before changing anything in config/profiles.yaml
beyond the two cost numbers.
```
