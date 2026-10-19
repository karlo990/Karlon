"""Evaluate Karl's STATE step with local GGUF models on the real October chats.
usage: python tests/eval_state.py . /path/to/model.gguf [threads]   (from server/)"""
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone

server, model_path = sys.argv[1], sys.argv[2]
threads = int(sys.argv[3]) if len(sys.argv) > 3 else 2
sys.path.insert(0, server)
from app.davis import mind, nlu  # noqa: E402

T0 = datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc)


def conv(*rows):
    out = []
    for i, (who, text) in enumerate(rows):
        d = "in" if who == "G" else "out"
        sender = {"G": "Guest", "K": "Karl", "T": "You (WhatsApp)", "C": "Karl (catalogue)"}[who]
        out.append({"direction": d, "sender": sender, "kind": "text", "text": text,
                    "created_at": (T0 + timedelta(minutes=i)).isoformat()})
    return out


CASES = [
    ("rusape_from_23_to_25", conv(
        ("G", "His 🧸"), ("G", "Greetings"),
        ("K", "Hello! I'm Karl from KarlCon Elite Retreats 🏡 Which city would you like to stay in, and for which dates?"),
        ("G", "Rusape"), ("K", "Lovely, Rusape! Which dates are you looking at, and for how many guests?"),
        ("G", "From 23 to 25 for two guests")),
     {"city": "rusape", "check_in": "2026-10-23", "check_out": "2026-10-25", "guests": 2}),
    ("harare_4november_one_guest", conv(
        ("G", "Hi"), ("G", "Looking for a room to rent budget"), ("G", "150 -170"),
        ("K", "Hello! I'm Karl from KarlCon Elite Retreats 🏡 Which city would you like to stay in, and for which dates?"),
        ("G", "Avondale Mountplesent borwdale"), ("T", "okay noted"),
        ("K", "Lovely, Harare! Which dates are you looking at, and for how many guests?"),
        ("G", "4november"), ("T", "okay its rent ?"), ("G", "One guest"),
        ("T", "because we rather do short term retreats bookings only"), ("G", "Ok")),
     {"city": "harare", "check_in": "2026-11-04", "guests": 1}),
    ("bulawayo_15_guests_name_pool", conv(
        ("G", "Good day Sir or Madam"),
        ("K", "Hello! I'm Karl from KarlCon Elite Retreats 🏡 Which city would you like to stay in, and for which dates?"),
        ("G", "Bulawayo..i want to book a bnb full house for 21November\n\ncheck in time 3.00pm 21November\n\ncheck out time 10:00am 22November"),
        ("T", "Whats your budget"), ("G", "15guests.."),
        ("G", "21 November and22\nits my friends birthday party only 15 close friend and its a quiet celebration..no loud music.."),
        ("G", "How much do you charge for full house"),
        ("C", "Option 1 · KCER 115\n📍 Bulawayo\n👥 8 guests · 4 bedrooms\n📅 21 Nov → 22 Nov (1 night)\n💵 USD 81.00/night · USD 93.15 incl. 15% service fee"),
        ("G", "KCER 115"),
        ("K", "Great choice: KCER 115, 21–22 Nov, USD 93.15 incl. 15% service fee. To prepare your invoice, please send your full name and ID or passport number."),
        ("G", "Howmuch deposit?"),
        ("K", "Full payment confirms the booking and is due within 48 hours of the invoice. Please send your full name and ID number for the invoice."),
        ("G", "Blessing Nyasha Mpofu"), ("G", "Bulawayo"),
        ("G", "Its for 15guests  so i want to know how many bedrooms plus is there a pool")),
     {"city": "bulawayo", "check_in": "2026-11-21", "check_out": "2026-11-22", "guests": 15,
      "full_name": "Blessing Nyasha Mpofu", "chosen_ref": 115, "q": ["bedroom", "pool"]}),
    ("bulawayo_pics_amenities_location", conv(
        ("G", "Goodmorning"), ("G", "Looking for Accomodation"),
        ("T", "Good day sorry we were having technical  dificulties ,but fortunately evrything is well now"),
        ("G", "Okay"), ("G", "Looking for 3 bedroom IN 18 December 2026 and OUT 20 December 2026"),
        ("K", "Which city would you like to stay in? We have homes in Harare, Bulawayo, Victoria Falls and more 🏠"),
        ("G", "Good day \n\nHouse in Bulawayo 3 bedroom in 18 Dec and out 20 December 2026"),
        ("K", "Hello! Here is 1 home in Bulawayo for 18–20 Dec 🏡 Reply with the KCER number you like and I'll prepare your invoice."),
        ("C", "KCER 110\n📍 Bulawayo\n👥 6 guests · 3 bedrooms\n📅 18 Dec → 20 Dec (2 nights)\n💵 USD 99.00/night · USD 227.70 incl. 15% service fee"),
        ("G", "May I kindly have pics"), ("G", "And what amenties available"),
        ("G", "Where is this located want something closer to town")),
     {"city": "bulawayo", "check_in": "2026-12-18", "check_out": "2026-12-20", "bedrooms": 3, "photos_for": [110],
      "q": ["pic", "amenit", "locat"]}),
    ("two_couples_corrected_date", conv(
        ("G", "Good afternoon Kari.\nJust enquiring about accommodation and cost for 2 couples for one night ...24 .3.2027  and the cost please.\nThank you, \nMarianne Van de Ruit"),
        ("G", "Sorry wrong date ..\nin Monday  22.3.27...out Tuesday 23 3.37.\nThank you")),
     {"check_in": "2027-03-22", "check_out": "2027-03-23", "guests": 4, "city": None}),
    ("marondera_monthly_room", conv(
        ("G", "Hi"), ("G", "Looking for 1 room in town"), ("T", "Good day"), ("T", "Which city"), ("G", "MaronderA town"),
        ("T", "This is what I have found for you:\n\nOption 1 of 5\n🏠 KCER 111\n📍 Marondera\n👥 6 guests · 4 bedrooms · 4 beds · 4 baths\n📅 Free 18 Oct 2026 → 20 Oct 2026 (2 nights)\n💵 USD 133.50 per night · USD 267.00 total"),
        ("G", "I want to rent"), ("G", "Not booking"),
        ("K", "Lovely, Marondera! Which dates are you looking at, and for how many guests?"),
        ("G", "Room to rent from 80 to90 per month")),
     {"city": "marondera", "check_in": None, "stay_type": "long_term_rental"}),
    ("one_night_offers_question", conv(
        ("G", "Good day"), ("G", "Do you have offers for 1 night stay?")),
     {"check_in": None, "q": ["offer"]}),
]


