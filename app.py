"""
WAVify – Backend Flask
Funciona local y en Railway/Render/Fly.io
Inicia local:  python app.py  → http://localhost:5000
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
from flask import Flask, request, jsonify, send_file, make_response
from flask_cors import CORS
import traceback

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

# ── Cookies de YouTube ────────────────────────────────────────────────────────
COOKIES_FILE = None
COOKIES_LINES = 0

def setup_cookies():
    global COOKIES_FILE, COOKIES_LINES
    cookies_b64 = os.environ.get("YOUTUBE_COOKIES_B64", "")
    if not cookies_b64:
        print("  Cookies: YOUTUBE_COOKIES_B64 no configurada")
        return
    try:
        cookies_data = base64.b64decode(cookies_b64).decode("utf-8")
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        tmp.write(cookies_data)
        tmp.close()
        COOKIES_FILE  = tmp.name
        COOKIES_LINES = sum(1 for l in cookies_data.splitlines() if l and not l.startswith("#"))
        print(f"  Cookies: {COOKIES_LINES} entradas cargadas → {COOKIES_FILE}")
    except Exception as e:
        print(f"  Cookies: ERROR al cargar – {e}")

setup_cookies()

# ── Endpoint para actualizar cookies sin redeployar ───────────────────────────
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
        cookies_data = base64.b64decode(b64).decode("utf-8")
        global COOKIES_FILE, COOKIES_LINES
        if COOKIES_FILE and Path(COOKIES_FILE).exists():
            Path(COOKIES_FILE).unlink()
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        tmp.write(cookies_data)
        tmp.close()
        COOKIES_FILE  = tmp.name
        COOKIES_LINES = sum(1 for l in cookies_data.splitlines() if l and not l.startswith("#"))
        return jsonify({"status": "ok", "cookies_lines": COOKIES_LINES})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Detectar FFmpeg ───────────────────────────────────────────────────────────
def find_ffmpeg():
    if shutil.which("ffmpeg"):
        return str(Path(shutil.which("ffmpeg")).parent)
    WINDOWS_PATHS = [
        r"C:\Users\ttrac\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin",
        r"C:\ProgramData\chocolatey\bin",
        r"C:\tools\ffmpeg\bin",
        r"C:\ffmpeg\bin",
    ]
    for p in WINDOWS_PATHS:
        if (Path(p) / "ffmpeg.exe").exists():
            return p
    winget_root = Path(r"C:\Users\ttrac\AppData\Local\Microsoft\WinGet\Packages")
    if winget_root.exists():
        for f in winget_root.rglob("ffmpeg.exe"):
            return str(f.parent)
    return None

FFMPEG_DIR = find_ffmpeg()
print(f"  FFmpeg: {FFMPEG_DIR or 'NO ENCONTRADO'}")


# ── Limpieza automática (1 hora) ──────────────────────────────────────────────
def cleanup_old_files():
    while True:
        time.sleep(1800)
        for f in DOWNLOADS_DIR.iterdir():
            if f.is_file() and (time.time() - f.stat().st_mtime) > 3600:
                try:
                    f.unlink()
                except Exception:
                    pass

threading.Thread(target=cleanup_old_files, daemon=True).start()


# ── Sanitizar nombre de archivo ───────────────────────────────────────────────
def safe_filename(title: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", title)
    name = re.sub(r'\s+', "_", name.strip())
    return (name[:80] or "audio")


# ── Flags comunes de yt-dlp ───────────────────────────────────────────────────
def yt_dlp_base_flags():
    flags = [
        "--no-playlist",
        "--no-warnings",
        "--extractor-args", "youtube:player_client=web,default",
    ]
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        flags += ["--cookies", COOKIES_FILE]
    return flags


# ── Obtener título del video ──────────────────────────────────────────────────
def get_video_title(url: str) -> str:
    try:
        cmd = [sys.executable, "-m", "yt_dlp"] + yt_dlp_base_flags() + ["--get-title", url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        title = result.stdout.strip().split("\n")[0]
        return title if title else "audio"
    except Exception:
        return "audio"


# ── Servir el frontend ────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file("index.html")


# ── /convert ──────────────────────────────────────────────────────────────────
@app.route("/convert", methods=["POST"])
def convert():
    data    = request.get_json(force=True)
    url     = data.get("url", "").strip()

    if not url:
        return jsonify({"status": "error", "error": "URL requerida"}), 400
    if "youtube.com" not in url and "youtu.be" not in url:
        return jsonify({"status": "error", "error": "Solo URLs de YouTube"}), 400

    ffmpeg_dir = FFMPEG_DIR or ""
    if not ffmpeg_dir and not shutil.which("ffmpeg"):
        return jsonify({"status": "error", "error": "FFmpeg no instalado en el servidor"}), 500

    title    = get_video_title(url)
    basename = safe_filename(title)
    file_id  = uuid.uuid4().hex[:8]
    out_name = f"{basename}_{file_id}"
    out_tmpl = str(DOWNLOADS_DIR / f"{out_name}.%(ext)s")

    cmd = [sys.executable, "-m", "yt_dlp"] + yt_dlp_base_flags() + [
        "--format", "bestaudio/best",
        "-x",
        "--audio-format", "wav",
        "-o", out_tmpl,
        url,
    ]
    if ffmpeg_dir:
        cmd += ["--ffmpeg-location", ffmpeg_dir]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        stdout_safe = result.stdout[:500].encode("ascii", "ignore").decode()
        stderr_safe = result.stderr[:500].encode("ascii", "ignore").decode()
        print(f"STDOUT: {stdout_safe}")
        print(f"STDERR: {stderr_safe}")

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "Error desconocido")[-600:]
            print(f"ERROR yt-dlp: {err.encode('ascii','ignore').decode()}")
            return jsonify({"status": "error", "error": err}), 500

        wav_files = sorted(
            DOWNLOADS_DIR.glob(f"{out_name}*.wav"),
            key=lambda x: x.stat().st_mtime,
            reverse=True
        )
        if not wav_files:
            files = [f.name for f in DOWNLOADS_DIR.iterdir()]
            print(f"Archivos en downloads: {str(files).encode('ascii','ignore').decode()}")
            return jsonify({"status": "error", "error": "WAV no generado"}), 500

        final = wav_files[0]

        # Verificar header RIFF
        with open(final, "rb") as f:
            header = f.read(4)
        if header != b"RIFF":
            ffmpeg_exe = shutil.which("ffmpeg") or (str(Path(ffmpeg_dir) / "ffmpeg.exe") if ffmpeg_dir else None)
            if ffmpeg_exe:
                wav_out = final.with_name(final.stem + "_conv.wav")
                subprocess.run(
                    [ffmpeg_exe, "-y", "-i", str(final), "-ar", "44100", "-ac", "2", str(wav_out)],
                    capture_output=True, timeout=120
                )
                if wav_out.exists():
                    try:
                        final.unlink()
                    except Exception:
                        pass
                    final = wav_out

        return jsonify({
            "status":   "ok",
            "filename": final.name,
            "title":    title,
            "size":     final.stat().st_size,
        })

    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "error": "Tiempo agotado (3 min). El video es muy largo."}), 504
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500


@app.errorhandler(500)
def internal_error(error):
    traceback.print_exc()
    return jsonify({"status": "error", "error": "Internal Server Error"}), 500


# ── /download/<filename> ──────────────────────────────────────────────────────
@app.route("/download/<filename>")
def download(filename):
    safe_name = Path(filename).name
    safe      = DOWNLOADS_DIR / safe_name
    if not safe.exists() or not safe.is_file():
        return jsonify({"error": "Archivo no encontrado"}), 404
    dl_name = safe_name if safe_name.lower().endswith(".wav") else safe_name.rsplit(".", 1)[0] + ".wav"
    response = make_response(send_file(
        safe, mimetype="audio/wav", as_attachment=True, download_name=dl_name,
    ))
    response.headers["Content-Type"]           = "audio/wav"
    response.headers["Content-Disposition"]    = f'attachment; filename="{dl_name}"'
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ── /health ───────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({
        "status":       "ok",
        "v":            "7",
        "ffmpeg":       FFMPEG_DIR or shutil.which("ffmpeg") or "no encontrado",
        "cookies":      "ok" if (COOKIES_FILE and Path(COOKIES_FILE).exists()) else "no",
        "cookies_lines": COOKIES_LINES,
    })


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "=" * 60)
    print("  >>> WAVify Backend")
    print(f"  >>> URL local: http://localhost:{port}")
    print(f"  >>> FFmpeg: {FFMPEG_DIR or 'PATH del sistema'}")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=port, debug=True)
