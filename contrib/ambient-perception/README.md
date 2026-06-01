# Ambient Perception for HyperHDR (anime / animejanai)

A GPU **"ambient perception"** bias-lighting service. It taps decoded video
frames inside the `mpv-upscale-2x_animejanai` VapourSynth chain, runs
foreground/background **segmentation** on the GPU, computes a **stable per-side
ambient color** that mirrors HyperHDR's C++ `advanced_ambient` math, and streams
it to HyperHDR so your two side lights behave as an extension of the scene's
**background lighting** -- not a vivid accent, not the character's clothing.

This is the *premium* path. HyperHDR already ships a `advanced_ambient` baseline
mode that does the same color math **without** perception; this service adds real
foreground/background separation so a small saturated costume can't pull the
lights.

---

## > UNTESTED -- NEEDS YOUR GPU / VAPOURSYNTH / MODELS <

**This reference implementation could not be run end-to-end in the authoring
environment (no GPU, no VapourSynth, no ONNX models).** What *was* verified here:

- The HyperHDR **FlatBuffers wire protocol** -- the message encoders in
  `hyperhdr_client.py` were validated **byte-for-byte against the reference
  `flatbuffers` Python library** and round-tripped through its runtime parser
  (Register / Image / Color / Clear). See "Protocol: verified vs. assumed".
- The **color + temporal math** -- ported line-by-line from the repo's C++ and
  exercised with synthetic frames (OKLab round-trip, segmentation demotion,
  whole-frame fallback, scene-cut + flash rejection).

What you must validate on your machine: the **ONNX model tensor names/shapes**
(interfaced with `# TODO/ASSUMPTION` notes), the **VapourSynth wiring**, GPU
**execution providers**, and overall look. Use the checklist at the bottom.

---

## Architecture

```
 mpv-upscale-2x_animejanai (.vpy)
   decoded source clip ──┬─────────────► vs-mlrt / TensorRT 2x upscale ──► mpv out
                         │ (branch, ~512px, RGB24)
                         ▼
                  vapoursynth_sink.vpy  (per frame, keyed by PTS)
                         │
                         ▼
        ┌────────────────────────────────────────────────┐
        │ AmbientService.process_frame(rgb_u8, pts_ms)    │
        │  1. segmentation → foreground mask (bgConf=1-fg)│
        │     (+ optional depth → far weight)             │
        │  2. per-side estimate_side()  (OKLab, mirrors   │
        │     C++ calcAmbientForLeds; weight =            │
        │     linearLuma·visible·bgConf·edgeFalloff)      │
        │  3. scene-cut (histogram, +opt TransNetV2) +    │
        │     dt-correct OKLab EMA (snap on confirmed cut)│
        │  4. paint 64×36 image: LEFT half=left color,    │
        │     RIGHT half=right color                      │
        └───────────────────────┬────────────────────────┘
                                 ▼
        HyperHDR FlatBuffers grabber input (port 19400)
                                 ▼
        HyperHDR LED layout: two side LEDs, mapping "advanced"
                                 ▼
                    two physical ambient lights
```

The **painted-image** approach means this service never speaks any per-LED
protocol. It hands HyperHDR a tiny image whose left/right halves are the two
colors; HyperHDR's own LED layout maps the halves to the two lights. Set the
instance's **image-to-LED mapping** to `advanced` (or `advanced_ambient` -- since
each half is already a flat color, any averaging mode reproduces it).

### Modules

| File | Role |
|------|------|
| `ambient_perception/oklab.py` | sRGB↔OKLab color math (Björn Ottosson matrices), gamut clamp. Mirrors `ColorSpace.cpp`/`.h`. |
| `ambient_perception/ambient.py` | Per-side estimator. Mirrors `ImageColorAveraging::calcAmbientForLeds` + adds `bgConfidence`/depth weighting. |
| `ambient_perception/temporal.py` | dt-correct OKLab EMA + histogram scene-cut. Mirrors `ImageToLedManager::applyAmbientTemporal` / `ambientSceneCutDistance`. |
| `ambient_perception/segmentation.py` | Anime foreground segmentation (ONNX) → `foreground_mask`. Graceful fallback. |
| `ambient_perception/cutdetect.py` | Optional TransNetV2 ONNX scene-cut (default is the histogram). |
| `ambient_perception/depth.py` | Optional Depth-Anything-V2 ONNX → far-depth weight. |
| `ambient_perception/hyperhdr_client.py` | **Verified** FlatBuffers grabber client. |
| `ambient_perception/service.py` | Main loop tying it all together + YAML config. |
| `ambient_perception/vapoursynth_sink.vpy` | animejanai integration example. |

