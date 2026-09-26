# SUNWAI · सुनवाई

*n. a hearing.* Speak a civic complaint in any Indian language and get a formal, CPGRAMS-ready grievance. Every fact in the draft is cited back to your own words.

Built for Sarvam Campus '26 (Idea #46, "Grievance Drafter").

## How it works

| Step | Model | What it does |
|---|---|---|
| 1 | **Saaras v3** (`/speech-to-text`) | Transcribes the recording in the speaker's language (`mode=transcribe`) and translates it to English (`mode=translate`). Detects the language automatically. Recordings over 25 s are split into chunks. |
| 2 | **Sarvam-105B** (`/v1/chat/completions`) | Returns structured JSON: title, category, jurisdiction, authority, urgency, a formal letter, relief sought, **facts with verbatim supporting quotes**, and follow-up questions in the citizen's language. |
| 3 | Server-side check | Each quote is matched against the transcript. Facts whose quote isn't found are flagged ⚠ in the UI. |
| 4 | **Bulbul v3** (`/text-to-speech`) | Reads a short summary back to the citizen in their language. |
| 5 | Accounts + SQLite tracker | Email/password accounts (PBKDF2-hashed) or one-click demo login. Each user saves drafts, adds the CPGRAMS registration number, and moves each one through Drafted → Submitted → Under review → Resolved, with a timeline. |

Follow-up questions can be answered by typing or by voice. Re-drafting puts the answers into the letter.

## Run

```bash
pip install -r requirements.txt
```

Put your key in `.env`:

```
SARVAM_API_KEY=sk_...
```

```bash
python app.py
```

Then open http://127.0.0.1:8000 and choose **Try the demo**, or create an account. No mic? Use one of the sample complaints (Hindi, Tamil, or Bengali).

## Screens

Landing/login → **01 Speak** (a live voiceprint follows your voice) → **02 Check** (edit the transcript) → **03 Draft** (letter on paper, evidence, read-back, follow-up questions; a FILED stamp lands when you save) → **My grievances** → grievance detail (status, registration no., timeline). Light and dark themes (follows your system, with a toggle)..

## Notes

- Audio is recorded in the browser as 16 kHz mono WAV, so no ffmpeg is needed.
- `reasoning_effort` is set to `None` for drafting. With reasoning on, the thinking tokens used up the output budget and the JSON came back empty.
- Bulbul supports 11 languages. For other languages (e.g. Assamese, Urdu), the read-back falls back to English.
