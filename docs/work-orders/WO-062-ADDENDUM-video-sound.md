# WO-062 addenda — demo video, presenter mode, sound (owner directives, binding)

Received 2026-10-17 ~06:15 PDT. Verbatim owner text follows; coordinator notes at the end. These extend `WO-062-demo-runner.md` (Part B) and define the final deliverable: a ≤ 3:00 video that is clear with the sound off, a presenter mode so the Sunday live demo is the same run, and sound on both.

## Directive 1 — WO-062 expansion: demo video

Target: a ≤3:00 YouTube video that is clear with the sound off, and a presenter mode so the live demo on Sunday is the same run. Read docs/demo-script.yaml, docs/DECK-INPUTS.md, and the hub pitch order (0:30 the problem · show it working first · twist, how it holds up · next, feasibility).

1. PRESENTER MODE (build first; the video is a recording of it).
   - `/?presenter=1` adds a caption bar (bottom, 2 lines max, 44 px, high contrast) and a step counter, driven by docs/demo-script.yaml. Each step has: t, scenario or action, caption, speed (1x/20x), optional spotlight (element id to highlight), optional pause_for_click.
   - Space advances a step; the same script runs on stage. No step depends on the network or a live model (MODEL_MODE=replay, fixtures + fallbacks).
   - A "reset to step 0" that restores the seeded world in < 2 s.

2. SHOT LIST (write it into docs/demo-script.yaml; total 2:50–3:00).
   0:00–0:12 Cold open on the harbor map at night. Caption: "A working harbor sees ~40 things underwater on a Tuesday. One of them isn't filed."
   0:12–0:30 The problem. Caption: "Today the safe answer is close the berth. Soundings climbs the gentlest rung that works: watch → ask → slow → hold → hand to a person." Show the ladder as five chips lighting up.
   0:30–1:00 Busy Tuesday at 20x. Filed jobs clear at ask; rung ledger counts; a plan is filed from a phone (QR), conflict → proposed slot → accepted; a vessel transit triggers a slow advisory near divers. Caption on each beat.
   1:00–1:50 Harbor Night at 1x. The rogue appears; evidence ledger fills (facts one colour, assumptions another); probes: registry none, hail no reply, drift low, tonal present, closing on fuel berth; posterior moves; hold on one berth under signed standing orders; SITREP; approve; HANDED_OFF. Caption: "Our work stops here. The call is a person's."
   1:50–2:15 Twist. Toggle network_down + approver_unreachable: backup paged, hold released at its limit, same decision from kinematics and hail. Caption: "3 AM, nobody answers: it does only what the watch commander pre-signed."
   2:15–2:40 Evidence. Rung ledger vs the red "berth closures under the blunt policy" counter; eval table (missed threats 0, false shutdowns, seconds seen→handled, interruptions/hour). Caption: "Detection is simulated. The decisions aren't."
   2:40–3:00 Dual use. "VTS for the harbor floor." Who pays on Tuesday; who is safer on Harbor Night; next: NULL Labs sim, Navy SBIR. End card: Soundings · repo URL · team.

3. RECORDING.
   - Playwright, headless Chromium, 1920×1080, 30 fps, deviceScaleFactor 1, video recording on; drive the presenter script by timed `Space` presses from the yaml.
   - Output `artifacts/demo-fallback.mp4` (silent, captions baked in — the stage backup).
   - Convert with the bundled imageio-ffmpeg: H.264, yuv420p, CRF 18, AAC audio track even if silent, faststart.
   - Render `artifacts/thumbnail.png` (1280×720): map frame + title + "VTS for the harbor floor".

4. VOICEOVER (the owner records; you prepare).
   - Write `docs/vo-script.md`: one line per shot with its start time, ≤ 28 words per 10 s, plain words, no jargon, no denylist words, no model or vendor names. Say "simulated" once, in the evidence section.
   - Provide `make video-merge VO=path.m4a`: trims/pads VO to the timeline, loudnorm to −16 LUFS, mixes the hail ping at −20 dB, outputs `artifacts/demo-youtube.mp4`.
   - Also write `docs/youtube.md`: title (≤ 60 chars), two-paragraph description, chapter timestamps from the shot list, and the repo link. Unlisted upload is the owner's step.

5. EXIT TESTS.
   - `make demo-video` produces both mp4s from a clean checkout with no network, MODEL_MODE=replay.
   - Duration 170–180 s; 1920×1080; 30 fps; audio stream present.
   - A frame sampled at each shot boundary contains the expected caption (OCR with tesseract if present, else assert the presenter log timestamps match the yaml within 0.5 s).
   - Two recordings have the same frame count and identical caption timings.
   - `scripts/denylist-check.sh` passes over the yaml, vo-script, and youtube.md.
   - Watch it once at phone size: every caption legible, every beat understandable muted. Report what you cut if it ran long.

