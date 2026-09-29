# pc-bridge

The PC-side processes that connect WhatsApp Web, Airbnb and the invoice
renderer to the Karlon server. Run them together with
`python karlon_supervisor.py` (or one at a time; see each file's docstring).

```
pip install -r requirements.txt      # + `playwright install chromium`, LibreOffice for invoices
python -m pytest tests               # 14 tests, no WhatsApp/Airbnb/network needed
```

| file | role |
|---|---|
| `wa_bridge.py` | WhatsApp Web ⇄ Karlon (import messages, deliver outbox) |
| `invoice_worker.py` | pending invoice → .docx → PDF → upload |
| `airbnb_parallel_system.py` | listing scraper → `/api/houses/ingest` |
| `karlon_supervisor.py` | starts the three above, restarts them with backoff |
| `karlon_client.py` | shared HTTP session: pooling, retries, timeouts, optional auth |
| `local_config.py` | the one place URLs, cadences and paths live |

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

Still to do **on the server and in the Android app, whose source is not in this
repository** (the APK is compiled; there is nothing here to edit):

1. **Server, on import:** coerce anything other than `out` to `in` before
   inserting, so no other client can reintroduce `unknown`.
2. **Server, one-off backfill** for rows imported before this change:
   `UPDATE messages SET direction = 'in' WHERE direction IS NULL OR direction NOT IN ('in','out');`
   then re-run `python wa_bridge.py --once --all --deep-history` to let the
   corrected direction replace guesses. (Your existing `external_key` values are
   unchanged on purpose, so history is not duplicated.)
3. **App:** align by `direction` only, and order by `(created_at, insertion id)`.
   Sketch for Jetpack Compose (the APK bundles AndroidX/Compose libraries;
   **untested, written without the app's source**):

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

If you can share the app or server source (the FastAPI `routers/`, and the
Android chat screen), the same treatment can be applied there.

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
