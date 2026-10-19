#!/usr/bin/env python3
"""Voice-call version of the order verifier (microphone + speaker).

Per turn:  Sonali speaks (Kokoro text-to-speech)  ->  customer answers
           ->  speech-to-text (Whisper fine-tuned for Indian speech)
           ->  Gemma reads the reply (intent + language)  ->  next line,
               in the customer's language.

Everything runs on this machine. It reuses the script, rules and outcomes
from verify.py. Models: run `python setup_models.py` once.
"""
import argparse
import queue
import re
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import verify
from phone import normalise_number

HERE = Path(__file__).parent
MODELS = HERE / "models"
KOKORO_MODEL = MODELS / "kokoro-v1.0.int8.onnx"
KOKORO_VOICES = MODELS / "voices-v1.0.bin"
SWIFT_STT = MODELS / "whisper-hinglish-swift"
MIC_RATE = 16000

# Kokoro voice names: hf_ / hm_ are Hindi female / male.
# Sonali keeps one voice for every language so she sounds like one person.
DEFAULT_VOICE = "hf_alpha"
ESPEAK_LANG = {"hindi": "hi", "hinglish": "hi", "english": "en-gb"}


def require(*paths):
    missing = [p for p in paths if not Path(p).exists()]
    if missing:
        sys.exit("Missing model files: " + ", ".join(str(p) for p in missing) +
                 "\nRun once:  python setup_models.py")


# ------------------------------------------------------------------ speaking

def prepare_for_speech(text, lang):
    """Make a written line easier to say aloud."""
    text = re.sub(r"\b(\d{6})\b", lambda m: " ".join(m.group(1)), text)  # pincode digit by digit
    text = re.sub(r"\bml\b", "M L", text, flags=re.I)
    if lang == "english":
        text = re.sub(r"\bRs\.?\s*(\d)", r"rupees \1", text)
        text = re.sub(r"\bji\b", "jee", text)       # otherwise English says "jai"
        return text.replace("+", " and ")
    return text.replace("+", " और ")


def sentences(text):
    return [p for p in re.split(r"(?<=[.!?।])\s+", text.strip()) if p]


