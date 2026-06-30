"""Download the embedding model to a local directory for offline use."""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)


def main():
    from cli.tools.embedding import _EMBEDDING_MODEL_NAME, _resolve_model_path

    model_path = _resolve_model_path(_EMBEDDING_MODEL_NAME)
    if model_path:
        print(f"Model already cached at: {model_path}")
        return

    print(f"Downloading {_EMBEDDING_MODEL_NAME} ...")
    print("Set HF_ENDPOINT env var to use a mirror (e.g., https://hf-mirror.com)")

    target_dir = os.environ.get("CODEWOOD_MODELS_DIR", "").strip()
    if not target_dir:
        target_dir = os.path.join(ROOT_DIR, "models", _EMBEDDING_MODEL_NAME)
    os.makedirs(target_dir, exist_ok=True)

    import sentence_transformers
    model = sentence_transformers.SentenceTransformer(_EMBEDDING_MODEL_NAME, device="cpu")
    model.save(target_dir)
    print(f"Model saved to: {target_dir}")


if __name__ == "__main__":
    main()
