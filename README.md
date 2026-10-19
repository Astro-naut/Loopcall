# Loopcall - Order verifier: Sonali's thank-you call

A small business gets an order with a thin address ("Shivaji Nagar, Pune 411005"). Today someone phones the customer to check the order is real and to fix the address. This project makes that call by voice. It is framed as a **thank-you** from **Sonali** at Sustainable Universe, and every model runs on one ordinary laptop: speech-to-text, a small open-weight language model and text-to-speech. A call costs nothing to run, and no audio, transcript or address is sent to an AI vendor.

The orders come from a **local Excel workbook** (`Sonali_SU_DB.xlsx`). After each call only the cells that case needs are written: `Verified/Called`, a one-line `Agent Call Review` (for example "Address updated successfully" or "Manual call needed, could not obtain Address"), and the **Address** cell when the customer actually gave a new one. Name, phone, quantities and flagged concerns are not touched.

Sonali does not cancel or edit orders. She records an outcome for a human to act on. She introduces herself by name and does not say she is an AI. She **opens in Hindi**, then **replies in whatever language the customer uses**: Hindi, English or Hinglish.

## The open models (all free, all local)

| Job | Model | License | Measured on an i5-13500H laptop, CPU only |
|-----|-------|---------|-------------------------------------------|
| Hearing | [Oriserve Whisper-Hindi2Hinglish-Swift](https://huggingface.co/Oriserve/Whisper-Hindi2Hinglish-Swift) via faster-whisper (int8) | Apache-2.0 | about 0.6 s per reply |
| Thinking | [Gemma 4 E4B](https://ollama.com/library/gemma4) via Ollama | Gemma terms (open weights) | about 6 s per call, kept off the waiting path |
| Speaking | [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) via kokoro-onnx, voice `hf_alpha` | Apache-2.0 | 0.37 s of compute per 1 s of speech |

**Why these models?**
- **Speech-to-text.** Stock Whisper `small` took about 4 s per reply on this laptop. It often detected Hindi as English or French and invented sentences. Swift is Whisper fine-tuned on about 550 hours of noisy Indian speech. It writes what was said in romanized Hinglish ("haan ji, maine order kiya tha"), writes English as English, and turns spoken numbers into digits. It is also about 6× faster.
- **Text-to-speech.** Kokoro's Hindi female voice reads Devanagari with native pronunciation. English words inside a Hindi sentence ("order", "thank you") are pronounced as English. Sonali uses that one voice for all three languages, so she always sounds like the same person.
- **Gemma.** The Gemma 4 small model was the most reliable at reading hedged Hinglish replies ("Haan, actually I ordered it for my mother"). The 2B version was faster but got such replies wrong.

## How a call works

```
Sonali_SU_DB.xlsx  (worksheet Orders)
  Name / Phone Number / Address / quantities / Flagged Concerns
  Verified/Called = Not yet
     │
     ▼
 address rules ── gaps: no_house_number / missing_pincode / address_too_short
     │                (Flagged Concerns plus the Address cell)
     ▼
 Sonali speaks (Kokoro) ──> customer answers ──> Swift turns speech into text
     ▲                                               │
     └──── reply in the customer's language <── language + intent (rules, then Gemma)
     │
     ▼
 same Excel row:  Verified/Called = Yes
                  Agent Call Review = No  (or Yes, if a colleague should look)
```

1. **Before dialling**, Gemma writes the customer's name, product and address in Devanagari (for example, "Rosemary Conditioner" becomes "रोज़मेरी कंडीशनर"). Kokoro then renders the opening lines to audio, so the call starts instantly. The Devanagari spellings are cached in `cache/spoken.json`, and the next order is prepared while the current call wraps up.
2. **Opening in Hindi:** "नमस्ते राहुल जी! मैं सोनाली, सस्टेनेबल यूनिवर्स से बोल रही हूँ। आपके ऑर्डर के लिए बहुत-बहुत धन्यवाद! क्या आपने रोज़मेरी कंडीशनर का ऑर्डर किया था?"
3. **Language detection** runs on every reply. Romanized Hindi and English word lists give an instant answer: mostly Hindi words means Hindi, mostly English means English, and a real mix means Hinglish. Gemma's own label is the fallback. Sonali switches scripts from her next sentence:
   - Hindi lines are in full Devanagari.
   - Hinglish lines are in Devanagari but keep English words in English (for example, "Perfect जी, note कर लिया!").
   - English lines are in plain English.
4. **Understanding the reply.** Answers that end up meaning yes count as yes: "Yeah, maybe my daughter did it" and "pata nahi, shayad wife ne kiya ho" both confirm the order, and the reply is kept in `call_notes` for the shop. The same goes for no. Clear answers like these are read by plain rules and need no model. Gemma does the thinking for everything else, such as questions back, doubt and off-topic replies. It answers with one label word: yes, no, rude or other. For "other", it goes on to write one short line that Sonali says back before asking again. For example, "Why should I tell you?" gets "It's only so that your parcel reaches you safely and on time." and "Kaunsa order?" gets "यह हमारे वेबसाइट पर कुछ दिन पहले दिया गया ऑर्डर है।". Lines that aren't safe to say (Gemma's own questions, wrong script, any mention of AI) are dropped, and Sonali uses her scripted line instead.
5. **Address gaps** are asked one question at a time. Plain code reads pincodes (including spoken ones like "chaar ek ek shoony shoony paanch", which becomes 411005) and phrases like "we don't have house numbers" instantly, so the next question is never delayed. Otherwise Gemma decides whether the reply answered the question, the same way as above. Either way, Gemma reads the answer again in the background for the house or building name, the landmark and the pincode.
6. **Three tries per question.** If an answer is missing or doesn't answer the question, Sonali responds to what they said and asks again, up to 3 times. After three tries without a real answer, she closes with "Okay, my colleague will get in touch with you shortly. Have a nice day!" in the customer's language.
7. **No indecent talk.** Swearing, insults and sexual or flirty remarks end the call at once with the same polite closing. Clear profanity is caught by a word list, and Gemma catches the rest. A question back or a refusal ("Why should I tell you?") is not rude: the conversation carries on.
8. **Outcome per order:**
   - `CONFIRMED`
   - `CONFIRMED_ADDRESS_UPDATED`
   - `NEEDS_HUMAN_REVIEW`
   - `DENIED_REVIEW`
   - `ENDED_INAPPROPRIATE`
   - `NO_ANSWER`

   `NO_ANSWER` leaves `Verified/Called` as `Not yet` and writes `Agent Call Review` as `No answer, retry needed`. Everything else sets `Verified/Called = Yes`. `Agent Call Review` is a short sentence for that outcome (order confirmed, address updated, denied, no answer, could not obtain address, and so on). On `CONFIRMED_ADDRESS_UPDATED` the Address cell is rewritten with the house number / pincode the customer gave.

**Speed:** Gemma is loaded into memory while the app starts (a cold load takes about 30 s, which is what made an early test call pause for 21 s). Every fixed line (hold, retry and closing lines in all three languages) and the next customer's own lines are rendered to audio ahead of time.

## Run it (Windows, Python 3.12)

```
# one time
pip install -r requirements.txt
python setup_models.py           # Kokoro voice + Swift speech model into models/
ollama pull gemma4:e4b

# see which Excel row would be called (Name, Phone Number, Flagged Concerns)
python setup_sheet.py

# check the Excel loop: no microphone, no models, no real dial
python call.py --dry-run --limit 1

# hear Sonali without a microphone (still writes the Excel outcome)
python call.py --dry-run --speak --limit 1

# a real conversation on the laptop mic and speaker (wear headphones)
# reads Phone Number from the first Not yet row; does not place a carrier call
python call.py --limit 1 --verbose

# same logic in text mode, with scripted replies (Gemma, no audio)
python verify.py --replies excel_demo_replies.json --limit 1 --verbose
```

Speak after the beep. Your turn ends after 0.8 seconds of silence, or after 8 seconds if you say nothing (then the order is recorded as `NO_ANSWER`).

Other options:
- `--list-voices` and `--voice hm_omega` to try other voices.
- `--speech-speed 0.95` to make Sonali speak slower.
- `--stt-model small` to use stock Whisper instead of Swift.
- `--list-devices` with `--mic` and `--speaker` to pick a specific laptop device.

**About `setup_models.py`:** It downloads Kokoro from its GitHub release. Swift is only published in PyTorch format, so the script converts it once to CTranslate2. If PyTorch isn't installed, it prints the commands to run the conversion in a throwaway venv instead.

## The Excel workbook (source of truth)

Default file: `Sonali_SU_DB.xlsx`, worksheet **Orders**. Close it in Excel before a run, or Windows will refuse the save.

| Column | Sonali uses it to | Written back? |
|--------|-------------------|---------------|
| **Name** | greet them | no |
| **Phone Number** | show before you press Enter | no |
| **Address** | read out, then ask only what is missing | **yes, only when the call updated it** |
| **Herbal Shampoo Quantity (250 ml)** / **Rosemary Conditioner Quantity (250 ml)** | name the product | no |
| **Flagged Concerns** | extra hint for the script (`Missing Pincode`, `No house number`, …), on top of the Address cell | no |
| **Verified/Called** | skip rows already `Yes`; after a completed call set to `Yes`; leave `Not yet` if nobody answered | **yes** |
| **Agent Call Review** | one sentence for that outcome, e.g. `Address updated successfully` or `Manual call needed, could not obtain Address` | **yes** |

A row is pending when **Name** and **Phone Number** are filled and **Verified/Called** is `Not yet` (or blank). Rows with only a phone and no name are skipped. `--limit 1` is the demo: one pending row, then stop. `--redo` calls `Yes` rows again. `--no-write-back` leaves the workbook untouched.

`--orders path.csv` or `--orders path.xlsx` picks a different file. `--sheet-url` still talks to a Google Sheet if you want one (see `google_sheets/Code.gs`).

**n8n (optional):** use `--webhook <url>` or `N8N_WEBHOOK_URL` to also POST each outcome as JSON.

## Laptop calls (current demo)

Phone Link / `--dial` is disabled. Sonali talks on **this laptop's speaker** and listens on **this laptop's microphone**. Wear headphones so she does not hear herself.

```
python call.py --limit 1 --verbose
```

Press Enter after the row is printed. Speak after the beep. `--dial` is ignored if you still pass it.

## Privacy and rules

- All three models run locally after the first download. No AI vendor sees audio, transcripts or addresses.
- "Local" means the AI processing and the Excel file. This demo does not place a carrier call.
- `cache/`, `results*.csv` and `Sonali_SU_DB.xlsx` hold customer details. `.gitignore` keeps them out of the repo.
- Sonali doesn't announce that she is automated. Before using this on real customers, check India's DPDP Act, TRAI's rules on calling, and consent to record. Call only your own customers about their own orders.

## Known limits

- Tested with scripted replies and the laptop microphone. Speak after the beep.
- Turn-taking is one turn at a time, with no interrupting Sonali mid-sentence.
- When Gemma is needed, it takes about 1–2 s on this CPU for a label, and 2–4 s when it also writes a line back. Two things made that possible. First, the code talks to `127.0.0.1` instead of `localhost`, since `localhost` on Windows tries IPv6 first and added about 2 s to every request. Second, each system prompt is read once at start-up and before each call, and Ollama keeps it cached, so a turn only reads the new words. Gemma 4's sliding-window attention otherwise makes Ollama re-read the whole prompt every turn. Sonali only says her hold line ("One second, let me just check") if Gemma takes longer than 0.7 s.
- Speech-to-text can mishear names and numbers. Unclear cases go to `NEEDS_HUMAN_REVIEW` instead of guessing.
- The house-number rule counts any digit, so "Sector 62" passes. Tune `check_address` for your customers.

## Files

- `call.py`: the voice call (Kokoro speaker, Swift transcriber, microphone).
- `verify.py`: Sonali's scripts in three languages, language detection, address rules, Gemma prompts, outcomes.
- `phone.py`: number normalisation (Phone Link dialling is disabled).
- `sheets.py`: Excel, CSV and optional Google Sheets order stores.
- `google_sheets/Code.gs`: optional Apps Script bridge.
- `setup_models.py`: model download and conversion.
- `Sonali_SU_DB.xlsx`: local shop database (not committed).
- `excel_demo_replies.json`, `sample_orders.csv`, `sample_replies.json`: fake data for dry runs.
