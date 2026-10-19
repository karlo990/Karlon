# Karl: KarlCon's WhatsApp booking assistant

Karl is the new name for Davis. The code still lives in `app/davis/` and the dashboard keeps the
`/api/davis/*` URLs, so the app and the bridge don't need any change. Messages Karl sends carry the
sender names `Karl`, `Karl (catalogue)` and `Karl (documents)`. Older messages signed `Davis` still
count as Karl's own.

## 1. What the October chats showed

These come from four exported chats (+263 71 079 4501, 77 280 4484, 77 226 2810, 78 594 1273) and
five screenshots.

| What happened | Why | Fixed by |
|---|---|---|
| "From 23 to 25 for two guests" was answered with "Which dates are you looking at?" | A day range with no month was left unread. | The model reads the whole chat and resolves dates; an assumed month is confirmed with the guest. |
| "4november" and "One" were followed by "How many nights?" four times | The message arrived just before a staff reply, so no pass ever read it. | Guest messages that no pass handled are now read too. A repeated ask becomes "Sorry, I didn't catch that…". |
| "Lovely, Bulawayo!" and the greeting went out twice in a minute | Nothing stopped an identical line. | Karl never sends one of its recent lines word for word. |
| Marondera: "Checking … 19–21 Oct", dates the guest never gave | The dates printed on the team's house cards were taken as the guest's dates. | House cards are labelled as ours, and their dates are ignored. |
| "Room to rent from 80 to 90 per month" was sent holiday homes and then "nothing free" | Nothing recognised a long-term rental. | Karl explains kindly that we only do short stays, and doesn't search. |
| 15 guests were sent homes for 6 to 8 people as if they fit | The capacity filter fell back to "anything" when nothing fit. | Karl says honestly that no single home fits, sends the biggest, and offers two homes. |
| "💵 Pay $0 today" was shown as a price | An Airbnb payment-plan label was treated as a price. | That label doesn't count as a price; Karl requests the live price instead. |
| "Blessing Nyasha Mpofu" was answered with "Which city…?" | The name was only accepted at one exact stage. | The model takes the name from the conversation; code checks it. |
| "Pics / amenities / where is it / pool?" got a generic line | Questions weren't tracked. | Every unanswered question is listed for the reply, photos are sent, and answers come from the facts. |
| "Hello! I'm Davis… How are you, are you in SunwayCity?" | The small model echoed the guest's words. | Karl never copies the guest's sentence; the reply check rejects guest-voice text. |

The common cause: the old pipeline read only the newest message, with regular expressions, and gave
the wording to a 1.5B-parameter model on the Space's CPU. People write dates, names and requests in
far more ways than rules can list.

## 2. How Karl works now

Each guest turn goes through these stages, in order:

1. **Debounce.** Wait 5 s so a guest who is still typing can finish (`DAVIS_DEBOUNCE_SEC`).
2. **Gate.** Decide whether Karl should answer at all.
3. **Understand** (Claude, JSON schema). The model reads the last 40 messages and writes the booking
   state: city, dates, party size, budget, stay type, chosen house, photo requests, name, ID, and the
   guest's unanswered questions. Code then checks every value:
   - dates must be real and in the future;
   - the house must exist;
   - a name has only letters, and an ID has digits.
4. **Plan** (`policy.py`, code). Decides the actions: send houses with photos, request live prices,
   send the Terms PDF, create the invoice. Prices come only from the catalogue.
5. **Reply** (Claude, JSON schema). The model writes Karl's message from:
   - the full conversation;
   - the checked facts;
   - what the system is about to do;
   - the points the booking step must carry.
   The guest's newest messages and open questions go last in the prompt.
6. **Check** (`check_reply`). Blocks any amount, house, number or link that isn't in the facts, and
   any reply that repeats an earlier line. A rejected reply is rewritten once; if it fails again,
   Karl retries, and if that still fails, the plan's own wording goes out.
7. **Send.** Messages go to the outbox and the PC bridge delivers them to WhatsApp.

**The model understands and speaks; code acts.** A wrong model reply can never book wrong dates,
quote an invented price or invoice a house that wasn't offered.

### The communication model (Shannon and Weaver 1949, with Schramm's feedback loop)