def score(u, f, exp):
    hits, total, misses = 0, 0, []
    for k, v in exp.items():
        if k in ("bedrooms",):
            continue                                   # read by the rules, not the model
        if k == "q":
            qs = " ".join(u.get("open_questions") or []).lower()
            for word in v:
                total += 1
                ok = word in qs
                hits += ok
                if not ok:
                    misses.append(f"question~{word}")
            continue
        total += 1
        got = f.get(k) if k in ("check_in", "check_out", "guests", "city", "bedrooms", "full_name", "photos_for") \
            else u.get(k)
        if k == "chosen_ref":
            got = f.get("option_ref")
        if k == "stay_type":
            got = u.get("stay_type")
        ok = (got == v) or (v is None and not got)
        hits += ok
        if not ok:
            misses.append(f"{k}={got!r} (want {v!r})")
    return hits, total, misses


from llama_cpp import Llama, LlamaRAMCache  # noqa: E402

t = time.time()
llm = Llama(model_path=model_path, n_ctx=4096, n_threads=threads, n_threads_batch=threads, n_batch=256, verbose=False)
llm.set_cache(LlamaRAMCache(capacity_bytes=1 << 30))
load = time.time() - t
today = date(2026, 10, 18)
schema = mind.STATE_SCHEMA


def state(msgs):
    m = mind.understand_messages(msgs, {}, [], today)
    t = time.time()
    out = llm.create_chat_completion(messages=m, response_format={"type": "json_object", "schema": schema},
                                     temperature=0.1, max_tokens=420)
    return out, time.time() - t


t = time.time()
state(conv(("G", "Hi")))                    # warm-up: the system prompt is evaluated once, like at Space start
warm = time.time() - t
H = T = 0
rows = []
for name, msgs, exp in CASES:
    _, prev_s = state(msgs[:-1])                         # the turn before (cached afterwards)
    out, took = state(msgs)                              # this turn: only the new message is new input
    usage = out.get("usage") or {}
    text = out["choices"][0]["message"]["content"]
    try:
        u = json.loads(text)
    except ValueError:
        u = {}
    month = nlu.last_month([x["text"] for x in msgs])
    f = mind.to_features(u, today, {}, [], month)
    h, tot, miss = score(u, f, exp)
    H += h
    T += tot
    row = {"case": name, "score": f"{h}/{tot}", "cold_s": round(prev_s, 1), "turn_s": round(took, 1),
           "prompt": usage.get("prompt_tokens"), "out": usage.get("completion_tokens"), "misses": miss,
           "dates_text": u.get("dates_text"), "questions": u.get("open_questions")}
    rows.append(row)
    print("CASE", json.dumps(row, ensure_ascii=False), flush=True)
print("RESULT", json.dumps({"model": model_path.rsplit("/", 1)[-1], "threads": threads, "load_s": round(load, 1),
                            "warmup_s": round(warm, 1), "accuracy": f"{H}/{T}", "pct": round(100 * H / T),
                            "median_turn_s": sorted(r["turn_s"] for r in rows)[len(rows) // 2]}), flush=True)