---

## The HyperHDR protocol (verified, do not guess)

All of the following was read directly from this repo, not assumed:

### Default port

**`19400`** -- `sources/base/schema/schema-flatbufServer.json` →
`properties.port.default = 19400`.

### Schema (`include/flatbuffers/parser/hyperhdr_request.fbs`)

```fbs
namespace hyperhdrnet;

table Register   { origin:string (required); priority:int; }
table RawImage   { data:[ubyte]; width:int = -1; height:int = -1; }
table NV12Image  { data_y:[ubyte]; data_uv:[ubyte]; width:int; height:int;
                   stride_y:int = 0; stride_uv:int = 0; }
union  ImageType { RawImage, NV12Image }            // RawImage = 1
table Image      { data:ImageType (required); duration:int = -1; }
table Clear      { priority:int; }
table Color      { data:int = -1; duration:int = -1; }
union  Command   { Color, Image, Clear, Register }  // Color=1 Image=2 Clear=3 Register=4
table Request    { command:Command (required); }
root_type Request;
```

Reply (`hyperhdr_reply.fbs`): `table Reply { error:string; video:int=-1;
registered:int=-1; }`.

### Wire framing (`FlatBuffersServerConnection.cpp`, `FlatBuffersClient.cpp`)

Every message -- both directions -- is **a 4-byte big-endian length prefix
followed by the FlatBuffer bytes**. Max accepted frame is 10,000,000 bytes.

### Handshake & priority

- On connect, send `Register{ origin, priority }`. The server replies with a
  `Reply` whose `registered` echoes the accepted priority. The C++ client only
  starts streaming images *after* that reply; this client mirrors that.
- **Priority MUST be in `[50, 250]`** -- `FlatBuffersServerConnection.cpp` logs
  *"Valid priority for Flatbuffer connections is between 50 and 250"* and
  ignores out-of-range registers. Default here: `150`.

### Encodings (`FlatBuffersParser.cpp`)

- `RawImage` is interpreted as **packed RGB**, 3 bytes/pixel, row-major
  (`FLATBUFFERS_IMAGE_FORMAT::RGB`); `size = width*height*3`.
- `Color.data = (r<<16)|(g<<8)|b`.

### Verification we ran

`hyperhdr_client.py`'s hand-rolled encoders were diffed **byte-for-byte** against
the official `flatbuffers` Python `Builder` replicating the exact
`CreateRawImage/CreateImage/CreateRequest/CreateRegister/CreateColor/CreateClear`
sequences from `FlatBuffersParser.cpp` -- all messages matched (including
default-field omission and vtable truncation). The buffers were then navigated
with the `flatbuffers` runtime to confirm field values. The hand-rolled builder
needs **no `flatbuffers` package at runtime**.

#### Alternative: generated `flatc` bindings

If you'd rather not trust a hand-rolled builder, generate Python bindings and use
them instead:

```bash
# flatc ships with the flatbuffers project (external/flatbuffers).
flatc --python -o ./gen include/flatbuffers/parser/hyperhdr_request.fbs
flatc --python -o ./gen include/flatbuffers/parser/hyperhdr_reply.fbs
```

Then build `Request` with the generated `hyperhdrnet` module and keep the same
4-byte big-endian framing from `hyperhdr_client.py::_frame`. Both paths are
correct; the hand-rolled one is the default so the install has zero codegen.

### Fallback: JSON `color` API (single color only)

HyperHDR's JSON API accepts
`{"command":"color","color":[R,G,B],"priority":150,"origin":"x"}` over its JSON
port (default `19444`). **It can only set ONE color**, so it cannot drive two
different side colors -- use it only to sanity-check connectivity, not as the
real feed.

---

## Install

```bash
cd contrib/ambient-perception
python -m pip install -r requirements.txt
# or: python -m pip install -e .[gpu]
```

- **`onnxruntime-gpu`** with the **TensorRT** and **CUDA** execution providers is
  the intended runtime. On an **RTX 5090 (Blackwell / sm_120)** you need a recent
  ORT + CUDA 12.x / TensorRT build with Blackwell support -- check the
  onnxruntime release notes and match your installed CUDA/TensorRT. If the GPU
  package is unavailable the service still runs on CPU (slowly) or with no models
  at all (baseline behaviour).
