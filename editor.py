import os
from dotenv import load_dotenv

load_dotenv()

EDIT_PROMPT = (
    "Edit this product image: change the 3D printed product/object color to red. "
    "Replace the background with a clean, natural neutral studio backdrop — a soft "
    "light grey-to-white gradient like professional product photography. "
    "Keep the exact same product shape, design, structure, and all fine details "
    "completely unchanged. Only recolor the product to red and make the background a "
    "natural neutral color — do not alter the product geometry or details."
)

_client = None

def _get_client():
    global _client
    if _client is None:
        from google import genai
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set. Add it to your .env file.")
        _client = genai.Client(api_key=api_key)
    return _client


def edit_image(source_path: str) -> str:
    """Send image to Gemini for red color editing. Returns path to edited image."""
    from google.genai import types

    with open(source_path, "rb") as f:
        image_bytes = f.read()

    mime = "image/jpeg" if source_path.lower().endswith((".jpg", ".jpeg")) else "image/png"
    client = _get_client()

    response = client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime),
            EDIT_PROMPT,
        ],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"]
        ),
    )

    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.mime_type.startswith("image/"):
            # The google-genai SDK returns raw image bytes (not base64-encoded)
            edited_bytes = part.inline_data.data
            base = os.path.splitext(source_path)[0]
            out_path = base + "_edited.png"
            with open(out_path, "wb") as f:
                f.write(edited_bytes)
            return out_path

    raise RuntimeError("Gemini did not return an image in its response")


def edit_all(image_paths: list[str]) -> list[str]:
    """Edit each image and return list of edited paths."""
    edited = []
    for path in image_paths:
        try:
            edited.append(edit_image(path))
        except Exception as e:
            print(f"[editor] Failed to edit {path}: {e}")
    return edited