class KokoroSpeaker:
    """Kokoro-82M (open weights, Apache-2.0) through ONNX Runtime on the CPU.

    Each sentence is rendered on a background thread while the previous one
    plays, so only the first sentence of a line costs any waiting. Rendered
    lines are cached; the opening lines of every call are rendered before
    dialling."""

    def __init__(self, voice=DEFAULT_VOICE, speed=1.0, device=None, threads=8):
        require(KOKORO_MODEL, KOKORO_VOICES)
        import numpy as np
        import onnxruntime as rt
        import phonemizer
        import sounddevice as sd
        from kokoro_onnx import Kokoro
        from kokoro_onnx.tokenizer import _espeak_lock

        self.np, self.sd, self.phonemizer, self.lock = np, sd, phonemizer, _espeak_lock
        opts = rt.SessionOptions()
        opts.intra_op_num_threads = threads
        session = rt.InferenceSession(str(KOKORO_MODEL), opts, providers=["CPUExecutionProvider"])
        self.kokoro = Kokoro.from_session(session, str(KOKORO_VOICES))
        if voice not in self.kokoro.get_voices():
            sys.exit(f"Unknown voice '{voice}'. Try: python call.py --list-voices")
        self.voice, self.speed, self.device = voice, speed, device
        self.rate = 24000
        self.out_rate = None
        self.out_channels = 2
        self.play_extra = None
        self.cache = {}
        self.renderer = ThreadPoolExecutor(max_workers=1)    # lines being said right now
        self.background = ThreadPoolExecutor(max_workers=1)  # lines that may be needed later
        self.between_calls = threading.Event()  # background rendering pauses during a call
        self.between_calls.set()
        self.jobs = queue.Queue()
        self.idle = threading.Event()
        self.idle.set()
        threading.Thread(target=self._player, daemon=True).start()
        print(f"Voice: Kokoro {voice}")
        print("Audio: this laptop's speaker and microphone")

    def _phonemes(self, text, lang):
        # remove-flags: espeak would otherwise insert "(en)"/"(hi)" markers that
        # Kokoro reads out loud when a Hindi line contains English words.
        with self.lock:
            return self.phonemizer.phonemize(
                text, language=ESPEAK_LANG[lang], backend="espeak",
                preserve_punctuation=True, with_stress=True,
                language_switch="remove-flags").strip()

    def _render(self, sentence, lang):
        key = (sentence, lang)
        if key not in self.cache:
            audio, rate = self.kokoro.create(self._phonemes(sentence, lang), voice=self.voice,
                                             speed=self.speed, lang=ESPEAK_LANG[lang],
                                             is_phonemes=True)
            self.rate = rate
            gap = self.np.zeros(int(rate * 0.12), dtype=self.np.float32)
            self.cache[key] = self.np.concatenate([audio.astype(self.np.float32), gap])
        return self.cache[key]

    def _render_later(self, sentence, lang):
        self.between_calls.wait()
        return self._render(sentence, lang)

    def prerender(self, lines, lang, wait=True):
        """Render lines ahead of time, one sentence at a time, only between calls,
        so it never holds up what Sonali is saying now."""
        jobs = [self.background.submit(self._render_later, s, lang)
                for line in lines for s in sentences(prepare_for_speech(line, lang))]
        if wait:
            for job in jobs:
                job.result()

    def call_started(self):
        self.between_calls.clear()

    def call_finished(self):
        self.between_calls.set()

    def _now(self, sentence, lang):
        audio = self.cache.get((sentence, lang))
        if audio is not None:
            done = Future()
            done.set_result(audio)
            return done
        return self.renderer.submit(self._render, sentence, lang)

    def use_device(self, device):
        """Send playback to a specific device (unused — laptop default speaker)."""
        self.device = device
        self.play_extra = None
        if device is None:
            self.out_rate = None
            self.out_channels = 2
            return
        info = self.sd.query_devices(device)
        self.out_rate = int(info.get("default_samplerate") or self.rate)
        self.out_channels = int(info.get("max_output_channels") or 1)
        host = self.sd.query_hostapis()[info["hostapi"]]["name"]
        if "WASAPI" in host:
            self.play_extra = self.sd.WasapiSettings(exclusive=False, auto_convert=True)
        print(f"   [audio] Sonali on the call -> #{device} {info['name']} @ {self.out_rate} Hz ({host})")

    def _player(self):
        while True:
            futures = self.jobs.get()
            for f in futures:
                audio = f.result()
                rate = self.rate
                if getattr(audio, "ndim", 1) > 1:
                    audio = audio.mean(axis=1)
                out_rate = self.out_rate or rate
                if out_rate != rate:
                    audio = resample_audio(self.np, audio, rate, out_rate)
                    rate = out_rate
                if self.out_channels == 1 and getattr(audio, "ndim", 1) == 1:
                    audio = audio.reshape(-1, 1)
                audio = self.np.clip(audio.astype(self.np.float32) * 2.2, -1.0, 1.0)
                try:
                    kw = {"device": self.device}
                    if self.play_extra is not None:
                        kw["extra_settings"] = self.play_extra
                    self.sd.play(audio, rate, **kw)
                    self.sd.wait()
                except Exception as e:
                    print(f"   [audio] playback on the call failed: {e}")
            if self.jobs.empty():
                self.idle.set()

    def __call__(self, text, lang="hindi", wait=False):
        parts = sentences(prepare_for_speech(text, lang))
        self.idle.clear()
        self.jobs.put([self._now(s, lang) for s in parts])
        if wait:
            self.wait()

    def wait(self):
        self.idle.wait()


def list_voices():
    require(KOKORO_MODEL, KOKORO_VOICES)
    from kokoro_onnx import Kokoro
    names = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES)).get_voices()
    print("Hindi (best for Sonali):", ", ".join(v for v in names if v[:2] in ("hf", "hm")))
    print("English US / UK:", ", ".join(v for v in names if v[:2] in ("af", "am", "bf", "bm")))


