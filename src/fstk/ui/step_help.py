"""Plain-language help for each step, shown as the tooltip of its row in the Progress tab.

Keyed by stage id (a plan step names its stage). Each entry: what the step does, what it produces, and whether a
person is asked to look/listen before the run goes on. Kept here, next to the UI, so the words are written for the
person running the app rather than for the code; ``tests/unit/test_step_help.py`` checks every stage has one.
"""
from __future__ import annotations

#: (what it does, what you get, when it stops for you — "" when it never does)
STEP_HELP: dict[str, tuple[str, str, str]] = {
    "archive-download": (
        "Downloads this episode's LaserDisc captures (TBC, JSON and EFM files) from the Internet Archive, checking "
        "each file's size and MD5. An interrupted download resumes where it stopped.",
        "The capture files in your downloads folder.",
        ""),
    "capture-inventory": (
        "Pairs each capture's picture, metadata and audio files, names the captures (L1, M1, P1 …) and confirms "
        "they are all the same episode.",
        "The list of captures every later step uses.",
        ""),
    "efm-decode": (
        "Decodes the digital (EFM) audio of every capture with Orc and checks each result.",
        "One verified audio file per capture, with how much of it was damaged.",
        ""),
    "efm-select": (
        "Picks the capture whose digital audio is least damaged (fewest silenced samples). You can override the "
        "choice with a note.",
        "The audio capture the rest of the episode uses.",
        ""),
    "stack-reference": (
        "Lines up all the captures with Orc and chooses the best reference, so every picture can be built from all "
        "of them. Orc's CLV time-code PAD frames are removed from the plan (ADR 0004).",
        "The stack plan: reference capture, how the captures line up, and the frame count.",
        ""),
    "stack-render": (
        "Orc combines all the captures, picture by picture, into one lossless stack master (FFV1). An existing "
        "master is reused; one still holding Orc's time-code PAD frames loses them by a lossless copy.",
        "The stack master that every picture step reads.",
        ""),
    "cadence-evidence": (
        "Measures the film cadence (which fields belong to which film frame) across the whole episode, finds the "
        "stretches that need repair, and finds the visual-effects stretches that were finished on video rather "
        "than film (see “Preview video-origin stretches”).",
        "The cadence map, the repair ranges and the list of video-origin stretches for video-trim.",
        "Stops for your review only when the measurements fall outside what earlier episodes showed."),
    "dvd-probe": (
        "Checks the DVD rip: its streams, timestamps, frame count, and a fingerprint of the file.",
        "A verified description of the DVD the alignment and audio steps use.",
        ""),
    "dvd-alignment": (
        "Matches every LaserDisc picture to its DVD picture, act by act, and checks the match on real pictures.",
        "Which DVD frame each LD frame shows, used for trims, audio timing and DVD frame fixes.",
        "Stops for your review (pictures side by side) when any automatic check is unsure."),
    "video-trim": (
        "Builds the exact film-frame map (IVTC), finds where the programme starts and ends, and writes the "
        "script the final encode runs.",
        "The verified encode script and the trims.",
        ""),
    "dvd-ld-audio": (
        "Times the LaserDisc stereo against the DVD through the picture alignment and builds a sample-exact LD "
        "track at the measured rate.",
        "The LD stereo track, in sync with the picture.",
        ""),
    "dvd-free-audio": (
        "Without a DVD: uses the LaserDisc stereo at the disc's own 44.1 kHz and places it from the capture "
        "geometry, after checking its length against the picture.",
        "The LD stereo track, in sync with the picture.",
        ""),
    "dvd-retime": (
        "Moves the DVD's 5.1 audio and English subtitles onto the LaserDisc timeline (finding the DVD's edits "
        "automatically) and checks the result against the LD audio.",
        "The retimed 5.1 track and subtitles.",
        ""),
    "stretch-preview": (
        "<b>Why:</b> DS9's scenes were shot on film (24 pictures a second), but most of its visual effects (space "
        "shots, phasers, transporter beams, composites) and some titles were finished on video, which has a new "
        "picture 60 times a second. For film the app recovers the original 24 pictures exactly. A video stretch has "
        "no hidden film pictures to recover, so delivering it at the same 24 a second as the rest means choosing "
        "which moments to show. Simply dropping every fifth picture would make motion visibly hitch."
        "<br><br><b>What:</b> for each such stretch this makes a short side-by-side clip of the two ways to do it. "
        "Left, <i>nearest real frames</i> (the default): every picture is a real captured one, and motion is very "
        "slightly uneven. Right, <i>interpolated</i>: perfectly even motion, but half the pictures are made by the "
        "computer, which can warp beams, explosions and starfields and softens grain.",
        "Preview clips only; nothing here is delivered.",
        "None in this step. In the final review (tab “Video-origin stretches”) you watch each clip and choose "
        "for each stretch; the default needs no action."),
    "frame-fixes": (
        "Applies the frame fixes you chose in the final review (interpolate, patch a region, or borrow from the "
        "DVD). With none chosen it records the chain unchanged.",
        "The picture chain the final encode reads.",
        ""),
    "fix-preview": (
        "Shows one proposed frame fix before you choose it: the frames around it as captured and as fixed.",
        "Preview pictures and clips; nothing here is delivered.",
        ""),
    "video-quality": (
        "One pass over every picture: levels, grain, chroma noise, dropouts and the active picture area. Builds "
        "the finishing script (dropout concealment, crop, chroma-only denoise when needed). Luma grain is never "
        "touched.",
        "The finishing script and the picture-quality measurements.",
        ""),
    "encode-final": (
        "The one final video encode, with the episode's output settings (default x265 10-bit 4:2:2), checked "
        "frame by frame before it is used.",
        "The delivered video stream.",
        ""),
    "audio-quality": (
        "Finds clicks and digital gaps in the LD stereo and fills only short, clearly broken ones; checks channels, "
        "phase and layout; measures loudness without changing it; checks subtitle timing against speech.",
        "The finished audio tracks and their measurements.",
        ""),
    "remux": (
        "Puts the encoded video, the LD stereo, the DVD 5.1 and the subtitles (when there are any) into one file, "
        "without re-encoding the video.",
        "The deliverable file.",
        ""),
    "qc-report": (
        "Checks the deliverable end to end: decodes every frame, counts frames, lists streams, tests seeking, "
        "and flags single black frames and retime gaps.",
        "The QC report.",
        "Ends in the delivery review: you watch and listen at the points it picks, then approve or ask for fixes."),
    "av-correction": (
        "Records what you heard in the A/V check (in sync, audio early, audio late) and works out the correction "
        "the audio, remux and QC steps then apply.",
        "The next audio offset/drift correction.",
        ""),
    "episode-report": (
        "Gathers every quality and QC result into one report next to the deliverable and makes a contact sheet "
        "of the worst moments.",
        "The episode report and contact sheet.",
        ""),
}

STATUS_HELP = {
    "pending": "Waiting for the steps it needs to finish.",
    "running": "Working now.",
    "passed": "Finished and checked.",
    "failed": "Stopped with a problem; the message on the right says what. Fix it and run again: finished steps "
              "are kept.",
    "blocked": "Cannot start yet (for example, not enough free disk space, or an earlier step is not done).",
    "awaiting-review": "Waiting for you: open the Reviews tab to look or listen and decide.",
    "stale": "Its inputs changed since it finished, so it will run again.",
    "cancelled": "Stopped because the run was stopped.",
    "interrupted": "Will run again: the app closed while it was running.",
}


def step_tooltip(stage_id: str, title: str = "") -> str:
    """Rich-text tooltip for a step row."""
    what, result, review = STEP_HELP.get(stage_id, ("", "", ""))
    head = f"<b>{title or stage_id}</b>"
    if not what:
        return head
    parts = [head, what, f"<i>Produces:</i> {result}"]
    parts.append(f"<i>Your part:</i> {review}" if review else "<i>Your part:</i> none, it runs on its own.")
    return "<qt>" + "<br><br>".join(parts) + "</qt>"
