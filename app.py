import os
import json
import uuid
import subprocess

from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

WORK_DIR = "/tmp/reels_editor"
os.makedirs(WORK_DIR, exist_ok=True)

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "service": "reels-video-editor"
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok"
    })


def map_time_after_cuts(time_value, cuts):
    time_value = float(time_value)
    removed = 0.0

    for start, end in cuts:
        if time_value >= end:
            removed += end - start
        elif time_value > start:
            return max(0.0, start - removed)
        else:
            break

    return max(0.0, time_value - removed)


@app.route("/edit", methods=["POST"])
def edit_video():

    if "video" not in request.files:
        return jsonify({
            "error": "Video file is required"
        }), 400

    video = request.files["video"]
    actions_raw = request.form.get("actions", "[]")

    try:
        actions = json.loads(actions_raw)
    except Exception:
        return jsonify({
            "error": "Invalid actions JSON"
        }), 400

    job_id = str(uuid.uuid4())

    input_path = os.path.join(
        WORK_DIR,
        f"{job_id}_input.mp4"
    )

    cut_path = os.path.join(
        WORK_DIR,
        f"{job_id}_cut.mp4"
    )

    output_path = os.path.join(
        WORK_DIR,
        f"{job_id}_output.mp4"
    )

    video.save(input_path)

    cuts = []
    text_actions = []
    zoom_actions = []
    text_files = []

    for action in actions:

        action_type = str(
            action.get("action", "")
        ).upper()

        if action_type == "CUT":

            start = float(action.get("start", 0))
            end = float(action.get("end", 0))

            if end > start:
                cuts.append((start, end))

        elif action_type == "TEXT":

            start = float(action.get("start", 0))
            end = float(action.get("end", start + 2))
            text = str(action.get("text", "")).strip()

            if text and end > start:
                text_actions.append({
                    "start": start,
                    "end": end,
                    "text": text
                })

        elif action_type == "ZOOM":

            start = float(action.get("start", 0))
            end = float(action.get("end", start + 2))

            scale = float(
                action.get("scale", 1.10)
            )

            scale = max(
                1.01,
                min(scale, 1.25)
            )

            if end > start:
                zoom_actions.append({
                    "start": start,
                    "end": end,
                    "scale": scale
                })

    cuts.sort()

    try:

        # --------------------------------
        # ЭТАП 1 — CUT
        # --------------------------------

        if not cuts:

            command = [
                "ffmpeg",
                "-y",
                "-i", input_path,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-threads", "1",
                "-x264-params",
                "rc-lookahead=0:sync-lookahead=0",
                "-c:a", "aac",
                "-movflags", "+faststart",
                cut_path
            ]

        else:

            probe = [
                "ffprobe",
                "-v", "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                input_path
            ]

            duration = float(
                subprocess.check_output(probe)
                .decode()
                .strip()
            )

            keep_segments = []
            current = 0.0

            for start, end in cuts:

                if start > current:
                    keep_segments.append(
                        (current, start)
                    )

                current = max(current, end)

            if current < duration:
                keep_segments.append(
                    (current, duration)
                )

            if not keep_segments:
                return jsonify({
                    "error":
                    "CUT actions remove the entire video"
                }), 400

            filters = []
            concat_inputs = []

            for index, (start, end) in enumerate(
                keep_segments
            ):

                filters.append(
                    f"[0:v]trim=start={start}:end={end},"
                    f"setpts=PTS-STARTPTS[v{index}]"
                )

                filters.append(
                    f"[0:a]atrim=start={start}:end={end},"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                )

                concat_inputs.append(
                    f"[v{index}][a{index}]"
                )

            filters.append(
                "".join(concat_inputs)
                + f"concat=n={len(keep_segments)}:"
                  "v=1:a=1[outv][outa]"
            )

            command = [
                "ffmpeg",
                "-y",
                "-i", input_path,
                "-filter_complex",
                ";".join(filters),
                "-map", "[outv]",
                "-map", "[outa]",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-threads", "1",
                "-x264-params",
                "rc-lookahead=0:sync-lookahead=0",
                "-c:a", "aac",
                "-movflags", "+faststart",
                cut_path
            ]

        subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # --------------------------------
        # ЭТАП 2 — TEXT + ZOOM
        # --------------------------------

        video_filters = []

        # ---------- TEXT ----------

        for index, item in enumerate(text_actions):

            text_file = os.path.join(
                WORK_DIR,
                f"{job_id}_text_{index}.txt"
            )

            with open(
                text_file,
                "w",
                encoding="utf-8"
            ) as f:
                f.write(item["text"])

            text_files.append(text_file)

            start = map_time_after_cuts(
                item["start"],
                cuts
            )

            end = map_time_after_cuts(
                item["end"],
                cuts
            )

            if end <= start:
                continue

            video_filters.append(
                "drawtext="
                f"fontfile={FONT_PATH}:"
                f"textfile={text_file}:"
                "fontcolor=white:"
                "fontsize=h*0.035:"
                "box=1:"
                "boxcolor=black@0.60:"
                "boxborderw=12:"
                "x=(w-text_w)/2:"
                "y=h*0.08:"
                f"enable='between(t,{start},{end})'"
            )

        # ---------- ZOOM ----------

        for item in zoom_actions:

            start = map_time_after_cuts(
                item["start"],
                cuts
            )

            end = map_time_after_cuts(
                item["end"],
                cuts
            )

            scale = item["scale"]

            if end <= start:
                continue

            zoom_width = f"iw/{scale}"
            zoom_height = f"ih/{scale}"

            video_filters.append(
                "crop="
                f"{zoom_width}:"
                f"{zoom_height}:"
                f"(iw-{zoom_width})/2:"
                f"(ih-{zoom_height})/2:"
                f"enable='between(t,{start},{end})'"
            )

            video_filters.append(
                "scale=iw:ih"
            )

        if video_filters:

            final_command = [
                "ffmpeg",
                "-y",
                "-i", cut_path,
                "-vf",
                ",".join(video_filters),
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-threads", "1",
                "-x264-params",
                "rc-lookahead=0:sync-lookahead=0",
                "-c:a", "copy",
                "-movflags", "+faststart",
                output_path
            ]

            subprocess.run(
                final_command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

        else:

            os.replace(
                cut_path,
                output_path
            )

        return send_file(
            output_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name="edited_reel.mp4"
        )

    except subprocess.CalledProcessError as e:

        error_text = (
            e.stderr.decode(errors="ignore")
            if e.stderr
            else str(e)
        )

        return jsonify({
            "error": "FFmpeg processing failed",
            "details": error_text[-5000:]
        }), 500

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500

    finally:

        for path in [
            input_path,
            cut_path,
            *text_files
        ]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", 10000)
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
