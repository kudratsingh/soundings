"""Record the demo video from presenter mode (WO-062B; imported only by the record command).

Starts the API + built UI on a free loopback port, opens ``/?presenter=1&tiles=off`` in the
cached headless Chromium at 1920x1080 (deviceScaleFactor 1) with Playwright video on, and
drives the shot list by timed Space presses at the marks in ``docs/demo-script.yaml``. A
``pause_for_click`` step gets one click 2 s after it lands (the judge panel's Close when it
is open, else the caption), so the next Space is not swallowed. Every non-loopback request
is aborted (tiles off, no network). The run is the same one presenter mode gives on stage:
nothing here sets a rung, approves or calls a model (MODEL_MODE=replay).

The timeline origin is the presenter log's step-0 entry. The webm is cut at that origin,
converted to H.264 (``demo-fallback.mp4``) with the SFX the page fired (``window.__soundingsSfx``,
the same audit events the sound module plays) on its audio track, and the step landings,
SFX cues and console lines are written to ``demo-fallback.json``.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from soundings.demo import video as v

WEB_DIST = v.REPO / "web" / "dist"
LOOPBACK = {"127.0.0.1", "localhost"}
CLICK_AFTER_S = 2.0  # a pause_for_click step is resumed this long after it lands
TAIL_S = 1.5  # keep recording past `end` so the cut never runs short
THUMB_AT_S = 88.0  # Harbor Night, the contact on the map near the fuel berth
WARM_UP = (
    "/api/demo-script",
    "/api/harbor",
    "/api/physics",
    "/api/boundary",
    "/api/about",
    "/api/scenarios",
)


def find_chromium() -> str | None:
    """The newest cached ``chromium_headless_shell`` (never downloads one)."""
    roots = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env != "0":
        roots.append(Path(env))
    roots += [
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ]
    for root in roots:
        for build in sorted(root.glob("chromium_headless_shell-*"), reverse=True):
            for name in ("chrome-headless-shell", "headless_shell"):
                hits = sorted(build.glob(f"*/{name}"))
                if hits:
                    return str(hits[0])
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _start_server() -> tuple[str, Any, threading.Thread]:
    import uvicorn

    from soundings.api import create_app

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(WEB_DIST),
            host="127.0.0.1",
            port=port,
            log_level="error",
            ws="websockets-sansio",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:
            raise SystemExit("uvicorn did not start within 30 s")
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server, thread


def _api(base: str, path: str, method: str = "GET") -> Any:
    with urlopen(Request(f"{base}{path}", method=method), timeout=120) as r:  # noqa: S310 (loopback only)
        return json.loads(r.read().decode("utf-8"))


def _offline(route: Any) -> None:
    if urlparse(route.request.url).hostname in LOOPBACK:
        route.continue_()
    else:
        route.abort()


# Presenter mode's `startStepRun` (web/src/presenter.ts, WO-062P) posts /api/runs without
# `source`, so the server picks the batch source on runner v1: Harbor Night never holds and
# the twist's toggles are refused (400). The main screen posts `source: "v3"` (RUN_SOURCE,
# web/src/api.ts, WO-060B). Until presenter.ts sends it too, the recording adds the same
# source to a presenter run start, and nothing else (DECISIONS 2026-10-17, WO-062B).
RUN_SOURCE = "v3"


def _run_source(patched: list[str]) -> Any:
    def handler(route: Any) -> None:
        req = route.request
        if req.method == "POST" and urlparse(req.url).hostname in LOOPBACK:
            try:
                body = json.loads(req.post_data or "{}")
            except ValueError:
                body = None
            if isinstance(body, dict) and "source" not in body:
                body["source"] = RUN_SOURCE
                patched.append(str(body.get("scenario")))
                route.continue_(post_data=json.dumps(body))
                return
        _offline(route)

    return handler


def _wall(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _sleep_until(page: Any, wall: float) -> None:
    """Wait on the page (never time.sleep: the sync API runs route handlers only while the
    main thread is inside a Playwright call, so a plain sleep stalls every request)."""
    while True:
        left = wall - time.time()
        if left <= 0:
            return
        page.wait_for_timeout(max(1.0, min(left * 1000.0, 20.0)))


def _thumbnail(browser: Any, frame_png: Path, out_png: Path) -> Path:
    import base64

    data = base64.b64encode(frame_png.read_bytes()).decode("ascii")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
      html,body{{margin:0;width:{v.THUMB_W}px;height:{v.THUMB_H}px;overflow:hidden;background:#06121c}}
      .bg{{position:absolute;inset:0;
        background:url(data:image/png;base64,{data}) left top/cover no-repeat}}
      .shade{{position:absolute;inset:0;background:linear-gradient(90deg,
        rgba(4,12,20,.86) 0%,rgba(4,12,20,.45) 45%,rgba(4,12,20,0) 75%)}}
      .text{{position:absolute;left:64px;bottom:96px;
        font-family:-apple-system,Helvetica,Arial,sans-serif;color:#fff}}
      h1{{margin:0;font-size:132px;line-height:1;font-weight:800;letter-spacing:-.02em}}
      p{{margin:22px 0 0;font-size:54px;font-weight:600;color:#9fe3ff}}
    </style></head><body><div class="bg"></div><div class="shade"></div>
    <div class="text"><h1>Soundings</h1><p>VTS for the harbor floor</p></div></body></html>"""
    page = browser.new_page(
        viewport={"width": v.THUMB_W, "height": v.THUMB_H}, device_scale_factor=1
    )
    page.route("**/*", _offline)
    page.set_content(html)
    page.wait_for_timeout(200)
    page.screenshot(path=str(out_png))
    page.close()
    return out_png


