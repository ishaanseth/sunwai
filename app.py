"""Sunwai (सुनवाई) — speak a complaint in any Indian language, get a CPGRAMS-ready grievance.

Pipeline: Saaras v3 (speech -> native transcript + English translation)
          -> Sarvam-105B (structured, evidence-cited grievance)
          -> Bulbul v3 (reads the summary back in the citizen's language)
"""

import asyncio
import base64
import hashlib
import hmac
import secrets
import io
import json
import os
import re
import sqlite3
import wave
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
API_KEY = os.environ.get("SARVAM_API_KEY", "")
BASE = "https://api.sarvam.ai"
HEADERS = {"api-subscription-key": API_KEY}
# Vercel's filesystem is read-only apart from /tmp, and /tmp is per-instance, so
# data there is best-effort. Sessions are signed cookies so logins survive anyway.
DB_PATH = Path("/tmp/grievances.db") if os.environ.get("VERCEL") else ROOT / "grievances.db"
SECRET = (os.environ.get("SESSION_SECRET") or hashlib.sha256(f"sunwai:{API_KEY}".encode()).hexdigest()).encode()

# Bulbul v3 voices only cover these; anything else is read back in English.
TTS_LANGS = {"hi-IN", "bn-IN", "ta-IN", "te-IN", "gu-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "od-IN", "en-IN"}
CHUNK_SECONDS = 25  # Saaras REST accepts <= 30 s per request

STATUSES = ["Drafted", "Submitted", "Under review", "Resolved", "Closed"]

app = FastAPI(title="Sunwai")


async def post(path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(timeout=180.0) as client:
        return await client.post(f"{BASE}{path}", **kwargs)


# ---------------------------------------------------------------- storage

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


with db() as conn:
    conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT, pw TEXT)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS grievances (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            created_at TEXT,
            language_code TEXT,
            transcript TEXT,
            draft TEXT,
            status TEXT,
            reg_no TEXT,
            history TEXT
        )"""
    )


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row_to_dict(r):
    d = dict(r)
    d.pop("user_id", None)
    d["draft"] = json.loads(d["draft"])
    d["history"] = json.loads(d["history"])
    return d


# ---------------------------------------------------------------- accounts

DEMO_EMAIL = "demo@sunwai.app"
COOKIE = "sunwai_sid"


def hash_pw(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${digest}"


def check_pw(pw: str, stored: str) -> bool:
    if "$" not in stored:
        return False
    salt = stored.split("$", 1)[0]
    return secrets.compare_digest(hash_pw(pw, salt), stored)


def sign(payload: str) -> str:
    return hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()


def current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE, "")
    payload, _, sig = token.rpartition(".")
    if not payload or not hmac.compare_digest(sign(payload), sig):
        raise HTTPException(401, "Please log in")
    info = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    with db() as conn:
        row = conn.execute("SELECT id FROM users WHERE email=?", (info["email"],)).fetchone()
        uid = row["id"] if row else conn.execute(
            "INSERT INTO users (email, name, pw) VALUES (?,?,?)", (info["email"], info["name"], "!")
        ).lastrowid
    return {"id": uid, "email": info["email"], "name": info["name"]}


def start_session(response: Response, email: str, name: str) -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email, "name": name}).encode()).decode()
    response.set_cookie(COOKIE, f"{payload}.{sign(payload)}", httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30)


class AuthIn(BaseModel):
    email: str
    password: str
    name: str = ""


@app.post("/api/signup")
def signup(body: AuthIn, response: Response):
    email = body.email.strip().lower()
    if "@" not in email or len(body.password) < 6:
        raise HTTPException(400, "Use a valid email and a password of at least 6 characters.")
    name = body.name.strip() or email.split("@")[0]
    try:
        with db() as conn:
            conn.execute("INSERT INTO users (email, name, pw) VALUES (?,?,?)", (email, name, hash_pw(body.password)))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account with this email already exists.")
    start_session(response, email, name)
    return {"ok": True}


@app.post("/api/login")
def login(body: AuthIn, response: Response):
    with db() as conn:
        row = conn.execute("SELECT email, name, pw FROM users WHERE email=?", (body.email.strip().lower(),)).fetchone()
    if not row or not check_pw(body.password, row["pw"]):
        raise HTTPException(401, "Wrong email or password.")
    start_session(response, row["email"], row["name"])
    return {"ok": True}


@app.post("/api/demo")
def demo(response: Response):
    start_session(response, DEMO_EMAIL, "Demo citizen")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {**user, "demo": user["email"] == DEMO_EMAIL}


# ---------------------------------------------------------------- speech-to-text

def split_wav(data: bytes) -> list[bytes]:
    """Split a PCM WAV into <= CHUNK_SECONDS pieces so each fits the REST limit."""
    try:
        src = wave.open(io.BytesIO(data), "rb")
    except wave.Error:
        return [data]  # not PCM WAV; let Saaras try it as-is
    params = src.getparams()
    frames_per_chunk = params.framerate * CHUNK_SECONDS
    chunks = []
    while True:
        frames = src.readframes(frames_per_chunk)
        if not frames:
            break
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setparams(params)
            out.writeframes(frames)
        chunks.append(buf.getvalue())
    return chunks or [data]


async def stt(chunk: bytes, mode: str) -> dict:
    r = await post(
        "/speech-to-text",
        headers=HEADERS,
        files={"file": ("audio.wav", chunk, "audio/wav")},
        data={"model": "saaras:v3", "mode": mode},
    )
    if r.status_code != 200:
        raise HTTPException(502, f"Saaras error: {r.text[:300]}")
    return r.json()


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...), _: dict = Depends(current_user)):
    data = await file.read()
    chunks = split_wav(data)
    native = await asyncio.gather(*(stt(c, "transcribe") for c in chunks))
    english = await asyncio.gather(*(stt(c, "translate") for c in chunks))
    langs = [n.get("language_code") for n in native if n.get("language_code")]
    return {
        "native": " ".join(n.get("transcript", "") for n in native).strip(),
        "english": " ".join(e.get("transcript", "") for e in english).strip(),
        "language_code": max(set(langs), key=langs.count) if langs else "en-IN",
        "chunks": len(chunks),
    }


# ---------------------------------------------------------------- drafting

SYSTEM_PROMPT = """You are a grievance-drafting assistant for Indian citizens filing complaints on CPGRAMS (Centralised Public Grievance Redress and Monitoring System) or state grievance portals.

You receive a citizen's spoken complaint (native-language transcript plus an English translation) and optionally their answers to follow-up questions. Produce ONE JSON object and nothing else, with exactly these keys:

{
  "title": "short subject line, max 12 words, English",
  "category": "one of: Water Supply, Electricity, Roads & Infrastructure, Sanitation & Waste, Public Health, Education, Pension & Social Welfare, Ration / PDS, Police & Law and Order, Land & Revenue, Banking & Finance, Telecom & Postal, Transport, Corruption, Other",
  "jurisdiction": "Central or State or Local Body",
  "suggested_authority": "the most likely office to handle this, e.g. 'Municipal Corporation - Water Works Dept.' Use the location if given. Do not invent officer names.",
  "location": "place mentioned, or null",
  "incident_period": "when it happened / since when, or null",
  "facts": [
    {"fact": "one concrete factual statement in English", "quote": "the EXACT words from the native transcript that support it (copy verbatim, same script)"}
  ],
  "prior_attempts": "earlier complaints / visits the citizen mentioned, or null",
  "relief_sought": "specific, actionable remedy requested, in English",
  "urgency": "Low or Medium or High",
  "urgency_reason": "one sentence",
  "grievance_text": "formal English grievance, 120-250 words, first person, addressed 'To the Concerned Authority,'. Structure: context, facts with dates/places, impact on the citizen, prior attempts, specific relief requested, polite closing. Use ONLY facts from the complaint; where a detail is missing write a bracketed placeholder like [House No.] instead of inventing it.",
  "missing_info": [
    {"question_en": "question in English", "question_native": "same question in the citizen's language"}
  ],
  "summary_native": "2-3 short sentences in the citizen's own language and script summarising what will be filed and what relief is asked, suitable to be read aloud to them"
}

Rules:
- Never invent facts, names, numbers, dates or reference IDs. Every item in "facts" must have a verbatim supporting quote.
- "missing_info": at most 4 questions, only for details that materially strengthen the grievance (exact address/ward, dates, previous complaint numbers, account/connection numbers). Empty list if nothing important is missing.
- When the citizen's answers supply a detail, put it in the text directly and drop the matching [placeholder]; do not ask that question again.
- If the input is not a grievance at all, still return the JSON with category "Other" and ask in missing_info what the problem is.
- Output raw JSON only. No markdown fences, no commentary."""


class DraftIn(BaseModel):
    native: str
    english: str = ""
    language_code: str = "en-IN"
    answers: list[dict] = []  # [{"question": str, "answer": str}]


def extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])


def norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s or "").lower()


def verify_quotes(draft: dict, native: str, answers_text: str) -> None:
    """Mark each fact as grounded only if its quote really appears in what the citizen said."""
    haystack = norm(native + " " + answers_text)
    for f in draft.get("facts", []):
        q = norm(f.get("quote", ""))
        f["verified"] = bool(q) and q in haystack


async def chat(messages: list[dict]) -> str:
    r = await post(
        "/v1/chat/completions",
        headers={**HEADERS, "Authorization": f"Bearer {API_KEY}"},
        json={
            "model": "sarvam-105b",
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 4096,
            # Reasoning tokens count against max_tokens and aren't needed for this
            # extraction task; with them on, the JSON often gets cut off.
            "reasoning_effort": None,
        },
    )
    if r.status_code != 200:
        raise HTTPException(502, f"Sarvam-105B error: {r.text[:300]}")
    return r.json()["choices"][0]["message"].get("content") or ""


@app.post("/api/draft")
async def draft(body: DraftIn, _: dict = Depends(current_user)):
    if not body.native.strip():
        raise HTTPException(400, "Empty complaint")
    answers_text = "\n".join(
        f"Q: {a.get('question', '')}\nA: {a.get('answer', '')}" for a in body.answers if a.get("answer")
    )
    user = (
        f"Citizen language: {body.language_code}\n\n"
        f"Native transcript:\n{body.native}\n\n"
        f"English translation:\n{body.english or '(not available)'}\n"
    )
    if answers_text:
        user += f"\nCitizen's answers to follow-up questions:\n{answers_text}\n"
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]

    content = await chat(messages)
    try:
        result = extract_json(content)
    except (ValueError, json.JSONDecodeError):
        # One repair attempt: hand the model its own output back.
        messages += [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "That was not valid JSON. Return only the JSON object."},
        ]
        try:
            result = extract_json(await chat(messages))
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(502, "Model did not return valid JSON; please try again.")

    result["facts"] = [f for f in result.get("facts") or [] if isinstance(f, dict) and f.get("fact")]
    result["missing_info"] = [
        q for q in result.get("missing_info") or []
        if isinstance(q, dict) and (q.get("question_en") or q.get("question_native"))
    ][:4]
    verify_quotes(result, body.native, answers_text)
    result["language_code"] = body.language_code
    return result


# ---------------------------------------------------------------- text-to-speech

class TTSIn(BaseModel):
    text: str
    language_code: str = "en-IN"
    speaker: str = "priya"


@app.post("/api/tts")
async def tts(body: TTSIn, _: dict = Depends(current_user)):
    lang = body.language_code if body.language_code in TTS_LANGS else "en-IN"
    r = await post(
        "/text-to-speech",
        headers=HEADERS,
        json={
            "text": body.text[:2500],
            "target_language_code": lang,
            "model": "bulbul:v3",
            "speaker": body.speaker,
        },
    )
    if r.status_code != 200:
        raise HTTPException(502, f"Bulbul error: {r.text[:300]}")
    return {"audio": r.json()["audios"][0], "language_code": lang}


# ---------------------------------------------------------------- tracking

class SaveIn(BaseModel):
    transcript: str
    language_code: str
    draft: dict


class UpdateIn(BaseModel):
    status: str | None = None
    reg_no: str | None = None
    note: str | None = None


@app.get("/api/grievances")
def list_grievances(user: dict = Depends(current_user)):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM grievances WHERE user_id=? ORDER BY created_at DESC", (user["id"],)
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@app.post("/api/grievances")
def save_grievance(body: SaveIn, user: dict = Depends(current_user)):
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM grievances").fetchone()[0] + 1
        gid = f"GRV-{datetime.now().year}-{n:04d}"
        history = [{"at": now(), "status": "Drafted", "note": "Draft created from voice complaint"}]
        conn.execute(
            "INSERT INTO grievances VALUES (?,?,?,?,?,?,?,?,?)",
            (gid, user["id"], now(), body.language_code, body.transcript, json.dumps(body.draft, ensure_ascii=False),
             "Drafted", "", json.dumps(history)),
        )
        row = conn.execute("SELECT * FROM grievances WHERE id=?", (gid,)).fetchone()
    return row_to_dict(row)


@app.patch("/api/grievances/{gid}")
def update_grievance(gid: str, body: UpdateIn, user: dict = Depends(current_user)):
    with db() as conn:
        row = conn.execute("SELECT * FROM grievances WHERE id=? AND user_id=?", (gid, user["id"])).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        g = row_to_dict(row)
        if body.status and body.status not in STATUSES:
            raise HTTPException(400, f"status must be one of {STATUSES}")
        status = body.status or g["status"]
        reg_no = body.reg_no if body.reg_no is not None else g["reg_no"]
        if body.status or body.note or body.reg_no:
            note = body.note or (f"Registration no. {reg_no}" if body.reg_no else "")
            g["history"].append({"at": now(), "status": status, "note": note})
        conn.execute(
            "UPDATE grievances SET status=?, reg_no=?, history=? WHERE id=?",
            (status, reg_no, json.dumps(g["history"]), gid),
        )
        row = conn.execute("SELECT * FROM grievances WHERE id=?", (gid,)).fetchone()
    return row_to_dict(row)


@app.delete("/api/grievances/{gid}")
def delete_grievance(gid: str, user: dict = Depends(current_user)):
    with db() as conn:
        conn.execute("DELETE FROM grievances WHERE id=? AND user_id=?", (gid, user["id"]))
    return {"ok": True}


# ---------------------------------------------------------------- static

@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
