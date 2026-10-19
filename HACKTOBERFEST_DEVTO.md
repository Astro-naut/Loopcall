---
title: "Loopcall: a thank-you call that confirms the order and fixes the address"
published: false
tags: devchallenge, hf26challenge, gemma, python, ai
---

*This is a submission for the [Hacktoberfest Open-Source AI Challenge Week 2: Side Hustle](https://dev.to/challenges/hacktoberfest-week2-2026-10-12)*

## What I Built

Small D2C shops in India still spend an afternoon on the phone because checkout addresses look like “Shivaji Nagar, Pune.” Someone has to check the order is real and fill in the house number or pincode before the parcel goes out.

**Loopcall** is that call, done by **Sonali** — a voice agent for the shop.

She thanks the customer, confirms they ordered the product, and asks only what is missing. She starts in Hindi, then follows Hindi, English, or Hinglish. She never says she is an AI. She does not cancel orders. She writes a one-line outcome back into the same Excel sheet the shop already uses.

Who would pay: any small brand that is already making these calls by hand. After the models sit on the laptop, each call costs nothing, and customer audio never leaves the machine.

The first shop is **Sustainable Universe**. Their sheet has names, phones, addresses, product quantities, and flagged concerns. Sonali reads a pending row, talks, and updates only `Verified/Called`, `Agent Call Review`, and `Address` if they actually gave a new one.

**What she does well**

- Opens as a thank-you, not an interrogation
- Speaks Hindi, English, or Hinglish in one voice
- Asks one gap at a time (house number, pincode, short address)
- Treats “maybe my daughter ordered it” as a yes
- Tries three times, then hands it to a human
- Runs Whisper, Gemma, and Kokoro locally on CPU

## Demo

The loop is Excel → Sonali talks → Excel.

A pending row goes from `Verified/Called = Not yet` to `Yes`, with a review line such as “Address updated successfully.”

<!-- Paste your demo video or Loom link here. -->

On a laptop:

```
python call.py --limit 1 --verbose
```

Press Enter, speak after the beep, reopen the workbook.

## Code

{% github Astro-naut/order-verifier %}

## How I Built It

Three open-weight models on one laptop:

- **Hearing** — Whisper-Hindi2Hinglish-Swift, because Indian customers mix languages
- **Thinking** — Gemma 4 E4B via Ollama, for messy replies the rules cannot place
- **Speaking** — Kokoro-82M, Hindi voice `hf_alpha`, same person in every language

Plain rules own the script and the address checks. Gemma only steps in when the customer hedges, asks a question, or goes off-script. That keeps the call short and the bill at zero.

## Why Does Open Innovation Matter?

A hosted voice API would charge per minute and upload names, phones, and addresses. A shop making dozens of confirmation calls a day cannot afford that, and should not ship that data out.

Open models meant I could run the whole agent offline, swap Whisper when it failed on Hinglish, and keep Gemma as a helper instead of the whole product. That is what makes Loopcall a side hustle instead of a demo that dies on the invoice.

## My Agent Session

Built in Cursor on this repo, from the Excel loop through the voice call.

## My First User

Sustainable Universe. We ran it on a real flagged order — missing house number and pincode. Sonali opened in Hindi and asked about the rosemary conditioner. They have not paid yet; the next ask is putting the same conversation on a real phone line.

## Prize Categories

- **Best Use of Gemma** — Gemma 4 E4B is the local brain for intent and off-script replies.
