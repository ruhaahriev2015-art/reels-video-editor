import os
import re
import json
import uuid
import subprocess

from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

WORK_DIR = "/tmp/reels_editor"
os.makedirs(WORK_DIR, exist_ok=True)


# =========================================================
# HEALTH
# =========================================================

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


# =========================================================
# FFmpeg helpers
# =========================================================

def run_ffmpeg(command):
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )


def get_duration(path):
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ]

    return float(
        subprocess.check_output(command)
        .decode()
        .strip()
    )


def get_video_size(path):
    command = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        path
    ]

    raw = subprocess.check_output(command).decode()
    data = json.loads(raw)

    stream = data["streams"][0]

    return int(stream["width"]), int(stream["height"])


# =========================================================
# CUT helpers
# =========================================================

def merge_cuts(cuts):
    if not cuts:
        return []

    cuts = sorted(cuts)

    merged = [cuts[0]]

    for start, end in cuts[1:]:

        last_start, last_end = merged[-1]

        if start <= last_end:

            merged[-1] = (
                last_start,
                max(last_end, end)
            )

        else:
            merged.append(
                (start, end)
            )

    return merged


def map_time_after_cuts(time_value, cuts):

    time_value = float(time_value)

    removed = 0.0

    for start, end in cuts:

        if time_value >= end:

            removed += (
                end - start
            )

        elif time_value > start:

            return max(
                0.0,
                start - removed
            )

        else:
            break

    return max(
        0.0,
        time_value - removed
    )


# =========================================================
# ASS subtitles
# =========================================================

def ass_time(seconds):

    seconds = max(
        0.0,
        float(seconds)
    )

    hours = int(
        seconds // 3600
    )

    minutes = int(
        (seconds % 3600) // 60
    )

    secs = seconds % 60

    return (
        f"{hours}:"
        f"{minutes:02d}:"
        f"{secs:05.2f}"
    )


def escape_ass_text(text):

    text = str(text)

    # ASS управляющие символы
    text = text.replace(
        "\\",
        r"\\"
    )

    text = text.replace(
        "{",
        r"\{"
    )

    text = text.replace(
        "}",
        r"\}"
    )

    text = text.replace(
        "\n",
        r"\N"
    )

    return text


def add_highlight(text, highlight):

    text = str(text).strip()
    highlight = str(highlight).strip()

    safe_text = escape_ass_text(
        text
    )

    if not highlight:
        return safe_text

    # Ищем highlight внутри исходного текста
    pattern = re.compile(
        re.escape(highlight),
        re.IGNORECASE
    )

    match = pattern.search(
        text
    )

    if not match:
        return safe_text

    before = escape_ass_text(
        text[:match.start()]
    )

    selected = escape_ass_text(
        text[
            match.start():
            match.end()
        ]
    )

    after = escape_ass_text(
        text[match.end():]
    )

    # ASS:
    # жёлтый = BBGGRR = 00FFFF
    # белый = FFFFFF

    return (
        before
        + r"{\c&H0000FFFF&}"
        + selected
        + r"{\c&H00FFFFFF&}"
        + after
    )


def create_ass_file(
    path,
    width,
    height,
    subtitles
):

    # примерно как fontsize=h*0.022
    font_size = max(
        18,
        int(height * 0.022)
    )

    # Субтитры располагаем примерно
    # в районе 72% высоты кадра
    bottom_margin = int(
        height * 0.24
    )

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,DejaVu Sans,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,3,1,0,2,35,35,{bottom_margin},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""

    lines = [
        header
    ]

    for item in subtitles:

        start = ass_time(
            item["start"]
        )

        end = ass_time(
            item["end"]
        )

        formatted_text = add_highlight(
            item["text"],
            item.get(
                "highlight",
                ""
            )
        )

        line = (
            f"Dialogue: 0,"
            f"{start},"
            f"{end},"
            f"Default,,0,0,0,,"
            f"{formatted_text}\n"
        )

        lines.append(
            line
        )

    with open(
        path,
        "w",
        encoding="utf-8"
    ) as file:

        file.writelines(
            lines
        )


# =========================================================
# EDIT
# =========================================================

