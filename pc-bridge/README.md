# pc-bridge

The PC-side processes that connect WhatsApp Web, Airbnb and the invoice
renderer to the Karlon server. Run them together with
`python karlon_supervisor.py` (or one at a time; see each file's docstring).

```
pip install -r requirements.txt      # + `playwright install chromium`, LibreOffice for invoices
python -m pytest tests               # 77 tests (server: 33), no WhatsApp/Airbnb/network needed
```

| file | role |
|---|---|
| `wa_bridge.py` | WhatsApp Web ⇄ Karlon (import messages, deliver outbox) |
| `invoice_worker.py` | pending invoice → KARLCON-layout PDF (Chromium; LibreOffice/Word fallback) → upload |
| `airbnb_parallel_system.py` | listing scraper → `/api/houses/ingest`; books queued reservations |
| `airbnb_reserve.py` | the Airbnb booking steps behind the app's Reserve button |
| `karlon_supervisor.py` | starts the three above, restarts them with backoff |
| `karlon_client.py` | shared HTTP session: pooling, retries, timeouts, optional auth |
| `local_config.py` | the one place URLs, cadences and paths live |
| `wa_clean.py` | good-vs-bad data rules (names, notices, times); identical copy in `server/app/` |

## Syncing chats: one JSON snapshot per chat

Each pass reads the **15 most recent chats** (`WA_SYNC_LAST_N_CHATS`, in
WhatsApp's own sidebar order). For each chat the bridge:

1. opens it, checks the header shows the chat it meant to open (otherwise it
   skips the chat), and scrolls up to load history;
2. reads the entire conversation in **one** browser call: every message and
   every divider, in on-screen order;
3. builds one JSON **snapshot** for the chat (`build_chat_snapshot`), where
   every row is either a message or is listed under `rejected` with a reason:

   | rejected as | what it is |
   |---|---|
   | `system_notice` | "Messages and calls are end-to-end encrypted…", deleted-message notices, unknown dividers |
   | `no_message_metadata` | rows with neither WhatsApp's `[time, date] sender:` metadata nor a bubble time, e.g. "X changed their profile photo" |
   | `time_only` / `empty` / `unsupported_media` | a bare "20:16", voice notes and stickers with no text |

   Times come from the metadata, or from the bubble's time plus the date
   divider above it ("TODAY", "Yesterday", "Monday", "29/09/2026"). Failing
   both, a message takes its neighbour's time. Times never go backwards on
   screen, and messages sharing a minute get +1 ms each, so sorting by time
   reproduces WhatsApp's order exactly. All times are sent in UTC.
4. writes it to `chat_snapshots/<chat id>.json` so you can inspect exactly what
   was read and rejected;
5. POSTs it to `POST /api/chats/{id}/sync`, which applies it in one transaction
   (see `server/app/routers/chat_sync.py`). The server merges the old
   "1 unread message…" duplicate chats and corrects existing rows. It removes
   stale rows in the snapshot's time window, capped by a safety limit. It
   reports which photos it doesn't have yet, and the bridge uploads only
   those, each with its WhatsApp time.

How a chat is read. Current WhatsApp Web only draws message content near
the visible part of the list; everything else is an empty placeholder. So the
bridge scrolls through the chat in overlapping steps and keeps each message's
drawn version. A message that was never drawn is counted as `not drawn`,
not as rejected, and nothing before it is pruned on the server.

* **full** read (first time a chat is synced by this run, or `--chat`): loads
  history, then steps top to bottom.
* **recent** read (a chat whose sidebar row changed): only the last ~4 screens.
  This is the live path: a new message shows in Karlon within one pass, a few
  seconds per chat.

Each line in the log shows the mode and time taken, e.g.
`Karl [recent, 2.1s]: 29 msg — +1 new, 0 fixed, 0 stale removed`.
Chats that can't be opened are retried after 1, 2, 4… minutes (max 30)
instead of on every pass. WhatsApp Web is reloaded every `WA_RELOAD_HOURS`
(default 6) to keep the browser's memory in check.

**Profile pictures.** After each chat is synced, the bridge reads the photo
next to the name in the chat's header and uploads it to
`POST /api/chats/{id}/profile-pic`. It doesn't open "Contact info", so groups
and business accounts work too. The upload is a JPEG of at most 320 px,
resized with Pillow if installed, otherwise in the browser. The server serves
it as `/static/profile_pics/<chat id>.jpg?v=<hash>`, and the app shows it
instead of the initials ("EN", "DA"…).
- A picture is only uploaded when it changed or the server doesn't have it.
  The sync response says which one the server has, and the Space's disk is
  wiped on every restart.
- Chats with no photo, or whose photo is hidden by privacy settings, keep
  their initials.
- Pictures are on by default; `--no-pics` turns them off. The log line ends
  in `, profile picture` when one was uploaded.

The server stores times in UTC and serves them in Harare time
(`DISPLAY_TZ_OFFSET_MINUTES`, default 120), so the Android app shows the same
times as WhatsApp.

Try it on one chat first:

```
python wa_bridge.py --chat "Karl" --dry-run   # writes chat_snapshots/<id>.json, sends nothing
python wa_bridge.py --chat "Karl"             # applies that one chat on the server
python wa_bridge.py                           # continuous, 15 most recent chats
```

If the server hasn't been updated yet, the bridge falls back to the old
endpoints and still sends cleaned, ordered, UTC-timed data, but stale rows
can't be removed until `server/` is deployed.

### Deploying the server part

Copy `server/app/` over `app/` in the Hugging Face Space repo
(`Davincii-code/Karlcon`) and push. The new column and indexes are added
automatically on start. Then, once:

```
curl -X POST "https://davincii-code-karlcon.hf.space/api/chats/repair-names"             # dry run: shows the merges
curl -X POST "https://davincii-code-karlcon.hf.space/api/chats/repair-names?apply=true"  # merge "1 unread message…" chats
```

Chats whose name has no real name in it at all (just "3 unread messages")
are listed as `unrecoverable`. Add `&delete_unrecoverable=true` to delete
those that hold nothing composed in the app and no invoices. The bridge's
next pass then re-syncs each chat cleanly.

If a sync reports `prune skipped`, the snapshot would have removed more than
half the chat's imported rows in that window. Check its JSON, then re-run
the POST with `?force=true` if it's right.

## Invoices, Terms & Conditions and other documents

`invoice_worker.py` prints each pending invoice as an A4 PDF in the KARLCON
layout (as `KARLCON_Invoice_KCER-2026-0010.pdf`): number `KCER-<year>-0001…`,
billed from/to, stay band with 2:00 PM / 10:00 AM times, items, a 15% service
fee (`INVOICE_SERVICE_FEE_PCT`), payment methods and notes. It uses Chromium,
which Playwright already installed for the WhatsApp bridge; LibreOffice and
Word are only used if Chromium fails (`INVOICE_PDF_ENGINE` forces one). Check
it on the PC without a booking:

```
py invoice_worker.py --sample        # writes sample_invoice.pdf next to the script
```

Re-queue a failed invoice with:

```
curl.exe -X POST https://davincii-code-karlcon.hf.space/api/invoices/<invoice id>/retry
```

Finished invoice PDFs and the Terms & Conditions PDF
(`server/static/documents/KARLCON_Elite_Retreats_Terms_and_Conditions.pdf`) are
queued on the server as documents (`/api/outbox/documents`). The bridge sends
them through WhatsApp's **Document** option under their real file name, and
photos through **Photos & videos** (never the sticker maker). `wa_bridge.py` fetches that queue
ahead of normal messages. The same document queued twice for one chat is sent
once, and tapping Terms & Conditions again while one is waiting doesn't queue
another.

To send to a phone number with no chat row in WhatsApp (e.g. a guest who got
an invoice before ever messaging), the bridge opens it through WhatsApp's
`web.whatsapp.com/send?phone=…` link. Only outbound sends do this, as it
reloads WhatsApp Web; numbers not on WhatsApp are reported and given up on.

## Invoices from the options you sent

Every time houses are sent to a chat, the server records which ones and the
dates and price each was offered at (`house_sends`). In the app, Invoices
opened from a chat (the $ button) then lists those houses **first**, flagged
"Sent to this guest", with the city preselected; tapping one fills in the
property (`KCER 248`), dates and rate. `GET /api/houses/sent?chat_id=` returns
the same list.

Most of this lives on the server, so it also helps older app builds:
- `/api/houses/available` names houses `KCER 248 · Greendale, Harare` (the
  Airbnb title is `airbnb_title`), so every list shows what the guest was told;
- creating an invoice whose property is named `KCER <n>` links it to that
  listing by itself (photo, place, size, the offered dates and price), whether
  or not the app sends the listing link;
- WhatsApp's copy of a message the app sent (emoji gone, blank lines between
  lines) is matched by its letters and digits only, so it links to the app's
  own bubble instead of appearing as a second one.

Only "sent first, per guest" needs the app to say which chat it is asking about
(`chat_id`).

## Airbnb scraper: every city, 10 listings each

`airbnb_parallel_system.py` sweeps the 20 cities in `ZIMBABWE_CITIES` (or
`AIRBNB_CITIES="Harare,Bulawayo"`), `AIRBNB_CITY_WORKERS` (3) at a time. Per
city it takes `AIRBNB_LISTINGS_PER_CITY` (10) listings: new ones first, then
any not refreshed for `AIRBNB_RESCRAPE_HOURS` (12). A city's listings are
scraped and pushed as soon as its search finishes, so each city fills the app
on its own. Houses wiped by a Space restart are pushed again on the next
refresh. Cycles rest `AIRBNB_CYCLE_REST_SECONDS` (300).

## Reserve button: booking a house on Airbnb from the app

In a chat, the 🏠 popup shows the houses (KCER 101…). Select **one** and tap
**Reserve**. Confirm the guests (and optionally a note to the host), and:

1. the app POSTs `/api/reservations` (`server/app/routers/reservations.py`).
   The server stores the listing's Airbnb room id, dates and guests, plus the
   quoted total (per-night price × nights, only when it was scraped for exactly
   those dates). It refuses a second booking of the same house and dates
   while one is queued, running, requested or unknown (HTTP 409);
2. `airbnb_parallel_system.py` (already logged in to Airbnb) asks
   `/api/reservations/pending` every `AIRBNB_RESERVE_POLL_SEC` (15 s). It gets
   **one** job at a time, and `airbnb_reserve.py` books it in the same browser:
   opens the listing with the dates and guests → scrolls to and clicks
   **Reserve** (or opens the booking page directly) → checks a saved card is
   there, the dates are still available and the total → writes the message to
   the host → clicks **Request to book** / **Confirm and pay** → waits for the
   trip page;
3. it reports the outcome. On `requested`, the server queues this WhatsApp to
   the chat, which `wa_bridge.py` delivers like any other message:

   > ✅ Your reservation has been made
   > 🏠 KCER 101 · 05 Oct 2026 → 08 Oct 2026 (3 nights)
   > Now waiting for the host to share the live location.

The app shows the status as it goes:

| status | meaning | WhatsApp sent? |
|---|---|---|
| `pending` | queued, waiting for the PC (can still be cancelled in the app) | no |
| `in_progress` | the PC is on Airbnb now | no |
| `requested` | "Request to book" went through; the host has 24 h to accept | **yes** |
| `dry_run` | test mode: every step ran except the final click | no |
| `failed` | stopped **before** paying (no card, price changed, dates gone, button not found). Nothing booked; Reserve again when fixed | no |
| `unknown` | something went wrong **after** the final click, or the PC crashed mid-booking. **Check Airbnb → Trips.** Never retried automatically | no |
| `cancelled` | cancelled in the app before the PC started | no |

**It starts in test mode.** Until you set `AIRBNB_RESERVE_LIVE=1` the PC
does everything except click "Request to book", and the app shows "🧪 Test run
OK". Do one test run on a real listing first, check the screenshot in
`airbnb_data/reservations/reservation_<id>_<step>.png`, then go live:

```
set AIRBNB_RESERVE_LIVE=1                 # PowerShell: $env:AIRBNB_RESERVE_LIVE="1"
set AIRBNB_RESERVE_MAX_TOTAL_USD=1500     # optional: never pay more than this
python karlon_supervisor.py
```

Money safety, in order:
- the price guard stops if Airbnb's total is more than 10% (and at least $25)
  above the quote, or above `AIRBNB_RESERVE_MAX_TOTAL_USD`;
- popups are closed only by an exact button name. A substring match on "OK"
  would also hit "Request to bOOK"; a test covers this;
- after the final click, anything short of a clear confirmation is `unknown`
  and the server never hands it out again, so nothing is booked twice;
- if Airbnb asks to verify the payment (3-D Secure, "confirm it's you"), the
  booking stops as `unknown`. Finish it in the scraper's browser window.

Airbnb's terms don't allow automated booking, and Airbnb can lock an account it
thinks is a bot. Keep the volume human (one at a time, as here), and watch the
first live bookings.

## Chat layout: the contract the app must honour

Karlon draws a bubble on the **right if `direction == "out"`, on the left
otherwise** (the WhatsApp convention). That rule is only stable if the stored
`direction` is always exactly `"in"` or `"out"`, and if the client derives the
side from that field alone, never from a sender-name comparison and never from
per-session state.

Source of truth (done in this repo): `wa_bridge.py` now guarantees every
imported message, text or image, carries `direction ∈ {in, out}`. It reads
five signals in order (accessibility label, bubble tail, legacy class,
WhatsApp's `true_`/`false_` message key, and bubble geometry), then falls back
to a per-sender vote inside the same chat, then to `in`. `unknown` is never
sent.

The same contract on the server (`server/`) and in the Android app
(`android_app/`, added to this repository later):

1. **Server, on import:** coerce anything other than `out` to `in` before
   inserting, so no other client can reintroduce `unknown`.
2. **Server, one-off backfill** for rows imported before this change:
   `UPDATE messages SET direction = 'in' WHERE direction IS NULL OR direction NOT IN ('in','out');`
   then re-run `python wa_bridge.py --once --all --deep-history` to let the
   corrected direction replace guesses. (Your existing `external_key` values are
   unchanged on purpose, so history is not duplicated.)
3. **App:** align by `direction` only, and order by `(created_at, insertion id)`.
   Shape of the rule in Jetpack Compose:

   ```kotlin
   @Composable
   fun MessageRow(m: Message) {
       val mine = m.direction == "out"
       Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 2.dp),
           horizontalArrangement = if (mine) Arrangement.End else Arrangement.Start) {
           Surface(color = if (mine) OutgoingGreen else IncomingSand,
                   shape = RoundedCornerShape(16.dp),
                   modifier = Modifier.widthIn(max = 300.dp)) { BubbleContent(m) }
       }
   }
   ```

## What changed vs the originals (commit history has the verbatim baseline)

Correctness
- **Duplicate imports.** Every text message was exported twice: once with its
  timestamp, and again via the `data-id` fallback under a different
  `external_key` (so server-side dedup could never match them). The fallback
  now skips ids the first pass covered. On a model chat, 8 messages produced
  16 records before, 8 after. Whether users saw doubles depends on whether the
  server also dedups by content; it is not visible from here.
- **Urgent replies were gated on the wrong queue.** `sync_once` only called
  `process_outbox` when the *normal* queue was non-empty, so an urgent reply
  could wait for an entire pass over every chat. It now checks both, and the
  idle sleep delivers outbound within ~0.5 s instead of a flat 5 s.
- Retried urgent sends stay urgent; `wa-ack` is retried (a lost ack meant a
  restart re-sent the message to the customer).

Efficiency
- **Incremental sync**: unchanged chats (by sidebar-row fingerprint) are not
  reopened; every `WA_FULL_SWEEP_EVERY` (default 10) passes is a full sweep as
  a backstop. Set it to `1` for the old behaviour.
- Sidebar sweep no longer re-extracts rows it already captured (each row was
  re-read several times per pass). Direction and message id come back from a
  single browser round-trip, so the new de-duplication costs no extra calls.
- One pooled HTTP session per thread (`requests`, and `aiohttp` in the
  scraper) instead of a new TCP+TLS handshake per call.
- Supervisor: exponential restart backoff (5 s → 5 min) that resets after a
  60 s stable run.
- Invoice worker: private LibreOffice profile per conversion (a running
  LibreOffice window otherwise makes headless conversion silently produce
  nothing).

## Design rationale and references

Cited for the specific decision each informed; verified to exist, but applied
as engineering principles, not as a formal proof.

- Saltzer, Reed & Clark (MIT), *End-to-End Arguments in System Design*, ACM
  TOCS 2(4), 1984: replay safety belongs to the endpoint that owns the
  operation, hence retries in `karlon_client.py` are limited to GET/HEAD and
  the idempotent `wa-ack` retries itself.
- Saltzer & Schroeder (MIT), *The Protection of Information in Computer
  Systems*, Proc. IEEE 63(9), 1975: fail-safe defaults. The bridge's send
  guards (identity gate, echo guard, duplicate-blast guard) refuse rather than
  guess, and direction defaults to `in`.
- Parker et al. (UCLA), *Detection of Mutual Inconsistency in Distributed
  Systems*, IEEE TSE SE-9(3), 1983: cheap per-replica summaries to detect
  divergence; full reconciliation only where they differ. Basis of the
  fingerprint + periodic full sweep.
- Hoare (Oxford), *Communicating Sequential Processes*, CACM 21(8), 1978: the
  bridge's threads share nothing but queues; kept as is.
- Little, *A Proof for the Queuing Formula L = λW*, Operations Research 9(3),
  1961 (written at Case Institute of Technology, later MIT): time spent
  waiting is queue length; the fix for outbound latency was to stop being busy
  or asleep, not to poll faster.

I found no MIT/UCLA/Oxford result on chat-bubble alignment specifically; the
left/right rule above is the WhatsApp convention, not a research finding.

## Known issues not changed here

- **The API appears unauthenticated.** `GET /api/outbox` and friends return
  pending customer messages to anyone who knows the Space URL. Set
  `KARLON_API_TOKEN` on the PC (sent as `Authorization: Bearer …` by every
  script here) and enforce it in the server.
- Invoices: `claim` marks an invoice claimed before rendering; a crash between
  claim and upload strands it. The server needs a claim timeout/lease.
- `airbnb_parallel_system.py` constants look like leftover debug values:
  `CYCLE_REST_SECONDS = 1` (comment says 30 minutes), `MAX_SECONDS_PER_CITY =
  900000` (comment says 15 minutes; that is ~10 days), `MAX_SEARCH_WORKERS = 40`
  concurrent browser pages. I left them; you may want to review them.
- WhatsApp Web selectors are best guesses at its current DOM and are the first
  thing to check after a WhatsApp update (`wa_bridge.py --discover`). The tests
  model that DOM; they do not prove today's live markup matches.
