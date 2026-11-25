# Smoke runs

`fstk smoke` runs the video half of the delivery chain (cadence → trim → PAD audit → PAD repair →
final encode) on a short excerpt of a real stack master in an isolated sandbox. Each run is logged
here: what ran, where, what broke, and the fix. Newest first.

Environment used by the unattended run on 2026-11-21/22: the Cowork Linux VM on PC-A
(2 cores, 3 GB RAM, no GPU), Python 3.12 venv, VapourSynth R82 with `bs`, `tivtc`, `mv`, `znedi3`,
`eedi3m`, `vszip`, `akarin` from PyPI wheels, FFmpeg 4.x/6.x system package. Not available there:
Orc, Windows tools, the X: drive (LaserDisc TBC/EFM captures), a GPU. So stack-render and EFM decode
are not exercised; the existing S01E20 stack master and its Orc project on Z: are the inputs.
