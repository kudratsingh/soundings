# Twist 1 — response

Written response, not a build change. The build is unchanged; this folder is the proof trail (`notice.md`, `delta.json`, this file).

---

**Primary: Navy SBIR/STTR (Department of War).** The Navy has funded this exact problem through SBIR before — topic N152-113, "UUV Detection and Classification in Harbor Environments," and in the 25.2 cycle N252-116 and N252-117 (delay/denial and interception of UUVs, sponsored by Strategic Systems Programs). Soundings sits upstream of those: the 25.2 topics *assume the UUV has already been determined to be unauthorized* — that determination is what we build. Phase I is a feasibility study (~6 months, modeling and simulation, which is what we have), Phase II is a prototype. Phase III explicitly names commercial port protection as the transition path — that's our Tuesday customer. Entry points: the DoD SBIR/STTR open-topic cycles (three a year), the Navy's NavalX and ONR Global TechSolutions for earlier-stage ideas.

**Alternative: DHS Science & Technology, Maritime Safety and Security Program.** DHS S&T funds port and coastal surveillance, maritime domain awareness sensors, and a "Maritime Port Resiliency and Security" project with a research testbed, transitioning to the Coast Guard and Army Corps. The Coast Guard's Captain of the Port sets harbor security zones, so DHS/USCG is the natural civilian owner of the registry and the standing orders. Path: DHS SBIR topics and the S&T Long Range BAA.

**Fast on-ramp: Defense Innovation Unit (DIU)** open solicitations — commercial-first, awards often in 60–90 days, no SBIR paperwork. Watch for maritime domain awareness and counter-UAS/UUV calls.

**Non-dilutive, non-defense: NSF SBIR (America's Seed Fund)** via Project Pitch — up to $2M, no equity, fits the decision-under-uncertainty software framing if we want to stay away from classified Phase II work.

**Honest caveat for the response:** the Navy 25.2 topics lean toward Phase II being classified and require U.S. ownership and facility clearance. Our detect-ask-decide-handoff layer stays unclassified and commercial; that's a feature — it's the part a port can buy.

## Sources
- [N252-116 Delay and Denial of UUVs](https://www.navysbir.com/n25_2/N252-116.htm)
- [N152-113 UUV Detection in Harbor Environments](https://navysbir.com/n15_2/N152-113.htm)
- [N252-117 Interception of UUVs](https://www.navysbir.com/n25_2/N252-117.htm)
- [DHS S&T Maritime Border Security](https://www.dhs.gov/science-and-technology/maritime-border-security)
- [DHS S&T Long Range BAA](https://brdo.berkeley.edu/news/dhs-st-directorate-fy24-long-range-baa)
- [DHS port resiliency testbed RFI](https://sea-technology.com/dhs-rfi-maritime-port-resiliency-and-security-research-testbed)

## Where Soundings sits
Upstream of the Navy 25.2 topics: those start after a contact has been determined to be unauthorized. Soundings is the determination — registry, hail, hypothesis-driven investigation, deterministic guards, and the handoff. Everything after the handoff is a person's call (CLAUDE.md §2.1).
