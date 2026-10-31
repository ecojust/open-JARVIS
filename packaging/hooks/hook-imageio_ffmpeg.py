"""Keep the wheel's native FFmpeg executable and its resource package."""

from pathlib import Path

from imageio_ffmpeg._definitions import FNAME_PER_PLATFORM, get_platform
from PyInstaller.utils.hooks import collect_data_files, get_module_file_attribute


package = Path(get_module_file_attribute("imageio_ffmpeg")).parent
filename = FNAME_PER_PLATFORM.get(get_platform())
if not filename or not (package / "binaries" / filename).is_file():
    raise RuntimeError("The imageio-ffmpeg wheel lacks FFmpeg for this native target.")

# Classify the executable as a binary so macOS architecture checks and signing
# also apply to FFmpeg. Never substitute an executable found on the build PATH.
binaries = [(str(package / "binaries" / filename), "imageio_ffmpeg/binaries")]
datas = collect_data_files(
    "imageio_ffmpeg",
    include_py_files=True,
    includes=["binaries/__init__.py", "binaries/*.md", "binaries/*.txt"],
)
hiddenimports = ["imageio_ffmpeg.binaries"]
