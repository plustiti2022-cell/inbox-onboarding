"""Small DeepSeek tool-calling agent, sharing this project's Lark OAuth client."""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT / "onboarding_source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
