# Davis: KarlCon Elite Retreats' WhatsApp reception and booking assistant

Davis reads every guest message that reaches Karlon from WhatsApp. It decides whether to answer, and then moves
the guest from "hello" to a paid booking:

```
greeting → city + dates + guests → 3 houses with photos (live catalogue)
        → guest picks a KCER → full name + ID → invoice PDF + Terms & Conditions PDF
        → "I have paid" / proof photo → handed to staff to confirm
```

Davis runs inside the Karlon server on the Karlon Space, next to the outbox, the house catalogue and the invoice
queue. What it sends goes through the same outbox to `wa_bridge.py` as anything sent from the app.

**There is no external AI service.** Davis's language model is a small open model (Qwen2.5 1.5B Instruct,
4-bit GGUF, about 1.1 GB). It runs on the Space's own CPU with llama.cpp. There is no API key, and no guest
message leaves the Space.

## Who Davis is: `app/davis/persona.py`

One file holds Davis's identity, voice, selling style and limits, plus **every line Davis says**. Edit it to
change how Davis sounds:

- `PERSONA` sets the character: warm, quick and confident, like a five-star front desk. Davis greets back in
  Shona or Ndebele, uses one emoji at most, asks one question per message, and always gives the next step.
  It also lists the hard "never" rules: no invented prices or amenities, no links, no Airbnb, never pretends
  to be human.
- `LINES` holds the exact sentences for each situation: greeting, asking dates, presenting houses, confirming
  the choice, asking for name and ID, invoice sent, payment, handoff, and so on. Each one is under 40 words
  (a test checks this).
- `EXAMPLES` are worked examples shown to the model.

## Where the model is used, and why only there

The booking itself is code: understanding the message, deciding the step, choosing houses and the exact
reply. The persona's lines go out **instantly** and are always correct.

The local model answers **open questions that a team note can answer** ("does KCER 102 have wifi?"). Write
notes per house on the dashboard (Catalogue → team note: "wifi, pool, borehole, solar backup").

1. If the question shares a word with a house's note, Davis replies with the note ("KCER 102 has wifi, pool,
   solar backup ✨ …").
2. The model may reword that reply. Its answer must still pass the guard and must use the note's word.
3. If no note covers the question ("can we bring our dog?", "is it near the airport?"), Davis says the team
   will confirm and hands the chat to staff. The model isn't asked, so there's no wait.

This was decided from real tests of the 1.5B model on 2 CPU threads:

- It sometimes wrote as the guest ("Hello Davis! I'd like to stay…").
- It once invented a fact ("near the airport") that no note contained.
- Each reply took 5–20 s.

The guard now rejects all of these. With the model on booking steps the bookings were slower and no better.
You can still switch the dashboard's Settings → "Model writes" to **every reply**. Each rewrite must keep the
draft's KCERs, amounts and closing question, or the persona line goes out. A bigger model on better hardware
(Ollama on a GPU PC, `DAVIS_PROVIDER=ollama`) makes that mode worthwhile.

**The guard** (`policy.check_reply`) rejects a model reply that:

- has more than 40 words;
- contains any number not in the draft or the facts (no invented prices, dates or counts);
- names a KCER not in play;
- has a link or mentions Airbnb;
- is written in the guest's voice;
- drops the draft's KCER, amount or closing question (in "every reply" mode).

## The rules you asked for, and where they are enforced

| Rule | Where |
|---|---|
| A message never exceeds 40 words | persona lines (tested), `nlu.clip_words` on every reply, the guard, `catalogue.compact_card` |
| One reply, at most 2 per guest turn, then wait for the guest | `gate.davis_replies_this_turn` + `Engine._cap`; Davis speaks only when the guest spoke last (the second reply is only "here are the houses" after "checking live prices…") |
| Decide whether to answer at all | `gate.decide`: off/paused, allow/ignore lists, group chats, messages older than 15 min, already answered, opted out, with staff, a staff reply in the last 30 min, a plain "ok/thanks" |
| Send T&Cs and the invoice | `send_terms` (`messages.queue_terms`), `create_invoice` (`invoices.create_invoice`, `created_by="Davis"`) |
| Houses with images per city from the existing endpoints | `catalogue.find_options` → `houses.available_listings`; `houses.queue_listings` (shared with `POST /api/houses/send`) |
| Session memory | `davis_sessions`, the booking frame, rebuilt from chat history after a restart |
| Caching, pooling, process management | see "Speed" |

