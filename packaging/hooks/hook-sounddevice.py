"""Keep only native PortAudio libraries from the mixed-platform wheel."""

from pathlib import Path
import platform
import sys

from PyInstaller.utils.hooks import get_module_file_attribute


module_dir = Path(get_module_file_attribute("sounddevice")).parent
library_dir = module_dir / "_sounddevice_data" / "portaudio-binaries"
if sys.platform == "darwin":
    filenames = ("libportaudio.dylib",)
elif sys.platform == "win32" and platform.machine().lower() in {"amd64", "x86_64"}:
    filenames = ("libportaudio64bit.dll", "libportaudio64bit-asio.dll")
else:
    raise RuntimeError(
        "Open JARVIS release builds require native macOS or Windows x64."
    )

if not (library_dir / filenames[0]).is_file():
    raise RuntimeError(
        "Native PortAudio is missing; install the target sounddevice wheel before building."
    )
binaries = [
    (str(library_dir / filename), "_sounddevice_data/portaudio-binaries")
    for filename in filenames
    if (library_dir / filename).is_file()
]
datas = (
    [(str(library_dir / "README.md"), "_sounddevice_data/portaudio-binaries")]
    if (library_dir / "README.md").is_file()
    else []
)