def resample_audio(np, audio, src_rate, dst_rate):
    if src_rate == dst_rate or audio is None or len(audio) == 0:
        return audio
    n = max(1, int(round(len(audio) * dst_rate / src_rate)))
    x_old = np.linspace(0.0, 1.0, len(audio), endpoint=False)
    x_new = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.interp(x_new, x_old, audio.astype(np.float32)).astype(np.float32)


# def _device_label(dev):
#     return " ".join(str(dev.get("name") or "").replace("\r", " ").replace("\n", " ").split()).lower()


# Phone Link Hands-Free routing — disabled. Laptop default speaker/mic only.
# def find_call_audio_devices():
#     ...
# def find_pc_handset_audio():
#     ...
# def wait_for_call_audio(timeout=8):
#     ...


def list_devices():
    import sounddevice as sd
    print(sd.query_devices())


# ----------------------------------------------------------------- listening

def beep(device=None):
    """Short tone meaning 'your turn to speak'."""
    try:
        import numpy as np
        import sounddevice as sd
        t = np.arange(int(24000 * 0.12)) / 24000
        sd.play((0.2 * np.sin(2 * np.pi * 880 * t)).astype(np.float32), 24000, device=device)
        sd.wait()
    except Exception:
        print("   [listening]")


def record_utterance(device=None, wait_seconds=8.0, silence_seconds=0.8, max_seconds=15.0):
    """Record until the speaker stops. Returns float32 mono 16 kHz audio, or None
    if nobody spoke. Loudness threshold is calibrated on the room noise first."""
    import numpy as np
    import sounddevice as sd

    q = queue.Queue()

    def callback(indata, frames, time_info, status):
        q.put(indata[:, 0].copy())

    chunks, preroll = [], []
    voiced_blocks = 0
    speaking = False
    last_voice = 0.0

    def _mic_fail(e):
        sys.exit(f"Cannot open the microphone: {e}\n"
                 "Check Windows Settings > Privacy > Microphone, or pick one with --mic.")

    attempts = []
    if device is None:
        attempts.append((MIC_RATE, "float32", None, None))
    else:
        info = sd.query_devices(device)
        native = int(info.get("default_samplerate") or MIC_RATE)
        host = sd.query_hostapis()[info["hostapi"]]["name"]
        extra = None
        if "WASAPI" in host:
            extra = sd.WasapiSettings(exclusive=False, auto_convert=True)
        for rate in (native, 16000, 8000):
            if extra is not None:
                attempts.append((rate, "float32", extra, device))
            attempts.append((rate, "int16", None, device))
            attempts.append((rate, "float32", None, device))

    stream = None
    rec_rate = MIC_RATE
    rec_dtype = "float32"
    last_err = None
    for rate, dtype, extra, dev in attempts:
        try:
            kw = dict(samplerate=rate, channels=1, dtype=dtype,
                      blocksize=int(rate * 0.05), callback=callback, device=dev)
            if extra is not None:
                kw["extra_settings"] = extra
            stream = sd.InputStream(**kw)
            rec_rate, rec_dtype = rate, dtype
            break
        except Exception as e:
            last_err = e
            stream = None
    if stream is None:
        if device is None:
            _mic_fail(last_err)
        print(f"   [audio] call mic failed ({last_err})")
        return None

    try:
        with stream:
            preroll = [q.get() for _ in range(4)]  # ~0.2 s of room noise, kept in case they already speak
            noise = [float(np.sqrt(np.mean(b.astype(np.float32) ** 2))) for b in preroll]
            threshold = max(0.012, 3.0 * float(np.median(noise)))
            if rec_dtype == "int16":
                threshold = max(400.0, 3.0 * float(np.median(noise)))
            start = time.time()
            speech_start = None
            while True:
                data = q.get()
                now = time.time()
                loud = float(np.sqrt(np.mean(data.astype(np.float32) ** 2))) > threshold
                if loud:
                    voiced_blocks += 1
                    last_voice = now
                    if not speaking and voiced_blocks >= 4:   # ~0.2 s of sound, not a click
                        speaking = True
                        speech_start = now
                        chunks.extend(preroll)
                preroll.append(data)
                preroll[:] = preroll[-8:]
                if speaking:
                    chunks.append(data)
                    if now - last_voice > silence_seconds or now - speech_start > max_seconds:
                        break
                elif now - start > wait_seconds:
                    return None
    except Exception as e:
        if device is None:
            _mic_fail(e)
        print(f"   [audio] call mic failed ({e})")
        return None
    if not chunks:
        return None
    audio = np.concatenate(chunks).astype(np.float32)
    if rec_dtype == "int16":
        audio = audio / 32768.0
    if rec_rate != MIC_RATE:
        audio = resample_audio(np, audio, rec_rate, MIC_RATE)
    return audio


