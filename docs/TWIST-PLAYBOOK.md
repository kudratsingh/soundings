# Twist playbook — six hours, change the build, ship proof

Twists land Fri 11:30 PM, Sat 4:30 AM, Sat 7:00 AM via the portal, WhatsApp, and email. Best Shift Adapter is scored on "what changed, why, and proof it works," and the organizers wrote: the best adapters change the build, not just the slides.

## The 45-minute loop

1. **Capture (2 min).** Paste the notice verbatim into `twists/0N/notice.md`. Commit on `twist/N-<slug>`.
2. **Parse (5 min).** Run `soundings twist parse twists/0N/notice.md` → `delta.json`: `{facts: [], assumptions: [], new_constraints: [], affected_components: [], candidate_toggles: []}`. The `twist_parser` role may run live (one call, owner approval) or the agent fills the JSON by hand. Facts vs assumptions must be separated; if the notice is ambiguous, write the ambiguity down and pick the reading that is most adverse to the current build.
3. **Map to a toggle or a new scenario (10 min).** Prefer an existing toggle from `SPEC-SIM §3`. If none fits, add a scenario file with an `expected` block. Add or extend a test that fails before the change.
4. **Change the build (20 min).** Smallest change in `soundings/` that makes the new test pass without breaking the catalog. Typical shapes: a new likelihood row, a new guard input, a new probe, a standing-orders clause, a new feature in the evidence table.
5. **Prove (5 min).** `make check`; `python -m soundings.eval --suite twist0N --mode replay`; screenshot of the UI on the new scenario. Save to `twists/0N/proof/`.
6. **Respond (3 min).** `twists/0N/response.md`: what changed (one paragraph), why (one paragraph), proof (test names, eval numbers, screenshot, commit hash). Submit via the portal. Record the submission timestamp in `docs/STATE.md`. Merge to `main`.

## Likely twists and the prepared answer

| if the twist says… | toggle | build change |
|---|---|---|
| more contacts than you can handle / surge | `contact_surge(n)` | priority queue by `time_to_nearest_critical_asset × zone_criticality`; show interruptions/hour staying flat |
| a sensor fails / goes dark | `sensor_dropout` | sensor-health weighting; entropy widens; coverage-gap shown on map |
| network is down / cloud unavailable | `network_down` | degraded mode in standing orders; fallbacks for every LLM role; local-only run demonstrated |
| the watch officer doesn't answer | `approver_unreachable` | backup approver path; hold released at limit; SITREP flags unreachable |
| AIS is spoofed / surface picture untrusted | `ais_spoof` | `ais_surface_correlation` confidence drops to the sensor-health floor; decision rests on kinematics + acoustics |
| two vehicles / coordinated | `second_coordinated_contact` | multi-contact correlation feature (SPEC-INVESTIGATION §9.6) |
| standing orders expired / commander changed | `standing_orders_expired` | nothing above ask executes; UI shows the unsigned state; re-sign flow |
| contact goes quiet mid-investigation | `contact_goes_quiet` | last-known-position + drift box; re-acquisition matcher |
| it's foggy / camera useless | `fog_no_camera` | planner skips camera; second_sensor_dwell preferred |
| budget cut / a tenth of the budget | — | single-node config; show the eval still holds with one sensor and no paid probes |

If the twist is about a different track entirely (energy, comms, ports), respond with how the ladder and standing orders transfer, and make one small real change (e.g., a `network_down` toggle) so the response is still a build change.