## Modes (dashboard: `/davis`)

- **OFF**: Davis does nothing.
- **SHADOW** (the default): Davis drafts. Staff approve (and can edit) or discard each draft on the dashboard.
- **LIVE**: Davis replies by itself.

Per chat you can pause Davis, take the chat over, give it back, reset its memory, or run it now. **Only these
chats** in Settings is the safe way to go live with one or two test chats first.

## How a message is handled

1. **Hook.** The bridge sync, the legacy import and inbound photos call `davis.notify_inbound`. A sweeper
   every 30 s catches anything a hook missed.
2. **Debounce, 8 s.** People type in bursts, so a burst is read as one turn (Sacks, Schegloff & Jefferson
   1974).
3. **Admission.** A global semaphore and one lock per chat (SEDA: Welsh, Culler & Brewer 2001).
4. **Preprocess** (`nlu.py`, no model, under 1 ms): intents and slots (city or suburb, dates in every format
   guests use, guests, budget, KCER / "option 2", full name, Zimbabwe ID or passport).
5. **Gate** (`gate.py`): answer or not, with a reason on the dashboard. Turn order uses arrival order,
   because WhatsApp stamps guest messages to the minute.
6. **Claim.** An atomic update, so a message is never answered twice.
7. **Policy** (`policy.py`): a frame-based booking dialogue (GUS: Bobrow et al. 1977; dialog state tracking:
   Williams, Raux & Henderson 2016). Code decides the actions and picks the persona line.
8. **Model** (`local_llm.py`): only where described above, always behind the guard.
9. **Execute or draft.** The reply first, then house cards and photos, the invoice, the Terms PDF.
10. **Events and metrics** go to the dashboard over the WebSocket `/api/davis/ws`.

If there are no prices yet for the guest's dates, Davis queues a scraper refresh job and says it's checking.
When the scraper finishes, Davis sends the houses by itself. If no prices arrive within 20 minutes, it hands
the chat to staff.

## Speed

- **Booking steps:** the persona lines take milliseconds; only the 8 s debounce waits.
- **Local model (open questions):**
  - The model loads and computes the persona prefix at boot, so the first guest doesn't wait for it.
  - The persona and examples never change, so llama.cpp reuses that computed prefix and only reads each
    turn's new text.
  - The answer is constrained to `{"reply", "handoff"}` JSON by a grammar, so the output is short.
  - Threads are set to the container's real CPU quota. Oversubscribing threads is what collapsed the
    Conscious Worker to under 1 token/s.
  - Measured: about 5–8 s per answer on 2 threads.
- **Timeout and breaker:** after 45 s, or after 3 failures in a row, the persona line goes out instead (Dean &
  Barroso, "The Tail at Scale", 2013).
- **Caches:** catalogue queries per (city, dates) are cached for 60 s; model answers are cached per prompt.
- **Why it matters:** in an audit of 2,241 firms only 37% answered a web lead within an hour (Oldroyd,
  McElheran & Elkington, HBR 2011). Speed to lead is on the dashboard.

## Setup on the Karlon Space

The Dockerfile installs llama.cpp as a prebuilt CPU wheel (no compiling) and downloads the model into the
image at build time. The first build takes a few minutes longer; later builds reuse the cached layer.

Space variables and secrets (Settings → Variables and secrets):

| Name | Value |
|---|---|
| `DAVIS_MODE` | `shadow` to start; switch to live on the dashboard |
| `DAVIS_ADMIN_TOKEN` | any long random string; the dashboard asks for it (🔑) before changes |

Optional:

