import argparse
import logging

from gatekeeper.runner import GatekeeperRunner


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Gatekeeper edge runtime")
    parser.add_argument("--source", type=str, default=None, help="RTSP CCTV stream URL or local source override")
    parser.add_argument("--mock-feed", action="store_true", help="Use local test feed (tests/sample_gate.mp4)")
    parser.add_argument("--test-video", type=str, default=None, help="Optional local MP4 path for mock mode")
    parser.add_argument("--show-window", action="store_true", help="Enable interactive live feed window and polygon calibration")
    args = parser.parse_args()

    runner = GatekeeperRunner()
    runner.start(source=args.source, mock_feed=args.mock_feed, test_video=args.test_video, show_window=args.show_window)


if __name__ == "__main__":
    main()