| Element | In Karl |
|---|---|
| Source and message | The guest. One thought is often spread over several WhatsApp lines, so Karl reads the whole turn and the whole chat. |
| Noise | Typos, split messages, deleted messages, "your version of WhatsApp Web doesn't support it", photo bursts, our own house cards. `mind.transcript()` removes these or labels them instead of guessing. |
| Receiver / decoder | `understand`: decodes the conversation into a checked state. |
| Destination | `policy.plan`: what the booking needs next. |
| Feedback | Karl restates what it understood ("Rusape, 23–25 Oct, 2 guests") so the guest can correct it, and asks one clear question when something is ambiguous. This is conversational grounding (Clark & Brennan 1991). |
| Channel capacity | A phone screen: at most 60 words per reply when Claude writes it, one emoji, no repeated lines, at most 2 replies per guest turn. |

### The conversation cache (memory)

- **Working memory.** The real last 40 messages, read every turn. Nothing is summarised away, so
  the newest question is always answered in its context.
- **Booking state.** Slots, the stage and the chosen house, stored in `davis_sessions` between
  turns. The model receives the stored state and keeps or corrects it.
- **Offers.** The houses already sent to this guest, from `house_sends`, so "option 2", "KCER 105"
  and "this one" resolve to a real house.
- **Knowledge.** The persona, house rules and FAQs (`persona.py`), team notes per house, and skills
  learned from the team's past replies.

### About the "neural network architecture"

We don't train or change the network inside a language model; that's done by the model's maker.
What we control is:

- **Which model.** Moving from a 1.5B model on a 2-core CPU to Claude is the biggest single gain in
  reasoning.
- **The system around it.** Perception (understand) → memory → planning in code → language (reply)
  → verification (check) → feedback. That loop is what makes it reliable rather than just fluent.

## 3. Turning it on

On the Karlcon Space (Settings → Variables and secrets):

| Name | Kind | Value |
|---|---|---|
| `ANTHROPIC_API_KEY` | secret | Your Anthropic API key. With it, Karl uses Claude automatically. |
| `KARL_MODEL` | variable (optional) | `claude-opus-5-5` (default). `claude-sonnet-5-5` or `claude-haiku-5-5` are cheaper. |
| `KARL_EFFORT` | variable (optional) | `low` (default) or `medium`. |
| `KARL_MAX_WORDS` | variable (optional) | 60 (default). |

With no key, Karl answers with the improved rules at once and never waits. The dashboard shows
"no ANTHROPIC_API_KEY: rules answer". You can switch providers on the dashboard at any time
(Claude, Local model, Ollama, Rules only). The dashboard feed shows an "understood:" line for every
turn: what Karl took from the chat and which questions it still has to answer.

**Cost.** Each guest turn makes two Claude calls. The fixed instructions are cached, so each call
mostly pays for the conversation itself, a few thousand tokens. Check your usage on the Anthropic
Console during the first days, and switch `KARL_MODEL` to Sonnet or Haiku if volume is high.

## 4. Invoices

Yes, invoices can be produced reliably, and the parts already exist:

1. Karl collects the full name and ID once a house with a live price is chosen.
2. The `create_invoice` action writes the invoice: house, dates, rate, guests, the 15% service fee.
3. `pc-bridge/invoice_worker.py` renders the PDF on the PC and the bridge sends it on WhatsApp,
   together with the Terms PDF.

What failed in the chats was step 1:
- the name was lost (now fixed);
- the chosen house sometimes had no live price ("Pay $0 today", now fixed: Karl requests the live
  price first);
- the guest hadn't sent an ID yet: Karl asks for whichever of name and ID is still missing.

An invoice is created only when house, dates, price, name and ID are all present and checked. Code
enforces this, not the model.

## 5. Sources

- Shannon, C. E. (1948). A Mathematical Theory of Communication. Weaver, W. (1949), in Shannon &
  Weaver, *The Mathematical Theory of Communication*. Schramm, W. (1954), the interactive model with
  feedback.
- Clark, H. H. & Brennan, S. E. (1991). Grounding in communication.
- Wu, Dong & Xu (2023). Semantic Parsing by Large Language Models for Intricate Updating Strategies
  of Zero-Shot Dialogue State Tracking. Findings of EMNLP 2023. https://arxiv.org/abs/2310.10520
- Liu et al. (2024). Lost in the Middle: How Language Models Use Long Contexts. TACL.
  https://arxiv.org/abs/2307.03172
- Williams, Raux & Henderson (2016). The Dialog State Tracking Challenge series.
- Grice, H. P. (1975). Logic and Conversation (maxims of quantity and relevance: one clear question
  per message).
