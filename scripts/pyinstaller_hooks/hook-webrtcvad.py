"""Use the distribution name of the project's prebuilt WebRTC VAD wheels."""

from PyInstaller.utils.hooks import copy_metadata

datas = copy_metadata("webrtcvad-wheels")
