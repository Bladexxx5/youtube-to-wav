"""
WAVify – Backend Flask
Inicia local:  python app.py  → http://localhost:5000
En producción: gunicorn + Render/Railway/Fly.io
"""

import os
import re
import sys
import uuid
import base64
import tempfile
import threading
import time
import subprocess
import shutil
import requests as req_lib
from pathlib import Path
from flask import Flask, request, jsonify, send_file, make_response
from flask_cors import CORS
import traceback

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

# ── Cookies de YouTube ────────────────────────────────────────────────────────
COOKIES_FILE  = None
COOKIES_LINES = 0

def setup_cookies():
    global COOKIES_FILE, COOKIES_LINES
    b64 = os.environ.get("YOUTUBE_COOKIES_B64", "")
    if not b64:
        print("  Cookies: YOUTUBE_COOKIES_B64 no configurada")
        return
    try:
        data = base64.b64decode(b64).decode("utf-8")
        tmp  = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        tmp.write(data)
        tmp.close()
        COOKIES_FILE  = tmp.name
        COOKIES_LINES = sum(1 for l in data.splitlines() if l and not l.startswith("#"))
        print(f"  Cookies: {COOKIES_LINES} entradas → {COOKIES_FILE}")
    except Exception as e:
        print(f"  Cookies: ERROR – {e}")

setup_cookies()

# ── Detectar FFmpeg ───────────────────────────────────────────────────────────
def find_ffmpeg():
    if shutil.which("ffmpeg"):
        return str(Path(shutil.which("ffmpeg")).parent)
    for p in [
        r"C:\Users\ttrac\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin",
        r"C:\ProgramData\chocolatey\bin",
        r"C:\tools\ffmpeg\bin",
        r"C:\ffmpeg\bin",
    ]:
        if (Path(p) / "ffmpeg.exe").exists():
            return p
    root = Path(r"C:\Users\ttrac\AppData\Local\Microsoft\WinGet\Packages")
    if root.exists():
        for f in root.rglob("ffmpeg.exe"):
            return str(f.parent)
    return None

FFMPEG_DIR = find_ffmpeg()
print(f"  FFmpeg: {FFMPEG_DIR or 'PATH del sistema'}")

# ── Limpieza automática ───────────────────────────────────────────────────────
def cleanup_old_files():
    while True:
        time.sleep(1800)
        for f in DOWNLOADS_DIR.iterdir():
            if f.is_file() and (time.time() - f.stat().st_mtime) > 3600:
                try: f.unlink()
                except Exception: pass

threading.Thread(target=cleanup_old_files, daemon=True).start()

