"""Ambient perception service for HyperHDR bias lighting.

A GPU "ambient perception" path that taps decoded (anime) video frames, runs
foreground/background segmentation, computes a stable per-side ambient color
mirroring HyperHDR's C++ ``advanced_ambient`` math, and streams it to HyperHDR
over the FlatBuffers grabber input.

Public surface:
    * service.AmbientService / ServiceConfig / load_config
    * ambient.estimate_side          (the per-side OKLab estimator)
    * temporal.AmbientEma / SceneCutDetector
    * hyperhdr_client.HyperHDRFlatClient
    * oklab                          (color math)
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "ambient",
    "oklab",
    "temporal",
    "segmentation",
    "cutdetect",
    "depth",
    "hyperhdr_client",
    "service",
]
