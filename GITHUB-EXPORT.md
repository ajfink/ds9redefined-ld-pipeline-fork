# GitHub source-only export

This public repository contains workflow source, documentation, tests, and portable
templates, V7 doctor/stage-runner tools, and platform Python locks. It intentionally does
**not** contain:

- `machine.json` or machine-specific profiles;
- measured episode manifests or work outputs;
- the archival `reference-pool/` and historical `reference-runs/` data;
- TBC/EFM captures, WAV/FLAC audio, video, descriptor caches, or other generated images;
- all but the intentionally curated, single-frame cut-PAD stills in `Cut Pad Images/<episode>/`;
- the third-party AviSynth plugin DLLs in `avsplugins/`.

`episodes/_template.json` and the generic Windows, macOS, and Linux x86_64 candidate example
profiles are included. Platform dependency locks/environment recipes are under
`dependencies/`; the Linux recipe does not claim end-to-end workflow support.
A clone is not a ready-to-run operator pack: create a local profile/episode manifest,
provide the source media, and obtain the required platform tools/plugins before running
the workflow. The curated cut-PAD stills are single-frame PNG repair aids, not source
DVDs, video sequences, or episode footage.

No project-wide license has been selected yet. Public visibility does not grant a license
to reuse the project; add a LICENSE file only after the owner chooses the intended terms.

The documented Windows x64 workflow is the reference. macOS Apple-silicon tooling is
included, but fresh-Mac end-to-end delivery remains unvalidated. Linux x86_64 has a
dependency candidate only, not workflow support; Linux ARM64 and Windows ARM64 are
untested unless a release explicitly says otherwise.
