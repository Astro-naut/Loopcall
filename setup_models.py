#!/usr/bin/env python3
"""Download the open models once. After this, calls run fully offline.

  models/kokoro-v1.0.int8.onnx, voices-v1.0.bin   Kokoro-82M voice (Apache-2.0)
  models/whisper-hinglish-swift/                   Oriserve Whisper-Hindi2Hinglish-Swift
                                                   (Apache-2.0), converted to CTranslate2
Gemma itself comes from Ollama:  ollama pull gemma4:e4b
"""
import subprocess
import sys
import urllib.request
from pathlib import Path

MODELS = Path(__file__).parent / "models"
KOKORO = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
SWIFT = "Oriserve/Whisper-Hindi2Hinglish-Swift"
SWIFT_DIR = MODELS / "whisper-hinglish-swift"

CONVERT_HELP = f"""
The speech-to-text model must be converted once to CTranslate2 (needs PyTorch,
so do it in a throwaway environment and delete it afterwards):

  python -m venv %LOCALAPPDATA%\\ct2conv
  %LOCALAPPDATA%\\ct2conv\\Scripts\\pip install torch --index-url https://download.pytorch.org/whl/cpu
  %LOCALAPPDATA%\\ct2conv\\Scripts\\pip install "transformers<5" ctranslate2
  %LOCALAPPDATA%\\ct2conv\\Scripts\\ct2-transformers-converter --model {SWIFT} ^
      --output_dir models\\whisper-hinglish-swift --quantization int8 ^
      --copy_files tokenizer.json preprocessor_config.json
"""


def fetch(url, dest):
    if dest.exists():
        print(f"ok    {dest.name}")
        return
    print(f"get   {dest.name} ...")
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.rename(dest)


def main():
    MODELS.mkdir(exist_ok=True)
    for name in ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin"):
        fetch(KOKORO + name, MODELS / name)

    if (SWIFT_DIR / "model.bin").exists():
        print(f"ok    {SWIFT_DIR.name}")
    else:
        try:
            import ctranslate2.converters  # noqa: F401
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError:
            print(CONVERT_HELP)
            sys.exit(1)
        subprocess.check_call([
            sys.executable, "-m", "ctranslate2.converters.transformers",
            "--model", SWIFT, "--output_dir", str(SWIFT_DIR), "--quantization", "int8",
            "--copy_files", "tokenizer.json", "preprocessor_config.json"])
    print("\nAll models ready. Also run once:  ollama pull gemma4:e4b")


if __name__ == "__main__":
    main()