class Transcriber:
    """faster-whisper on the CPU. Default model: Oriserve Whisper-Hindi2Hinglish-Swift,
    fine-tuned on ~550 h of noisy Indian-accented speech. It writes Hindi in Latin
    letters ("haan ji maine order kiya tha") and English as English, which Gemma
    reads well. Any stock Whisper size (e.g. "small") also works."""

    def __init__(self, model=str(SWIFT_STT), threads=8):
        from faster_whisper import WhisperModel
        if model == str(SWIFT_STT):
            require(SWIFT_STT)
            self.language = "en"   # Swift's Hinglish output is decoded with the English token
        else:
            self.language = None
        print(f"Speech-to-text: {Path(model).name}")
        self.model = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads)

    def __call__(self, audio):
        segments, _info = self.model.transcribe(
            audio, language=self.language, beam_size=3, vad_filter=True,
            condition_on_previous_text=False, without_timestamps=True)
        return " ".join(s.text.strip() for s in segments).strip()


class VoiceReplies:
    """Same interface as verify.Replies, but the reply comes from the laptop mic."""

    def __init__(self, speaker, transcriber, mic=None, phone=None):
        self.speaker = speaker
        self.transcriber = transcriber
        self.mic = mic
        self.phone = None  # Phone Link disabled — always laptop speaker/mic + beep

    def get(self, order_id, filler=None):
        self.speaker.wait()
        time.sleep(0.08)  # let the speaker tail die out before listening
        beep(self.speaker.device)
        audio = record_utterance(self.mic)
        if audio is None:
            print("CUSTOMER: (no speech heard)")
            return ""
        t0 = time.time()
        text = self.transcriber(audio)
        print(f"CUSTOMER (heard): {text}   [speech-to-text {time.time() - t0:.1f}s]")
        return text


# def wait_for_hello(phone, transcriber, mic=None, timeout=45):
#     """Phone Link pickup wait — disabled."""
#     ...


# ---------------------------------------------------------------------- main

