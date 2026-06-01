# animejanai profile: 2x upscale + HyperHDR ambient lighting.
#
# Requires ambient.py dropped into your animejanai\core\ folder (next to
# animejanai_core.py). No hardcoded paths, no logging. Change the 1002 below to
# the upscale id your normal profile uses.
from ambient import attach_ambient
import animejanai_core

animejanai_core.run_animejanai_with_keybinding(
    attach_ambient(video_in, container_fps), container_fps, 1002)
