#!/usr/bin/env python3
"""Order verification assistant (proof of concept).

Sonali from Sustainable Universe runs a short thank-you call. Plain code owns
the script and the address rules. A small local model (Gemma via Ollama) only:
  - writes names and addresses in Devanagari before the calls,
  - reads each reply the rules can't place (did they confirm? did they answer?),
  - when the reply is off-script, writes what Sonali says back before asking again,
  - pulls address facts out of a reply.
The call opens in Hindi, then follows the customer's language (Hindi, Hinglish
or English). Outcomes are marked on the same sheet the orders came from.

This file runs the text-only version (type the replies); call.py adds voice.
Standard library only.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

import sheets

# 127.0.0.1, not "localhost": on Windows, localhost tries IPv6 first and adds ~2 s per request.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")
LANGUAGES = ("hindi", "hinglish", "english")
OPENING_LANGUAGE = "hindi"

HOUSE_WORDS = ("flat", "house", "plot", "bldg", "building", "apartment",
               "apt", "villa", "bungalow", "wada", "room")

# Asked in this order; a full address often answers the later gaps too.
GAP_ORDER = ("address_too_short", "no_house_number", "missing_pincode")

# ---------------------------------------------------------------- rules (no AI)


def check_address(address):
    """Return a list of flags. Plain rules, no model involved."""
    addr = (address or "").strip()
    flags = []
    if len(addr) < 15 or len(re.findall(r"[A-Za-z]+", addr)) < 3:
        flags.append("address_too_short")
    if not re.search(r"\b\d{6}\b", addr):
        flags.append("missing_pincode")
    no_pin = re.sub(r"\b\d{6}\b", "", addr).lower()
    has_digit = re.search(r"\d", no_pin) is not None
    has_word = any(re.search(rf"\b{w}\b", no_pin) for w in HOUSE_WORDS)
    if not (has_digit or has_word):
        flags.append("no_house_number")
    return flags


FLAG_LABELS = {"address_too_short": "Address too short", "missing_pincode": "Missing pincode",
               "no_house_number": "No house number"}
_FLAG_HINTS = {"address_too_short": ("short", "incomplete", "full address"),
               "missing_pincode": ("pincode", "pin code", "pin"),
               "no_house_number": ("house", "flat", "building")}


def order_flags(order):
    """Gaps found by the rules, plus any the shop flagged by hand on the sheet
    (e.g. a "Flagged Concerns" cell saying "Missing Pincode")."""
    flags = check_address(order.get("address", ""))
    noted = (order.get("address_discrepancies") or "").lower()
    for flag, hints in _FLAG_HINTS.items():
        if flag not in flags and any(re.search(rf"\b{h}\b", noted) for h in hints):
            flags.append(flag)
    return [f for f in GAP_ORDER if f in flags]


def describe_flags(flags, already_noted=""):
    """Readable gap list for the sheet, keeping whatever the shop already wrote."""
    parts = [p.strip() for p in re.split(r"[;,]", already_noted or "") if p.strip()]
    for flag in flags:
        label = FLAG_LABELS[flag]
        if label.lower() not in (p.lower() for p in parts):
            parts.append(label)
    return "; ".join(parts)


def gap_resolved(flag, facts):
    if flag == "missing_pincode":
        return bool(facts["pincode"]) or bool(re.search(r"\d{6}", facts["full_address"]))
    if flag == "address_too_short":
        return bool(facts["full_address"])
    if flag == "no_house_number":
        return bool(facts["house_info"]) or bool(facts["area_has_no_house_numbers"])
    return True


def compose_updated_address(original, facts):
    """Merge what the customer said into the Address cell. Empty if nothing new."""
    original = (original or "").strip()
    if facts.get("full_address"):
        addr = facts["full_address"].strip()
    else:
        parts = []
        house = (facts.get("house_info") or "").strip()
        if house and house.lower() not in original.lower():
            parts.append(house)
        if original:
            parts.append(original)
        landmark = (facts.get("landmark") or "").strip()
        blob = " ".join(parts).lower()
        if landmark and landmark.lower() not in blob:
            parts.append(landmark if landmark.lower().startswith("near ") else f"near {landmark}")
        addr = ", ".join(p.strip(" ,") for p in parts if p)
    pin = (facts.get("pincode") or "").strip()
    if pin and not re.search(r"\b\d{6}\b", addr):
        addr = f"{addr}, {pin}" if addr else pin
    if facts.get("area_has_no_house_numbers") and "no house number" not in addr.lower():
        addr = f"{addr} (no house numbers in this area)" if addr else addr
    if addr.rstrip(" ,") == original.rstrip(" ,"):
        return ""  # nothing actually changed
    return addr


def format_address_notes(flags, facts):
    """Human-readable notes for the sheet: what was wrong + what the customer said."""
    bits = []
    if facts.get("house_info"):
        bits.append(f"house/building: {facts['house_info']}")
    if facts.get("area_has_no_house_numbers"):
        bits.append("customer says area has no house numbers")
    if facts.get("landmark"):
        bits.append(f"landmark: {facts['landmark']}")
    if facts.get("pincode"):
        bits.append(f"pincode given: {facts['pincode']}")
    if facts.get("full_address"):
        bits.append(f"full address restated: {facts['full_address']}")
    return "; ".join(bits)


# ---------------------------------------------------------------- what Sonali says
# Short lines on purpose: one idea per sentence, one question per turn.
# Hindi words are written in Devanagari so the voice pronounces them as Hindi;
# English words stay in Latin script so they are pronounced as English.

SCRIPTS = {
    "hindi": {
        "greet": ["नमस्ते {name} जी!", "मैं सोनाली, सस्टेनेबल यूनिवर्स से बोल रही हूँ।",
                  "आपके ऑर्डर के लिए बहुत-बहुत धन्यवाद!"],
        "confirm": "क्या आपने {product} का ऑर्डर किया था?",
        "confirm_amount": "{amount} रुपये का?",
        "confirm_retry": "क्या {product} का ऑर्डर आपने किया था? बस हाँ या ना बता दीजिए।",
        "not_heard": "हेलो? माफ़ कीजिए, आवाज़ नहीं आई।",
        "not_understood": "माफ़ कीजिए, मैं ठीक से समझ नहीं पाई।",
        "who_am_i": "मैं सोनाली, सस्टेनेबल यूनिवर्स से। आपके ऑर्डर के लिए कॉल किया है।",
        "no_answer": "कोई बात नहीं जी। हम बाद में फिर कॉल करेंगे। धन्यवाद!",
        "denied": "अच्छा जी, बताने के लिए धन्यवाद। हमारी टीम इसे जाँच लेगी।",
        "confirmed": "बहुत बढ़िया! आपका ऑर्डर जल्दी ही भेज दिया जाएगा। धन्यवाद जी!",
        "address_intro": ["बस एक छोटी सी बात।", "आपका पता है: {address}।"],
        "address_too_short": "क्या आप पूरा पता एक बार बता देंगे?",
        "no_house_number": "इसमें घर का नंबर नहीं दिख रहा। कोई नंबर या बिल्डिंग का नाम है?",
        "missing_pincode": "और आपका पिनकोड क्या है?",
        "address_why": "ये बस इसलिए, ताकि आपका ऑर्डर सही पते पर पहुँचे।",
        "address_done": "ठीक है जी, मैंने नोट कर लिया। आपके ऑर्डर के लिए फिर से धन्यवाद!",
        "address_done_unknown": "कोई बात नहीं जी, मैंने नोट कर लिया। आपके ऑर्डर के लिए फिर से धन्यवाद!",
        "colleague": "ठीक है जी। मेरी साथी जल्द ही आपसे संपर्क करेंगी। आपका दिन शुभ हो!",
        "filler": "जी, एक सेकंड, मैं देख लेती हूँ।",
    },
    "hinglish": {
        "greet": ["Hello {name} जी!", "मैं सोनाली, Sustainable Universe से।",
                  "आपके order के लिए thank you so much!"],
        "confirm": "आपने {product} order किया था ना?",
        "confirm_amount": "{amount} रुपये का?",
        "confirm_retry": "{product} का order आपने किया था? बस हाँ या ना बता दीजिए।",
        "not_heard": "Hello? Sorry, आवाज़ नहीं आई।",
        "not_understood": "Sorry जी, मैं ठीक से समझ नहीं पाई।",
        "who_am_i": "मैं सोनाली, Sustainable Universe से। आपके order के लिए call किया है।",
        "no_answer": "No problem जी। हम बाद में call कर लेंगे। Thank you!",
        "denied": "Okay जी, बताने के लिए thank you। हमारी team इसे check कर लेगी।",
        "confirmed": "Great! आपका order जल्दी dispatch हो जाएगा। Thank you so much!",
        "address_intro": ["बस एक छोटी सी बात।", "आपका address है: {address}।"],
        "address_too_short": "क्या आप full address एक बार बता देंगे?",
        "no_house_number": "इसमें house number नहीं दिख रहा। कोई number या building name है?",
        "missing_pincode": "और आपका pincode क्या है?",
        "address_why": "ये बस इसलिए, ताकि आपका order सही address पर पहुँचे।",
        "address_done": "Perfect जी, note कर लिया! आपके order के लिए फिर से thank you!",
        "address_done_unknown": "No problem जी, note कर लिया! आपके order के लिए फिर से thank you!",
        "colleague": "Okay जी। मेरी colleague आपसे जल्दी contact करेंगी। Have a nice day!",
        "filler": "जी, one second, मैं check कर लेती हूँ।",
    },
    "english": {
        "greet": ["Hello {name} ji!", "This is Sonali, from Sustainable Universe.",
                  "Thank you so much for your order!"],
        "confirm": "You ordered the {product}, right?",
        "confirm_amount": "For {amount} rupees?",
        "confirm_retry": "Did you place the order for the {product}? Just a yes or no is fine.",
        "not_heard": "Hello? Sorry, I couldn't hear you.",
        "not_understood": "Sorry, I didn't quite catch that.",
        "who_am_i": "This is Sonali, from Sustainable Universe. I'm calling about your order.",
        "no_answer": "No problem at all. We will call you later. Thank you!",
        "denied": "Okay, thanks for telling me. Our team will check this.",
        "confirmed": "Lovely! Your order will be on its way soon. Thank you so much!",
        "address_intro": ["Just one small thing.", "I have your address as: {address}."],
        "address_too_short": "Could you tell me the full address once?",
        "no_house_number": "I can't see a house number. Is there a number, or a building name?",
        "missing_pincode": "And what is your pincode?",
        "address_why": "It's only so that your order reaches the right address.",
        "address_done": "Perfect, I've noted that. Thank you again for your order, ji!",
        "address_done_unknown": "No problem, I've noted that. Thank you again for your order!",
        "colleague": "Okay, my colleague will get in touch with you shortly. Have a nice day!",
        "filler": "One second, let me just check.",
    },
}

MAX_TRIES = 3  # times Sonali asks one question before handing over to a colleague


def static_lines(lang):
    """Lines with no customer details in them, rendered to audio once at start-up."""
    out = []
    for value in SCRIPTS[lang].values():
        out.extend(v for v in (value if isinstance(value, list) else [value]) if "{" not in v)
    return out

# ---------------------------------------------------------------- language of a reply

_HINDI_WORDS = {
    "haan", "han", "haa", "ji", "jee", "nahin", "nahi", "nai", "hai", "hain", "tha", "thi",
    "kiya", "kia", "maine", "mainne", "mera", "meri", "mere", "hamara", "hamare",
    "aap", "aapka", "aapne", "ka", "ki", "ke", "ko", "mein", "se", "par", "yah",
    "yeh", "ye", "vah", "voh", "wo", "koi", "kuchh", "kuch", "bilkul", "theek", "thik",
    "achchha", "accha", "acha", "ghar", "naam", "paas", "hoon", "hun", "hum", "kar",
    "diya", "raha", "rahi", "nahin", "kaun", "kya", "kyon", "lekin", "aur", "bhi",
}
_ENGLISH_WORDS = {
    "yes", "yeah", "no", "not", "never", "i", "i'm", "it", "it's", "is", "was", "my",
    "did", "didn't", "placed", "ordered", "this", "that", "we", "our", "have", "has",
    "near", "please", "who", "what", "where", "there", "here", "you", "your", "sure",
    "correct", "right", "wrong", "is", "are", "and", "but", "number", "only", "name",
}


def guess_language(text):
    """Cheap fallback when the model gives no language: hindi / hinglish / english / None."""
    if re.search(r"[\u0900-\u097F]", text or ""):
        return "hinglish" if re.search(r"[A-Za-z]{3,}", text) else "hindi"
    words = re.findall(r"[a-z']+", (text or "").lower())
    hi = sum(w in _HINDI_WORDS for w in words)
    en = sum(w in _ENGLISH_WORDS and w not in _HINDI_WORDS for w in words)
    if not hi and not en:
        return None
    if hi and en and min(hi, en) / (hi + en) >= 0.2:
        return "hinglish"
    return "hindi" if hi >= en else "english"


# ---------------------------------------------------------------- the brains

# Each system prompt is read once (OllamaBrain.warm_up) and Ollama keeps it
# cached, so per turn Gemma only reads the short user message (~1 s on a laptop
# CPU instead of ~5 s). That is why these prompts can afford rules and examples.
# Gemma answers with a label word first. Only when the reply is off-script does
# it go on to write what Sonali says back, so a clear answer costs one token.
_ABOUT = ("Sonali from Sustainable Universe, a small natural haircare shop, is on a "
          "short thank-you call with a customer. Hindi may be written in Latin letters.")
_RUDE_RULE = ("rude - swearing, insults, or sexual or flirty talk. A question back, "
              "doubt, refusal or impatience is NOT rude.")
_SAY_BACK = (
    "For other only, write line 2: what Sonali says back to it. Warm and helpful, "
    "one sentence of at most 15 words, in the language given. No question (Sonali "
    "asks again right after it). Sonali is a person: never mention AI. Promise nothing.")

CONFIRM_SYSTEM = f"""{_ABOUT} Sonali asked if they placed their order.

