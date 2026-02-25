import argparse
import base64
import mimetypes
import os

from openai import OpenAI

from config import EXPLORER_OPENAI_BASE_URL, EXPLORER_OPENAI_MODEL_NAME
from secret_api_keys import API_OPENAI_EXPLORER


def _load_image_as_data_url(image_path: str) -> str:
    mime_type, _ = mimetypes.guess_type(image_path)
    if not mime_type:
        mime_type = "image/png"
    with open(image_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _resolve_defaults() -> tuple[str, str, str | None]:
    return API_OPENAI_EXPLORER, EXPLORER_OPENAI_MODEL_NAME, EXPLORER_OPENAI_BASE_URL or None


def main() -> None:
    parser = argparse.ArgumentParser(description="Test image input on an OpenAI-compatible endpoint.")
    parser.add_argument(
        "--image",
        default=os.path.join("ui", "agent-images", "explorer.jpg"),
        help="Path to the image file",
    )
    parser.add_argument("--model", default=None, help="Override model name")
    parser.add_argument("--base-url", default=None, help="Override base URL")
    parser.add_argument("--prompt", default="Describe this image briefly.")
    parser.add_argument("--max-tokens", type=int, default=300)
    args = parser.parse_args()

    if not os.path.exists(args.image):
        raise SystemExit(f"Image not found: {args.image}")

    api_key, default_model, default_base_url = _resolve_defaults()
    model = args.model or default_model
    base_url = args.base_url or default_base_url

    client = OpenAI(api_key=api_key, base_url=base_url)
    image_url = _load_image_as_data_url(args.image)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": args.prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
        temperature=0.2,
        max_tokens=args.max_tokens,
    )

    text = response.choices[0].message.content if response.choices else ""
    print("Model:", getattr(response, "model", model))
    if getattr(response, "usage", None):
        print("Usage:", response.usage)
    print("Response:")
    print(text or "<empty>")


if __name__ == "__main__":
    main()