- **VapourSynth** is provided by your mpv/animejanai install -- do **not**
  pip-install it into that environment. It's only listed (commented) for a
  standalone companion process.

### Tests

Behavior tests (need only `numpy`) validate the color math without any GPU/model:

```bash
cd contrib/ambient-perception
python tests/test_ambient.py          # or: python -m pytest tests
```

They assert the headline guarantees on synthetic pixels: OKLab round-trips
within tolerance; a small vivid red blob over a muted-green background yields a
gentle green (not red, not neon); segmentation only strengthens that; and a
background-starved zone reports starvation so the service falls back.

---

## Wiring into the animejanai VapourSynth chain

Open the `.vpy` that `mpv-upscale-2x_animejanai` loads (the upscale graph) and
add one line right after you have the decoded source clip, **before** the
vs-mlrt / TensorRT upscale node:

```python
from ambient_perception.vapoursynth_sink import attach_ambient

clip = video_in                     # decoded source from mpv
clip = attach_ambient(clip)         # perceive the SOURCE (cheap, ~512px branch)
clip = upscale_with_mlrt(clip)      # your existing vs-mlrt TensorRT 2x
clip.set_output()
```

`attach_ambient` branches a downscaled RGB24 copy, runs perception per frame
keyed by PTS, streams to HyperHDR, and returns your clip **unchanged** (the sink
is side-effect only and never breaks playback -- exceptions are logged, not
raised). Point it at your edited config:

```python
clip = attach_ambient(clip, config_path=r"C:/path/to/config.yaml")
```

### Companion-process design (no .vpy edit)

If you don't want to touch the animejanai chain, run a separate process that
decodes frames with its own `onnxruntime-gpu` / VapourSynth / ffmpeg pipeline and
calls `AmbientService.process_frame(rgb_u8, pts_ms)` itself. Same service, same
config; it just owns its frame source. This trades a second decode for zero
coupling to animejanai.

---

## HDR tone-map caveat (read this for projector/anime HDR)

**mpv applies HDR tone-mapping AFTER VapourSynth.** So frames seen by this sink
are whatever VapourSynth produced, *not* the SDR pixels you actually see:

- **SDR content:** frames are already display-referred → the ambient math is
  correct as-is.
