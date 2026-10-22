"""factory_feed.py - report any job to the Factory Console. Standard library only; copy this one file into your project.

In Python:
    from factory_feed import Feed
    job = Feed("walnut chair turntable", frames_dir=r"D:\\renders\\walnut")   # frames_dir: the monitor shows its newest frame
    for i in range(240):
        ...render frame i...
        job.update(progress=(i + 1) / 240, log=f"frame {i + 1} / 240")
    job.done("240 frames")            # or job.fail("out of GPU memory")

    with Feed("nightly backup") as job:   # done on success, failed with the error text on an exception
        ...

From a shell (PowerShell, cmd, bash, a .bat, a scheduled task):
    python factory_feed.py "nightly backup" running --progress 0.3 --log "copying D:"
    python factory_feed.py "nightly backup" done --log "1,204 files"

Over HTTP from anything (curl, Node, a game engine...): POST JSON to http://127.0.0.1:8787/api/feed
    {"name": "...", "state": "running|queued|done|failed", "progress": 0.0-1.0, "log": "...", "frames_dir": "..."}

If the console is not running, updates are written straight into its feeds folder (FACTORY_FEEDS env var, or ./feeds
next to this file), so nothing is lost and the console picks them up when it starts. Reporting never raises in your job.
"""
import json, os, sys, time, urllib.request

URL = os.environ.get("FACTORY_URL", "http://127.0.0.1:8787")
FEEDS = os.environ.get("FACTORY_FEEDS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "feeds"))


def report(name, state="running", progress=None, log="", frames_dir=""):
    """send one update; returns True when the console took it, False when it went to the feeds folder (or nowhere)"""
    body = {"name": name, "state": state, "progress": progress, "log": log, "frames_dir": frames_dir}
    try:
        req = urllib.request.Request(URL.rstrip("/") + "/api/feed", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status == 200
    except Exception:
        try:
            os.makedirs(FEEDS, exist_ok=True)
            slug = "".join(c if c.isalnum() or c in "._-" else "_" for c in name).strip("._")[:80] or "job"
            body["updated"] = time.time()
            tmp = os.path.join(FEEDS, slug + ".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(body, f)
            os.replace(tmp, os.path.join(FEEDS, slug + ".json"))
        except Exception:
            pass
        return False


class Feed:
    """one job's reporter. Updates are throttled to one per second unless the state changes."""

    def __init__(self, name, frames_dir="", state="running", log=""):
        self.name, self.frames_dir, self._last = name, frames_dir, 0.0
        report(name, state, None, log, frames_dir)

    def update(self, progress=None, log="", force=False):
        now = time.time()
        if force or now - self._last >= 1.0:
            self._last = now
            report(self.name, "running", progress, log, self.frames_dir)

    def done(self, log=""):
        report(self.name, "done", 1.0, log, self.frames_dir)

    def fail(self, log=""):
        report(self.name, "failed", None, log, self.frames_dir)

    def __enter__(self):
        return self

    def __exit__(self, kind, err, tb):
        self.fail(f"{kind.__name__}: {err}") if kind else self.done()
        return False


if __name__ == "__main__":
    import argparse
    a = argparse.ArgumentParser(description="Report a job to the Factory Console")
    a.add_argument("name"); a.add_argument("state", nargs="?", default="running", choices=["running", "queued", "done", "failed"])
    a.add_argument("--progress", type=float); a.add_argument("--log", default=""); a.add_argument("--frames-dir", default="")
    x = a.parse_args()
    ok = report(x.name, x.state, x.progress, x.log, x.frames_dir)
    print("sent to the console" if ok else "console not reachable: saved to " + FEEDS)