Line 1 is one word:
yes - they ordered it, or someone they know or live with did, even if not sure who ("maybe my daughter did", "ghar pe kisi ne kiya hoga", "haan wife ne kiya"), or they accept the order
no - nobody they know ordered it, or they don't want it
{_RUDE_RULE}
other - anything else: a question, doubt, off-topic, hard to tell

{_SAY_BACK}
Examples of line 2: "Who is this?" -> "I'm Sonali from Sustainable Universe, calling about your recent order." / "Which order?" -> "It's the order placed on our website a few days ago." / "I'm busy" -> "I'll be very quick, it's just one small thing."
"""
ADDRESS_SYSTEM = f"""{_ABOUT} Sonali asked for a missing detail of their delivery address.

Line 1 is one word:
answered - they gave what was asked, or said there is none ("no house numbers here")
dont_know - they don't know it
{_RUDE_RULE}
other - anything else: "why?", refusal, off-topic

{_SAY_BACK}
Examples of line 2: "Why should I tell you?" -> "It's only so that your parcel reaches you safely and on time." / "Is this a scam?" -> "Not at all, we only need it to deliver your order." / "I'm busy" -> "I'll be very quick, it's just for your delivery."
"""
CONFIRM_LABELS = {"yes": "confirmed", "no": "denied", "rude": "rude", "other": "other"}
ADDRESS_LABELS = {"answered": "answered", "dont_know": "dont_know", "rude": "rude",
                  "other": "other"}
SAY_BACK_LANGUAGE = {
    "hindi": "If you write line 2, write it in Hindi using Devanagari letters only "
             "(write 'आपका ऑर्डर', never 'aapka order').",
    "hinglish": "If you write line 2, write Hindi words in Devanagari letters and English "
                "words in Latin (write 'आपका order', never 'aapka order').",
    "english": "If you write line 2, write it in English.",
}

EXTRACT_SYSTEM = (
    "Extract address facts from a customer's reply (Hindi may be in Latin "
    "letters). JSON only, Latin script, include only keys they actually said: "
    '"house" (house/flat number or building name), "no_numbers" (true if their '
    'area has no house numbers), "landmark", "pin" (6 digits; convert number '
    'words like "chaar ek ek"), "full" (whole address, if restated).'
)

TRANSLIT_SYSTEM = (
    "Rewrite each field in Devanagari exactly as an Indian would pronounce it "
    "aloud in a Hindi phone call. Transliterate, do not translate: 'Rosemary "
    "Conditioner' -> 'रोज़मेरी कंडीशनर', 'Pune' -> 'पुणे'. Write 'ml' as "
    "'एम एल' and '+' as 'और'. Keep all digits as ASCII digits. Reply with "
    'JSON {"name": ..., "product": ..., "address": ...}.'
)

_YES = {"yes", "yeah", "yep", "haan", "han", "haa", "ha", "correct", "bilkul",
        "right", "हाँ", "हां"}
_YES_PHRASES = ("maine hi", "maine kiya", "kiya tha", "kiya hai", "i did", "i ordered",
                "i placed", "that was me", "it was me", "मैंने किया", "किया था")
_NO = {"no", "nope", "nahin", "nahi", "nai", "na", "never", "didn't", "didnt", "नहीं", "नही"}
# Anything that might change the meaning goes to Gemma instead. ("Haan, wife ne
# kiya" is still a yes, so family words are not on this list.)
_HEDGE = {"shayad", "maybe", "perhaps", "who", "kaun", "kisne", "hoga", "someone",
          "what", "why", "kyon", "kyun", "kya", "but", "lekin", "magar", "actually",
          "wait", "ruko", "sorry", "wrong", "galat", "cancel"}
_DONT_KNOW = ("pata nahi", "pata nahin", "yaad nahi", "yaad nahin", "maloom nahi",
              "malum nahi", "don't know", "dont know", "do not know", "no idea", "not sure",
              "पता नहीं", "याद नहीं")

# Clear profanity and sexual talk end the call at once; Gemma catches the subtler cases.
_RUDE = set("""
fuck fucking fucker fuk shit bitch bastard asshole dick cock pussy slut whore sexy sex
nude nudes boobs horny porn chutiya chutiye chutia madarchod maderchod behenchod bhenchod
benchod bsdk bhosdike bhosdi bhosda gandu gaand randi harami haramkhor kutte kutti kamina
kamine kaminey lavda lauda lawda lund chod chodu saali mc bc
चूतिया चुतिया मादरचोद बहनचोद भेनचोद भोसड़ी भोसडी गांडू गांड रंडी हरामी कमीना कमीने लौड़ा लंड साली
""".split())


def asks_who(reply):
    low = (reply or "").lower()
    return bool(set(re.findall(r"[\w']+", low)) & {"kaun", "who", "कौन"}) or "kahan se" in low


def is_rude(reply):
    return bool(set(re.findall(r"[\w']+", (reply or "").lower())) & _RUDE)


def says_dont_know(reply):
    low = (reply or "").lower()
    return any(p in low for p in _DONT_KNOW)


_FAMILY = set("""
daughter son wife husband brother sister mother mom mum mummy father dad papa family
beti beta bete betey biwi patni pati bhai bhaiya behen bahen didi maa mummy ammi abbu
bhabhi bahu saas sasur chacha mama nana dada gharwale gharwalon household
""".split())


def family_ordered(reply):
    """'Maybe my daughter did it', 'pata nahi, shayad wife ne kiya ho': someone in
    the family placed it, which counts as a yes. Not when they say no, or ask."""
    low = (reply or "").lower()
    for phrase in _DONT_KNOW:
        low = low.replace(phrase, " ")
    words = set(re.findall(r"[\w']+", low))
    return bool(words & _FAMILY) and not words & _NO and "?" not in low


def quick_intent(reply):
    """Plain, clear answers need no model. Returns None when Gemma should decide."""
    if is_rude(reply):
        return "rude"
    if family_ordered(reply):
        return "confirmed"
    low = reply.lower()
    words = set(re.findall(r"[\w']+", low))
    if len(words) > 12 or words & _HEDGE or says_dont_know(reply):
        return None
    yes_word, no = bool(words & _YES), bool(words & _NO)
    if no:
        # "maine nahi kiya tha" contains "kiya tha", so only a plain yes word conflicts
        return None if yes_word else "denied"
    if yes_word or any(p in low for p in _YES_PHRASES):
        return "confirmed"
    return None


_DIGIT_WORDS = {
    "zero": "0", "shoony": "0", "shoonya": "0", "shunya": "0", "sunya": "0", "sifar": "0",
    "one": "1", "ek": "1", "two": "2", "do": "2", "three": "3", "teen": "3", "tin": "3",
    "four": "4", "chaar": "4", "char": "4", "five": "5", "paanch": "5", "panch": "5",
    "six": "6", "chhah": "6", "chhe": "6", "chhai": "6", "che": "6", "seven": "7", "saat": "7",
    "sat": "7", "eight": "8", "aath": "8", "ath": "8", "nine": "9", "nau": "9", "no": "9",
}


def spoken_digits_to_numbers(text):
    """'chaar ek ek shoony shoony paanch' -> '411005'. Only runs of 3+ digits are
    joined, so words like 'do' (give) or 'no' stay as they are elsewhere."""
    tokens = re.findall(r"\w+|\W+", text)
    out, run = [], []

    def flush():
        digits = [d for _, d, _ in run]
        if len("".join(digits)) >= 3:
            out.append("".join(digits))
            if run and run[-1][2]:
                out.append(run[-1][2])
        else:
            out.extend(word + sep for word, _, sep in run)
        run.clear()

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        digit = _DIGIT_WORDS.get(tok.lower()) or (tok if tok.isdigit() and len(tok) == 1 else None)
        if digit:
            sep = tokens[i + 1] if i + 1 < len(tokens) and not tokens[i + 1].strip(" ,-") else ""
            run.append((tok, digit, sep))
            i += 2 if sep else 1
            continue
        flush()
        out.append(tok)
        i += 1
    flush()
    return "".join(out).strip()


def blank_facts():
    return {"house_info": "", "area_has_no_house_numbers": False, "landmark": "",
            "pincode": "", "full_address": ""}


def reply_language(reply, model_guess=None):
    """Word lists first (instant, and they ignore address-only replies like
    'Plot 18, Noida 201309'); the model's label only when the words say nothing."""
    return guess_language(reply) or (model_guess if model_guess in LANGUAGES else None)


