"""
WAVify – Backend Flask
Modos:
  - LOCAL: python app.py → http://localhost:5000 (descarga real con IP residencial)
  - RENDER: gunicorn → proxy al backend local cuando está registrado
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
from pathlib import Path
from flask import Flask, request, jsonify, send_file, make_response, Response
from flask_cors import CORS
import traceback
import requests as req

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

# ── Backend local registrado (proxy mode) ─────────────────────────────────────
_remote = {"url": None}   # {"url": "https://xxx.trycloudflare.com"} cuando PC está online

# ── Cookies de YouTube ────────────────────────────────────────────────────────
COOKIES_FILE  = None
COOKIES_LINES = 0

def setup_cookies():
    global COOKIES_FILE, COOKIES_LINES
    b64 = os.environ.get("YOUTUBE_COOKIES_B64", "")
    if not b64:
        return
    try:
        data = base64.b64decode(b64).decode("utf-8")
        tmp  = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        tmp.write(data); tmp.close()
        COOKIES_FILE  = tmp.name
        COOKIES_LINES = sum(1 for l in data.splitlines() if l and not l.startswith("#"))
        print(f"  Cookies: {COOKIES_LINES} entradas")
    except Exception as e:
        print(f"  Cookies: ERROR – {e}")

setup_cookies()

# ── Proxy URL ─────────────────────────────────────────────────────────────────
PROXY_URL = os.environ.get("PROXY_URL", "")
if PROXY_URL:
    print(f"  Proxy: {PROXY_URL.split('@')[-1]}")

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
        if m: return m.group(1)
    return None

def yt_dlp_flags():
    flags = ["--no-playlist", "--no-warnings",
             "--extractor-args", "youtube:player_client=ios,web,default"]
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        flags += ["--cookies", COOKIES_FILE]
    if PROXY_URL:
        flags += ["--proxy", PROXY_URL]
    return flags

def ffmpeg_exe():
    return shutil.which("ffmpeg") or (str(Path(FFMPEG_DIR) / "ffmpeg.exe") if FFMPEG_DIR else None)

def convert_to_wav(src: Path, ffmpeg: str) -> Path:
    out = src.with_suffix(".wav")
    subprocess.run([ffmpeg, "-y", "-i", str(src), "-ar", "44100", "-ac", "2", str(out)],
                   capture_output=True, timeout=120, check=True)
    try: src.unlink()
    except Exception: pass
    return out

def download_via_pytubefix(url: str, out_dir: Path) -> tuple:
    from pytubefix import YouTube
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else {}
    yt      = YouTube(url, use_po_token=False, proxies=proxies)
    title   = yt.title
    stream  = (yt.streams.filter(only_audio=True).order_by("abr").last()
               or yt.streams.filter(progressive=True).order_by("resolution").last())
    if not stream:
        raise RuntimeError("No stream de audio disponible")
    out_file = Path(stream.download(output_path=str(out_dir),
                                    filename=f"ptx_{uuid.uuid4().hex[:8]}"))
    return title, out_file

# ── /register-backend  (llamado por el script de tu PC) ──────────────────────
@app.route("/register-backend", methods=["POST"])
def register_backend():
    secret = os.environ.get("ADMIN_SECRET", "")
    data   = request.get_json(force=True)
    if not secret or data.get("secret") != secret:
        return jsonify({"error": "No autorizado"}), 403
    url = data.get("url", "").rstrip("/")
    _remote["url"] = url
    print(f"  [PROXY] Backend local registrado: {url}")
    return jsonify({"status": "ok", "backend": url})

@app.route("/unregister-backend", methods=["POST"])
def unregister_backend():
    secret = os.environ.get("ADMIN_SECRET", "")
    data   = request.get_json(force=True)
    if not secret or data.get("secret") != secret:
        return jsonify({"error": "No autorizado"}), 403
    _remote["url"] = None
    print("  [PROXY] Backend local desconectado")
    return jsonify({"status": "ok"})

# ── Frontend ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file("index.html")

# ── /convert ──────────────────────────────────────────────────────────────────
@app.route("/convert", methods=["POST"])
def convert():
    # ── Modo proxy: redirigir al backend local ────────────────────────────────
    rb = _remote["url"]
    if rb:
        try:
            r = req.post(f"{rb}/convert", json=request.get_json(force=True), timeout=190)
            return jsonify(r.json()), r.status_code
        except Exception as e:
            _remote["url"] = None   # marcar como caído
            return jsonify({"status": "error", "error": f"Tu PC se desconectó: {e}"}), 503

    # ── Modo nube: intentar descarga directa ──────────────────────────────────
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
    title    = "audio"
    wav_file = None
    ytdlp_ok = False

    try:
        cmd_title = [sys.executable, "-m", "yt_dlp"] + yt_dlp_flags() + ["--get-title", url]
        r = subprocess.run(cmd_title, capture_output=True, text=True, timeout=30)
        t = r.stdout.strip().split("\n")[0]
        if t: title = t

        out_name = f"{safe_filename(title)}_{file_id}"
        out_tmpl = str(DOWNLOADS_DIR / f"{out_name}.%(ext)s")
        cmd = [sys.executable, "-m", "yt_dlp"] + yt_dlp_flags() + [
            "--format", "bestaudio/best", "-x", "--audio-format", "wav",
            "-o", out_tmpl, url,
        ]
        if FFMPEG_DIR:
            cmd += ["--ffmpeg-location", FFMPEG_DIR]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode == 0:
            wavs = sorted(DOWNLOADS_DIR.glob(f"{out_name}*.wav"),
                          key=lambda x: x.stat().st_mtime, reverse=True)
            if wavs:
                wav_file = wavs[0]
                ytdlp_ok = True

        if not ytdlp_ok:
            err_lower = (result.stderr + result.stdout).lower()
            if "sign in" not in err_lower and "bot" not in err_lower:
                err = (result.stderr or result.stdout or "Error desconocido")[-600:]
                return jsonify({"status": "error", "error": err}), 500

    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "error": "Tiempo agotado (3 min)."}), 504
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500

    if not ytdlp_ok:
        if not video_id:
            return jsonify({"status": "error", "error": "No se pudo extraer el ID del video"}), 400
        try:
            title, raw_file = download_via_pytubefix(url, DOWNLOADS_DIR)
            wav_file = convert_to_wav(raw_file, ffmpeg)
        except Exception as e:
            traceback.print_exc()
            return jsonify({"status": "error",
                            "error": "Tu PC no está conectada. Enciéndela para usar el convertidor."}), 503

    with open(wav_file, "rb") as f:
        header = f.read(4)
    if header != b"RIFF":
        try: wav_file = convert_to_wav(wav_file, ffmpeg)
        except Exception as e:
            return jsonify({"status": "error", "error": f"Conversión WAV falló: {e}"}), 500

    return jsonify({"status": "ok", "filename": wav_file.name,
                    "title": title, "size": wav_file.stat().st_size})

# ── /download/<filename> ──────────────────────────────────────────────────────
@app.route("/download/<filename>")
def download(filename):
    # Modo proxy
    rb = _remote["url"]
    if rb:
        try:
            r = req.get(f"{rb}/download/{Path(filename).name}", stream=True, timeout=120)
            return Response(r.iter_content(8192),
                            headers={k: v for k, v in r.headers.items()
                                     if k.lower() in ("content-type","content-disposition","content-length")},
                            status=r.status_code)
        except Exception as e:
            return jsonify({"error": str(e)}), 503

    safe_name = Path(filename).name
    safe      = DOWNLOADS_DIR / safe_name
    if not safe.exists() or not safe.is_file():
        return jsonify({"error": "Archivo no encontrado"}), 404
    dl_name = safe_name if safe_name.lower().endswith(".wav") else safe_name.rsplit(".", 1)[0] + ".wav"
    resp = make_response(send_file(safe, mimetype="audio/wav", as_attachment=True, download_name=dl_name))
    resp.headers["Content-Type"]           = "audio/wav"
    resp.headers["Content-Disposition"]    = f'attachment; filename="{dl_name}"'
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

# ── /health ───────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({
        "status":         "ok",
        "v":              "13",
        "ffmpeg":         FFMPEG_DIR or shutil.which("ffmpeg") or "no",
        "cookies":        "ok" if (COOKIES_FILE and Path(COOKIES_FILE).exists()) else "no",
        "remote_backend": _remote["url"] or "no",
    })

@app.errorhandler(500)
def internal_error(_error):
    traceback.print_exc()
    return jsonify({"status": "error", "error": "Internal Server Error"}), 500

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n{'='*60}\n  WAVify  →  http://localhost:{port}\n{'='*60}\n")
    app.run(host="0.0.0.0", port=port, debug=True)