@app.route("/edit", methods=["POST"])
def edit_video():

    if "video" not in request.files:

        return jsonify({
            "error":
            "Video file is required"
        }), 400


    video = request.files[
        "video"
    ]


    actions_raw = request.form.get(
        "actions",
        "[]"
    )


    try:

        actions = json.loads(
            actions_raw
        )

        if not isinstance(
            actions,
            list
        ):
            raise ValueError(
                "actions must be list"
            )

    except Exception as e:

        return jsonify({
            "error":
            "Invalid actions JSON",

            "details":
            str(e)
        }), 400


    job_id = str(
        uuid.uuid4()
    )


    input_path = os.path.join(
        WORK_DIR,
        f"{job_id}_input.mp4"
    )

    cut_path = os.path.join(
        WORK_DIR,
        f"{job_id}_cut.mp4"
    )

    zoom_path = os.path.join(
        WORK_DIR,
        f"{job_id}_zoom.mp4"
    )

    output_path = os.path.join(
        WORK_DIR,
        f"{job_id}_output.mp4"
    )

    ass_path = os.path.join(
        WORK_DIR,
        f"{job_id}_subtitles.ass"
    )


    video.save(
        input_path
    )


    cuts = []
    text_actions = []
    zoom_actions = []


    try:

        # =================================================
        # READ ACTIONS
        # =================================================

        for action in actions:

            action_type = str(
                action.get(
                    "action",
                    ""
                )
            ).upper().strip()


            # ----------------------
            # CUT
            # ----------------------

            if action_type == "CUT":

                start = float(
                    action.get(
                        "start",
                        0
                    )
                )

                end = float(
                    action.get(
                        "end",
                        0
                    )
                )

                if end > start:

                    cuts.append(
                        (
                            start,
                            end
                        )
                    )


            # ----------------------
            # TEXT / SUBTITLE
            # ----------------------

            elif action_type in (
                "TEXT",
                "SUBTITLE"
            ):

                start = float(
                    action.get(
                        "start",
                        0
                    )
                )

                end = float(
                    action.get(
                        "end",
                        start + 2
                    )
                )

                text = str(
                    action.get(
                        "text",
                        ""
                    )
                ).strip()

                highlight = str(
                    action.get(
                        "highlight",
                        ""
                    )
                ).strip()


                if (
                    text
                    and
                    end > start
                ):

                    text_actions.append({
                        "start":
                            start,

                        "end":
                            end,

                        "text":
                            text,

                        "highlight":
                            highlight
                    })


            # ----------------------
            # ZOOM
            # ----------------------

            elif action_type == "ZOOM":

                start = float(
                    action.get(
                        "start",
                        0
                    )
                )

                end = float(
                    action.get(
                        "end",
                        start + 1
                    )
                )

                scale = float(
                    action.get(
                        "scale",
                        1.12
                    )
                )


                scale = max(
                    1.01,
                    min(
                        scale,
                        1.35
                    )
                )


                if end > start:

                    zoom_actions.append({
                        "start":
                            start,

                        "end":
                            end,

                        "scale":
                            scale
                    })


        cuts = merge_cuts(
            cuts
        )


        # =================================================
        # STAGE 1 — CUT
        # =================================================

        if not cuts:

            command = [
                "ffmpeg",
                "-y",

                "-i",
                input_path,

                "-c:v",
                "libx264",

                "-preset",
                "ultrafast",

                "-threads",
                "1",

                "-c:a",
                "aac",

                "-b:a",
                "128k",

                "-movflags",
                "+faststart",

                cut_path
            ]


            run_ffmpeg(
                command
            )


        else:

            duration = get_duration(
                input_path
            )


            keep_segments = []

            current = 0.0


            for start, end in cuts:

                start = max(
                    0.0,
                    min(
                        start,
                        duration
                    )
                )

                end = max(
                    0.0,
                    min(
                        end,
                        duration
                    )
                )


                if start > current:

                    keep_segments.append(
                        (
                            current,
                            start
                        )
                    )


                current = max(
                    current,
                    end
                )


            if current < duration:

                keep_segments.append(
                    (
                        current,
                        duration
                    )
                )


            if not keep_segments:

                return jsonify({
                    "error":
                    "CUT actions remove the entire video"
                }), 400


            filters = []

            concat_inputs = []


            for index, (
                start,
                end
            ) in enumerate(
                keep_segments
            ):


                filters.append(
                    f"[0:v]"
                    f"trim=start={start}:end={end},"
                    f"setpts=PTS-STARTPTS"
                    f"[v{index}]"
                )


                filters.append(
                    f"[0:a]"
                    f"atrim=start={start}:end={end},"
                    f"asetpts=PTS-STARTPTS"
                    f"[a{index}]"
                )


                concat_inputs.append(
                    f"[v{index}]"
                    f"[a{index}]"
                )


            filters.append(
                "".join(
                    concat_inputs
                )
                +
                f"concat=n={len(keep_segments)}:"
                f"v=1:a=1"
                f"[outv][outa]"
            )


            command = [
                "ffmpeg",
                "-y",

                "-i",
                input_path,

                "-filter_complex",
                ";".join(
                    filters
                ),

                "-map",
                "[outv]",

                "-map",
                "[outa]",

                "-c:v",
                "libx264",

                "-preset",
                "ultrafast",

                "-threads",
                "1",

                "-c:a",
                "aac",

                "-b:a",
                "128k",

                "-movflags",
                "+faststart",

                cut_path
            ]


            run_ffmpeg(
                command
            )


        # =================================================
        # STAGE 2 — ZOOM
        # =================================================

        mapped_zooms = []


        cut_duration = get_duration(
            cut_path
        )


        for item in zoom_actions:

            start = map_time_after_cuts(
                item["start"],
                cuts
            )

            end = map_time_after_cuts(
                item["end"],
                cuts
            )


            start = max(
                0.0,
                min(
                    start,
                    cut_duration
                )
            )

            end = max(
                0.0,
                min(
                    end,
                    cut_duration
                )
            )


            if end > start:

                mapped_zooms.append({
                    "start":
                        start,

                    "end":
                        end,

                    "scale":
                        item["scale"]
                })


        if mapped_zooms:

            zoom_filters = []


            for item in mapped_zooms:

                start = item[
                    "start"
                ]

                end = item[
                    "end"
                ]

                scale = item[
                    "scale"
                ]


                zoom_filters.append(

                    "scale="

                    f"w='if(between(t,{start},{end}),"
                    f"trunc(iw*{scale}/2)*2,iw)':"

                    f"h='if(between(t,{start},{end}),"
                    f"trunc(ih*{scale}/2)*2,ih)':"

                    "eval=frame"
                )


                zoom_filters.append(

                    "crop="

                    "w='iw/"
                    f"if(between(t,{start},{end}),"
                    f"{scale},1)':"

                    "h='ih/"
                    f"if(between(t,{start},{end}),"
                    f"{scale},1)':"

                    "x='(iw-ow)/2':"

                    "y='(ih-oh)/2'"
                )


            zoom_filters.append(
                "setsar=1"
            )


            zoom_command = [
                "ffmpeg",
                "-y",

                "-i",
                cut_path,

                "-vf",
                ",".join(
                    zoom_filters
                ),

                "-c:v",
                "libx264",

                "-preset",
                "ultrafast",

                "-threads",
                "1",

                "-c:a",
                "copy",

                "-movflags",
                "+faststart",

                zoom_path
            ]


            run_ffmpeg(
                zoom_command
            )


        else:

            os.replace(
                cut_path,
                zoom_path
            )


        # =================================================
        # STAGE 3 — SUBTITLES
        # =================================================

        mapped_texts = []


        final_duration = get_duration(
            zoom_path
        )


        for item in text_actions:

            start = map_time_after_cuts(
                item["start"],
                cuts
            )

            end = map_time_after_cuts(
                item["end"],
                cuts
            )


            start = max(
                0.0,
                min(
                    start,
                    final_duration
                )
            )

            end = max(
                0.0,
                min(
                    end,
                    final_duration
                )
            )


            if end <= start:
                continue


            mapped_texts.append({
                "start":
                    start,

                "end":
                    end,

                "text":
                    item["text"],

                "highlight":
                    item.get(
                        "highlight",
                        ""
                    )
            })


        if mapped_texts:

            width, height = get_video_size(
                zoom_path
            )


            create_ass_file(
                ass_path,
                width,
                height,
                mapped_texts
            )


            subtitle_filter = (
                f"subtitles={ass_path}"
            )


            text_command = [
                "ffmpeg",
                "-y",

                "-i",
                zoom_path,

                "-vf",
                subtitle_filter,

                "-c:v",
                "libx264",

                "-preset",
                "ultrafast",

                "-threads",
                "1",

                "-c:a",
                "copy",

                "-movflags",
                "+faststart",

                output_path
            ]


            run_ffmpeg(
                text_command
            )


        else:

            os.replace(
                zoom_path,
                output_path
            )


        # =================================================
        # RETURN
        # =================================================

        return send_file(
            output_path,

            mimetype=
                "video/mp4",

            as_attachment=
                True,

            download_name=
                "edited_reel.mp4"
        )


    except subprocess.CalledProcessError as e:

        error_text = (

            e.stderr.decode(
                errors="ignore"
            )

            if e.stderr

            else str(e)
        )


        print(
            "FFMPEG ERROR:",
            error_text
        )


        return jsonify({

            "error":
                "FFmpeg processing failed",

            "details":
                error_text[-6000:]

        }), 500


    except Exception as e:

        print(
            "SERVER ERROR:",
            str(e)
        )


        return jsonify({
            "error":
                str(e)
        }), 500


    finally:

        cleanup_paths = [
            input_path,
            cut_path,
            zoom_path,
            ass_path
        ]


        for path in cleanup_paths:

            if os.path.exists(
                path
            ):

                try:

                    os.remove(
                        path
                    )

                except Exception:

                    pass


if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )


    app.run(
        host="0.0.0.0",
        port=port
    )
