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

# ── Cookies de YouTube (para evitar bloqueo de bot en producción) ─────────────
COOKIES_FILE = None

def setup_cookies():
    global COOKIES_FILE
    cookies_b64 = os.environ.get("YOUTUBE_COOKIES_B64", "")
    if cookies_b64:
        try:
            cookies_data = base64.b64decode(cookies_b64).decode("utf-8")
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            )
            tmp.write(cookies_data)
            tmp.close()
            COOKIES_FILE = tmp.name
            print(f"  Cookies: cargadas desde YOUTUBE_COOKIES_B64 → {COOKIES_FILE}")
        except Exception as e:
            print(f"  Cookies: error al cargar ({e})")
    else:
        print("  Cookies: YOUTUBE_COOKIES_B64 no configurada (solo funciona local)")

setup_cookies()

# ── Detectar FFmpeg (local + Railway) ─────────────────────────────────────────
def find_ffmpeg():
    # 1. En el PATH (Railway lo instala aquí vía Dockerfile/nixpacks)
    if shutil.which("ffmpeg"):
        return str(Path(shutil.which("ffmpeg")).parent)
    # 2. Rutas locales de Windows
    WINDOWS_PATHS = [
        r"C:\Users\ttrac\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin",
        r"C:\ProgramData\chocolatey\bin",
        r"C:\tools\ffmpeg\bin",
        r"C:\ffmpeg\bin",
    ]
    for p in WINDOWS_PATHS:
        if (Path(p) / "ffmpeg.exe").exists():
            return p
    # 3. Buscar en WinGet packages
    winget_root = Path(r"C:\Users\ttrac\AppData\Local\Microsoft\WinGet\Packages")
    if winget_root.exists():
        for f in winget_root.rglob("ffmpeg.exe"):
            return str(f.parent)
    return None

FFMPEG_DIR = find_ffmpeg()
print(f"  FFmpeg: {FFMPEG_DIR or 'NO ENCONTRADO – el servidor debe tenerlo en PATH'}")


# ── Limpieza automática (1 hora) ───────────────────────────────────────────────
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


# ── Sanitizar nombre de archivo ────────────────────────────────────────────────
def safe_filename(title: str) -> str:
    """Convierte el título del video en un nombre de archivo seguro."""
    name = re.sub(r'[\\/*?:"<>|]', "", title)   # quitar chars inválidos
    name = re.sub(r'\s+', "_", name.strip())     # espacios → guiones bajos
    name = name[:80]                              # máximo 80 chars
    return name or "audio"


# ── Obtener título del video ───────────────────────────────────────────────────
def get_video_title(url: str) -> str:
    """Obtiene el título del video con yt-dlp sin descargarlo."""
    try:
        cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist",
               "--get-title", "--no-warnings", url]
        if COOKIES_FILE:
            cmd += ["--cookies", COOKIES_FILE]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        title = result.stdout.strip().split("\n")[0]
        return title if title else "audio"
    except Exception:
        return "audio"


# ── Servir el frontend ─────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file("index.html")


# ── /convert ──────────────────────────────────────────────────────────────────
@app.route("/convert", methods=["POST"])
def convert():
    data    = request.get_json(force=True)
    url     = data.get("url", "").strip()
    quality = data.get("quality", "192")

    if not url:
        return jsonify({"status": "error", "error": "URL requerida"}), 400
    if "youtube.com" not in url and "youtu.be" not in url:
        return jsonify({"status": "error", "error": "Solo URLs de YouTube"}), 400
    if quality not in ("128", "192", "256", "320"):
        quality = "192"

    ffmpeg_dir = FFMPEG_DIR or ""
    if not ffmpeg_dir and not shutil.which("ffmpeg"):
        return jsonify({"status": "error", "error": "FFmpeg no instalado en el servidor"}), 500

    # Obtener título para el nombre del archivo
    title    = get_video_title(url)
    basename = safe_filename(title)
    file_id  = uuid.uuid4().hex[:8]
    out_name = f"{basename}_{file_id}"
    out_tmpl = str(DOWNLOADS_DIR / f"{out_name}.%(ext)s")

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "--format", "bestaudio/best",
        "-x",
        "--audio-format", "wav",
        "--no-warnings",
        "-o", out_tmpl,
        url,
    ]
    if ffmpeg_dir:
        cmd += ["--ffmpeg-location", ffmpeg_dir]
    if COOKIES_FILE:
        cmd += ["--cookies", COOKIES_FILE]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        # Sanitizar para evitar errores de consola en Windows
        stdout_safe = result.stdout[:400].encode('ascii', 'ignore').decode()
        stderr_safe = result.stderr[:400].encode('ascii', 'ignore').decode()
        print(f"STDOUT: {stdout_safe}")
        print(f"STDERR: {stderr_safe}")

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "Error desconocido")[-600:]
            err_safe = err.encode('ascii', 'ignore').decode()
            print(f"ERROR yt-dlp: {err_safe}")
            return jsonify({"status": "error", "error": err}), 500

        wav_files = sorted(
            DOWNLOADS_DIR.glob(f"{out_name}*.wav"),
            key=lambda x: x.stat().st_mtime,
            reverse=True
        )
        if not wav_files:
            try:
                files = [f.name for f in DOWNLOADS_DIR.iterdir()]
                print(f"Archivos en downloads: {str(files).encode('ascii', 'ignore').decode()}")
            except:
                pass
            return jsonify({"status": "error", "error": "WAV no generado"}), 500

        final = wav_files[0]

        # Verificar header RIFF (WAV real)
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
        print("!!! EXTREME ERROR IN /convert !!!")
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500


@app.errorhandler(500)
def internal_error(error):
    print("!!! 500 INTERNAL SERVER ERROR !!!")
    traceback.print_exc()
    return jsonify({"status": "error", "error": "Internal Server Error"}), 500


# ── /download/<filename> ───────────────────────────────────────────────────────
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


# ── /health ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "ffmpeg": FFMPEG_DIR or shutil.which("ffmpeg") or "no encontrado"})


# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Render usa la variable de entorno PORT
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "=" * 60)
    print("  >>> WAVify Backend")
    print(f"  >>> URL local: http://localhost:{port}")
    print(f"  >>> FFmpeg: {FFMPEG_DIR or 'PATH del sistema'}")
    print("=" * 60 + "\n")
    # debug=False para producción (Render)
    app.run(host="0.0.0.0", port=port, debug=True)
