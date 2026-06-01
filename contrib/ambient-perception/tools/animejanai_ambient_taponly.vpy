# animejanai profile: HyperHDR ambient lighting WITHOUT upscaling (passthrough).
#
# Use this for the mpv [upscale-off] profile so the lights keep working when you
# turn upscaling off (Ctrl+0). It streams the source frame to HyperHDR and outputs
# the video unchanged (no upscale). Requires ambient.py in your animejanai\core\.
from ambient import attach_ambient

attach_ambient(video_in, container_fps).set_output()