| Name | Default | Notes |
|---|---|---|
| `DAVIS_PROVIDER` | `local` | `ollama`, `worker` or `rules` |
| `DAVIS_LLM_FOR` | `open` | `all` = the model rewrites every reply |
| `DAVIS_LOCAL_MODEL_REPO`, `DAVIS_LOCAL_MODEL_FILE` | | change them together with the Dockerfile's `ENV` lines |
| `DAVIS_LOCAL_THREADS` | auto | |
| `DAVIS_MAX_WORDS` | 40 | |
| `DAVIS_MAX_REPLIES_PER_TURN` | 2 | |
| `DAVIS_DEBOUNCE_SEC` | 8 | |
| `DAVIS_FRESH_MINUTES` | 15 | |
| `DAVIS_HUMAN_QUIET_MINUTES` | 30 | |

For Ollama elsewhere: `DAVIS_PROVIDER=ollama`, `DAVIS_OLLAMA_URL`, `DAVIS_OLLAMA_MODEL`.

The model needs about 2 GB of RAM; the free CPU Space has 16 GB.

Open `https://<space>/davis` (same password as the web dashboard). Settings shows the local model's state
(loading / ready / error), its load time and the last reply's speed.

**Rollout:**
1. Run in shadow mode for a day.
2. Add team notes for your houses.
3. Put one test chat in "Only these chats" and switch to LIVE.
4. Empty the list when happy.

## Things to know

- **WhatsApp's terms.** Automating WhatsApp Web (wa_bridge) is against WhatsApp's terms, and a number can be
  banned, more likely with fast automated replies. The WhatsApp Business Platform (Cloud API) is the durable
  route; only the delivery step would change.
- **Restarts.** The Space has no persistent disk, so a restart wipes Davis's settings and memory along with
  the rest of the database. Davis rebuilds each frame from the re-synced chats and never answers messages
  older than 15 minutes.
- **Prices** come only from the dated offers the scraper stored. Payment is never confirmed by Davis.
  Group chats are never answered.

## Code map

```
app/davis/
  persona.py    who Davis is: character, voice, selling style, limits, every line, model examples
  config.py     environment defaults (modes, limits, provider, local model)
  nlu.py        preprocessing: intents and slots from text
  gate.py       answer or not (turn-taking, staff, freshness, groups, caps)
  policy.py     the booking dialogue: actions + persona lines; note grounding; reply guard
  catalogue.py  live catalogue: options per city/dates, house cards, which option the guest means
  prompts.py    model messages: persona + examples (fixed prefix) + this turn
  local_llm.py  the local model: llama.cpp, warm-up, CPU threads, JSON grammar
  llm.py        providers (local, Ollama, Conscious Worker), breaker, cache
  engine.py     debounce, admission, pipeline, outbox execution, metrics, live feed
  store.py      tables: davis_settings, davis_sessions, davis_events, davis_drafts, davis_catalogue
  router.py     /davis, /api/davis/ws, /api/davis/*
static/davis.html  the dashboard
tests/test_davis.py
```

## References

- Bobrow, D. G., Kaplan, R. M., Kay, M., Norman, D. A., Thompson, H., & Winograd, T. (1977). GUS, a
  frame-driven dialog system. *Artificial Intelligence*, 8(2), 155–173. doi:10.1016/0004-3702(77)90018-2
- Williams, J. D., Raux, A., & Henderson, M. (2016). The Dialog State Tracking Challenge Series: A Review.
  *Dialogue & Discourse*, 7(3), 4–33. doi:10.5087/dad.2016.301
- Sacks, H., Schegloff, E. A., & Jefferson, G. (1974). A simplest systematics for the organization of
  turn-taking for conversation. *Language*, 50(4), 696–735. doi:10.1353/lan.1974.0010
- Grice, H. P. (1975). Logic and conversation. In *Syntax and Semantics 3: Speech Acts*, 41–58.
- Welsh, M., Culler, D., & Brewer, E. (2001). SEDA: An Architecture for Well-Conditioned, Scalable Internet
  Services. *SOSP '01*.
- Dean, J., & Barroso, L. A. (2013). The Tail at Scale. *Communications of the ACM*, 56(2), 74–80.
- Oldroyd, J. B., McElheran, K., & Elkington, D. (2011). The Short Life of Online Sales Leads. *Harvard
  Business Review*, 89(3), 28.
