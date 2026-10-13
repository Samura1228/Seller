from functools import cache
from pathlib import Path

_DIR = Path(__file__).parent


@cache
def load(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
