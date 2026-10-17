# Davis: KarlCon Elite Retreats' WhatsApp reception and booking assistant

Davis reads every guest message that reaches Karlon from WhatsApp. It decides whether to answer, and then moves
the guest from "hello" to a paid booking:

```
greeting → city + dates + guests → 3 houses with photos (live catalogue)
        → guest picks a KCER → full name + ID → invoice PDF + Terms & Conditions PDF
        → "I have paid" / proof photo → handed to staff to confirm
```

It runs inside the Karlon server on the Karlon Space, next to the outbox, the house catalogue and the invoice
queue. It has no separate service to deploy or keep in sync. What it sends goes through the same outbox to
`wa_bridge.py` as anything sent from the app.

The Conscious Worker's useful parts were brought into Davis: SEDA admission control, the live trace dashboard
over a WebSocket, metrics and adaptive timeouts. The worker Space itself can still be used as a model provider
(`DAVIS_PROVIDER=worker`), but phi4-mini on CPU decodes at about 1 token/s, which is too slow for live chat.
Use it only for shadow drafts.

## The rules you asked for, and where they are enforced

| Rule | Where |
|---|---|
| A message never exceeds 40 words | `nlu.clip_words` on every reply; `policy.check_reply` rejects longer model text; house cards trim themselves (`catalogue.compact_card`) |
| One reply, at most 2 per guest turn, then wait for the guest | `gate.davis_replies_this_turn` + `Engine._cap`; Davis never writes twice in a row unless it promised live prices ("checking…" then "here are…") |
| Decide whether to answer at all | `gate.decide`: off/paused, allow-list/ignore-list, group chats, message older than 15 min, already answered, guest opted out, chat with staff, a staff member replied in the last 30 min, a plain "ok/thanks" that needs no answer |
| Send T&Cs and the invoice | actions `send_terms` (`messages.queue_terms`) and `create_invoice` (`invoices.create_invoice`, `created_by="Davis"`); the PDF reaches WhatsApp through invoice_worker as usual |
| Houses with images for the city, using the existing endpoints | `catalogue.find_options` reads `houses.available_listings` (the dated offers the app searches); `houses.queue_listings` (now shared with `POST /api/houses/send`) queues each card and its photos and records `house_sends`, so the invoice form and "option 2" both work |
| Session memory | `davis_sessions`: the booking frame (city, dates, guests, budget, chosen KCER, name, ID), rebuilt from chat history if the Space restarted |
| Caching, pooling, process management | see "Speed" below |

## Modes and switches (dashboard: `/davis`)

- **OFF**: Davis does nothing.
- **SHADOW** (the default): Davis drafts what it would send. Staff approve it (and can edit it), or discard
  it, on the dashboard. Nothing reaches WhatsApp until approved.
- **LIVE**: Davis replies by itself.

Per chat you can pause Davis, take the chat over (handoff), give it back to Davis, reset its memory, or run it
now. **Only these chats** in Settings is the safe way to go live: list one or two test chats first.

## How a message is handled

1. **Hook.** The bridge sync (`POST /api/chats/{id}/sync`), the legacy import and inbound photos call
   `davis.notify_inbound`. A sweeper every 30 s catches anything a hook missed.
2. **Debounce, 8 s.** People type in bursts, so Davis waits until the guest stops (turn-taking: Sacks,
   Schegloff & Jefferson 1974). Several quick messages are read as one turn.
3. **Admission.** A global semaphore (`DAVIS_CONCURRENCY`) and one lock per chat (SEDA: Welsh, Culler &
   Brewer 2001).
4. **Preprocess** (`nlu.py`, no model, under 1 ms). Intents (greeting, booking, price, terms, invoice,
   payment, complaint, wants a person, opt-out, bot question) and slots (city or suburb → city, dates in every
   format guests use, guests, bedrooms, budget, KCER / "option 2" / "the second one", full name, Zimbabwe ID
   or passport).
5. **Gate** (`gate.py`). Answer or not, with a reason that shows on the dashboard.
6. **Claim.** An atomic update, so a message is never answered twice, even with two server processes.
7. **Policy** (`policy.py`). A frame-based booking dialogue (GUS: Bobrow et al. 1977; dialog state
   tracking: Williams, Raux & Henderson 2016). Code decides the actions: the "hands". These are reply,
   send houses, send T&Cs, create invoice, ask the scraper for live prices, and hand over to staff.
8. **Model** (optional). It only rewrites the reply text, to be warmer and to close the deal. Its text is
   used only if it passes `policy.check_reply`: at most 40 words, no prices that aren't in the facts, no
   unknown KCER numbers, no links, no "Airbnb". Otherwise the template is sent. It may also fill slots the
   rules missed (validated), or ask for a person.
9. **Execute or draft.** The reply first, then the house cards and photos, the invoice, the T&Cs PDF.
10. **Events and metrics** go to the dashboard over the WebSocket `/api/davis/ws`.

If the guest's dates have no prices yet, Davis queues a scraper refresh job (`/api/houses/refresh`) and tells
the guest it is checking. When the scraper completes the job, Davis sends the houses by itself. If no prices
arrive within 20 minutes, it hands the chat to staff.

