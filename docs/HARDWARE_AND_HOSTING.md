# What the backend actually needs, and what it costs

For a **15-participant, 4-week** study round.

Everything in section 1 is measured on this project's own model and prompt. Prices in section 5
were looked up on **12 September 2026** and are the part most likely to be stale — cloud pricing
moved twice this year already.

---

## 1. The measurement that decides everything

The API and the database are trivial to host. **The model is the entire sizing question**, and it
comes down to one number: how slow is Qwen3-4B-Q4_K_M without a GPU?

Measured today, `llama-server -ngl 0` (nothing on the GPU), 8 threads, on a
**Ryzen 7 5800HS (8C/16T, 15.4 GB)**:

| | cold 512-token prompt |
|---|---|
| prompt processing (prefill) | **1.7 s** |
| generation | **7.0 tokens/sec** |
| ~60-token reply, end to end | **8.8 s** |

The app's real prompt is ~1000 tokens, so prefill roughly doubles: **expect 10–11 s per reply on
CPU**, against **1.3 s on the laptop's RTX 3050**.

> **Two earlier measurements were wrong and both were too fast.** The first reported 7.1 s with
> `prompt tokens: 1` — llama.cpp had cached the prompt, so prefill was not in the number at all.
> The second still showed 13, because only the tail of the prompt varied and the shared prefix
> was reused. Only the third, with the prompt varied at the front, paid the real cost. If you
> re-run this, check `prompt tokens` is in the hundreds before believing the result.

### So: GPU or not?

**This is your call, not a technical necessity.** Both work.

| | reply time | verdict |
|---|---|---|
| CPU only | ~10 s | Usable. A craving lasts minutes, so 10 s is not fatal — but it is a noticeably slow chat, and a participant may type again before it lands. |
| Any modern GPU | ~1–2 s | What you have been testing against. |

There is a third option people forget: **run with no model at all**. `LLAMA_SERVER_URL` empty is
a supported state — every reply comes from the deterministic layer, which is a real answer by
design (spec 10 §24). For a pilot that is testing the *app* rather than the *model*, this costs
nothing and removes the whole question.

---

## 2. Cloud server — sizing

### RAM is the binding constraint

| | |
|---|---|
| model weights (mmap, Q4_K_M) | 2.5 GB |
| KV cache — 288 MiB per 2048-token slot × 4 slots | 1.15 GB |
| llama-server overhead | ~0.5 GB |
| FastAPI + uvicorn | ~0.3 GB |
| PostgreSQL | ~0.5 GB |
| Ubuntu itself | ~0.5 GB |
| **total** | **≈ 5.5–6 GB** |

**8 GB is the minimum. 16 GB is comfortable.** Below 8 GB the kernel starts evicting the mmapped
weights, which is exactly the failure that took the model off the phone — it does not error, it
just gets slower and slower until replies stop arriving.

### The rest

| | minimum | comfortable | why |
|---|---|---|---|
| vCPU | 4 dedicated | 8 dedicated | Generation is compute-bound. **Avoid burstable/shared** (AWS `t`-class, GCP `e2-micro`): sustained inference exhausts the credits and the box collapses to a fraction of a core. |
| Disk | 40 GB | 80 GB | 2.4 GB model + ~13 GB bundle/OS/Postgres. The rest is headroom for logs and exports. |
| Bandwidth | 100 GB/mo | — | Tiny. A reply is a few hundred bytes; sync batches are small JSON. 15 users will not reach 1 GB/month. |
| Region | closest to participants | — | India-region if participants are in India — it is round-trip latency on every turn, on top of the 10 s. |

### Does 15 users need more?

No. `llama-server` defaults to **4 slots**, so four replies generate concurrently and the rest
queue. Fifteen participants do not chat simultaneously; even if five did, the fifth waits one
reply-time. The database side is nothing — the live one currently holds **85 events total**.

---

## 3. Minimum laptop to host it

If a laptop stays on instead of a cloud VM. Same requirements, plus: it must not sleep, and it
needs a reachable address (or the ngrok tunnel the bundle already carries).

| | Intel | AMD Ryzen |
|---|---|---|
| **Minimum (CPU-only, ~10 s replies)** | Core i5-11400H / i5-1240P or newer — 6C/12T | Ryzen 5 5600H / 5600U or newer — 6C/12T |
| **Comfortable (CPU-only)** | Core i7-12700H, 8P+4E | Ryzen 7 5800H / 7735HS — 8C/16T |
| **Fast (~1–2 s replies)** | any of the above **+ NVIDIA RTX 3050 4 GB or better** | same |
| RAM | **16 GB** | **16 GB** |
| Disk | 50 GB free SSD | 50 GB free SSD |

