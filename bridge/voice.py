"""Voice support: speech-to-text (Whisper) and text-to-speech (Piper).

All dependencies are pip-installable. Models are auto-downloaded on first use
and cached in the user's home directory. No manual setup required.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hermes_bridge.voice")

# --- Lazy singletons ---------------------------------------------------------

_whisper_model = None
_piper_voices: dict[str, str] = {}

# --- Edge TTS voice mapping (ISO 639-1 → Edge Neural voice name) -------------

EDGE_VOICE_MAP: dict[str, str] = {
    "en": "en-US-AriaNeural",
    "es": "es-ES-ElviraNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "it": "it-IT-ElsaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "nl": "nl-NL-ColetteNeural",
    "pl": "pl-PL-ZofiaNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "tr": "tr-TR-EmelNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "ar": "ar-SA-ZariyahNeural",
    "cs": "cs-CZ-VlastaNeural",
    "el": "el-GR-AthinaNeural",
    "fi": "fi-FI-SelmaNeural",
    "hu": "hu-HU-NoemiNeural",
    "no": "nb-NO-PernilleNeural",
    "ro": "ro-RO-AlinaNeural",
    "sv": "sv-SE-SofieNeural",
    "vi": "vi-VN-HoaiMyNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "hi": "hi-IN-SwaraNeural",
}


def _get_whisper():
    """Lazy-load the Whisper model (downloads on first call)."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        model_size = os.environ.get("WHISPER_MODEL", "base")
        device = os.environ.get("WHISPER_DEVICE", "cpu")
        compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
        logger.info("Loading Whisper model '%s' (device=%s, compute=%s)", model_size, device, compute_type)
        _whisper_model = WhisperModel(model_size, device=device, compute_type=compute_type)
    return _whisper_model


def _get_ffmpeg() -> str:
    """Return path to ffmpeg — system install if available, else bundled."""
    import shutil

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    from imageio_ffmpeg import get_ffmpeg_exe

    return get_ffmpeg_exe()


def _detect_language(text: str) -> str:
    """Detect the language of text, return ISO 639-1 code. Falls back to 'en'."""
    try:
        from langdetect import detect

        lang = detect(text)
        return lang if lang in EDGE_VOICE_MAP else "en"
    except Exception:
        return "en"


def _get_edge_voice(lang: str) -> str:
    """Return the Edge TTS voice name for a language code."""
    return EDGE_VOICE_MAP.get(lang, EDGE_VOICE_MAP["en"])


def transcribe(audio_path: Path) -> str:
    """Transcribe an audio file using faster-whisper. Returns transcript text."""
    # Convert to 16kHz mono wav using ffmpeg (handles webm/opus from browser)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = Path(tmp.name)

    try:
        ffmpeg = _get_ffmpeg()
        subprocess.run(
            [ffmpeg, "-i", str(audio_path), "-ar", "16000", "-ac", "1", "-y", str(wav_path)],
            check=True,
            capture_output=True,
        )

        model = _get_whisper()
        segments, _info = model.transcribe(str(wav_path), beam_size=5)
        text = " ".join(segment.text for segment in segments).strip()
        logger.info("Transcribed %d chars from audio", len(text))
        return text
    finally:
        wav_path.unlink(missing_ok=True)


async def synthesize(text: str, lang: Optional[str] = None) -> Path:
    """Synthesize speech from text using Edge TTS (Microsoft Neural voices). Returns path to mp3."""
    if not text.strip():
        raise ValueError("Cannot synthesize empty text")

    if lang is None:
        lang = _detect_language(text)

    voice = _get_edge_voice(lang)
    out_path = Path(tempfile.mktemp(suffix=".mp3"))

    try:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(out_path))
        logger.info("Synthesized %d chars via Edge TTS voice '%s'", len(text), voice)
        return out_path
    except Exception as e:
        out_path.unlink(missing_ok=True)
        raise
