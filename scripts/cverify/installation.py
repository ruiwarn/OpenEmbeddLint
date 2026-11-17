import os
from pathlib import Path
import sys


def user_data_directory():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "OpenEmbeddLint"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/OpenEmbeddLint"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "openembeddlint"


def tool_directory(skill_dir, target):
    configured = os.environ.get("OPENEMBEDDLINT_TOOLS_DIR")
    if configured:
        directory = Path(configured)
        if not directory.is_absolute():
            raise ValueError("OPENEMBEDDLINT_TOOLS_DIR must be an absolute toolchain parent directory")
        return directory / target
    bundled = skill_dir / "tools/platforms" / target
    return bundled if bundled.is_dir() else user_data_directory() / "tools/platforms" / target


def misra_rule_texts():
    configured = os.environ.get("OPENEMBEDDLINT_MISRA_RULE_TEXTS")
    if configured:
        path = Path(configured)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("OPENEMBEDDLINT_MISRA_RULE_TEXTS must name an existing absolute rule-text file")
        return path.resolve()
    path = user_data_directory() / "misra-c2012-rule-texts.txt"
    if path.exists() and not path.is_file():
        raise ValueError("local MISRA rule texts path is not a file: " + str(path))
    return path.resolve()
