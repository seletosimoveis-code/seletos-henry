"""
audio.py
========
Transcrição de áudios WhatsApp com OpenAI Whisper.

Fluxo:
  Z-API envia webhook com audio.audioUrl
  → download do arquivo de áudio (OGG/OPUS)
  → envio para OpenAI Whisper (whisper-1)
  → retorno do texto transcrito em português

Custo: ~$0.006 por minuto de áudio (praticamente zero).
"""

import io
import logging
import requests
from openai import OpenAI
from config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

_client = OpenAI(api_key=OPENAI_API_KEY)


def transcribe_audio_url(url: str, mime_type: str = "audio/ogg") -> str | None:
    """
    Baixa o áudio da URL fornecida pela Z-API e transcreve com Whisper.

    Args:
        url:       URL temporária do áudio (expira em 30 dias no Z-API)
        mime_type: MIME type do arquivo (ex: 'audio/ogg; codecs=opus')

    Returns:
        Texto transcrito, ou None em caso de falha.
    """
    if not url:
        return None

    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY não configurada — transcrição de áudio desabilitada")
        return None

    try:
        # 1. Baixa o arquivo de áudio
        logger.info(f"Baixando áudio: {url[:80]}...")
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        audio_bytes = r.content
        logger.info(f"Áudio baixado: {len(audio_bytes)} bytes")

        # 2. Determina a extensão correta pelo mime_type
        ext = _ext_from_mime(mime_type)

        # 3. Cria objeto de arquivo em memória (Whisper aceita BytesIO com .name)
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = f"audio.{ext}"

        # 4. Transcreve com Whisper — language="pt" aumenta precisão para português
        result = _client.audio.transcriptions.create(
            model    = "whisper-1",
            file     = audio_file,
            language = "pt",
        )

        transcript = (result.text or "").strip()
        if not transcript:
            logger.warning("Whisper retornou transcrição vazia")
            return None

        logger.info(f"Transcrito: '{transcript[:100]}{'...' if len(transcript) > 100 else ''}'")
        return transcript

    except requests.RequestException as e:
        logger.error(f"Erro ao baixar áudio: {e}")
        return None
    except Exception as e:
        logger.error(f"Erro na transcrição Whisper: {e}")
        return None


def _ext_from_mime(mime_type: str) -> str:
    """Retorna a extensão de arquivo correta para o mime_type."""
    mt = (mime_type or "").lower()
    if "ogg" in mt:
        return "ogg"
    if "mp4" in mt or "m4a" in mt:
        return "mp4"
    if "mpeg" in mt or "mp3" in mt:
        return "mp3"
    if "wav" in mt:
        return "wav"
    if "webm" in mt:
        return "webm"
    return "ogg"  # padrão WhatsApp
