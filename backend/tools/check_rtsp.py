"""Checks whether an RTSP camera is live, using the SAME opening code the
app uses (app/vision/capture.py), so a pass here means the app can open it.

Usage (from the repo root, in the env where the backend runs):
    python backend/tools/check_rtsp.py "rtsp://user:password@192.168.1.101:554/Streaming/Channels/101"
    python backend/tools/check_rtsp.py "<url>" --seconds 20
    python backend/tools/check_rtsp.py "<url>" --transport tcp

Put the URL in double quotes. The password is never printed.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.vision import capture  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Check that an RTSP stream is live.")
    parser.add_argument("url")
    parser.add_argument("--seconds", type=float, default=10.0, help="how long to read frames (default 10)")
    parser.add_argument("--transport", choices=["tcp", "udp"], help="force the RTP transport (default: OpenCV's own)")
    args = parser.parse_args()

    if not capture.is_rtsp_source(args.url):
        print("That is not an rtsp:// or rtsps:// URL.")
        return 2
    if args.transport:
        os.environ["RTSP_TRANSPORT"] = args.transport

    print(f"Camera: {capture.mask_credentials(args.url)}")
    print(f"Transport: {args.transport or 'OpenCV default'} | open timeout {os.environ.get('RTSP_OPEN_TIMEOUT_MS', '8000')} ms | read timeout {os.environ.get('RTSP_READ_TIMEOUT_MS', '5000')} ms")

    started = time.monotonic()
    cap = capture.open_capture(args.url)
    opened_after = time.monotonic() - started
    if not cap.isOpened():
        cap.release()
        print(f"\nNOT LIVE: could not open the stream (gave up after {opened_after:.1f}s).")
        print("Check, in order: the camera IP and port, the username and password, that RTSP is enabled on the camera,")
        print("that no other viewer has used up the camera's connection limit, and try --transport tcp, or the /102 sub-stream.")
        return 1
    print(f"Opened in {opened_after:.1f}s")

    frames = 0
    failed_reads = 0
    first_frame_after = None
    last_frame_at = None
    max_gap = 0.0
    shape = None
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            ok, frame = cap.read()
            now = time.monotonic()
            if not ok:
                failed_reads += 1
                continue
            if first_frame_after is None:
                first_frame_after = now - started
                shape = frame.shape
            elif last_frame_at is not None:
                max_gap = max(max_gap, now - last_frame_at)
            last_frame_at = now
            frames += 1
    finally:
        cap.release()

    if frames == 0:
        print(f"\nNOT LIVE: opened, but no frame arrived in {args.seconds:.0f}s ({failed_reads} failed reads).")
        return 1

    height, width = shape[0], shape[1]
    span = max((last_frame_at - started) - first_frame_after, 0.001)
    fps = (frames - 1) / span if frames > 1 else 0.0
    print(f"First frame after {first_frame_after:.1f}s | resolution {width}x{height}")
    print(f"{frames} frames in {args.seconds:.0f}s (~{fps:.1f} fps) | longest gap {max_gap:.2f}s | failed reads {failed_reads}")
    if max_gap > 2.0 or failed_reads > 0:
        print("\nLIVE, but unstable: frames stalled or reads failed. Try --transport tcp, or the /102 sub-stream, and check the network/Wi-Fi.")
    else:
        print("\nLIVE and steady.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
