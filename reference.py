"""Model reading of each script line with a local TTS (Kokoro-82M via kokoro-onnx), cached per line.

Optional: needs `uv sync --extra reference` (kokoro-onnx pulls in phonemizer and espeak-ng, both GPL-3.0).
"""

import hashlib
import os
import urllib.request
from pathlib import Path

import soundfile as sf

import store

VOICE = os.environ.get("PRON_TTS_VOICE", "af_heart")
MODEL_DIR = Path(os.environ.get("PRON_TTS_DIR", "~/.cache/pronunciation-practice/kokoro")).expanduser()
# Full-precision model: the int8 export drifts from fp32 (spectral correlation 0.916 per its release notes).
FILES = {"kokoro-v1.0.onnx": "beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a",
         "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"}
URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/"
_engine = None


def available() -> bool:
    try:
        import kokoro_onnx  # noqa: F401
    except ImportError:
        return False
    return True


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _model(name: str) -> Path:
    path = MODEL_DIR / name
    if not path.exists():
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(URL + name, tmp)
        if _sha256(tmp) != FILES[name]:
            tmp.unlink()
            raise RuntimeError(f"checksum mismatch for {name}")
        tmp.rename(path)
    return path


def _tts():
    global _engine
    if _engine is None:
        from kokoro_onnx import Kokoro

        _engine = Kokoro(str(_model("kokoro-v1.0.onnx")), str(_model("voices-v1.0.bin")))
    return _engine


def key(text: str) -> str:
    return hashlib.sha256(f"{VOICE}\n{text}".encode()).hexdigest()[:16]


def line_audio(text: str) -> Path:
    """WAV of the model reading, generated once per (voice, line)."""
    path = store.DATA / "reference" / f"{key(text)}.wav"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        x, sr = _tts().create(text, voice=VOICE, lang="en-us")
        tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp.wav")  # GUI and server may race on a line
        sf.write(tmp, x, sr)
        tmp.replace(path)
    return path


def phonemes(text: str) -> str:
    """The TTS's own IPA for the line (espeak-ng, American English)."""
    from kokoro_onnx.tokenizer import Tokenizer

    return Tokenizer().phonemize(text, "en-us")
