#!/usr/bin/env python3
#
# Calculate bitrate stats from video
#
# Output is in kilobit per second unless specified otherwise.
#
# Author: Werner Robitza
# License: MIT

import argparse
import subprocess
import math
import json
import sys
import pandas as pd
import numpy as np

from .__init__ import __version__ as version


def print_stderr(msg):
    print(msg, file=sys.stderr)


def run_command(cmd, dry_run=False, verbose=False):
    """
    Run a command directly
    """
    if dry_run or verbose:
        print_stderr("[cmd] " + " ".join(cmd))
        if dry_run:
            return None, None

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()

    if process.returncode == 0:
        return stdout.decode("utf-8"), stderr.decode("utf-8")
    else:
        print_stderr("[error] running command: {}".format(" ".join(cmd)))
        print_stderr(stderr.decode("utf-8"))
        sys.exit(1)


class BitrateStats:
    def __init__(
        self,
        input_file,
        stream_type="video",
        aggregation="time",
        chunk_size=1,
        dry_run=False,
        verbose=False,
    ):
        self.input_file = input_file

        if stream_type not in ["audio", "video"]:
            print_stderr("Stream type must be audio/video")
            sys.exit(1)
        self.stream_type = stream_type

        if aggregation not in ["time", "gop"]:
            print_stderr("Wrong aggregation type")
            sys.exit(1)
        if aggregation == "gop" and stream_type == "audio":
            print_stderr("GOP aggregation for audio does not make sense")
            sys.exit(1)
        self.aggregation = aggregation

        if chunk_size and chunk_size < 0:
            print_stderr("Chunk size must be greater than 0")
            sys.exit(1)
        self.chunk_size = chunk_size

        self.dry_run = dry_run
        self.verbose = verbose

        self.duration = 0
        self.fps = 0
        self.max_bitrate = 0
        self.min_bitrate = 0
        self.moving_avg_bitrate = []
        self.frames = []
        self.bitrate_stats = {}

        self.rounding_factor = 3

        self._chunks = []

    def calculate_statistics(self):
        self._calculate_frame_sizes()
        self._calculate_duration()
        self._calculate_fps()
        self._calculate_max_min_bitrate()
        self._assemble_bitrate_statistics()

    def _calculate_frame_sizes(self):
        """
        Get the frame sizes via ffprobe using the -show_packets option.
        This includes the NAL headers, of course.
        """
        if self.verbose:
            print_stderr(f"Calculating frame size from {self.input_file}")

        cmd = [
            "ffprobe",
            "-loglevel",
            "error",
            "-select_streams",
            self.stream_type[0] + ":0",
            "-show_packets",
            "-show_entries",
            "packet=pts_time,dts_time,duration_time,size,flags",
            "-of",
            "json",
            self.input_file,
        ]

        stdout, _ = run_command(cmd, self.dry_run)
        if self.dry_run:
            print_stderr("Aborting prematurely, dry-run specified")
            sys.exit(0)

        info = json.loads(stdout)["packets"]

        ret = []
        idx = 1

        default_duration = next(
            (x["duration_time"] for x in info if "duration_time" in x.keys()), "NaN"
        )

        for packet_info in info:
            frame_type = "I" if packet_info["flags"] == "K_" else "Non-I"

            if "pts_time" in packet_info.keys():
                pts = float(packet_info["pts_time"])
            else:
                pts = "NaN"

            if "duration_time" in packet_info.keys():
                duration = float(packet_info["duration_time"])
            else:
                duration = default_duration

            ret.append(
                {
                    "n": idx,
                    "frame_type": frame_type,
                    "pts": pts,
                    "size": int(packet_info["size"]),
                    "duration": duration,
                }
            )
            idx += 1

        # fix for missing durations, estimate it via PTS
        if default_duration == "NaN":
            ret = self._fix_durations(ret)

        self.frames = ret
        return ret

    def _fix_durations(self, ret):
        """
        Calculate durations based on delta PTS
        """
        last_duration = None
        for i in range(len(ret) - 1):
            curr_pts = ret[i]["pts"]
            next_pts = ret[i+1]["pts"]
            if next_pts < curr_pts:
                print_stderr("Non-monotonically increasing PTS, duration/bitrate may be invalid")
            last_duration = next_pts - curr_pts
            ret[i]["duration"] = last_duration
        ret[-1]["duration"] = last_duration
        return ret

    def _calculate_duration(self):
        """
        Sum of all duration entries
        """
        self.duration = round(sum(f["duration"] for f in self.frames), 2)
        return self.duration

    def _calculate_fps(self):
        """
        FPS = number of frames divided by duration. A rough estimate.
        """
        self.fps = len(self.frames) / self.duration
        return self.fps

    def _collect_chunks(self):
        """
        Collect chunks of a certain aggregation length (in seconds, or GOP).
        This is cached.
        """
        if len(self._chunks):
            return self._chunks

        if self.verbose:
            print_stderr("Collecting chunks for bitrate calculation")

        # this is where we will store the stats in buckets
        aggregation_chunks = []
        curr_list = []

        if self.aggregation == "gop":
            # collect group of pictures, each one containing all frames belonging to it
            for frame in self.frames:
                if frame["frame_type"] != "I":
                    curr_list.append(frame)
                if frame["frame_type"] == "I":
                    if curr_list:
                        aggregation_chunks.append(curr_list)
                    curr_list = [frame]
            # flush the last one
            aggregation_chunks.append(curr_list)

        else:
            # per-time aggregation
            agg_time = 0
            for frame in self.frames:
                if agg_time < self.chunk_size:
                    curr_list.append(frame)
                    agg_time += float(frame["duration"])
                else:
                    if curr_list:
                        aggregation_chunks.append(curr_list)
                    curr_list = [frame]
                    agg_time = float(frame["duration"])
            aggregation_chunks.append(curr_list)

        # calculate BR per group
        self._chunks = [
            BitrateStats._bitrate_for_frame_list(x) for x in aggregation_chunks if len(x) > 1
        ]

        return self._chunks

    @staticmethod
    def _bitrate_for_frame_list(frame_list):
        """
        Given a list of frames with size and PTS, get the bitrate,
        which is done by dividing size through Δ time.
        """
        if len(frame_list) > 1:
            size = sum(f["size"] for f in frame_list)
            times = [f["pts"] for f in frame_list]
            sum_delta_time = sum(float(curr) - float(prev) for curr, prev in zip(times[1:], times))
            bitrate = ((size * 8) / 1000) / sum_delta_time

            return bitrate

    def _calculate_max_min_bitrate(self):
        """
        Find the min/max from the chunks
        """
        self.max_bitrate = max(self._collect_chunks())
        self.min_bitrate = min(self._collect_chunks())
        return self.max_bitrate, self.min_bitrate

    def _assemble_bitrate_statistics(self):
        """
        Assemble all pre-calculated statistics plus some "easy" ones.
        """

        self.avg_bitrate = (
            sum(f["size"] for f in self.frames) * 8 / 1000
        ) / self.duration
        self.avg_bitrate_over_chunks = np.mean(self._collect_chunks())

        self.max_bitrate_factor = self.max_bitrate / self.avg_bitrate

        # output data
        ret = {
            "input_file": self.input_file,
            "stream_type": self.stream_type,
            "avg_fps": round(self.fps, self.rounding_factor),
            "num_frames": len(self.frames),
            "avg_bitrate": round(self.avg_bitrate, self.rounding_factor),
            "avg_bitrate_over_chunks": round(
                self.avg_bitrate_over_chunks, self.rounding_factor
            ),
            "max_bitrate": round(self.max_bitrate, self.rounding_factor),
            "min_bitrate": round(self.min_bitrate, self.rounding_factor),
            "max_bitrate_factor": round(self.max_bitrate_factor, self.rounding_factor),
            "bitrate_per_chunk": [
                round(b, self.rounding_factor) for b in self._collect_chunks()
            ],
            "aggregation": self.aggregation,
            "chunk_size": self.chunk_size,
            "duration": round(self.duration, self.rounding_factor),
        }

        self.bitrate_stats = ret
        return self.bitrate_stats

    def print_statistics(self, output_format):
        if output_format == "csv":
            self._print_csv()
        elif output_format == "json":
            self._print_json()

    def _print_csv(self):
        df = pd.DataFrame(self.bitrate_stats)
        df.reset_index(level=0, inplace=True)
        df.rename(index=str, columns={"index": "chunk_index"}, inplace=True)
        cols = df.columns.tolist()
        cols.insert(0, cols.pop(cols.index("input_file")))
        df = df.reindex(columns=cols)
        print(df.to_csv(index=False))

    def _print_json(self):
        print(json.dumps(self.bitrate_stats, indent=4))


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="ffmpeg_bitrate_stats v" + version,
    )
    parser.add_argument("input", help="input file")

    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Do not run command, just show what would be done",
    )

    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show verbose output"
    )

    parser.add_argument(
        "-s",
        "--stream-type",
        default="video",
        choices=["video", "audio"],
        help="Stream type to analyze",
    )

    parser.add_argument(
        "-a",
        "--aggregation",
        default="time",
        choices=["time", "gop"],
        help="Window for aggregating statistics, either time-based (per-second) or per GOP",
    )

    parser.add_argument(
        "-c",
        "--chunk-size",
        type=float,
        default=1.0,
        help="Custom aggregation window size in seconds",
    )

    parser.add_argument(
        "-of",
        "--output-format",
        type=str,
        default="json",
        choices=["json", "csv"],
        help="output in which format",
    )

    cli_args = parser.parse_args()

    br = BitrateStats(
        cli_args.input,
        cli_args.stream_type,
        cli_args.aggregation,
        cli_args.chunk_size,
        cli_args.dry_run,
        cli_args.verbose,
    )
    br.calculate_statistics()
    br.print_statistics(cli_args.output_format)


if __name__ == "__main__":
    main()
