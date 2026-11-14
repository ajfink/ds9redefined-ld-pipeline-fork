# GitHub source-only export

This public repository contains workflow source, documentation, tests, and portable
templates. It intentionally does **not** contain:

- `machine.json` or machine-specific profiles;
- measured episode manifests or work outputs;
- the archival `reference-pool/` and historical `reference-runs/` data;
- TBC/EFM captures, WAV/FLAC audio, video, descriptor caches, or generated images;
- the third-party AviSynth plugin DLLs in `avsplugins/`.

`episodes/_template.json` and the generic `machine.example.json` are included. A clone is
not a ready-to-run operator pack: create a local profile/episode manifest, provide the
source media, and obtain the required AviSynth plugins under their respective upstream
terms before running the PAD workflow.

No project-wide license has been selected yet. Public visibility does not grant a license
to reuse the project; add a LICENSE file only after the owner chooses the intended terms.

Known portability limitation: `scripts/motion-probe.ps1` still resolves FFmpeg through an
authoring-machine absolute path and needs a profile-based follow-up before this source
snapshot can be treated as portable on another Windows host.
