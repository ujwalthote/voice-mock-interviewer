"""Voice mock interviewer: browser <-> FastAPI websocket <-> Gemini 3.8 Live."""
import asyncio, io, json, os, time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from google import genai
from google.genai import types

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
MODEL = os.getenv("LIVE_MODEL", "gemini-3.8-live")
VOICE = os.getenv("VOICE", "Aoede")
(ROOT / "transcripts").mkdir(exist_ok=True)

TEMPLATE = (ROOT / "persona.md").read_text(encoding="utf-8")
MAX_CHARS = 20000  # per document, keeps the prompt small
app = FastAPI()


def build_persona(resume: str, jd: str) -> str:
    # str.replace (not format) so braces in the documents are safe
    return (TEMPLATE.replace("{{RESUME}}", resume[:MAX_CHARS].strip())
                    .replace("{{JOB_DESCRIPTION}}", jd[:MAX_CHARS].strip()))


def extract_text(name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(data)).pages)
    if ext == ".docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    if ext in (".txt", ".md", ".text", ""):
        return data.decode("utf-8", errors="replace")
    raise HTTPException(415, f"Unsupported file type {ext}. Use PDF, DOCX, TXT or MD.")


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 8_000_000:
        raise HTTPException(413, "File too large (8 MB max).")
    try:
        text = extract_text(file.filename or "", data).strip()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(422, f"Could not read file: {e}")
    if not text:
        raise HTTPException(422, "No text found (scanned PDF? paste the text instead).")
    return {"text": text[:MAX_CHARS], "truncated": len(text) > MAX_CHARS}


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")


@app.get("/health")
def health():
    return {"key_configured": bool(API_KEY), "model": MODEL}


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    if not API_KEY:
        await sock.send_json({"type": "error", "text": "GEMINI_API_KEY not set. Put it in D:/work/voice-interviewer/.env and restart."})
        await sock.close()
        return

    # First message from the browser carries the candidate's resume and the job description.
    try:
        first = json.loads(await asyncio.wait_for(sock.receive_text(), timeout=30))
    except Exception:
        await sock.close()
        return
    resume, jd = (first.get("resume") or "").strip(), (first.get("jd") or "").strip()
    if first.get("type") != "start" or len(resume) < 30 or len(jd) < 30:
        await sock.send_json({"type": "error", "text": "Provide both a resume and a job description."})
        await sock.close()
        return
    system = build_persona(resume, jd)

    client = genai.Client(api_key=API_KEY)
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))
        ),
        system_instruction=types.Content(parts=[types.Part.from_text(text=system)]),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    log_path = ROOT / "transcripts" / time.strftime("interview_%Y%m%d_%H%M%S.txt")
    log = log_path.open("a", encoding="utf-8")
    cur = {"user": "", "agent": ""}

    def flush(role):
        if cur[role].strip():
            log.write(f"[{role.upper()}] {cur[role].strip()}\n\n")
            log.flush()
        cur[role] = ""

    try:
        async with client.aio.live.connect(model=MODEL, config=config) as session:
            await sock.send_json({"type": "ready"})
            # kick off: interviewer greets and asks the first question
            await session.send_realtime_input(text="Begin the interview now. Greet me briefly and ask your first question.")

            async def mic_to_model():
                while True:
                    msg = await sock.receive()
                    if msg.get("type") == "websocket.disconnect":
                        return
                    if msg.get("bytes"):
                        await session.send_realtime_input(
                            audio=types.Blob(data=msg["bytes"], mime_type="audio/pcm;rate=16000"))
                    elif msg.get("text"):
                        cmd = json.loads(msg["text"])
                        if cmd.get("type") == "end":
                            await session.send_realtime_input(
                                text="The interview is over. Stop asking questions. Give me concise feedback: "
                                     "top 3 strengths, top 3 gaps with what to study, and a score out of 10.")

            async def model_to_browser():
                while True:  # session.receive() ends each turn; loop for the next one
                    async for m in session.receive():
                        sc = m.server_content
                        if not sc:
                            continue
                        if sc.interrupted:
                            flush("agent")
                            await sock.send_json({"type": "interrupted"})
                        if sc.model_turn:
                            for p in sc.model_turn.parts:
                                if p.inline_data and p.inline_data.data:
                                    await sock.send_bytes(p.inline_data.data)
                        if sc.input_transcription and sc.input_transcription.text:
                            if cur["agent"]:
                                flush("agent")
                            cur["user"] += sc.input_transcription.text
                            await sock.send_json({"type": "user", "text": sc.input_transcription.text})
                        if sc.output_transcription and sc.output_transcription.text:
                            if cur["user"]:
                                flush("user")
                            cur["agent"] += sc.output_transcription.text
                            await sock.send_json({"type": "agent", "text": sc.output_transcription.text})
                        if sc.turn_complete:
                            flush("agent")
                            await sock.send_json({"type": "turn_complete"})

            tasks = [asyncio.create_task(mic_to_model()), asyncio.create_task(model_to_browser())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception():
                    raise t.exception()
    except WebSocketDisconnect:
        pass
    except Exception as e:  # surface API errors (quota, bad model id, bad key) to the UI
        try:
            await sock.send_json({"type": "error", "text": f"{type(e).__name__}: {e}"})
        except Exception:
            pass
    finally:
        flush("user"); flush("agent"); log.close()