## Speed

- **The decision is code.** The gate and policy run in milliseconds; only the wording waits on a model.
- **Model.** The default is the Claude API with `claude-opus-5-5` at `effort: low`, through one long-lived
  `AsyncAnthropic` client, so connections are reused. The system prompt is marked cacheable, so later
  calls read it from the prompt cache (shown as `cache_read_tokens` in the feed). For the lowest latency,
  set the model to `claude-haiku-5-5` on the dashboard.
- **Timeouts and breaker.** A hard timeout applies (`DAVIS_LLM_TIMEOUT_SEC`, 25 s). After 3 failures in a
  row the provider is skipped for 60 s and the rules templates answer. A late answer is worth less than a
  plain one (Dean & Barroso, "The Tail at Scale", CACM 2013).
- **Caches.** Catalogue queries per (city, dates) are cached for 60 s. Model responses are cached by exact
  prompt.
- **Why fast matters.** In an audit of 2,241 US firms, only 37% answered a web lead within an hour, and the
  average was 42 hours (Oldroyd, McElheran & Elkington, "The Short Life of Online Sales Leads", HBR 2011).
  Davis's speed to lead is on the dashboard.

**Fallbacks are on.** Claude requests opt into server-side fallback (`fallbacks: "default"`, beta
`server-side-fallback-2026-07-01`). If the main model declines a request, the API retries it on its
recommended fallback model within the same call. If the whole chain declines, Davis sends its template.
Haiku models don't support this, so it is left off for them.

## Setup on the Karlon Space

Add these Space secrets (Settings → Variables and secrets), then restart:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your Claude API key (without it Davis uses the rules templates only) |
| `DAVIS_MODE` | `shadow` to start; switch to live on the dashboard when happy |
| `DAVIS_ADMIN_TOKEN` | any long random string; the dashboard asks for it (🔑) before it changes anything |

Optional: `DAVIS_MODEL`, `DAVIS_EFFORT`, `DAVIS_MAX_WORDS` (40), `DAVIS_MAX_REPLIES_PER_TURN` (2),
`DAVIS_DEBOUNCE_SEC` (8), `DAVIS_FRESH_MINUTES` (15), `DAVIS_HUMAN_QUIET_MINUTES` (30),
`DAVIS_OPTIONS_PER_SEND` (3), `DAVIS_IMAGES_PER_OPTION` (3), `DAVIS_CONCURRENCY` (4).
For the Conscious Worker as a provider: `DAVIS_PROVIDER=worker`, `DAVIS_WORKER_URL`, `DAVIS_WORKER_KEY`
(the worker's `INTERNAL_API_KEY`).

Open `https://<space>/davis` (same password as the web dashboard).

**Rollout:**
1. Keep shadow mode for a day and approve or edit drafts.
2. In Settings, put one test chat in **Only these chats** and switch to LIVE.
3. Empty the list once the replies look right.

In **Catalogue**, mark houses **featured** (offered first) or **hidden** (never offered). Write a **team note**
per house (wifi, pool, borehole, solar…): Davis may quote it when guests ask.

## Things to know

- **WhatsApp's terms.** Driving WhatsApp Web with automation (wa_bridge) is against WhatsApp's terms, and a
  number can be banned for it, more likely with fast automated replies. The durable route for an automated
  assistant is the WhatsApp Business Platform (Cloud API). Davis's outbox design would carry over: only the
  delivery step changes.
- **Server restarts wipe the database** (the Space has no persistent disk). Davis's settings and memory go
  too. It rebuilds each frame from the chat history the bridge re-syncs, and never answers messages older
  than 15 minutes, so a restart doesn't make it message old chats.
- **Prices** come only from the dated offers the scraper stored. Davis never quotes the last-seen price of a
  house for other dates; it asks for live prices instead.
- **Payment** is never confirmed by Davis. A payment claim or a photo after the invoice goes to staff.
- **Group chats** are never answered.

## Code map

```
app/davis/
  config.py     environment defaults (modes, limits, provider)
  nlu.py        preprocessing: intents and slots from text
  gate.py       answer or not (turn-taking, staff, freshness, groups, caps)
  policy.py     the booking dialogue: actions + template replies; reply guard
  catalogue.py  live catalogue: options per city/dates, house cards, which option the guest means
  prompts.py    system prompt (cached) and the per-turn user message
  llm.py        providers (Claude API, Conscious Worker, Ollama), breaker, cache
  engine.py     debounce, admission, pipeline, execution on the outbox, metrics, live feed
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
- Grice, H. P. (1975). Logic and conversation. In *Syntax and Semantics 3: Speech Acts*, 41–58 (the maxim of
  quantity: say as much as needed, no more).
- Welsh, M., Culler, D., & Brewer, E. (2001). SEDA: An Architecture for Well-Conditioned, Scalable Internet
  Services. *SOSP '01*.
- Dean, J., & Barroso, L. A. (2013). The Tail at Scale. *Communications of the ACM*, 56(2), 74–80.
- Oldroyd, J. B., McElheran, K., & Elkington, D. (2011). The Short Life of Online Sales Leads. *Harvard
  Business Review*, 89(3), 28.