6. ORDER. Presenter mode as soon as WO-060 is usable (captions can run over the CLI-driven map first); the final recording only after Wave D merges. Deadline for the final render: 8:00 PM, so the owner can record VO and upload before 9:30 PM.

## Directive 2 — Sound addendum to WO-062

The video and the stage demo run with sound ON; captions stay as the safety net, not the primary.

1. Sound design (small, free, local assets in web/public/sfx, generated with a short Python/numpy script so there are no licensing questions): hail ping (short sonar-style tone), advisory chime (two notes), hold tone (low, 0.5 s), handoff chime (resolved, warm), twist "klaxon-lite" (brief, not alarming). Keep each < 1 s. Volume-balanced at −18 dBFS peak.
2. Presenter mode defaults to unmuted; a visible mute toggle; sounds fire on the same events as the audit log (hail sent, advisory raised, hold placed, handoff, twist toggle). No sound on routine ticks.
3. Voiceover is primary. Draft timing with macOS `say -v Samantha -r 175` from docs/vo-script.md into artifacts/vo-draft.m4a so the owner can check pacing; the owner records the final take over the fallback video with it playing. `make video-merge` mixes: VO at −16 LUFS, SFX at −22 dB, optional music bed at −28 dB only if the owner provides a royalty-free file (none by default).
4. Exit tests: demo-youtube.mp4 has an audio stream with VO present in every shot window (RMS > −40 dBFS); SFX events in the audio align with audit-log timestamps within 100 ms; fallback video keeps SFX even without VO.
5. Add to Sunday 9:00 AM tech check in docs/STATE.md: HDMI audio out, laptop volume 80%, play the hail ping in the room before the pitch.

## Coordinator notes (binding)
- Split: **WO-062P presenter mode + sound events** starts as soon as WO-060 part A is on `main` (captions over the map first); **WO-062 Part B recording/VO/youtube** runs in Wave D after WO-086 and WO-061 merge. Final render deadline 8:00 PM.
- Keep the WO-062 Part A scheduler compatible: each step keeps `t` and `say`; add `caption`, `speed`, `spotlight`, `pause_for_click`, `action`. The new shot list replaces docs/demo-script.yaml when WO-062P lands (not before WO-062 Part A merges).
- Dependencies: `imageio-ffmpeg` (dev, approved 23:05); `tesseract` is an optional system tool (fallback per exit test 3); macOS `say` for the VO draft only; numpy generates the SFX files, which are committed under web/public/sfx (each < 1 s, −18 dBFS peak) together with the generator script.
- The ping sound in WO-086 and the SFX events here are the same mechanism: one `sfx` module in web/src fed by the same WebSocket events the audit log records.

<!-- --- WO-088 begin --- -->
## WO-088 addendum: responder lines for the video and the vo-script

Recorded 2026-10-17 08:37 PDT by the WO-088 part C agent (owner directive 07:45 PDT; CLAUDE.md section 8 A2, numbered WO-088 per A1). Fold these into `docs/vo-script.md` when WO-062 Part B writes it.

### Presenter captions (`docs/demo-script.yaml`)

- **1:00 Harbor Night (S05).** The main caption stays verbatim: "Our work stops here. The call is a person's." A second, smaller caption line (`subcaption`) adds the owner's line: "The platform goes to look. It never goes closer than 25 m. The call is still a person's."
- **0:30 Busy Tuesday.** The `say` line gains the S02 search beat: "A silent glider is found in the drift box." The on-screen caption is unchanged, because it is held word for word to the directive's beat list.

### vo-script lines

- Harbor Night, before the hold: "The platform goes to look. It never goes closer than 25 m. The call is still a person's."
- Busy Tuesday, the S02 beat: "The glider went quiet. The platform searched the drift box, and it is found in the drift box."

### Sound

- `responder_dispatch.wav` plays two short rising notes (E5 then A5, 0.42 s, -18 dBFS). It comes from `scripts/gen_sfx.py` and plays through `web/src/sfx.ts` on the `responder` event that starts a dispatch to look (`dispatched: true`). Like the other sounds, it obeys the presenter mute.

### Vocabulary

Look, station, standoff, dispatch to look, return to dock. The responder is a probe at rung ask, and nothing happens after the handoff.
<!-- --- WO-088 end --- -->
