from dataclasses import dataclass, field
import json
import os
import sys
from pathlib import Path
import tempfile
from uuid import uuid4

from .paths import load_environment, preferences_path, resource_root, user_data_dir


ROOT = resource_root()


def project_path(value: str) -> Path:
    path = Path(value).expanduser()
    base = user_data_dir() if getattr(sys, "frozen", False) else ROOT
    return (path if path.is_absolute() else base / path).resolve()


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    brave_api_key: str = ""
    workspace: Path = field(default_factory=lambda: user_data_dir() / "workspace")
    data_dir: Path = field(default_factory=user_data_dir)
    max_tool_rounds: int = 8
    orchestrator: str = "maf"
    instance_token: str = ""
    instance_id: str = field(default_factory=lambda: uuid4().hex)
    max_pending_tasks: int = 3
    task_timeout: float = 300.0
    max_model_calls: int = 12
    max_tool_calls: int = 24

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    @classmethod
    def from_env(cls) -> "Settings":
        load_environment()
        data_dir = user_data_dir()
        return cls(
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip(
                "/"
            ),
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            brave_api_key=os.getenv("BRAVE_API_KEY", ""),
            workspace=project_path(os.environ["JARVIS_WORKSPACE"])
            if os.getenv("JARVIS_WORKSPACE", "").strip()
            else data_dir / "workspace",
            data_dir=data_dir,
            max_tool_rounds=max(
                1, min(20, int(os.getenv("JARVIS_MAX_TOOL_ROUNDS", "8")))
            ),
            orchestrator=os.getenv("JARVIS_ORCHESTRATOR", "maf").lower().strip(),
            instance_token=os.getenv("JARVIS_INSTANCE_TOKEN", ""),
            instance_id=os.getenv("JARVIS_INSTANCE_ID") or uuid4().hex,
            max_pending_tasks=max(
                1, min(10, int(os.getenv("JARVIS_MAX_PENDING_TASKS", "3")))
            ),
            task_timeout=max(
                1.0, min(3600.0, float(os.getenv("JARVIS_TASK_TIMEOUT", "300")))
            ),
            max_model_calls=max(
                1, min(100, int(os.getenv("JARVIS_MAX_MODEL_CALLS", "12")))
            ),
            max_tool_calls=max(
                1, min(100, int(os.getenv("JARVIS_MAX_TOOL_CALLS", "24")))
            ),
        )


PREFERENCE_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "BRAVE_API_KEY",
        "JARVIS_WORKSPACE",
        "JARVIS_ORCHESTRATOR",
        "JARVIS_MAX_TOOL_ROUNDS",
        "JARVIS_MAX_PENDING_TASKS",
        "JARVIS_TASK_TIMEOUT",
        "JARVIS_MAX_MODEL_CALLS",
        "JARVIS_MAX_TOOL_CALLS",
        "JARVIS_STT_MODEL_DIR",
        "JARVIS_STT_THREADS",
        "JARVIS_TTS_MODEL_DIR",
        "JARVIS_TTS_THREADS",
        "JARVIS_TTS_SPEAKER",
        "JARVIS_TTS_SPEED",
        "JARVIS_TTS_IDLE_SECONDS",
        "JARVIS_WAKE_WORD",
        "JARVIS_MIC_DEVICE",
        "JARVIS_SPEAKER_DEVICE",
        "JARVIS_DESKTOP_PORT",
        "UI_SPEECH_ENABLED",
        "UI_AUTO_FULLSCREEN",
        "UI_TILE_X",
        "UI_TILE_Y",
    }
)


def read_preferences() -> dict:
    try:
        data = json.loads(preferences_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        raise ValueError("无法读取本地设置，请检查 settings.json 是否有效。") from exc
    if not isinstance(data, dict):
        raise ValueError("本地设置必须是 JSON 对象。")
    return {key: value for key, value in data.items() if key in PREFERENCE_KEYS}


def save_preferences(updates: dict) -> None:
    if not isinstance(updates, dict) or set(updates) - PREFERENCE_KEYS:
        raise ValueError("包含不支持的设置字段。")
    if any(
        not isinstance(value, (str, int, float, bool)) for value in updates.values()
    ):
        raise ValueError("设置值必须为文本、数字或开关。")
    values = read_preferences()
    values.update(updates)
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".settings-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(values, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    for key, value in updates.items():
        os.environ[key] = str(value).lower() if isinstance(value, bool) else str(value)