- **HDR content (PQ/HLG):** frames are scene-referred and will look wrong to the
  SDR ambient math (washed/over-bright). Options, best first:
  1. **Tap the rendered framebuffer instead.** Drive the lights from what's
     actually on screen post-tonemap (e.g. mpv's own screenshot/`vo` capture, or
     HyperHDR's own screen grabber) rather than the VapourSynth branch.
  2. **Approximate the tonemap on the branch.** Before calling the sink, apply a
     tonemap to the downscaled branch (e.g. `core.placebo.Tonemap`, or a fixed
     Hable/Reinhard curve) so the colors roughly match the display. Set
     `matrix_in_s` correctly and convert PQ→linear→display first. This is an
     approximation, not mpv's exact pipeline.
  3. HyperHDR itself has an `hdrToneMapping` option on the flatbuffers input
     (`schema-flatbufServer.json`), but that maps the **incoming** image -- it
     won't reconstruct detail the scene-referred branch already lost.

---

## Models (all pretrained; download + ONNX convert; graceful fallback)

Every model is optional. Missing model ⇒ that signal becomes a no-op and the
service logs **one** warning, never crashes.

### Segmentation (load-bearing for the premium path)

- **SkyTNT `anime-segmentation`** (ISNet-based) -- recommended.
  - Repo: <https://github.com/SkyTNT/anime-segmentation>
  - Hugging Face `skytnt/anime-seg` publishes **`isnetis.onnx`** directly (no
    conversion needed). Put its path in `models.segmentation`.
- Alternatives: U²-Net / ISNet general matting exported to ONNX.
- Interface: `Segmenter.foreground_mask(rgb_u8) -> float[H,W]` in `[0,1]`
  (1 = character). The estimator uses `bgConfidence = 1 - mask`.
- `# TODO/ASSUMPTION` in `segmentation.py`: exact **input size/range/RGB-order**
  and **output tensor name/shape** vary per export. The defaults assume a
  1024×1024 RGB `[0,1]` NCHW input and a single sigmoided `(1,1,H,W)` mask --
  **confirm against your file** (the module prints the providers it loaded; you
  can dump tensor names with a one-liner shown in the source).
- **Absent ⇒ `bgConfidence = 1` everywhere** = the exact C++ baseline /
  whole-frame estimate.

### Scene-cut

- **Default: classical 8×8×8 RGB histogram** (no model) -- robust, mirrors the
  C++. Always on.
- **Optional: TransNetV2 ONNX** (<https://github.com/soCzech/TransNetV2>) OR-ed
  with the histogram. Note it is ~50 frames latent (needs lookahead), so the
  histogram remains the low-latency primary.

### Depth (optional, low weight)

- **Depth-Anything-V2 ONNX**
  (<https://github.com/DepthAnything/Depth-Anything-V2>; ONNX exports e.g.
  `fabio-sim/Depth-Anything-ONNX`). Multiplies the background weight by a
  far-depth weight so distant background outvotes near midground. **Absent ⇒
  skipped** (`far_weight = 1`).

---

## Config reference

See `config.example.yaml`. Key fields:

| Key | Default | Meaning |
|-----|---------|---------|
| `hyperhdr.host/port` | `127.0.0.1` / `19400` | HyperHDR flatbuffers endpoint (port verified). |
| `hyperhdr.priority` | `150` | Must be in `[50,250]`. |
| `output.width/height` | `64/36` | Painted feed image size. |
| `downscale` | `512` | Perception branch longest side (px). |
| `ambient.chroma_max` (Cmax) | `0.06` | Tint strength ceiling (OKLab). |
| `ambient.chroma_dead` (Cdead) | `0.012` | Neutral deadzone. |
| `ambient.edge_min` | `0.20` | Weight floor far from the lamp's edge. |
| `ambient.sigma_k` | `2.5` | Soft sigma-clip radius. |
| `temporal.tau_ms` | `650` | EMA settling time. |
| `temporal.cut_sensitivity` (k) | `4.0` | Cut threshold = `max(0.02, k·median)`. |
| `models.*` | `null` | Optional ONNX paths. |
| `models.depth_strength` | `0.5` | 0 disables depth weighting, 1 = full. |
| `providers` | TRT→CUDA→CPU | onnxruntime execution providers. |
| `zones` | left/right split | Per-lamp fractional x-bands. |

These defaults match the C++ schema fields (`ambientTintStrength = 0.06`,
`ambientSettlingMs = 650`, `ambientCutSensitivity = 4.0`).

---

## Validation checklist (run these on your rig)

Confirm the lights behave as "scene background lighting," not an accent:

- [ ] **Candle / dark scene** — lights stay **dim and warm**, never lifted to
      grey or boosted. (L is carried untouched; no auto-gain.)
- [ ] **Sunny exterior** — lights are **bright and faintly warm**, not neon.
- [ ] **Hospital / fluorescent** — near-**neutral white**, slightly cool;
      should sit inside the chroma deadzone (barely any tint).
- [ ] **Green apples / single vivid prop** on a neutral background — lights stay
      **neutral**; the small saturated blob is demoted by the sigma-clip *and*
      (with segmentation) by `bgConfidence`. Toggle the model off to see it pull
      green, on to see it stay neutral.
- [ ] **Fast fight scene** — colors **glide**, no strobing/hue-flips
      (EMA + tau). Brightness still tracks.
- [ ] **Hard cut** between two very different shots — color **snaps within ~1
      frame** (confirmed-cut). A **lightning flash** (2-frame spike) does **not**
      snap; the EMA absorbs it.
- [ ] **Full-frame close-up** (character fills the frame, no background) — lights
      **hold / fall back to the whole-frame estimate** instead of washing grey.
- [ ] **Left vs. right asymmetry** — a colored light source on one side of the
      frame tints only that side (edge-falloff).

If a model's tensor names are wrong you'll see a one-time warning and baseline
behaviour -- fix the `# TODO/ASSUMPTION` shapes in the relevant module.

---

## Notes on fidelity to the C++

The estimator and temporal stages are a faithful port of
`sources/base/ImageColorAveraging.cpp` and `sources/base/ImageToLedManager.cpp`.
One honest caveat: HyperHDR's published **OKLab inverse matrices are rounded**
constants, so an sRGB→OKLab→sRGB round-trip carries ~0.01 error — *this service
reproduces exactly that*, because it uses the same constants. The point is to
match HyperHDR's look, not a textbook-perfect OKLab.
