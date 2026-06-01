# animejanai → HyperHDR ambient lighting (drop‑in)

Stream the decoded video frame from animejanai's VapourSynth pipeline to HyperHDR's
FlatBuffers input, so ambient lights follow what you're watching. Everything is
**self‑contained** (stdlib + the numpy animejanai already ships) and **opt‑in** —
off by default, and it never affects users who don't use HyperHDR.

**You only need one file: `ambient.py`.** The `.vpy` profiles in this folder are an
*optional* throwaway way to test without touching animejanai's code — you do **not**
need them for the real setup.

---

## The clean way — one file + one line + a flag (recommended)

This makes **every existing profile** stream to HyperHDR when enabled, with **no
extra `.vpy` files and no per‑profile edits**.

### 1. Drop in the file
Copy **`ambient.py`** into your animejanai **core** folder, next to
`animejanai_core.py`:
`…\mpv-upscale-2x_animejanai-…\animejanai\core\ambient.py`

### 2. Add one line to `animejanai_core.py`
In `run_animejanai` (the function the profiles call), right after the working
`clip` exists and **before** the upscale resize, add:

```python
clip = __import__("ambient").maybe_tap(clip, container_fps, config)
```

That's it. `maybe_tap` returns the clip **unchanged** unless ambient is enabled, so
this line is a guaranteed no‑op for anyone who doesn't turn it on. (Pass whatever
your config object is called; if `run_animejanai` doesn't have one in scope, use
`maybe_tap(clip, container_fps)` and enable via the env var below.)

### 3. Flip the flag
Two ways, pick one:

- **animejanai.conf** — add a section:
  ```ini
  [ambient]
  enabled = true
  ```
- **environment variable** — set `AMBIENT_ENABLE=1` (e.g. a system env var, or in
  the launcher that starts mpv).

Default is **off**. Host/port default to `127.0.0.1:19400`; override with
`AMBIENT_HOST` / `AMBIENT_PORT` if HyperHDR is on another machine.

> **Why not a flag in `mpv.conf`?** mpv has no way to pass a setting *into* the
> VapourSynth Python that runs your profiles — so the toggle has to live where
> animejanai's own code reads it (`animejanai.conf` or the env var). Same result:
> one switch, all profiles. Later you can expose it as a checkbox in
> AnimeJaNaiConfEditor.

### Why non‑HyperHDR users are unaffected
- **Default off** → behavior identical to today out of the box.
- **`maybe_tap` only imports the socket/numpy tap when enabled** → no overhead, no
  import risk when off.
- **No new dependencies** → stdlib + the numpy animejanai already uses.
- **Upscale untouched** → the tap returns the source frame unchanged (byte‑for‑byte).
- **Fails silently** → if HyperHDR isn't running, the socket error is swallowed and
  a reconnect is attempted next frame; playback is never interrupted.

---

## Optional — test it without editing animejanai's code

If you just want to try it before adding the line above, use the throwaway
profiles in this folder (no `animejanai_core.py` edit needed):

1. Copy `ambient.py` into `animejanai\core\` (as above).
2. Copy `animejanai_ambient.vpy` into `animejanai\profiles\`; set the `1002` to the
   upscale id your normal profile uses. (`animejanai_ambient_taponly.vpy` is the
   no‑upscale variant for the `[upscale-off]` profile.)
3. In `portable_config\mpv.conf`:
   ```ini
   [upscale-on-ambient]
   vf=vapoursynth="~~/../animejanai/profiles/animejanai_ambient.vpy":buffered-frames=1:concurrent-frames=8
   ```
4. In `portable_config\input.conf`: `F6 apply-profile upscale-on-ambient`
5. Play something, press **F6**.

These profiles call `attach_ambient()` directly (always on, no flag) — handy for a
quick check, but the one‑line hook above is the real setup.

---

## HyperHDR side
Select a mapping mode (e.g. `advanced_ambient` or `unicolor_mean`) and tick
**System Control → VapourSynth/mpv capture** so the screen grabber stays off. The
feed registers at priority 150, which outranks the grabber anyway.

### Notes
- The tap downscales to 256 px (plenty for LED mapping) and sends synchronously in
  the frame callback. Synchronous matters: a background thread dies silently inside
  mpv's VapourSynth worker context — that was the cause of the earlier "no output".
- HDR caveat: mpv tone‑maps after VapourSynth, so for HDR sources the streamed
  colors are scene‑referred. For SDR anime this is correct as‑is.
