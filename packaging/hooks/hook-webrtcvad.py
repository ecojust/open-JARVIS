"""Collect the maintained wheel distribution that supplies webrtcvad.py."""

from PyInstaller.utils.hooks import copy_metadata


# The upstream hook assumes the older source-only `webrtcvad` distribution.
# Our supported native wheels use the same import names but different metadata.
datas = copy_metadata("webrtcvad-wheels")
hiddenimports = ["_webrtcvad"]