_NO_NUMBER_PHRASES = (
    "no house number", "don't have house number", "dont have house number",
    "don't have a house number", "no number", "only the building name", "only building name",
    "ghar number nahin", "number nahin hota", "number nahin hai", "sirf building",
)


_NOT_ADDRESS_WORDS = set(
    "the my our is it its it's pin pincode code number no. hai mera hamara apna "
    "ji haan yes ok okay sure toh to wo woh".split())


def rule_facts(reply):
    """What plain code can read from an address answer instantly (pincode, a
    restated address, 'we have no house numbers'). Gemma adds the rest."""
    low = reply.lower()
    pin = re.search(r"\b\d{6}\b", reply)
    address_words = [w for w in re.findall(r"[\w'-]+", low)
                     if w not in _NOT_ADDRESS_WORDS and not w.isdigit()]
    return {
        "house_info": "",
        "area_has_no_house_numbers": any(p in low for p in _NO_NUMBER_PHRASES),
        "landmark": "",
        "pincode": pin.group(0) if pin else "",
        "full_address": reply.strip() if pin and len(address_words) >= 3 else "",
    }


_ANSWER_HOUSE_WORDS = set(HOUSE_WORDS) | {"ghar", "makan", "makaan", "society", "tower",
                                          "wing", "floor", "gali", "lane", "chawl"}


