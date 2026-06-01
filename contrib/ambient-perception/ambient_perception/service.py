"""Main service: tie perception + ambient math + temporal + HyperHDR feed.

Flow per incoming frame (downscaled sRGB uint8 RGB, shape (H, W, 3)):

  1. Run perception models (segmentation -> foreground mask, optional depth)
     once on the whole frame. Both degrade gracefully to "no effect" when
     their model is absent.
  2. Split the frame into per-side zones (default: left half / right half) and
     run :func:`ambient.estimate_side` on each, weighting background pixels by
     ``bgConfidence = 1 - foreground`` (* far-depth). If a side is
     background-starved (full-frame close-up) fall back to a whole-frame
     estimate so it never washes to grey.
  3. Detect scene cuts (histogram by default; optional TransNetV2) and run the
     per-side OKLab EMA, snapping on a confirmed cut.
  4. Paint a tiny image (default 64x36): LEFT half = left color, RIGHT half =
     right color, and stream it over the HyperHDR FlatBuffers grabber input.
     HyperHDR's LED layout (two side LEDs, mapping ``advanced``) turns the
     painted halves into the two physical light colors.

The painted-image approach means we never speak any per-LED protocol: HyperHDR
does the LED mapping. (Its own ``advanced_ambient`` mode would even re-derive
ambient from our painted halves, but since each half is already a flat color
any averaging mode reproduces it.)

This module is transport-agnostic: call :meth:`AmbientService.process_frame`
from the VapourSynth sink (see ``vapoursynth_sink.vpy``) or from a companion
process that decodes frames itself.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np

from . import ambient, oklab
from .depth import DepthEstimator
from .hyperhdr_client import HyperHDRFlatClient
from .segmentation import Segmenter
from .temporal import AmbientEma, SceneCutDetector

logger = logging.getLogger(__name__)


@dataclass
class Zone:
    """A side zone defined as fractional x-range [x0, x1) of the frame."""

    name: str          # "left" / "right"
    side: str          # "left" / "right" (which edge gets full edge weight)
    x0: float
    x1: float


@dataclass
class ServiceConfig:
    # HyperHDR feed
    host: str = "127.0.0.1"
    port: int = 19400
    priority: int = 150
    origin: str = "AmbientPerception"
    duration_ms: int = -1          # -1 = infinite (held until next frame)

    # Painted output image fed to HyperHDR (LEFT half / RIGHT half).
    out_width: int = 64
    out_height: int = 36

    # Frame downscale target (longest side, px) -- the .vpy/companion does the
    # actual resize; this is documentation + a safety clamp.
    downscale: int = 512

    # Ambient math
    chroma_max: float = 0.06       # Cmax / tint strength
    chroma_dead: float = 0.012     # Cdead
    edge_min: float = 0.20
    sigma_k: float = 2.5

    # Temporal
    tau_ms: float = 650.0
    cut_sensitivity: float = 4.0   # k for histogram cut threshold

    # Model paths (all optional; absent => graceful fallback)
    segmentation_model: str | None = None
    depth_model: str | None = None
    transnet_model: str | None = None
    depth_strength: float = 0.5

    providers: list[str] | None = None

    # Zones (defaults: clean left/right split).
    zones: list[Zone] = field(default_factory=lambda: [
        Zone("left", "left", 0.0, 0.5),
        Zone("right", "right", 0.5, 1.0),
    ])


class AmbientService:
    """Stateful per-instance ambient estimator + HyperHDR streamer."""

    def __init__(self, cfg: ServiceConfig) -> None:
        self.cfg = cfg
        self._ambient_cfg = ambient.AmbientConfig(
            chroma_dead=cfg.chroma_dead,
            chroma_max=cfg.chroma_max,
            edge_min=cfg.edge_min,
            sigma_k=cfg.sigma_k,
        )
        self._segmenter = Segmenter(cfg.segmentation_model, cfg.providers)
        self._depth = DepthEstimator(cfg.depth_model, cfg.providers, cfg.depth_strength)
        self._transnet = None
        if cfg.transnet_model:
            from .cutdetect import TransNetCutDetector  # lazy: optional path
            self._transnet = TransNetCutDetector(cfg.transnet_model, cfg.providers)
        self._cut = SceneCutDetector(cfg.cut_sensitivity)
        self._ema = AmbientEma(cfg.tau_ms)
        self._client: HyperHDRFlatClient | None = None
        self._last_connect_attempt = 0.0

    # -- HyperHDR connection (reconnects every 5s, mirroring the C++ client) -
    def _ensure_client(self) -> HyperHDRFlatClient | None:
        if self._client is not None:
            return self._client
        now = time.monotonic()
        if now - self._last_connect_attempt < 5.0:
            return None
        self._last_connect_attempt = now
        try:
            c = HyperHDRFlatClient(self.cfg.host, self.cfg.port,
                                   self.cfg.priority, self.cfg.origin)
            c.connect()
            self._client = c
            logger.info("Connected to HyperHDR %s:%d (priority %d)",
                        self.cfg.host, self.cfg.port, self.cfg.priority)
        except OSError as exc:
            logger.warning("HyperHDR connect failed (%s); retrying in 5s.", exc)
            self._client = None
        return self._client

    def reset(self) -> None:
        """Drop all temporal state (call on seek / instance restart)."""
        self._cut.reset()
        self._ema.reset()

    # -- main entry point ---------------------------------------------------
    def process_frame(self, rgb_u8: np.ndarray, pts_ms: float | None = None) -> dict:
        """Process one frame and stream the result to HyperHDR.

        Parameters
        ----------
        rgb_u8:
            ``(H, W, 3)`` uint8 sRGB RGB frame, ALREADY downscaled (~512px).
            See the HDR caveat in the README: this must be SDR-mapped pixels.
        pts_ms:
            Frame presentation timestamp in ms (preferred for dt-correctness).
            Falls back to wall-clock if None.

        Returns a small dict of the per-side sRGB colors (for logging/tests).
        """
        if rgb_u8.dtype != np.uint8 or rgb_u8.ndim != 3 or rgb_u8.shape[2] != 3:
            raise ValueError("rgb_u8 must be an (H, W, 3) uint8 array")
        now_ms = pts_ms if pts_ms is not None else time.monotonic() * 1000.0
        h, w, _ = rgb_u8.shape

        # 1) Perception (whole frame, once).
        fg = self._segmenter.foreground_mask(rgb_u8)        # (H,W) in [0,1]
        bg_conf_full = 1.0 - fg
        far_full = self._depth.far_weight(rgb_u8) if self._depth.active else None

        # 2) Per-side ambient estimate -> raw OKLab.
        raw_oklab: dict[str, np.ndarray] = {}
        srgb_dbg: dict[str, tuple] = {}
        for zone in self.cfg.zones:
            x0 = int(round(zone.x0 * w))
            x1 = int(round(zone.x1 * w))
            x0 = max(0, min(x0, w - 1))
            x1 = max(x0 + 1, min(x1, w))
            sub = rgb_u8[:, x0:x1, :]
            sub_bg = bg_conf_full[:, x0:x1]
            sub_far = far_full[:, x0:x1] if far_full is not None else None

            res = ambient.estimate_side(
                sub, side=zone.side, bg_confidence=sub_bg,
                far_weight=sub_far, cfg=self._ambient_cfg,
            )
            if res.oklab is None or res.background_starved:
                # Full-frame close-up / low confidence: fall back to a
                # whole-frame estimate (bgConfidence = 1) so we never wash grey.
                res = ambient.whole_frame_oklab(sub, side=zone.side, cfg=self._ambient_cfg)
            raw_oklab[zone.name] = res.oklab  # may still be None (true black)

        # 3) Scene-cut + temporal EMA.
        scene_cut = self._cut.update(rgb_u8, now_ms)
        if self._transnet is not None and self._transnet.active:
            scene_cut = scene_cut or self._transnet.is_cut(rgb_u8)
        smoothed = self._ema.step(raw_oklab, now_ms, scene_cut)

        # 4) Convert smoothed OKLab -> sRGB u8 per side.
        side_rgb: dict[str, np.ndarray] = {}
        for name, lab in smoothed.items():
            if lab is None:
                side_rgb[name] = np.zeros(3, dtype=np.uint8)
                continue
            linear = oklab.oklab_to_linear_rgb(oklab.clamp_oklab_chroma_to_gamut(lab))
            side_rgb[name] = oklab.linear_to_srgb_u8(np.clip(linear, 0.0, 1.0))
            srgb_dbg[name] = tuple(int(v) for v in side_rgb[name])

        # 5) Paint and stream.
        img = self._paint(side_rgb)
        client = self._ensure_client()
        if client is not None:
            try:
                client.send_image(img.tobytes(), self.cfg.out_width,
                                  self.cfg.out_height, self.cfg.duration_ms)
            except OSError as exc:
                logger.warning("HyperHDR send failed (%s); will reconnect.", exc)
                self._client = None

        return {"cut": scene_cut, "colors": srgb_dbg}

    def _paint(self, side_rgb: dict[str, np.ndarray]) -> np.ndarray:
        """Paint the LEFT/RIGHT halves into the small output image.

        For the default two-zone config this is a clean left/right split. For
        N zones, each zone paints its fractional x-band, so the painted image
        matches the screen geometry HyperHDR will sample.
        """
        ow, oh = self.cfg.out_width, self.cfg.out_height
        img = np.zeros((oh, ow, 3), dtype=np.uint8)
        for zone in self.cfg.zones:
            color = side_rgb.get(zone.name, np.zeros(3, dtype=np.uint8))
            x0 = int(round(zone.x0 * ow))
            x1 = int(round(zone.x1 * ow))
            x0 = max(0, min(x0, ow))
            x1 = max(x0, min(x1, ow))
            img[:, x0:x1, :] = color
        return img

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_config(path: str) -> ServiceConfig:
    """Load a ServiceConfig from a YAML file (see config.example.yaml)."""
    import yaml  # lazy import so the module is usable without pyyaml in tests

    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    hh = raw.get("hyperhdr", {})
    models = raw.get("models", {})
    amb = raw.get("ambient", {})
    temporal = raw.get("temporal", {})
    out = raw.get("output", {})

    zones_raw = raw.get("zones")
    if zones_raw:
        zones = [Zone(z["name"], z.get("side", z["name"]), float(z["x0"]), float(z["x1"]))
                 for z in zones_raw]
    else:
        zones = ServiceConfig().zones

    return ServiceConfig(
        host=hh.get("host", "127.0.0.1"),
        port=int(hh.get("port", 19400)),
        priority=int(hh.get("priority", 150)),
        origin=hh.get("origin", "AmbientPerception"),
        duration_ms=int(hh.get("duration_ms", -1)),
        out_width=int(out.get("width", 64)),
        out_height=int(out.get("height", 36)),
        downscale=int(raw.get("downscale", 512)),
        chroma_max=float(amb.get("chroma_max", 0.06)),
        chroma_dead=float(amb.get("chroma_dead", 0.012)),
        edge_min=float(amb.get("edge_min", 0.20)),
        sigma_k=float(amb.get("sigma_k", 2.5)),
        tau_ms=float(temporal.get("tau_ms", 650.0)),
        cut_sensitivity=float(temporal.get("cut_sensitivity", 4.0)),
        segmentation_model=models.get("segmentation"),
        depth_model=models.get("depth"),
        transnet_model=models.get("transnet"),
        depth_strength=float(models.get("depth_strength", 0.5)),
        providers=raw.get("providers"),
        zones=zones,
    )


# A module-level singleton makes it trivial for the .vpy sink to call in.
_SERVICE: AmbientService | None = None


def get_service(config_path: str | None = None) -> AmbientService:
    """Return a process-wide :class:`AmbientService`, constructing it once.

    The VapourSynth sink calls this with the config path on the first frame.
    """
    global _SERVICE
    if _SERVICE is None:
        cfg = load_config(config_path) if config_path else ServiceConfig()
        _SERVICE = AmbientService(cfg)
    return _SERVICE