**Notes that matter more than the CPU model:**

- **16 GB RAM, not 8.** With 8 GB, Windows plus the 2.5 GB model plus Postgres leaves nothing,
  and you hit the eviction failure above. The measurement machine has 15.4 GB with 6.7 GB free.
- **4 GB of VRAM is enough** for this model at Q4_K_M — the RTX 3050 in the dev laptop does
  1.3 s. You do not need a 4090.
- **Any 8-core CPU from 2021 onwards** lands within a second or two of the 8.8 s measured here.
  The generation rate is memory-bandwidth-bound more than clock-bound, so newer RAM helps more
  than a faster core.
- An **Intel iGPU or AMD Radeon iGPU** can run the Vulkan build, which is usually **faster than
  CPU but well short of a discrete card**. The `--full` bundle ships that build, so it is worth
  trying before buying anything.

---

## 4. Free options for 4 weeks

Ordered by how likely they are to actually work for this.

### Google Cloud — $300 credit, 90 days ← **best fit**

Covers a 4-week round comfortably, and unlike the others the credit is large enough to afford a
**GPU** instance rather than forcing CPU-only. A T4 or L4 VM for 4 weeks at roughly $0.35–0.70/hr
is ~$240–470 for 28 days of continuous running — so run it **only during the hours the study
needs**, which is what the freeze-item-7 design already assumes. At 10 hours a day it is
comfortably inside $300.

### Azure — $200 credit, but only **30 days**

Tight for a 4-week round with no margin for setup. The 12-month free services do not include a VM
large enough. Workable if the round starts the day you sign up.

### AWS — $200 in credits, 6-month plan

$100 on sign-up, $100 more by using services. The always-free tier is `t`-class burstable, which
is the one shape to avoid for inference.

### Oracle Always Free — **read this before relying on it**

Genuinely free forever, but **two problems**:

1. **It was halved on 15 June 2026** — now **2 OCPU / 12 GB**, down from 4/24. Still enough RAM,
   but half the compute, so expect noticeably worse than the 10 s measured here.
2. **It is ARM (Ampere A1).** The deployment is x86_64: `ghcr.io/ggml-org/llama.cpp:server`
   and the `python:3.12-slim` base both have arm64 tags, so this is a smaller problem than it
   was, but it is untested and `deploy/gcp/` assumes Compute Engine throughout.

---

## 5. Paid options, in rupees

Converted at **₹95.68 / USD (11 September 2026)**. "Landed" is what actually leaves an Indian
card: price **+ ~3.5% cross-currency markup + 18% GST**. Both are covered in §5.1 — they are not
rounding errors.

### CPU-only VPS — one month covers the whole round

| provider | spec | list | **landed ₹/month** |
|---|---|---|---|
| **Contabo Cloud VPS 4** | 4 vCPU / 8 GB / 200 GB | $6.99 | **₹817** |
| Hostinger KVM | from 4 vCPU / 8 GB | $6.49 | ₹758 |
| Hetzner CPX31 | 4 vCPU / 8 GB | $17.99 → $24.99 | ₹2,102 → ₹2,920 |
| DigitalOcean | 8 GB | $48.00 | ₹5,609 |

**The entire four-week round on Contabo is about ₹820.** That is the cheapest honest answer to
the question, and it buys ~10 s replies.

### GPU by the hour — 280 hours (10 h/day × 28 days)

| provider | rate | **landed ₹ for the round** |
|---|---|---|
| **Vast.ai** | from $0.03/hr | **₹982** |
| RunPod | from $0.24/hr | ₹7,853 |
| **E2E Networks** (Indian, billed in ₹) | from ₹49/hr | ₹16,190 inc. GST |

Vast.ai's floor rate is a marketplace price on consumer cards and will not always be available —
budget ₹2,000–4,000 rather than ₹982 if you want a card you can actually get.

**E2E is the expensive row and still worth considering**, for reasons the table cannot show:
Indian data centres (lower round-trip on every turn, on top of the 10 s), billing in rupees with
no forex exposure, and a domestic GST invoice rather than an OIDAR self-assessment.

### Free credits, in rupees