def rules_answer_gap(flag, reply, facts):
    """True when plain code can already tell this answer is a real one, so the
    next question needn't wait for Gemma (which still reads it in the background)."""
    if gap_resolved(flag, facts):
        return True
    low = reply.lower()
    tokens = re.findall(r"[\w'-]+", low)
    has_house = bool(set(tokens) & _ANSWER_HOUSE_WORDS) or bool(
        re.search(r"\d", re.sub(r"\b\d{6}\b", "", low)))
    if flag == "no_house_number":
        return has_house
    if flag == "address_too_short":
        words = [w for w in tokens if w not in _NOT_ADDRESS_WORDS and not w.isdigit()]
        return has_house and len(words) >= 3
    return False


def merge_facts(facts, new):
    for k, v in new.items():
        if v and not facts[k]:
            facts[k] = v


def judge_prompt(question, reply, lang):
    # The question is passed in English: fewer tokens for Gemma to read than Devanagari.
    return f"Sonali: {question}\nCustomer: {reply}\n({SAY_BACK_LANGUAGE[lang]})"


def clean_say_back(text, lang):
    """Gemma's line for Sonali, or "" if it isn't safe to say aloud (the script's
    own retry line is used then)."""
    text = re.sub(r"^\W*(line\s*2\W*)?", "", (text or "").strip(), flags=re.I)
    text = text.split("\n")[0].strip().strip('"\'“”').replace("{", "").replace("}", "")
    # Sonali asks the scripted question herself next, so Gemma's own questions go
    text = " ".join(s for s in re.split(r"(?<=[.!?।])\s+", text) if "?" not in s).strip()
    if not text or len(text.split()) > 25 or re.search(
            r"\b(AI|bot|robot|model|language model|assistant)\b", text, re.I):
        return ""
    if lang != "english" and not re.search(r"[\u0900-\u097F]", text):
        return ""  # Hindi voice needs Devanagari to pronounce Hindi words right
    if lang == "english" and re.search(r"[\u0900-\u097F]", text):
        return ""
    return text


