"""tests/conftest.py — общие настройки pytest."""
import sys
from pathlib import Path

# Добавляем корень проекта в путь чтобы импорты работали
sys.path.insert(0, str(Path(__file__).parent.parent))
