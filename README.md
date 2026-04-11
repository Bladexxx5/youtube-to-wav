# WAVify – YouTube to WAV Converter

Convierte cualquier video de YouTube a WAV de alta calidad en segundos.

## 🚀 Instalación rápida

### 1. Instalar dependencias Python
```bash
pip install -r requirements.txt
```

### 2. Instalar FFmpeg (necesario para la conversión)
- **Windows**: Descarga desde https://ffmpeg.org/download.html  
  Extrae `ffmpeg.exe` en esta misma carpeta **o** añádelo al PATH del sistema.
- **Mac**: `brew install ffmpeg`
- **Linux**: `sudo apt install ffmpeg`

### 3. Iniciar el backend
```bash
python app.py
```

### 4. Abrir la web
Abre `index.html` directamente en el navegador (doble clic).

---

## 🎯 Uso
1. Pega la URL de YouTube en el campo
2. Selecciona la calidad (192 / 256 / 320 kbps)
3. Haz clic en **Convertir a WAV**
4. Descarga tu archivo cuando esté listo

---

## ⚙️ Estructura
```
converter youtube to wav/
├── index.html        ← Frontend (abre en navegador)
├── app.py            ← Backend Flask
├── requirements.txt  ← Dependencias Python
├── downloads/        ← Archivos WAV generados (auto-limpieza 1h)
└── README.md
```

## 🔧 Modo Demo
Si el backend no está corriendo, el frontend funciona en **modo demo** mostrando
la barra de progreso animada. Para conversión real necesitas el backend activo.

## 📋 Notas
- Los archivos se eliminan automáticamente después de 1 hora
- Solo para contenido libre de derechos o uso personal
- yt-dlp se actualiza frecuentemente: `pip install -U yt-dlp`
