import re
import unicodedata

def normalize_text(text: str) -> str:
    """Return text without accents, in lowercase and without punctuation."""
    if not isinstance(text, str):
        return ''
    # Remove accents and convert to lowercase
    normalized = unicodedata.normalize('NFD', text)
    normalized = ''.join(ch for ch in normalized if unicodedata.category(ch) != 'Mn')
    normalized = normalized.lower()
    # Remove punctuation
    normalized = re.sub(r'[\W_]+', ' ', normalized)
    return normalized.strip()

