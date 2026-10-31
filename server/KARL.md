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
| "From 23 to 25 for two guests" was answered with "Which dates are you looking at?" | A day range with no month was left unread. | The model reads the whole chat and finds the dates; code resolves them; an assumed month is confirmed with the guest. |
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

## 2. How Karl works now (all on the Space's own CPU, no API key)

Karl runs a pretrained, 4-bit open model under llama.cpp inside the Karlcon Space. The full design,
the model benchmark and the sources are in the project skill
[`.claude/skills/karl-local-llm/SKILL.md`](../.claude/skills/karl-local-llm/SKILL.md). In short,
each guest turn goes through these stages:

1. **Debounce and gate.** Wait for the guest to finish typing, then decide whether to answer.
2. **Rules first** (`nlu.py`, under 1 ms). A simple turn the rules fully explain never waits for the
   model. This is the cascade in `mind.needs_model`.
3. **STATE** (local model, JSON forced by a grammar). Reads the recent conversation into the booking
   state and the guest's unanswered questions. The model copies the guest's own date words, and code
   does the calendar. Code checks everything before use.
4. **Plan** (`policy.py`, code). Decides houses, live prices, Terms, the invoice, and what the reply
   must say.
5. **REPLY** (local model, JSON forced by a grammar). Writes from the same conversation; the guest's
   newest messages and questions go last. The prompt opening is identical to STATE's, so llama.cpp
   doesn't process the conversation a second time.
6. **Check.** No invented price, house or number, and no repeated line. One rewrite with the reason,
   then the plan's own wording.
7. **Send** through the PC bridge.

**The model reads and writes; code computes and acts.** A wrong model answer can't book wrong dates,
quote an invented price or invoice a house that wasn't offered.

The communication model (Shannon & Weaver with Schramm's feedback), the conversation memory, and
what we do and don't take from frontier assistants are all in the skill.

## 3. Turning it on

Nothing to add: Karl uses the local model by default (`DAVIS_PROVIDER=local`), and the `Dockerfile`
downloads it at build time. Optional Space variables:

| Variable | Default | Effect |
|---|---|---|
| `KARL_UNDERSTAND` | `auto` | `auto` runs STATE only when the rules can't explain the turn; `always` runs it on every turn; `off` uses rules only. |
| `KARL_REPLY` | `auto` | `auto` lets the model write only when the guest asked something; `always` writes every reply with the model (slow on a free Space). |
| `KARL_TRANSCRIPT` | 24 | How many messages Karl reads per turn. |
| `KARL_MAX_WORDS` | 50 | The longest reply the model may write. |
| `DAVIS_LOCAL_MODEL_REPO` / `DAVIS_LOCAL_MODEL_FILE` | see the skill | Which GGUF model to use. Change them together with the `Dockerfile`. |

The dashboard feed shows an "understood:" line for every turn: what Karl took from the chat, which
questions it still has to answer, and why the model was asked (or "understood_by_rules" when it
wasn't needed).

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