def parse_device(value):
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def main():
    verify.use_utf8_console()
    ap = argparse.ArgumentParser(description="Order verifier, voice-call version (Sonali)")
    verify.add_common_args(ap, "results_voice.csv")
    ap.add_argument("--voice", default=DEFAULT_VOICE, help="Kokoro voice (default hf_alpha)")
    ap.add_argument("--speech-speed", type=float, default=1.0, help="Sonali's speaking speed")
    ap.add_argument("--stt-model", default=str(SWIFT_STT),
                    help="speech-to-text model folder, or a Whisper size like 'small'")
    ap.add_argument("--mic", help="input device number or name (see --list-devices)")
    ap.add_argument("--speaker", help="output device number or name (see --list-devices)")
    ap.add_argument("--list-voices", action="store_true", help="show Kokoro voices and exit")
    ap.add_argument("--list-devices", action="store_true", help="show audio devices and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="no microphone or model: scripted replies + keyword rules")
    ap.add_argument("--speak", action="store_true",
                    help="with --dry-run: still speak Sonali's lines aloud (voice test)")
    ap.add_argument("--replies", help="with --dry-run: JSON file of scripted replies")
    ap.add_argument("--dial", action="store_true",
                    help="ignored: Phone Link is disabled; talks on the laptop speaker/mic")
    ap.add_argument("--call-number",
                    help="ignored: Phone Link is disabled")
    ap.add_argument("--country-code", default="+91", help="for numbers without one (default +91)")
    args = ap.parse_args()

    if args.list_voices:
        return list_voices()
    if args.list_devices:
        return list_devices()

    store = verify.open_store(args)
    orders = store.load(args.only, args.redo, args.limit)
    if not orders:
        sys.exit(f"No pending orders in {store.describe()}.")

    phone = None
    # Phone Link / real SIM dialling is disabled. Laptop speaker + mic only.
    # if args.dial and not args.dry_run:
    #     from phone import connect_phone
    #     phone = connect_phone()
    if args.dial:
        print("Phone Link is disabled. Talking on this laptop's speaker and microphone.")

    speaker = None
    if not args.dry_run or args.speak:
        speaker = KokoroSpeaker(args.voice, args.speech_speed, parse_device(args.speaker))
    prep = ThreadPoolExecutor(max_workers=1)
    if args.dry_run:
        print("DRY RUN: no microphone, no model. Checks the wiring only.\n")
        brain = verify.MockBrain()
        default_replies = ("excel_demo_replies.json"
                           if isinstance(store, verify.sheets.ExcelOrders)
                           else "sample_replies.json")
        replies = verify.Replies(args.replies or default_replies)
        gemma_ready = None
    else:
        brain = verify.OllamaBrain(args.model, args.verbose)
        # Gemma loads (~30 s from cold) while the voice and speech models start up.
        gemma_ready = ThreadPoolExecutor(max_workers=1).submit(brain.warm_up)
        transcriber = Transcriber(args.stt_model)
        replies = VoiceReplies(speaker, transcriber, parse_device(args.mic), phone=None)

    # The next order is prepared (Gemma names + its lines as audio) while the
    # previous call wraps up, so a long sheet never means a long wait.
    cache = verify.SpokenCache()
    next_ready = prep.submit(verify.prepare_order, orders[0], brain, cache, speaker)
    if speaker and not args.dry_run:
        # Retry, hold and closing lines: Hindi first, since every call opens in Hindi.
        # They render between calls; any line still missing is rendered when needed.
        for lang in (verify.OPENING_LANGUAGE,) + tuple(
                l for l in verify.LANGUAGES if l != verify.OPENING_LANGUAGE):
            speaker.prerender(verify.static_lines(lang), lang, wait=False)
    print(f"{len(orders)} pending order(s) from {store.describe()}.")
    for o in orders:
        concern = o.get("address_discrepancies") or "-"
        flags = verify.order_flags(o) or ["none"]
        print(f"   row {o['order_id']}: {o.get('customer_name')}  "
              f"Phone Number={o.get('phone')}  Flagged Concern={concern}  "
              f"script={', '.join(flags)}")
    if gemma_ready is not None:
        print(f"Gemma ready ({gemma_ready.result():.0f}s).")

    results = []
    for n, order in enumerate(orders):
        next_ready.result()
        sheet_number = order.get("phone", "")
        if not sheet_number:
            sys.exit(f"Row {order['order_id']} has no Phone Number in the sheet.")
        number = normalise_number(sheet_number, args.country_code)
        print(f"   sheet Phone Number {sheet_number} -> {number}  (laptop speaker/mic, not dialling)")
        if not args.dry_run:
            input(f"\nPress Enter to start row {order['order_id']} "
                  f"({order['customer_name']}, {number})...")
        print(f"--- Order {order['order_id']} (address gaps: "
              f"{verify.order_flags(order) or 'none'})")
        t0 = time.time()
        if speaker:
            speaker.call_started()
        res = None
        try:
            # Phone Link real-call block disabled. Sonali talks on the laptop:
            # if phone:
            #     phone.dial(...)
            #     wait_until_answered / wait_for_call_audio / hang_up
            res = verify.run_order(order, brain, replies, speak=speaker)
        finally:
            if speaker:
                speaker.call_finished()
                speaker.wait()
        if n + 1 < len(orders):
            next_ready = prep.submit(verify.prepare_order, orders[n + 1], brain, cache, speaker)
        print(f"=> {res['status']} ({res['customer_language']})   [call took {time.time() - t0:.0f}s]")
        if res.get("call_notes"):
            print(f"   note: {res['call_notes']}")
        results.append(res)
        verify.record_outcome(store, order, res, args.webhook, not args.no_write_back)

    verify.write_results_csv(args.out, results)
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
