# WO-062 addendum note: responder lines for the video and the vo-script (WO-088 part C)

Recorded 2026-10-17 08:37 PDT by the WO-088 part C agent. Owner directive 07:45 PDT (CLAUDE.md section 8 A2, numbered WO-088 per A1). This note sits beside `WO-062-ADDENDUM-video-sound.md`, which is still in PR #25. When that file lands, fold these lines into its vo-script and sound sections and delete this note.

## Presenter captions (`docs/demo-script.yaml`)

- **1:00 Harbor Night (S05).** The main caption stays verbatim: "Our work stops here. The call is a person's." A second, smaller caption line (`subcaption`) adds the owner's line: "The platform goes to look. It never goes closer than 25 m. The call is still a person's."
- **0:30 Busy Tuesday.** The `say` line gains the S02 search beat: "A silent glider is found in the drift box." The on-screen caption is unchanged, because it is held word for word to the directive's beat list.

## vo-script lines

- Harbor Night, before the hold: "The platform goes to look. It never goes closer than 25 m. The call is still a person's."
- Busy Tuesday, the S02 beat: "The glider went quiet. The platform searched the drift box, and it is found in the drift box."

## Sound

- `responder_dispatch.wav` plays two short rising notes (E5 then A5, 0.42 s, -18 dBFS). It comes from `scripts/gen_sfx.py` and plays through `web/src/sfx.ts` on the `responder` event that starts a dispatch to look (`dispatched: true`). Like the other sounds, it obeys the presenter mute.

## Vocabulary

Look, station, standoff, dispatch to look, return to dock. The responder is a probe at rung ask, and nothing happens after the handoff.