class OllamaBrain:
    """Calls a local Ollama server. Temperature 0 and a JSON schema keep a
    small model's output predictable."""

    def __init__(self, model, verbose=False):
        self.model = model
        self.verbose = verbose

    def _open(self, system, user, num_predict, json_mode, stream):
        body = {
            "model": self.model,
            "stream": stream,
            "think": False,          # thinking models would burn the token cap on reasoning
            "keep_alive": "30m",     # keep the model loaded between turns
            # Every request must use the same options, or Ollama reloads the model.
            "options": {"temperature": 0, "num_ctx": 1024, "num_predict": num_predict},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if json_mode:
            # Plain JSON mode: a full JSON schema roughly doubled Gemma's time per
            # turn on CPU, so values are validated in Python instead.
            body["format"] = "json"
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            sys.exit(f"Ollama error {e.code}: {e.read().decode(errors='replace')}\n"
                     f"Is the model pulled? Try: ollama pull {self.model}")
        except urllib.error.URLError as e:
            sys.exit(f"Cannot reach Ollama at {OLLAMA_URL} ({e.reason}). "
                     "Start it (ollama serve) or run with --mock.")

    def _log(self, t0, data, note=""):
        if self.verbose:
            print(f"   [gemma] {data.get('prompt_eval_count', '?')} tokens read, "
                  f"{data.get('eval_count', '?')} written, {time.time() - t0:.1f}s{note}")

    def _chat(self, system, user, num_predict=200, json_mode=True):
        t0 = time.time()
        with self._open(system, user, num_predict, json_mode, stream=False) as resp:
            data = json.load(resp)
        self._log(t0, data)
        content = (data.get("message") or {}).get("content", "")
        if not json_mode:
            return content
        try:
            out = json.loads(content)
            return out if isinstance(out, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _judge(self, system, user, labels):
        """Streams Gemma's answer. Line 1 is a label; the stream is cut as soon as
        it is known, unless it is "other", when Gemma goes on to write Sonali's
        reply. Returns (label, reply_text)."""
        t0, text, data = time.time(), "", {}
        with self._open(system, user, 60, json_mode=False, stream=True) as resp:
            for raw in resp:
                data = json.loads(raw)
                text += (data.get("message") or {}).get("content", "")
                m = re.match(r"\W*([a-z_]+)[^a-z_]", text.lower())
                if data.get("done") or (m and m.group(1) != "other"):
                    break
        word = (re.findall(r"[a-z_]+", text.lower()) or ["other"])[0]
        label = labels.get(word, "other")
        self._log(t0, data if data.get("done") else {}, f" -> {label}")
        rest = text.split("\n", 1)[1] if "\n" in text else ""
        return label, rest if label == "other" else ""

    def warm_up(self):
        """Load Gemma (a cold load takes ~30 s) and have it read each system prompt
        once, so Ollama caches them and every turn only reads the new words.
        Called again before each call, as the cache slowly drifts."""
        t0 = time.time()
        verbose, self.verbose = self.verbose, False
        self._chat(CONFIRM_SYSTEM, "Sonali:", num_predict=1, json_mode=False)
        self._chat(ADDRESS_SYSTEM, "Sonali:", num_predict=1, json_mode=False)
        self._chat(EXTRACT_SYSTEM, "Customer reply:", num_predict=1)
        self.verbose = verbose
        return time.time() - t0

    def judge_confirm(self, reply, question, lang):
        """Did they confirm the order? Returns (confirmed / denied / rude / other,
        what Sonali says back when it's other)."""
        label, back = self._judge(CONFIRM_SYSTEM, judge_prompt(question, reply, lang),
                                  CONFIRM_LABELS)
        return label, clean_say_back(back, lang)

    def judge_address(self, reply, question, lang):
        """Returns (answered / dont_know / rude / other, Sonali's reply for other)."""
        label, back = self._judge(ADDRESS_SYSTEM, judge_prompt(question, reply, lang),
                                  ADDRESS_LABELS)
        return label, clean_say_back(back, lang)

    def extract(self, reply):
        """Address facts from a reply (read in the background while the call goes on)."""
        out = self._chat(EXTRACT_SYSTEM, f"Customer reply: {reply}")

        def text(key):
            v = out.get(key)
            return v.strip() if isinstance(v, str) and v.strip().lower() not in ("null", "none") else ""

        pin = re.sub(r"\D", "", text("pin"))
        facts = {
            "house_info": text("house"),
            "area_has_no_house_numbers": out.get("no_numbers") is True,
            "landmark": text("landmark"),
            "pincode": pin if len(pin) == 6 else "",
            "full_address": text("full"),
        }
        return facts

    def transliterate(self, fields):
        out = self._chat(TRANSLIT_SYSTEM, json.dumps(fields, ensure_ascii=False), num_predict=300)
        return {k: (out.get(k) if isinstance(out.get(k), str) and out.get(k) else v)
                for k, v in fields.items()}


class MockBrain:
    """Keyword rules, NOT a model. Only for testing the flow without Ollama."""

    def warm_up(self):
        return 0.0

    def judge_confirm(self, reply, question, lang):
        return quick_intent(reply) or "other", ""

    def judge_address(self, reply, question, lang):
        if is_rude(reply):
            return "rude", ""
        if says_dont_know(reply):
            return "dont_know", ""
        f = self.extract(reply)
        return ("answered" if f["house_info"] or f["pincode"] or f["full_address"]
                or f["area_has_no_house_numbers"] else "other"), ""

    def extract(self, reply):
        facts = rule_facts(reply)
        house = re.search(r"\b(flat|house|plot|room)\s*(no\.?)?\s*[\w-]*\d[\w-]*", reply, re.I)
        near = re.search(r"near ([\w ]+)", reply, re.I)
        facts["house_info"] = house.group(0) if house else ""
        facts["landmark"] = near.group(1).strip() if near else ""
        return facts

    def transliterate(self, fields):
        return dict(fields)


# ---------------------------------------------------------------- the script


class Replies:
    """Customer replies come from the keyboard, or from a JSON file for demos."""

    def __init__(self, path=None):
        self.data = None
        if path:
            with open(path, encoding="utf-8-sig") as f:
                self.data = json.load(f)
        self.pos = {}

    def get(self, order_id, filler=None):
        if self.data is None:
            return input("CUSTOMER: ").strip()
        seq = self.data.get(order_id, [])
        i = self.pos.get(order_id, 0)
        self.pos[order_id] = i + 1
        reply = seq[i] if i < len(seq) else ""
        print(f"CUSTOMER: {reply}")
        return reply


SPOKEN_CACHE = Path(__file__).parent / "cache" / "spoken.json"


class SpokenCache:
    """Devanagari spellings already worked out, so re-runs skip Gemma."""

    def __init__(self, path=SPOKEN_CACHE):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def get(self, fields, brain):
        key = json.dumps(fields, ensure_ascii=False, sort_keys=True)
        if key not in self.data:
            self.data[key] = brain.transliterate(fields)
            if not isinstance(brain, MockBrain):
                self.path.parent.mkdir(exist_ok=True)
                self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        return self.data[key]


def spoken_fields(order, brain, cache=None):
    """Name, product and address per language, as Sonali should say them."""
    latin = {
        "name": (order["customer_name"] or "").split()[0] or order["customer_name"],
        "product": order["product"],
        "address": order["address"],
    }
    dev = (cache or SpokenCache()).get(latin, brain)
    return {
        "hindi": dev,
        # English product names sound right in English; Indian names and places in Hindi
        "hinglish": {**dev, "product": latin["product"]},
        "english": latin,
    }


def confirm_question(script, order):
    """The order question, with the amount only if the sheet has one."""
    return script["confirm"] + (" " + script["confirm_amount"] if order.get("amount") else "")


def opening_lines(order):
    """This customer's lines in the opening language, ready to render before dialling."""
    s, fill = SCRIPTS[OPENING_LANGUAGE], order["_spoken"][OPENING_LANGUAGE]
    fill = {**fill, "amount": order.get("amount", "")}
    lines = s["greet"] + [confirm_question(s, order), s["confirm_retry"]]
    if order_flags(order):
        lines += s["address_intro"]
    return [line.format(**fill) for line in lines]


def prepare_order(order, brain, cache=None, speaker=None):
    """Before dialling: Devanagari names via Gemma, and the opening lines rendered
    to audio, so the customer never hears dead air."""
    order["_spoken"] = spoken_fields(order, brain, cache)
    brain.warm_up()  # transliteration just pushed the cached prompts aside
    if speaker is not None and hasattr(speaker, "prerender"):
        speaker.prerender(opening_lines(order), OPENING_LANGUAGE)
    return order


class CallEnded(Exception):
    """Raised by a replies source when the customer hangs up mid-call."""


def new_result(order, status="", why=""):
    flags = order_flags(order)
    return {
        "order_id": order["order_id"],
        "customer_name": order["customer_name"],
        "status": status,
        "call_done": ("RETRY" if status == "NO_ANSWER" else "DONE") if status else "",
        "customer_language": "",
        "called_at": time.strftime("%Y-%m-%d %H:%M") if status else "",
        "address_discrepancies": describe_flags(flags, order.get("address_discrepancies", "")),
        "address_notes": "",
        "call_notes": why,
        "original_address": order.get("address", ""),
        **blank_facts(),
        "transcript": "",
    }


def run_order(order, brain, replies, speak=None):
    """Sonali's thank-you call. Opens in Hindi, then answers in the customer's language."""
    oid = order["order_id"]
    flags = order_flags(order)
    spoken = order.get("_spoken") or spoken_fields(order, brain)
    facts = blank_facts()
    log = []
    answers, unknown = [], []
    state = {"lang": OPENING_LANGUAGE, "heard": False}
    result = new_result(order)

    def line(key):
        return SCRIPTS[state["lang"]][key]

    def say(text):
        text = text.format(**spoken[state["lang"]], amount=order.get("amount", ""))
        print(f"SONALI [{state['lang']}]: {text}")
        log.append(f"SONALI: {text}")
        if speak:
            speak(text, state["lang"])

    def ask(text):
        say(text)
        reply = replies.get(oid, filler=SCRIPTS[state["lang"]]["filler"])
        log.append(f"CUSTOMER: {reply}")
        state["heard"] = state["heard"] or bool(reply)
        return reply

    def follow(lang):
        if lang in LANGUAGES and lang != state["lang"]:
            print(f"   [language] customer is speaking {lang}; switching")
            state["lang"] = lang
        result["customer_language"] = state["lang"]

    background = ThreadPoolExecutor(max_workers=1)
    reading = []
    thinker = ThreadPoolExecutor(max_workers=1)

    def think(fn, *args):
        """Gemma's turn. The hold line only plays if Gemma is not done in a moment."""
        job = thinker.submit(fn, *args)
        try:
            return job.result(timeout=0.7)
        except FuturesTimeout:
            say(line("filler"))
            return job.result()

    def finish(status, key=None, why=""):
        if key:
            say(line(key))
        for job in reading:          # Gemma's background reads of address answers
            merge_facts(facts, job.result())
        background.shutdown()
        thinker.shutdown()
        result.update(facts)
        result["status"] = status
        # NO_ANSWER stays pending so the next run calls them again
        result["call_done"] = "RETRY" if status == "NO_ANSWER" else "DONE"
        result["called_at"] = time.strftime("%Y-%m-%d %H:%M")
        notes = [format_address_notes(flags, facts)] if flags else []
        if unknown:
            notes.append("customer did not know: " + ", ".join(unknown))
        if answers:
            notes.append("customer said: " + " / ".join(f'"{a}"' for a in answers))
        result["address_notes"] = "; ".join(n for n in notes if n)
        result["call_notes"] = why
        result["transcript"] = " | ".join(log)
        return result

    def hand_over(why):
        return finish("NEEDS_HUMAN_REVIEW", "colleague", why)

    def ended_rude():
        return finish("ENDED_INAPPROPRIATE", "colleague", "ended politely: inappropriate language")

    def converse():
        # ---- 1. thank them and confirm the order (up to MAX_TRIES asks)
        for text in line("greet"):
            say(text)
        reply, intent, say_back = "", None, ""
        english_question = SCRIPTS["english"]["confirm"].format(**spoken["english"])
        for attempt in range(MAX_TRIES):
            if attempt == 0:
                question = confirm_question(SCRIPTS[state["lang"]], order)
            elif not reply:
                question = line("not_heard") + " " + line("confirm_retry")
            else:
                # Gemma's reply to what they said, else the script's own line
                lead = say_back or line("who_am_i" if asks_who(reply) else "not_understood")
                question = lead + " " + line("confirm_retry")
            reply = ask(question)
            if not reply:
                continue
            follow(reply_language(reply))
            intent = quick_intent(reply)
            plain = intent is not None and not family_ordered(reply)
            if intent is None:
                intent, say_back = think(brain.judge_confirm, reply, english_question,
                                         state["lang"])
            if intent == "confirmed" and not plain:
                state["confirm_note"] = f'confirmed by: "{reply}"'
            if intent != "other":
                break
        if intent == "rude":
            return ended_rude()
        if intent == "denied":
            return finish("DENIED_REVIEW", "denied", f'customer said: "{reply}"')
        if intent != "confirmed":
            if not state["heard"]:
                return finish("NO_ANSWER", "no_answer", f"no reply after {MAX_TRIES} tries")
            return hand_over(f"no clear answer after {MAX_TRIES} tries")
        if not flags:
            return finish("CONFIRMED", "confirmed", state.get("confirm_note", ""))

        # ---- 2. address gaps, one question at a time (up to MAX_TRIES asks each).
        # Plain rules accept a clear answer instantly and Gemma reads it in the
        # background; only answers the rules can't place wait for Gemma.
        for text in line("address_intro"):
            say(text)
        for flag in GAP_ORDER:
            if flag not in flags or gap_resolved(flag, facts):
                continue
            reply, answered, say_back = "", False, ""
            for attempt in range(MAX_TRIES):
                if attempt == 0:
                    question = line(flag)
                elif not reply:
                    question = line("not_heard") + " " + line(flag)
                else:
                    lead = say_back or line("address_why" if attempt == 1 else "not_understood")
                    question = lead + " " + line(flag)
                reply = ask(question)
                if not reply:
                    continue
                if is_rude(reply):
                    return ended_rude()
                reply = spoken_digits_to_numbers(reply)
                follow(reply_language(reply))
                merge_facts(facts, rule_facts(reply))
                if flag == "no_house_number" and quick_intent(reply) == "denied":
                    facts["area_has_no_house_numbers"] = True  # "No, there isn't one"
                if says_dont_know(reply):
                    status = "dont_know"
                elif rules_answer_gap(flag, reply, facts):
                    status = "answered"
                else:
                    status, say_back = think(brain.judge_address, reply,
                                             SCRIPTS["english"][flag], state["lang"])
                    if (status == "answered" and flag == "address_too_short"
                            and len(re.findall(r"[^\W\d_]{2,}", reply)) < 2):
                        status = "other"  # a pincode alone is not the full address
                if status == "rude":
                    return ended_rude()
                if status in ("answered", "dont_know"):
                    answers.append(reply)
                    if status == "dont_know":
                        unknown.append(flag)
                    # Gemma reads the facts out of it while the call goes on
                    reading.append(background.submit(brain.extract, reply))
                    answered = True
                    break
            if not answered:
                return hand_over(f"{flag} not answered after {MAX_TRIES} tries")

        say(line("address_done_unknown" if unknown else "address_done"))
        done = finish("CONFIRMED_ADDRESS_UPDATED", None, state.get("confirm_note", ""))
        if not all(gap_resolved(f, facts) for f in flags):
            done.update(status="NEEDS_HUMAN_REVIEW",
                        call_notes="address answered, but some gaps still need checking")
        return done

    try:
        return converse()
    except CallEnded:
        print("   [phone] customer hung up")
        if state["heard"]:
            return finish("NEEDS_HUMAN_REVIEW", None, "customer hung up during the call")
        return finish("NO_ANSWER", None, "line dropped before any reply")


# --------------------------------------------------------- where results go


def outcome_fields(result):
    """Fields passed to the store. Excel then writes only the cells this case needs."""
    updated = compose_updated_address(result.get("original_address", ""), result)
    shop = sheets.shop_columns({
        "call_done": result["call_done"],
        "call_status": result["status"],
        "call_notes": result.get("call_notes", ""),
        "updated_address": updated,
    })
    return {
        "call_done": shop.get("call_done", result["call_done"]),
        "call_status": result["status"],
        "customer_language": result["customer_language"],
        "called_at": result["called_at"],
        "address_discrepancies": result["address_discrepancies"],
        "address_notes": result["address_notes"],
        "call_notes": result["call_notes"],
        "agent_review": shop.get("agent_review", ""),
        "updated_address": updated,
        "address": shop.get("address", ""),
    }


SHEET_CONFIG = Path(__file__).parent / "sheet.json"  # {"url": ..., "token": ...}, git-ignored
DEFAULT_EXCEL = Path(__file__).parent / "Sonali_SU_DB.xlsx"


def open_orders_file(path):
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        return sheets.ExcelOrders(p)
    return sheets.CsvOrders(str(p))


def open_store(args):
    """Local Excel workbook by default (Sonali_SU_DB.xlsx). --orders picks a
    CSV or xlsx; --sheet-url still talks to a Google Sheet if you want one."""
    if args.sheet_url:
        token = os.environ.get("SHEET_TOKEN", "")
        if SHEET_CONFIG.exists():
            token = token or json.loads(SHEET_CONFIG.read_text(encoding="utf-8")).get("token", "")
        if not token:
            sys.exit("Set SHEET_TOKEN to the TOKEN in google_sheets/Code.gs.")
        return sheets.AppsScriptOrders(args.sheet_url, token)
    if args.orders:
        return open_orders_file(args.orders)
    if DEFAULT_EXCEL.exists():
        return sheets.ExcelOrders(DEFAULT_EXCEL)
    saved = {}
    if SHEET_CONFIG.exists():
        saved = json.loads(SHEET_CONFIG.read_text(encoding="utf-8"))
    url = os.environ.get("SHEET_URL", "") or saved.get("url", "")
    if url:
        token = os.environ.get("SHEET_TOKEN", "") or saved.get("token", "")
        if not token:
            sys.exit("Set SHEET_TOKEN to the TOKEN in google_sheets/Code.gs.")
        return sheets.AppsScriptOrders(url, token)
    return sheets.CsvOrders("sample_orders.csv")


def record_outcome(store, order, result, webhook=None, write_back=True):
    if write_back:
        fields = outcome_fields(result)
        try:
            written = store.mark(order["order_id"], fields)
            shop = sheets.shop_columns(fields)
            bits = [f"Verified/Called={shop.get('call_done', fields['call_done'])}"]
            if shop.get("agent_review"):
                bits.append(f"Agent Call Review={shop['agent_review']}")
            if shop.get("address"):
                bits.append(f"Address={shop['address']}")
            extra = f"  ({', '.join(written)})" if written else ""
            print(f"   [sheet] {'; '.join(bits)}{extra}")
        except Exception as e:
            print(f"   [sheet] could not mark order {order['order_id']}: {e}")
    post_webhook(webhook, order, result)


def write_results_csv(path, results):
    # utf-8-sig so Excel shows Devanagari transcripts correctly
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)


def post_webhook(url, order, result):
    """POST one call outcome to an n8n (or any) webhook."""
    if not url:
        return
    payload = {
        "order_id": result["order_id"],
        **outcome_fields(result),
        "customer_name": order.get("customer_name", ""),
        "phone": order.get("phone", ""),
        "address": order.get("address", ""),
        "product": order.get("product", ""),
        "amount": order.get("amount", ""),
        "transcript": result.get("transcript", ""),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print(f"   [webhook] posted order {result['order_id']}")
    except Exception as e:
        print(f"   [webhook] failed for {result['order_id']}: {e}")


def use_utf8_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def add_common_args(ap, default_out):
    ap.add_argument("--orders", help="local Excel (.xlsx) or CSV of orders "
                                     "(default: Sonali_SU_DB.xlsx if present)")
    ap.add_argument("--sheet-url", help="optional Google Sheet web app URL; "
                                        "token comes from SHEET_TOKEN")
    ap.add_argument("--out", default=default_out, help="full results with transcripts")
    ap.add_argument("--only", help="call a single order_id / row number (even if already done)")
    ap.add_argument("--limit", type=int, help="call at most this many pending orders (demo: 1)")
    ap.add_argument("--redo", action="store_true", help="also call orders already marked Yes/DONE")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model, e.g. gemma4:e4b")
    ap.add_argument("--verbose", action="store_true", help="print model timings")
    ap.add_argument("--no-write-back", action="store_true",
                    help="do not mark outcomes on the orders sheet")
    ap.add_argument("--webhook", default=os.environ.get("N8N_WEBHOOK_URL", ""),
                    help="also POST each outcome to this URL (e.g. an n8n webhook)")


def main():
    use_utf8_console()
    ap = argparse.ArgumentParser(description="Order verification assistant, text version")
    add_common_args(ap, "results.csv")
    ap.add_argument("--replies", help="JSON file of scripted customer replies (demo/test)")
    ap.add_argument("--mock", action="store_true", help="keyword rules instead of a model (testing only)")
    args = ap.parse_args()

    store = open_store(args)
    orders = store.load(args.only, args.redo, args.limit)
    if not orders:
        sys.exit(f"No pending orders in {store.describe()}.")
    if args.mock:
        print("MOCK MODE: keyword rules, no model. For testing the flow only.\n")
        brain = MockBrain()
    else:
        print(f"Using local model {args.model} via Ollama at {OLLAMA_URL}")
        brain = OllamaBrain(args.model, args.verbose)
        print(f"Gemma ready in {brain.warm_up():.0f}s\n")
    replies = Replies(args.replies)
    cache = SpokenCache()

    results = []
    for order in orders:
        print(f"\n--- Order {order['order_id']} (address gaps: "
              f"{order_flags(order) or 'none'})")
        prepare_order(order, brain, cache)
        res = run_order(order, brain, replies)
        print(f"=> {res['status']}")
        results.append(res)
        record_outcome(store, order, res, args.webhook, not args.no_write_back)

    write_results_csv(args.out, results)
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\nSummary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
