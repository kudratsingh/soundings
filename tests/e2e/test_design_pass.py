"""WO-060 design pass B1 browser smoke: a case-centric screen that holds still while it streams.

Opens ``/?scenario=S02&seed=3&speed=20&tiles=off`` at 1440x900 with every non-loopback
request aborted and a PerformanceObserver (layout-shift, longtask) injected before load, then:

(a) within 10 s: the cases list, a non-empty NOW line, the key-moments strip, and a case
    view whose ASK step shows the hail result;
(b) opens ``others · n`` (WO-060E) and picks another case: the case view switches and
    shows only that case's steps;
(c) measures 30 s of streaming: cumulative layout shift < 0.1 and no long task over 200 ms
    (the numbers are printed);
(d) with both drawers open (``show all`` on, WO-060E) the case view holds < 400 row nodes
    while the stream has reported > 1,000 rows;
(e) scrolls the case view and waits for new events: scrollTop does not jump;
(f) the legend opens; no horizontal scrollbar at 1440x900 or 1920x1080.

The README screenshots come from ``scripts/readme_shots.py``. Skips without a cached
chromium or a built ``web/dist`` (see ``conftest.py``).
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from tests.e2e._quiet import open_others

LOOPBACK = {"127.0.0.1", "localhost"}
ROWS_JS = (
    "Number(document.querySelector('[data-testid=rows-total]')?.getAttribute('data-rows') ?? 0)"
)

PERF_OBSERVER = """
(() => {
  window.__perf = { cls: 0, shifts: 0, long: [] };
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (!e.hadRecentInput) { window.__perf.cls += e.value; window.__perf.shifts += 1; }
      }
    }).observe({ type: 'layout-shift', buffered: true });
  } catch (err) { window.__perf.noLayoutShift = String(err); }
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) window.__perf.long.push(Math.round(e.duration));
    }).observe({ type: 'longtask', buffered: true });
  } catch (err) { window.__perf.noLongTask = String(err); }
})();
"""


def _offline(route: Any) -> None:
    if urlparse(route.request.url).hostname in LOOPBACK:
        route.continue_()
    else:
        route.abort()


def _no_horizontal_scroll(page: Any) -> bool:
    return bool(
        page.evaluate(
            "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
            " && document.body.scrollWidth <= document.documentElement.clientWidth"
        )
    )


def _case_view_case(page: Any) -> str:
    return page.locator("[data-testid=case-view]").get_attribute("data-case") or ""


def test_case_centric_and_stable(live_server: str, browser: Any) -> None:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.add_init_script(PERF_OBSERVER)
    page.route("**/*", _offline)
    started = time.monotonic()
    page.goto(f"{live_server}/?scenario=S02&seed=3&speed=20&tiles=off")

    # (a) the five-second read, within 10 s
    page.wait_for_selector("[data-testid=cases] [data-testid=case-row]", timeout=10_000)
    page.wait_for_function(
        "() => (document.querySelector('[data-testid=now-line]')?.textContent ?? '')"
        ".trim().length > 10",
        timeout=10_000,
    )
    assert page.locator("[data-testid=key-moments]").is_visible()
    hail = page.locator("[data-testid=case-view] [data-step=ask] [data-testid=hail-result]")
    hail.first.wait_for(timeout=max(1_000, int(10_000 - (time.monotonic() - started) * 1000)))
    assert "hail" in hail.first.inner_text()
    elapsed_a = time.monotonic() - started
    assert elapsed_a < 10.0, f"(a) took {elapsed_a:.1f} s"
    now_px = page.evaluate(
        "() => parseFloat(getComputedStyle("
        "document.querySelector('[data-testid=now-line]')).fontSize)"
    )
    assert now_px >= 18
    # the top bar is one line at 1440 px
    bar_h = page.evaluate("() => document.querySelector('.topbar').getBoundingClientRect().height")
    assert bar_h <= 52, f"top bar is {bar_h} px tall"

    # (b) select a different case: the view switches and shows only that case's steps
    # WO-060E: every case but the focus is folded behind `others · n`; open it first.
    page.wait_for_selector("[data-testid=others-row]", timeout=20_000)
    open_others(page)
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid=case-row]').length >= 2", timeout=20_000
    )
    current = _case_view_case(page)
    rows = page.locator("[data-testid=case-row]")
    target = ""
    members: list[str] = []
    for i in range(rows.count()):
        cid = rows.nth(i).get_attribute("data-case") or ""
        if cid and cid != current:
            target = cid
            members = (rows.nth(i).get_attribute("data-members") or cid).split(",")
            rows.nth(i).click()
            break
    assert target, "no second case to select"
    page.wait_for_function(
        "c => document.querySelector('[data-testid=case-view]')?.getAttribute('data-case') === c",
        arg=target,
        timeout=5_000,
    )
    shown = page.locator("[data-testid=case-view] [data-testid=step]")
    assert shown.count() > 0
    contacts = {shown.nth(i).get_attribute("data-contact") for i in range(shown.count())}
    assert contacts <= set(members), f"case {target} shows steps of {contacts - set(members)}"

    # Pin a case whose content above the fold is final (one id, investigating) for (c)-(e).
    pin = page.locator("[data-testid=case-row][data-state=INVESTIGATING][data-members-n='1']")
    if pin.count() > 0:
        pin.first.click()
    page.wait_for_timeout(500)

    # (c) 30 s of streaming: layout shift and long tasks
    page.evaluate(
        "() => { window.__perf.cls = 0; window.__perf.shifts = 0; window.__perf.long = [] }"
    )
    page.wait_for_timeout(30_000)
    perf = page.evaluate("() => window.__perf")
    longest = max(perf["long"], default=0)
    print(
        f"\nlayout stability over 30 s: CLS={perf['cls']:.4f} ({perf['shifts']} shifts), "
        f"long tasks={len(perf['long'])}, longest={longest} ms"
    )
    assert "noLayoutShift" not in perf and "noLongTask" not in perf, perf
    assert perf["cls"] < 0.1
    assert longest <= 200, f"long task of {longest} ms"

    # (d) both drawers open: the DOM stays bounded while the stream is large
    page.wait_for_function(
        f"() => {ROWS_JS} > 1000",
        timeout=60_000,
    )
    page.click("[data-testid=evidence-drawer]")
    # WO-060E: the every-case drawer is drawn only with `show all` on (debugging)
    page.click("[data-testid=show-all]")
    page.click("[data-testid=transitions-drawer]")
    page.wait_for_selector("[data-testid=transitions-drawer-list]")
    total = int(page.locator("[data-testid=rows-total]").get_attribute("data-rows") or 0)
    nodes = page.locator(
        "[data-testid=case-view] [data-testid=ledger-row],"
        " [data-testid=case-view] [data-testid=transition-row]"
    ).count()
    print(f"row nodes in the case view with both drawers open: {nodes} (stream rows: {total})")
    assert total > 1000
    assert nodes < 400

    # (e) scroll the case view; new events arrive; what the reader sees does not move
    view = page.locator("[data-testid=case-view]")
    page.wait_for_function(
        "() => { const v = document.querySelector('[data-testid=case-view]');"
        " return v.scrollHeight > v.clientHeight + 200 }",
        timeout=10_000,
    )
    view.evaluate("v => { v.scrollTop = 160 }")
    before = view.evaluate("v => v.scrollTop")
    seq_before = int(page.locator("[data-testid=rows-total]").get_attribute("data-rows") or 0)
    page.wait_for_timeout(4_000)
    after = view.evaluate("v => v.scrollTop")
    seq_after = int(page.locator("[data-testid=rows-total]").get_attribute("data-rows") or 0)
    print(f"case view scrollTop {before} -> {after} while rows {seq_before} -> {seq_after}")
    assert seq_after > seq_before, "no new events arrived during the scroll check"
    assert abs(after - before) <= 1

    # (f) the legend opens; no horizontal scrollbar at 1440x900 and 1920x1080
    page.click("[data-testid=legend]")
    assert page.locator("[data-testid=legend-panel]").is_visible()
    assert _no_horizontal_scroll(page)
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.wait_for_timeout(300)
    assert _no_horizontal_scroll(page)
    page.close()