| | credit | worth | window |
|---|---|---|---|
| **Google Cloud** | $300 | **₹28,704** | 90 days |
| AWS | $200 | ₹19,136 | 6 months |
| Azure | $200 | ₹19,136 | **30 days** |

A GPU VM at $0.50/hr for all 280 hours is **$140 ≈ ₹13,395** — comfortably inside the Google
credit, with room for a second round.

---

## 5.1 The two India-specific costs people forget

**18% GST applies either way.** How it lands depends on who is buying:

| | what happens | real cost |
|---|---|---|
| **GST-registered business** buying from a foreign provider | Reverse Charge: you self-deposit 18% IGST and **claim it back as input credit** | **effectively nil** — cashflow and paperwork only |
| **Individual / unregistered** buying from a foreign provider | The provider must charge 18% IGST under OIDAR and you cannot reclaim it | **a real 18%** |
| Either, buying from an **Indian provider** (E2E) | Normal domestic GST invoice, reclaimable if registered | nil if registered |

If ChakraVue AI is GST-registered, the "landed" column above **overstates** the true cost by the
GST portion — the honest figure is the forex-marked-up one. If this is being bought on a personal
card, the landed column is what you pay.

**Forex markup is ~3.5%** on most Indian credit cards, plus GST on that fee. Cards marketed as
zero-forex-markup avoid it. On ₹820 it is small; on a ₹16,000 GPU bill it is not.

## 6. What I would do

1. **Pilot on the Google Cloud credit — ₹28,704 free, 90 days.** It is the only free tier large
   enough to afford a **GPU**, so participants get the 1–2 s replies you have been testing
   against rather than 10 s. Use the **Mumbai region** and start/stop it around study hours.
2. **If that is too much setup, Contabo at ₹817/month.** The whole round for under a thousand
   rupees, at ~10 s a reply. Moving to a GPU later changes one line of `.env`.
3. **If the study wants Indian data residency**, E2E at ~₹16,000 for the round is the clean
   answer — rupee billing, Indian data centres, domestic GST invoice.
4. **Do not** use AWS/GCP burstable free tiers for the model. They look fine for ten minutes.

## What I could not verify

- **None of these prices were confirmed by buying anything**, and Hetzner's own pricing page
  renders its numbers in JavaScript, so I could not read them directly — those figures come from
  third-party trackers dated September 2026.
- **The deployment has not been run end to end yet.** `deploy/gcp/` is written and its
  scripts parse; no VM has been created from them. See `GCP_DEPLOYMENT.md`.
- **The rupee figures are conversions, not quotes.** ₹95.68/USD on 11 September 2026; the rate
  moves, and a provider's India price is sometimes set independently of the dollar one.
- The CPU measurement is **one laptop, one model, one prompt shape**. A cloud vCPU of the same
  nominal count is usually slower than a 5800HS core, so treat 10 s as the optimistic end.

## Sources

- [Hetzner price adjustment, 15 June 2026 — official](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/)
- [Hetzner 2026 price increases — Northflank](https://northflank.com/blog/hetzner-cloud-server-price-increases)
- [Hetzner pricing after April and June 2026 adjustments — AgentDeals](https://agentdeals.dev/hetzner-pricing-2026)
- [Oracle quietly halves Always Free Ampere A1 — InfoQ](https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/)
- [Oracle Always Free resources — official docs](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Google Cloud free trial FAQ — official](https://cloud.google.com/signup-faqs)
- [AWS Free Tier $200 credits — official](https://aws.amazon.com/about-aws/whats-new/2025/07/aws-free-tier-credits-month-free-plan/)
- [Azure free account FAQ — official](https://acom-sandbox.azure.net/en-us/free/free-account-faq/)
- [Contabo VPS pricing 2026](https://bestusavps.com/reviews/contabo/)
- [Vast.ai vs RunPod pricing 2026](https://gpus.io/en/providers/compare/runpod-vs-vast-ai)
- [GPU cloud pricing in India, in INR — getInfra](https://getinfra.cloud/cloud-gpus)
- [E2E Networks GPU cloud pricing](https://computestacker.com/providers/e2e-networks/)
- [USD/INR history, September 2026](https://www.exchangerates.org.uk/USD-INR-spot-exchange-rates-history-2026.html)
- [OIDAR compliance under GST — IndiaFilings](https://www.indiafilings.com/gst/cross-border-digital-services-oidar)
- [Reverse charge on imported cloud services](https://zero8.dev/blog/rcm-on-import-of-services-cloud-saas-india)
