"""Voice support: speech-to-text (Whisper) and text-to-speech (Piper).

All dependencies are pip-installable. Models are auto-downloaded on first use
and cached in the user's home directory. No manual setup required.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hermes_bridge.voice")

# --- Lazy singletons ---------------------------------------------------------

_whisper_model = None
_piper_voices: dict[str, str] = {}

# --- Piper voice mapping (ISO 639-1 → Piper voice name) ----------------------

PIPER_VOICE_MAP: dict[str, str] = {
    "en": "en_US-lessac-medium",
    "es": "es_ES-carlfm-x_low",
    "fr": "fr_FR-siwis-medium",
    "de": "de_DE-thorsten-medium",
    "it": "it_IT-riccardo-x_low",
    "pt": "pt_BR-faber-medium",
    "nl": "nl_NL-mls-medium",
    "pl": "pl_PL-gosia-medium",
    "ru": "ru_RU-dmitri-medium",
    "tr": "tr_TR-dfki-medium",
    "zh": "zh_CN-huayan-medium",
    "ar": "ar_JO-kareem-medium",
    "cs": "cs_CZ-jirka-medium",
    "el": "el_GR-rapunzelina-medium",
    "fi": "fi_FI-harri-medium",
    "hu": "hu_HU-anna-medium",
    "no": "no_NO-talesyntese-medium",
    "ro": "ro_RO-mihai-medium",
    "sv": "sv_SE-nst-medium",
    "vi": "vi_VN-vivos-x_low",
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
        return lang if lang in PIPER_VOICE_MAP else "en"
    except Exception:
        return "en"


def _get_piper_voice(lang: str) -> str:
    """Return the Piper voice model name for a language code."""
    return PIPER_VOICE_MAP.get(lang, PIPER_VOICE_MAP["en"])


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


def _get_voices_dir() -> Path:
    """Return the directory where Piper voice models are cached."""
    voices_dir = Path.home() / ".local" / "share" / "piper" / "voices"
    voices_dir.mkdir(parents=True, exist_ok=True)
    return voices_dir


def _get_voice_model_path(voice_name: str) -> Path:
    """Return the .onnx path for a Piper voice, downloading if needed."""
    voices_dir = _get_voices_dir()
    onnx_path = voices_dir / f"{voice_name}.onnx"
    if not onnx_path.exists():
        logger.info("Downloading Piper voice '%s'...", voice_name)
        result = subprocess.run(
            [sys.executable, "-m", "piper.download_voices", voice_name, "--data-dir", str(voices_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to download Piper voice '{voice_name}': {result.stderr or result.stdout}")
    return onnx_path


def synthesize(text: str, lang: Optional[str] = None) -> Path:
    """Synthesize speech from text using Piper TTS. Returns path to output wav."""
    if not text.strip():
        raise ValueError("Cannot synthesize empty text")

    if lang is None:
        lang = _detect_language(text)

    voice_name = _get_piper_voice(lang)
    out_path = Path(tempfile.mktemp(suffix=".wav"))

    try:
        from piper import PiperVoice

        model_path = _get_voice_model_path(voice_name)
        voice = PiperVoice.load(str(model_path))

        with wave.open(str(out_path), "wb") as wav_file:
            if hasattr(voice, "synthesize_wav"):
                voice.synthesize_wav(text, wav_file)
            else:
                voice.synthesize(text, wav_file)

        logger.info("Synthesized %d chars in language '%s' with voice '%s'", len(text), lang, voice_name)
        return out_path
    except Exception as e:
        out_path.unlink(missing_ok=True)
        raise
