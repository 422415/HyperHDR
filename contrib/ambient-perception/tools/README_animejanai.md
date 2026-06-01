# animejanai → HyperHDR ambient lighting (drop‑in)

These files stream the decoded video frame from animejanai's VapourSynth pipeline
to HyperHDR's FlatBuffers input, so ambient lights follow what you're watching.
Everything is **self‑contained** (stdlib + the numpy animejanai already ships) and
**opt‑in** — it does nothing unless you wire it in, and it never affects users who
don't use HyperHDR.

Files:
- **`ambient.py`** — the whole thing in one module (FlatBuffers client + the tap).
- **`animejanai_ambient.vpy`** — a profile: 2× upscale **+** ambient stream.
- **`animejanai_ambient_taponly.vpy`** — a profile: ambient stream, **no** upscale
  (for the `[upscale-off]` profile so lights survive Ctrl+0).

HyperHDR side: select a mapping mode (e.g. `advanced_ambient` or `unicolor_mean`)
and tick **System Control → VapourSynth/mpv capture** so the screen grabber stays
off. The feed registers at priority 150, which outranks the grabber anyway.

---

## A. Quick drop‑in (try it now)

1. Copy **`ambient.py`** into your animejanai **core** folder, next to
   `animejanai_core.py`, e.g.:
   `…\mpv-upscale-2x_animejanai-…\animejanai\core\ambient.py`
   (that folder is already on VapourSynth's path, so `from ambient import …` works).
2. Copy **`animejanai_ambient.vpy`** (and, if you want, `…_taponly.vpy`) into your
   `animejanai\profiles\` folder. Open `animejanai_ambient.vpy` in Notepad and set
   the `1002` to the upscale id your normal profile uses.
3. In `portable_config\mpv.conf` add:
   ```ini
   [upscale-on-ambient]
   vf=vapoursynth="~~/../animejanai/profiles/animejanai_ambient.vpy":buffered-frames=1:concurrent-frames=8
   ```
   (and, if you made the tap‑only one, point `[upscale-off]` at
   `animejanai_ambient_taponly.vpy` instead of `vf=""`).
4. In `portable_config\input.conf` bind a key:
   ```ini
   F6 apply-profile upscale-on-ambient
   ```
5. Play something, press **F6**. Host/port default to `127.0.0.1:19400`; override
   with the `AMBIENT_HOST` / `AMBIENT_PORT` env vars if HyperHDR is elsewhere.

---

## B. Proper integration (ship it; zero effect on non‑HyperHDR users)

Instead of special profiles, hook it into the core once, behind a config flag that
defaults to **off**. Then every profile gets ambient when enabled, and users who
don't enable it never load a line of this code.

1. Ship `ambient.py` in `animejanai/core/`.
2. Add an `[ambient]` section to `animejanai.conf` (and parse it in
   `animejanai_config.py`):
   ```ini
   [ambient]
   enabled = false        ; default OFF
   host = 127.0.0.1
   port = 19400
   downscale = 256
   ```
3. Add ONE guarded hook in `animejanai_core.run_animejanai` (right after you have
   the working `clip`, before the upscale resize):
   ```python
   if ambient_enabled:                       # from config; default False
       from ambient import attach_ambient    # lazy import -> not even loaded when off
       clip = attach_ambient(clip, container_fps, host=ambient_host, port=ambient_port,
                             downscale=ambient_downscale)
   ```

Why non‑HyperHDR users are unaffected:
- **Default off** → behavior identical to today out of the box.
- **Lazy import** → when off, `ambient.py` is never imported; no overhead, no risk
  of an import error.
- **No new dependencies** → standard library + the numpy animejanai already uses.
- **Upscale untouched** → the tap returns the source frame unchanged (byte‑for‑byte).
- **Fails silently** → if HyperHDR isn't running, the socket error is swallowed and
  a reconnect is attempted next frame; playback is never interrupted.

Optional polish: expose `enabled` / `host` / `port` in **AnimeJaNaiConfEditor** so
it's a checkbox instead of a config‑file edit.

---

### Notes
- The tap downscales to 256 px (plenty for LED mapping) and sends synchronously in
  the frame callback. Synchronous matters: a background thread dies silently inside
  mpv's VapourSynth worker context — that was the cause of the earlier "no output".
- HDR caveat: mpv tone‑maps after VapourSynth, so for HDR sources the streamed
  colors are scene‑referred. For SDR anime this is correct as‑is.
