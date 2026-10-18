# Crumb PC bridge: Crumb answers on WhatsApp

This program runs on a Windows PC. It runs WhatsApp Web for Crumb's own number, sends new messages to the Sunday Crumb Space, and types Crumb's replies back into WhatsApp. The Space decides what to answer; the bridge only reads and sends.

It is Karlon's WhatsApp bridge, renamed so that both can run on the **same PC** without touching each other:

| | Karlon | Crumb |
|---|---|---|
| Folder for state and logins | `~/.karlon` | `~/.crumb` |
| WhatsApp login (browser profile) | `~/.karlon/wa_session` | `~/.crumb/wa_session` |
| Scheduled task | `\Karlon\Karlon` | `\Crumb\Crumb` |
| Settings (environment variables) | `KARLON_*` | `CRUMB_*` |
| Scripts | `karlon_*.py`, `wa_bridge.py` | `crumb_*.py`, `crumb_whatsapp.py` |

Installing, pausing or removing one bridge never stops the other. The uninstall scripts also leave the shared Windows settings (power plan, automatic sign-in) alone while the other bridge is still installed.

## What you need

- The Sunday Crumb Space running, with the secret `CRUMB_BRIDGE_TOKEN` set to any long random text.
- A phone with WhatsApp on Crumb's number. Use a different number from Karlon's: one WhatsApp account can't be linked to two bridges.
- Python 3.11+ on the PC. Playwright's Chromium is already installed if Karlon runs on this PC; otherwise run `python -m playwright install chromium` once.

## Set it up (PowerShell, in this folder)

```powershell
pip install -r requirements.txt
$env:CRUMB_BRIDGE_TOKEN = "the-same-token-as-the-Space"
python crumb_login.py
powershell -ExecutionPolicy Bypass -File .\install_crumb.ps1 -BridgeToken "the-same-token-as-the-Space"
```

1. **`crumb_login.py`** opens a WhatsApp window. Scan the QR with Crumb's phone (**Settings → Linked devices → Link a device**). When the chats show, press **Enter**. The login is saved in `~/.crumb/wa_session`.
2. **`install_crumb.ps1`** makes it always on. It starts at sign-in and comes back after crashes, Wi-Fi drops and sleep, with the lid closed too. After the first login it runs headless. It saves the token for your user. Add `-AutoLogon` to have it start by itself after a restart, as Karlon does.

## Day to day

```powershell
.\crumb_status.ps1            # what it's doing
.\crumb_status.ps1 -Follow    # live log
.\crumb_status.ps1 -Restart   # fresh start
.\crumb_status.ps1 -Pause     # stop until -Resume
python crumb_login.py         # scan the QR again (login expired)
.\uninstall_crumb.ps1         # remove it (the login and files are kept)
```

The Space's dashboard (`/admin` → **WhatsApp**) shows the following:

- whether the bridge is online;
- every chat and Crumb's replies, with "waiting / sent / not sent" status;
- a switch to turn Crumb off for one chat;
- a box to write to the person yourself.

## When Crumb answers

Crumb answers only when all of these are true:

- Auto-reply is on, both in Settings and for that chat.
- The newest message in the chat is from the person, and it is less than 30 minutes old. The first sync of old chats never sends anything.
- Nobody answered that chat from the phone or the dashboard in the last 2 hours. If a person takes over, Crumb stays out.
- No reply to that chat is already waiting to go out, and the per-chat limit and the daily cap allow another.

Crumb waits a few seconds first, so a burst of messages gets a single answer. All of these limits can be changed with the Space variables `CRUMB_WA_*`.

## Settings (environment variables)

| Name | Default | What |
|---|---|---|
| `CRUMB_BRIDGE_TOKEN` | (none, required) | the Space's `CRUMB_BRIDGE_TOKEN` secret |
| `CRUMB_URL` | `https://quasar-intel-twykers-space.hf.space` | the Crumb server |
| `CRUMB_WA_HEADLESS` | `auto` | `0` = always a window (use this if chats read in a window but not headless) |
| `CRUMB_WA_SYNC_LAST_N_CHATS` | `15` | how many recent chats each pass reads |
| `CRUMB_WA_POLL_INTERVAL` | `5` | seconds between passes |
| `CRUMB_WA_MAX_MB` | `2500` | restart WhatsApp above this much memory |

## Tests

```powershell
python -m pytest -q tests
```