# ── Helpers ───────────────────────────────────────────────────────────────────
def safe_filename(title: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", title)
    name = re.sub(r'\s+', "_", name.strip())
    return name[:80] or "audio"

def extract_video_id(url: str):
    for pat in [r'(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})',
                r'youtube\.com/shorts/([a-zA-Z0-9_-]{11})']:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None

def yt_dlp_flags():
    flags = ["--no-playlist", "--no-warnings",
             "--extractor-args", "youtube:player_client=ios,web,default"]
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        flags += ["--cookies", COOKIES_FILE]
    return flags

def ffmpeg_exe():
    return shutil.which("ffmpeg") or (str(Path(FFMPEG_DIR) / "ffmpeg.exe") if FFMPEG_DIR else None)

# ── Invidious fallback ────────────────────────────────────────────────────────
INVIDIOUS_INSTANCES = [
    "https://inv.tux.pizza",
    "https://invidious.jing.rocks",
    "https://yt.artemislena.eu",
    "https://invidious.privacydev.net",
    "https://invidious.nerdvpn.de",
]

def download_via_invidious(video_id: str, out_dir: Path) -> tuple[str, Path]:
    """Descarga audio via Invidious API y devuelve (titulo, archivo)."""
    last_err = "Sin instancias disponibles"
    for instance in INVIDIOUS_INSTANCES:
        try:
            resp = req_lib.get(
                f"{instance}/api/v1/videos/{video_id}",
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if resp.status_code != 200:
                continue
            data  = resp.json()
            title = data.get("title", "audio")

            # Preferir audio-only (adaptiveFormats)
            audio_fmts = [f for f in data.get("adaptiveFormats", [])
                          if f.get("type", "").startswith("audio/")]
            if not audio_fmts:
                audio_fmts = data.get("formatStreams", [])
            if not audio_fmts:
                continue

            audio_fmts.sort(key=lambda x: int(x.get("bitrate", 0)), reverse=True)
            audio_url = audio_fmts[0]["url"]
            ext       = "webm" if "webm" in audio_fmts[0].get("type","") else "m4a"

            raw_path = out_dir / f"inv_{video_id}_{uuid.uuid4().hex[:6]}.{ext}"
            with req_lib.get(audio_url, stream=True, timeout=180,
                             headers={"User-Agent": "Mozilla/5.0"}) as r:
                r.raise_for_status()
                with open(raw_path, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)

            return title, raw_path
        except Exception as e:
            last_err = str(e)
            print(f"  Invidious {instance} falló: {e}")
            continue
    raise RuntimeError(f"Invidious: todos fallaron. Último error: {last_err}")

def convert_to_wav(src: Path, ffmpeg: str) -> Path:
    """Convierte cualquier audio a WAV 44100 Hz stereo."""
    out = src.with_suffix(".wav")
    subprocess.run(
        [ffmpeg, "-y", "-i", str(src), "-ar", "44100", "-ac", "2", str(out)],
        capture_output=True, timeout=120, check=True
    )
    try: src.unlink()
    except Exception: pass
    return out

# ── /update-cookies ───────────────────────────────────────────────────────────
@app.route("/update-cookies", methods=["POST"])
def update_cookies():
    secret = os.environ.get("ADMIN_SECRET", "")
    data   = request.get_json(force=True)
    if not secret or data.get("secret") != secret:
        return jsonify({"error": "No autorizado"}), 403
    b64 = data.get("cookies_b64", "")
    if not b64:
        return jsonify({"error": "cookies_b64 requerido"}), 400
    try:
        global COOKIES_FILE, COOKIES_LINES
        raw = base64.b64decode(b64).decode("utf-8")
        if COOKIES_FILE and Path(COOKIES_FILE).exists():
            Path(COOKIES_FILE).unlink()
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        tmp.write(raw)
        tmp.close()
        COOKIES_FILE  = tmp.name
        COOKIES_LINES = sum(1 for l in raw.splitlines() if l and not l.startswith("#"))
        return jsonify({"status": "ok", "cookies_lines": COOKIES_LINES})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Frontend ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file("index.html")

# ── /convert ──────────────────────────────────────────────────────────────────
@app.route("/convert", methods=["POST"])
def convert():
    data = request.get_json(force=True)
    url  = data.get("url", "").strip()

    if not url:
        return jsonify({"status": "error", "error": "URL requerida"}), 400
    if "youtube.com" not in url and "youtu.be" not in url:
        return jsonify({"status": "error", "error": "Solo URLs de YouTube"}), 400

    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        return jsonify({"status": "error", "error": "FFmpeg no instalado"}), 500

    video_id = extract_video_id(url)
    file_id  = uuid.uuid4().hex[:8]

    # ── Intento 1: yt-dlp ────────────────────────────────────────────────────
    title    = "audio"
    wav_file = None
    ytdlp_ok = False

    try:
        # Título
        cmd_title = [sys.executable, "-m", "yt_dlp"] + yt_dlp_flags() + ["--get-title", url]
        r = subprocess.run(cmd_title, capture_output=True, text=True, timeout=30)
        title_raw = r.stdout.strip().split("\n")[0]
        if title_raw:
            title = title_raw

        out_name = f"{safe_filename(title)}_{file_id}"
        out_tmpl = str(DOWNLOADS_DIR / f"{out_name}.%(ext)s")

        cmd = [sys.executable, "-m", "yt_dlp"] + yt_dlp_flags() + [
            "--format", "bestaudio/best",
            "-x", "--audio-format", "wav",
            "-o", out_tmpl, url,
        ]
        if FFMPEG_DIR:
            cmd += ["--ffmpeg-location", FFMPEG_DIR]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        print("yt-dlp STDOUT:", result.stdout[:300].encode("ascii","ignore").decode())
        print("yt-dlp STDERR:", result.stderr[:300].encode("ascii","ignore").decode())

        if result.returncode == 0:
            wavs = sorted(DOWNLOADS_DIR.glob(f"{out_name}*.wav"),
                          key=lambda x: x.stat().st_mtime, reverse=True)
            if wavs:
                wav_file = wavs[0]
                ytdlp_ok = True

        if not ytdlp_ok:
            err_lower = (result.stderr + result.stdout).lower()
            if "sign in" in err_lower or "bot" in err_lower:
                print("  yt-dlp bloqueado → intentando Invidious")
            else:
                err = (result.stderr or result.stdout or "Error desconocido")[-600:]
                return jsonify({"status": "error", "error": err}), 500

    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "error": "Tiempo agotado (3 min)."}), 504
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500

    # ── Intento 2: Invidious ──────────────────────────────────────────────────
    if not ytdlp_ok:
        if not video_id:
            return jsonify({"status": "error", "error": "No se pudo extraer el ID del video"}), 400
        try:
            title, raw_file = download_via_invidious(video_id, DOWNLOADS_DIR)
            wav_file = convert_to_wav(raw_file, ffmpeg)
        except Exception as e:
            traceback.print_exc()
            return jsonify({"status": "error", "error": f"Invidious falló: {e}"}), 500

    # ── Verificar WAV ─────────────────────────────────────────────────────────
    with open(wav_file, "rb") as f:
        header = f.read(4)
    if header != b"RIFF":
        try:
            fixed = convert_to_wav(wav_file, ffmpeg)
            wav_file = fixed
        except Exception as e:
            return jsonify({"status": "error", "error": f"Conversión WAV falló: {e}"}), 500

    return jsonify({
        "status":   "ok",
        "filename": wav_file.name,
        "title":    title,
        "size":     wav_file.stat().st_size,
    })

