# Voice-over script (WO-062B)

One line per shot, read over `artifacts/demo-fallback.mp4` while it plays. The bold mark is the
shot's start in `docs/demo-script.yaml`; each line stays at or under 28 words per 10 s of its
shot (`tests/test_demo_video.py` counts them). Plain words: no rung codes, no state names, no
product or vendor names. The draft take (`make demo-video` on a Mac, or
`uv run python -m soundings.demo.video vo-draft`) reads these lines with the macOS voice
Samantha at 175 words a minute into `artifacts/vo-draft.m4a`, each one 0.3 s after its mark,
so the pacing can be checked before the real take. Merge a take with
`make video-merge VO=path/to/take.m4a` (optional `MUSIC=path`, a royalty-free bed).

## Lines

- **0:00 Cold open** This is a working harbor at night. On a normal Tuesday, about forty things move underwater here. One of them was never filed.
- **0:12 The problem** Today the safe answer to an unknown contact is to close the berth. Soundings climbs a ladder instead: watch, ask, slow, hold, hand to a person. It stops at the gentlest step that works.
- **0:30 Busy Tuesday** A busy Tuesday, twenty times faster. Filed jobs clear when we ask: a registry check and a hail. A crew files its plan from a phone; a clash gets a new slot, and they accept it. The glider went quiet. The platform searched the drift box, and it is found in the drift box.
- **1:00 Harbor Night** Harbor Night. A contact appears that nobody filed. The evidence fills in, facts in one color, assumptions in another. No registry match. The hail gets no reply. It is closing on the fuel berth. The platform can go to look, never closer than 25 m. Tonight the call came first, and it is a person's. Under orders the watch commander signed in advance, it holds one berth, not the harbor, writes the report, and asks a person to approve. Our work stops here. The call is a person's.
- **1:50 Twist** Now it is three in the morning. The network is down and nobody answers. A backup is paged. It does only what the watch commander signed in advance, and reaches the same decision from the contact's movement and the silent hail.
- **2:15 Evidence** Detection here is simulated. The decisions are not: they are plain code you can test. Bottom right, the ledger counts where each contact was settled, next to the berth closures a blunt close-the-berth policy would have caused.
- **2:40 Dual use** A traffic service for the harbor floor. On a Tuesday the harbor department and the dive and survey crews pay for it. On Harbor Night everyone is safer. Next: the NULL Labs simulator and a Navy research grant.

## Notes for the take

- The WO-088 lines are folded in. Harbor Night carries the owner's look line as the corrected
  subcaption reads it: in this run the call comes before the platform could reach the contact
  (DECISIONS 2026-10-17 11:06), so the line says the platform *can* go to look. The owner's
  original line was "The platform goes to look. It never goes closer than 25 m. The call is
  still a person's."
- Busy Tuesday's glider line is the WO-088 S02 beat. The Busy Tuesday shot runs S01 until the
  shot list switches to BT-01 (WO-080 TODO in the yaml), so the glider search is not on screen
  yet: keep the line only if that shot shows it, otherwise drop its two sentences.
- Speak the marks, not the clock: each line starts on its cut and ends before the next one.
