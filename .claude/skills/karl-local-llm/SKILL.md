---
name: karl-local-llm
description: How Karl, KarlCon's WhatsApp booking assistant (server/app/davis/), thinks with a small open LLM running on the Hugging Face Space's own CPU — no API keys. Use whenever changing Karl's understanding, replies, prompts, memory, model choice, speed or evaluation; when a guest conversation went wrong and needs diagnosing; or when someone asks to make Karl "smarter" without a paid model.
---

# Karl on a local model

Karl answers KarlCon guests on WhatsApp. Everything runs inside the Karlcon Hugging Face Space: a
pretrained, 4-bit quantised instruct model under llama.cpp (`app/davis/local_llm.py`). No Anthropic,
OpenAI or other API key is used. This skill is the design: what to keep, why, and how to measure a
change.

## The one rule

**The model reads and writes; code computes and acts.** The model never chooses a price, a house, a
date's calendar value or an invoice. It turns messy WhatsApp text into a structured state and writes
the wording. `policy.py`, `catalogue.py` and the calendar code in `nlu.py` decide and do;
`policy.check_reply` verifies every reply. This is why a small model is enough: it only does the
parts language models are good at.

## The pipeline (one guest turn)

| Step | Where | What | Why |
|---|---|---|---|
| 0. Noise removal | `mind.transcript()` | Drops WhatsApp notices and deleted messages, collapses photo bursts, labels house cards `[house card KCER n: …]`, adds timestamps. | Shannon–Weaver: remove channel noise before decoding. House-card dates were being read as the guest's dates. |
| 1. Rules first | `nlu.extract`, `mind.needs_model` | Regex reading in under 1 ms. Turns the rules fully explain ("ok", "KCER 105", "Harare 21–22 Nov for 4", a name or ID when asked) skip the model. | A cascade (FrugalGPT, Chen et al. 2023): pay for the model only where cheap methods fail. |
| 2. STATE | `mind.understand_messages` + `STATE_SCHEMA` | The model reads the recent conversation and writes JSON: city, `dates_text` plus ISO dates, guests, stay type, chosen KCER, photo requests, name, ID, open questions. | Dialogue state tracking with an LLM (ParsingDST, Wu et al., EMNLP Findings 2023). JSON is constrained by a grammar, so it is always valid (Willard & Louf 2023; llama.cpp GBNF). |
| 3. Check | `mind.to_features`, `mind.merge` | Dates go through `nlu.parse_dates(dates_text)` (calendar maths in code; a day range with no month gets this month or next, and the reply asks the guest to confirm), must be in the future and check-out must follow check-in. KCER refs must be positive ints, names letters only, IDs need digits. | Small models find *which* words are the dates well, and do date arithmetic badly. Never trust model output unchecked. |
| 4. Plan | `policy.plan` | Actions (houses with photos, live-price request, Terms PDF, invoice) and the points the reply must carry. Long-term rental, photo requests, "too big for one home". | Deterministic, testable, auditable. |
| 5. REPLY | `mind.reply_messages` + `REPLY_SCHEMA` | Same system prompt and same opening as STATE, then the TASK block: facts, actions, must-say points, and **last** the guest's newest messages and open questions. | Prefix sharing (step 6). The guest's newest messages and questions go at the end because models use the start and end of the input best (Liu et al., "Lost in the Middle", TACL 2024). |
| 6. Verify | `policy.check_reply`, `engine._repeats` | Rejects invented amounts, houses, numbers and links, dropped must-say points, and repeats of an earlier line. One rewrite with the reason, then the plan's own wording. | Generate, then verify, then refine (Self-Refine, Madaan et al. 2023). |
| 7. Send | `engine.execute` | Outbox, then the PC bridge sends it on WhatsApp. | — |

## Speed on a free Space (2 vCPU, 16 GB)

Measured here at 2 threads:
- Qwen2.5-1.5B Q4_K_M reads a prompt at about 41 tokens/s and writes about 10 tokens/s.
- Qwen2.5-3B Q4_K_M reads about 21 tokens/s and writes about 6 tokens/s.

**Reading the prompt is the bottleneck**: a 1,500-token prompt read from scratch would take over a
minute on a 3B model. So the design never re-reads what it already read:

- **Append-only prompt.** System prompt first (`mind.SYSTEM`, never changes), then the conversation
  (it only grows), then what changes every turn (date, known state, houses sent), then the
  `=== TASK ===` block. The transcript window starts on a multiple of 8 messages
  (`mind.window_start`), so consecutive turns share the same opening. llama.cpp's `LlamaRAMCache`
  (`DAVIS_LOCAL_CACHE_MB`, default 1024) finds the longest cached prefix, so a new turn reads only
  the new messages plus the small changing part.