# ── /download/<filename> ──────────────────────────────────────────────────────
@app.route("/download/<filename>")
def download(filename):
    safe_name = Path(filename).name
    safe      = DOWNLOADS_DIR / safe_name
    if not safe.exists() or not safe.is_file():
        return jsonify({"error": "Archivo no encontrado"}), 404
    dl_name = safe_name if safe_name.lower().endswith(".wav") else safe_name.rsplit(".", 1)[0] + ".wav"
    resp = make_response(send_file(safe, mimetype="audio/wav", as_attachment=True, download_name=dl_name))
    resp.headers["Content-Type"]        = "audio/wav"
    resp.headers["Content-Disposition"] = f'attachment; filename="{dl_name}"'
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

# ── /health ───────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({
        "status":        "ok",
        "v":             "10",
        "ffmpeg":        FFMPEG_DIR or shutil.which("ffmpeg") or "no encontrado",
        "cookies":       "ok" if (COOKIES_FILE and Path(COOKIES_FILE).exists()) else "no",
        "cookies_lines": COOKIES_LINES,
    })

@app.errorhandler(500)
def internal_error(_error):
    traceback.print_exc()
    return jsonify({"status": "error", "error": "Internal Server Error"}), 500

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "=" * 60)
    print(f"  WAVify  →  http://localhost:{port}")
    print(f"  FFmpeg  →  {FFMPEG_DIR or 'PATH del sistema'}")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=port, debug=True)
