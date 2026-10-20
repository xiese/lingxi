"""笔记路径等基础服务辅助。"""
from pathlib import Path
from ..config import NOTES_DIR, KB_ROOT

__all__ = ["NOTES_DIR", "KB_ROOT", "note_path"]

def note_path(rel: str) -> Path:
    return (NOTES_DIR / rel).resolve()