- **STATE and REPLY share everything before the TASK block**, so REPLY reads only its task block.
  This is the same idea as SGLang's RadixAttention prefix sharing (Zheng et al. 2023).
  **Never put anything that changes between turns, or between STATE and REPLY, before the
  conversation or inside it.**
- **A lean STATE schema** (11 fields): only what the rules can't read. Bedrooms, budget and yes/no
  intents stay with `nlu.py`. Every generated token costs about 0.1–0.2 s.
- **Grammar-constrained JSON**: no rambling, never invalid. Output is capped by
  `KARL_STATE_MAX_TOKENS` / `KARL_REPLY_MAX_TOKENS`.
- **The cascade** skips the model on simple turns (dashboard counter `understood_by_rules`).
- **Threads = real container cores** (`local_llm.container_cpus`).
- **Warm-up at start** evaluates the system prompt once.

## Choosing the model

Measured with `tests/eval_state.py` on the October 2026 chats (7 conversations, 31 scored fields),
2 threads, Q4_K_M, the STATE step plus the code checks, exactly as Karl uses it:

| Model | Accuracy | STATE time per turn | Licence |
|---|---|---|---|
| **Qwen3-4B-Instruct-2507** (default, `unsloth/Qwen3-4B-Instruct-2507-GGUF`) | **94%** (29/31) | ~43 s | Apache-2.0 |
| Phi-4-mini-instruct (`unsloth/Phi-4-mini-instruct-GGUF`) | 90% (28/31) | ~43 s | MIT |
| Qwen2.5-3B-Instruct (`Qwen/Qwen2.5-3B-Instruct-GGUF`) | 77% (24/31) | ~25 s | Qwen research licence |
| Qwen2.5-1.5B-Instruct (`Qwen/Qwen2.5-1.5B-Instruct-GGUF`, the old default) | 68% (21/31) | ~20 s | Apache-2.0 |

The time is dominated by *writing* the JSON (about 100 tokens). On a free 2-vCPU Space, a turn that
needs the model takes about a minute in total (STATE plus REPLY). Turns the rules explain skip the
model and answer in seconds.
- **To go faster** without losing accuracy, use a paid CPU upgrade for the Space; no code change
  needed.
- **To go faster** on free hardware, switch to Qwen2.5-3B (about half the time, less accurate).

