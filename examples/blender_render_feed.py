"""Blender -> Factory Console: report render progress live (frame count, last frame, the output folder for the monitor).

Use it either way:
  - headless:  blender -b scene.blend --python blender_render_feed.py -a
  - in the UI: open it in the Text Editor, tick "Register" (runs on every file load) and press Run Script once.
Works in Blender 4.x and 5.x. Standard library only. Talks to http://127.0.0.1:8787 (FACTORY_URL to change).
"""
import bpy, json, os, time, urllib.request

URL = os.environ.get("FACTORY_URL", "http://127.0.0.1:8787") + "/api/feed"
_t = {"start": 0.0, "last": 0.0}


def _send(state, progress=None, log=""):
    sc = bpy.context.scene
    name = "%s · %s" % (os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "untitled", sc.name)
    out = os.path.dirname(bpy.path.abspath(sc.render.filepath)) or ""
    body = {"name": name, "state": state, "progress": progress, "log": log, "frames_dir": out}
    try:
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=1).close()
    except Exception:
        pass                                   # the render never waits on the console


def _init(scene, *a):
    _t["start"] = time.time()
    _send("running", 0.0, "starting frames %d-%d" % (scene.frame_start, scene.frame_end))


def _post(scene, *a):
    done = scene.frame_current - scene.frame_start + 1
    total = max(1, scene.frame_end - scene.frame_start + 1)
    per = (time.time() - _t["start"]) / max(1, done)
    _send("running", done / total, "frame %d / %d · %.1f s per frame · %d min left" % (done, total, per, per * (total - done) / 60))


def _complete(scene, *a):
    _send("done", 1.0, "%d frames in %.1f min" % (scene.frame_end - scene.frame_start + 1, (time.time() - _t["start"]) / 60))


def _cancel(scene, *a):
    _send("failed", None, "cancelled at frame %d" % scene.frame_current)


for name, fn in (("render_init", _init), ("render_post", _post), ("render_complete", _complete), ("render_cancel", _cancel)):
    handlers = getattr(bpy.app.handlers, name)
    for h in [h for h in handlers if getattr(h, "__name__", "") == fn.__name__]:
        handlers.remove(h)                     # running the script twice must not report twice
    handlers.append(fn)
print("Factory Console feed: on (%s)" % URL)
