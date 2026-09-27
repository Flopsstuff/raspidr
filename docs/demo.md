# Demo

The speaker in action (2026-09-27): the Żabka Triki BLE token works as a wireless knob, and the NeoPixel ring shows
each stage of the voice loop.

<video controls playsinline preload="metadata" poster="./media/demo-poster.jpg" style="width: 100%; max-width: 420px; display: block; margin: 0 auto;">
  <source src="./media/demo.mp4" type="video/mp4">
</video>

## The device

<p align="center">
  <img src="./images/photo-front.jpg" alt="RaspiDR from the front: the stereo speaker pair" width="320">
  <img src="./images/photo-back.jpg" alt="RaspiDR from the back: rotary encoder, LED ring, WM8960 HAT over the Pi Zero 2 W and the UPS-Lite battery" width="320">
</p>

From the back, top to bottom: the rotary encoder and the 7-LED NeoPixel ring on the speaker enclosure, the WM8960 audio
HAT, the Pi Zero 2 W and the UPS-Lite battery board. See [Hardware](hardware.md) for pins and quirks, and
[Architecture](architecture.md#voice-loop) for what each ring animation means.