What the code checks fixed on every model:
- invented dates (each day number must be in the guest's words);
- invented towns (the town must be one the guest wrote; misspelt suburbs map to their city);
- wrong party sizes (an explicit "15guests" wins);
- missed monthly rentals;
- copied example text (keep examples out of the prompt);
- dropped questions (requests in the newest messages are always passed on).

Change it with the Space variables `DAVIS_LOCAL_MODEL_REPO` and `DAVIS_LOCAL_MODEL_FILE`, and the
same values in the `Dockerfile` ENV, which downloads the model at build time. Prefer Q4_K_M
quantisation. Re-run the evaluation below before switching.

## Memory (the conversation cache)

1. **Working memory:** the real last `KARL_TRANSCRIPT` messages (default 24), each line up to
   `KARL_LINE_CHARS`. Not summarised, so the newest question is answered in context.
2. **Booking state:** `davis_sessions.slots`, the stage and the chosen house, kept between turns and
   shown to the model as WHAT KARL ALREADY KNOWS.
3. **Offers:** `house_sends`, the houses this guest already received. "Option 2", "KCER 105" and
   "this one" resolve against it.
4. **Knowledge:** `persona.py` (identity, rules, FAQs), team notes per house, and skills learned from
   team replies.
5. **Missed messages:** guest lines that arrived before a staff reply, so no pass handled them, are
   still read (`engine._missed_guest_texts`).

## Communication model (Shannon & Weaver 1949, plus Schramm's feedback)

| Element | In Karl |
|---|---|
| Source and message | The guest's lines, read as one turn. |
| Noise | Removed by `transcript()`. |
| Decoder | STATE, then the checks. |
| Destination | `policy.plan`. |
| Feedback | Karl restates what it understood (town, dates, party, house) and asks one clear question when something is ambiguous, e.g. a month that had to be assumed. This is grounding (Clark & Brennan 1991). |
| Channel | A phone: at most `KARL_MAX_WORDS` words, one emoji, at most 2 replies per turn, never the same line twice (`engine._dedupe`). |

## What we take from frontier assistants (and what we can't)

A frontier model's weights and training are not public, and nothing here copies or extracts them.
What can be reproduced is **behaviour**, by giving a small model the scaffolding a large one does
internally:

| Behaviour of a strong assistant | How Karl gets it on a 3–4B model |
|---|---|
| Reads the whole conversation before answering | STATE over the transcript, not only the last line. |
| Keeps an explicit picture of the task | The JSON state is the scratchpad. Structured fields replace free-form reasoning, which is too slow on a CPU. |
| Answers the actual latest question | `open_questions` + "ANSWER FIRST", placed last in the prompt. |
| Doesn't invent facts | FACTS block only, plus `check_reply` guards; "not in our listing" when unknown. |
| Checks its own work | Verifier, then one revision with the reason. |
| Asks when unsure | Assumed dates are flagged and confirmed with the guest. |
| Doesn't repeat itself | Last messages shown, repeats rejected or rephrased. |

### Getting smarter later: distillation

The honest path to a smarter small model is fine-tuning on KarlCon's own data:

1. Export real chats (the WhatsApp exports and the `davis_events` "understood:" lines).
2. Label the correct state and the ideal reply, starting from the team's own answers.
3. LoRA-fine-tune the chosen base model (Hu et al. 2021) on a GPU machine or Colab, not on the Space.
4. Convert to GGUF Q4_K_M and point `DAVIS_LOCAL_MODEL_*` at it.

Distillation trains a student on a teacher's outputs (Hinton et al. 2015). Use only teachers whose
licence allows it (open-weight models, or your own team's labels). Some commercial model terms forbid
training competing models on their outputs.

## Evaluating a change

The October chats are the regression set. Run:

```bash
cd server && python tests/eval_state.py . /path/to/model.gguf 2
```

It runs STATE on 7 real conversations at 2
threads and scores city, dates, guests, bedrooms, name, chosen house, stay type, photo requests and
open questions.
- Report accuracy **and** seconds per case.
- Unit and flow tests: `cd server && python -m pytest -q tests`. `tests/test_karl.py` encodes each
  October failure.
- Never ship a prompt change that drops accuracy on the regression set, or that puts changing content
  before the TASK block (it breaks prefix reuse and doubles latency).

## Settings (Space variables)

| Variable | Default | Effect |
|---|---|---|
| `DAVIS_PROVIDER` / `KARL_PROVIDER` | `local` | `local` (Karl's mind), `ollama`, `worker`, `rules`. |
| `KARL_UNDERSTAND` | `auto` | `auto` = cascade, `always` = STATE every turn, `off` = rules only. |
| `KARL_TRANSCRIPT` | 24 | Messages read per turn. |
| `KARL_MAX_WORDS` | 50 | Longest model reply. |
| `KARL_STATE_MAX_TOKENS` / `KARL_REPLY_MAX_TOKENS` | 420 / 200 | Output caps. |
| `DAVIS_LOCAL_MODEL_REPO` / `_FILE` | see above | The GGUF model. |
| `DAVIS_LOCAL_CACHE_MB` | 1024 | Prefix cache. |
| `DAVIS_LOCAL_THREADS` | 0 = auto | Threads. |

## Sources

- Shannon, C. E. (1948), A Mathematical Theory of Communication; Weaver, W. (1949); Schramm, W. (1954).
- Clark, H. H. & Brennan, S. E. (1991), Grounding in communication.
- Chen, Zaharia & Zou (2023), FrugalGPT. https://arxiv.org/abs/2305.05176
- Willard & Louf (2023), Efficient Guided Generation for LLMs (Outlines). https://arxiv.org/abs/2307.09702
- Wu, Dong & Xu (2023), Semantic Parsing by LLMs for Zero-Shot DST (ParsingDST), Findings of EMNLP.
  https://arxiv.org/abs/2310.10520
- Liu et al. (2024), Lost in the Middle, TACL. https://arxiv.org/abs/2307.03172
- Madaan et al. (2023), Self-Refine. https://arxiv.org/abs/2303.17651
- Zheng et al. (2023), SGLang / RadixAttention. https://arxiv.org/abs/2312.07104
- Hu et al. (2021), LoRA. https://arxiv.org/abs/2106.09685
- Hinton, Vinyals & Dean (2015), Distilling the Knowledge in a Neural Network. https://arxiv.org/abs/1503.02531