def record_demo(out_dir: Path = v.ARTIFACTS, executable_path: str | None = None) -> dict[str, Any]:
    """Record, convert and thumbnail; returns the timeline summary (also demo-fallback.json)."""
    os.environ.setdefault("MODEL_MODE", "replay")
    if os.environ.get("MODEL_MODE") != "replay":
        raise SystemExit("the demo video records with MODEL_MODE=replay only (CLAUDE.md 2.3)")
    if not (WEB_DIST / "index.html").is_file():
        raise SystemExit("web/dist is not built (run `make web`)")
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    shots, total = v.load_shots()
    script = json.loads(json.dumps([s.__dict__ for s in shots]))
    from soundings.demo.script import DemoScript

    steps = DemoScript.load(v.SCRIPT_PATH).steps
    # WO-062C: the scripted plan filing (auto_file) goes into a throwaway registry, so a
    # take never touches soundings.db and two takes start from the same empty registry.
    db_dir = tempfile.TemporaryDirectory(prefix="wo062c-registry-")
    previous_db = os.environ.get("SOUNDINGS_DB")
    os.environ["SOUNDINGS_DB"] = str(Path(db_dir.name) / "registry.db")
    base, server, thread = _start_server()
    # Warm the API before the page opens: the first calls build the harbor, physics and
    # scenario tables, and a cold first load must not eat into step 0.
    for path in WARM_UP:
        _api(base, path)
    _api(base, "/api/presenter-log", "DELETE")
    console: list[dict[str, Any]] = []
    presses: list[dict[str, Any]] = []
    patched: list[str] = []
    with tempfile.TemporaryDirectory(prefix="wo062b-rec-") as tmp, sync_playwright() as pw:
        exe = executable_path
        if exe is None and not Path(pw.chromium.executable_path).exists():
            exe = find_chromium()
            if exe is None:
                raise SystemExit("no cached chromium (uv run playwright install chromium, once)")
        browser = pw.chromium.launch(executable_path=exe)
        ctx = browser.new_context(
            viewport={"width": v.WIDTH, "height": v.HEIGHT},
            device_scale_factor=1,
            record_video_dir=tmp,
            record_video_size={"width": v.WIDTH, "height": v.HEIGHT},
        )
        ctx.route("**/*", _offline)
        ctx.route("**/api/runs", _run_source(patched))
        page = ctx.new_page()
        page.on("console", lambda m: console.append({"wall": time.time(), "text": m.text}))
        page.goto(f"{base}/?presenter=1&tiles=off")
        deadline = time.monotonic() + 120
        log: list[dict[str, Any]] = []
        while not log:
            if time.monotonic() > deadline:
                raise SystemExit("presenter mode never landed on step 0")
            page.wait_for_timeout(50)
            log = _api(base, "/api/presenter-log")
        t0 = _wall(log[0]["wall"])
        for i, step in enumerate(steps):
            if i > 0:
                _sleep_until(page, t0 + step.mark_s)
                page.keyboard.press("Space")
                presses.append({"index": i, "pressed_s": round(time.time() - t0, 3)})
            if step.pause_for_click:
                _sleep_until(page, t0 + step.mark_s + CLICK_AFTER_S)
                close = page.locator("[data-testid=judge-close]")
                if close.count() and close.first.is_visible():
                    close.first.click()
                    what = "judge-close"
                else:
                    page.locator("[data-testid=presenter-caption]").click()
                    what = "caption"
                presses.append(
                    {"index": i, "clicked": what, "pressed_s": round(time.time() - t0, 3)}
                )
        _sleep_until(page, t0 + total + TAIL_S)
        page_sfx = page.evaluate(
            "() => ({origin: performance.timeOrigin, sfx: window.__soundingsSfx || []})"
        )
        log = _api(base, "/api/presenter-log")
        page.close()
        t_close = time.time()
        ctx.close()
        if page.video is None:
            raise SystemExit("Playwright recorded no video")
        webm = Path(page.video.path())

        webm_probe = v.probe(webm)
        video_start = t_close - webm_probe.duration_s
        offset = t0 - video_start
        landings = [e for e in log if e["kind"] in ("step", "resume")]
        steps_out = []
        for e in landings:
            if e["kind"] != "step":
                continue
            idx = int(e["index"])
            steps_out.append(
                {
                    "index": idx,
                    "mark_s": shots[idx].start_s,
                    "logged_s": round(_wall(e["wall"]) - t0, 3),
                    "caption": e.get("caption") or "",
                }
            )
        resumes = [
            {"index": int(e["index"]), "logged_s": round(_wall(e["wall"]) - t0, 3)}
            for e in log
            if e["kind"] == "resume"
        ]
        origin_s = float(page_sfx["origin"]) / 1000.0
        cues = []
        for entry in page_sfx["sfx"]:
            t_s = origin_s + float(entry["t_ms"]) / 1000.0 - t0
            if not entry.get("muted") and 0.0 <= t_s < total - 1.0:
                cues.append({"t_s": round(t_s, 3), "sound": entry["sound"]})
        sfx_wav = v.write_wav(Path(tmp) / "sfx.wav", v.sfx_track(cues, total))
        mp4 = v.encode_mp4(webm, out_dir / "demo-fallback.mp4", offset, total, sfx_wav)
        frame = v.write_thumbnail_frame(mp4, THUMB_AT_S, Path(tmp) / "frame.png")
        cropped = Path(tmp) / "frame-crop.png"
        v.run_ffmpeg(["-y", "-i", str(frame), "-vf", "crop=1152:648:0:128", str(cropped)])
        _thumbnail(browser, cropped, out_dir / "thumbnail.png")
        browser.close()
    server.should_exit = True
    thread.join(timeout=10)
    if previous_db is None:
        os.environ.pop("SOUNDINGS_DB", None)
    else:
        os.environ["SOUNDINGS_DB"] = previous_db
    db_dir.cleanup()
    autos = [  # WO-062C: the scripted filing's presenter-log entries
        {
            "index": int(e["index"]),
            "note": e.get("note", ""),
            "logged_s": round(_wall(e["wall"]) - t0, 3),
        }
        for e in log
        if e["kind"] == "auto"
    ]

    timeline = {
        "total_s": total,
        "fps": v.FPS,
        "size": [v.WIDTH, v.HEIGHT],
        "offset_s": round(offset, 3),
        "webm_s": round(webm_probe.duration_s, 3),
        "steps": steps_out,
        "resumes": resumes,
        "auto": autos,  # WO-062C
        "presses": presses,
        "run_source_patched": patched,
        "sfx": cues,
        "console": [{"t_s": round(c["wall"] - t0, 3), "text": c["text"]} for c in console],
        "shots": script,
    }
    (out_dir / "demo-fallback.json").write_text(
        json.dumps(timeline, indent=2) + "\n", encoding="utf-8"
    )
    return timeline
