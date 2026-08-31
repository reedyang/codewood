"""Download the ONNX embedding model to a local directory for offline use."""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)


def main():
    from cli.tools.embedding import _EMBEDDING_MODEL_NAME, _resolve_model_path, _default_model_dir, ensure_onnx_model

    model_path = _resolve_model_path(_EMBEDDING_MODEL_NAME)

    if model_path:
        print(f"Model already cached at: {model_path}")
        return

    print(f"Downloading {_EMBEDDING_MODEL_NAME} (ONNX) ...")
    print("Set HF_ENDPOINT env var to use a mirror (e.g., https://hf-mirror.com)")
    target_dir = model_path or _default_model_dir()
    if ensure_onnx_model(target_dir):
        print(f"Model saved to: {target_dir}")
    else:
        print("Failed to download model files.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
