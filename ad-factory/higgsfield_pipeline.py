import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
import requests

BASE_URL = "https://openrouter.ai/api/v1"


def get_api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "OPENROUTER_API_KEY":
                return v.strip().strip('"').strip("'")
    raise RuntimeError("OPENROUTER_API_KEY not found in environment or .env")


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def render_prompt(template_str: str, vars_dict: dict, shot_id: int) -> str:
    result = template_str
    for k, v in vars_dict.items():
        result = result.replace(f"{{{k}}}", str(v))
    unfilled = re.findall(r"\{[a-zA-Z0-9_]+\}", result)
    if unfilled:
        raise ValueError(f"Shot {shot_id} has unfilled template variables: {unfilled}")
    return result


def generate_anchor_image(
    api_key: str,
    model: str,
    prompt: str,
    aspect_ratio: str,
    out_path: Path,
) -> tuple[Path, float]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "output_format": "png",
    }
    resp = requests.post(f"{BASE_URL}/images", headers=headers, json=payload, timeout=120)
    if not resp.ok:
        raise RuntimeError(f"Image generation failed ({resp.status_code}): {resp.text}")

    data = resp.json()
    items = data.get("data") or []
    if not items:
        raise RuntimeError(f"No image returned in response: {data}")

    first = items[0]
    if first.get("b64_json"):
        img_bytes = base64.b64decode(first["b64_json"])
    elif first.get("url"):
        img_bytes = requests.get(first["url"], timeout=120).content
    else:
        raise RuntimeError(f"Image item missing b64_json and url: {first}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(img_bytes)
    cost = float((data.get("usage") or {}).get("cost") or 0.0)
    return out_path, cost


def to_data_url(image_path: Path) -> str:
    raw = image_path.read_bytes()
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def submit_video_job(
    api_key: str,
    model: str,
    prompt: str,
    duration: int,
    resolution: str,
    aspect_ratio: str,
    first_frame_data_url: str | None,
    generate_audio: bool = True,
) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "duration": duration,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "generate_audio": generate_audio,
    }
    if first_frame_data_url:
        payload["frame_images"] = [
            {
                "type": "image_url",
                "image_url": {"url": first_frame_data_url},
                "frame_type": "first_frame",
            }
        ]

    resp = requests.post(f"{BASE_URL}/videos", headers=headers, json=payload, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"Video submit failed ({resp.status_code}): {resp.text}")
    return resp.json()


def poll_until_done(
    api_key: str,
    job_id: str,
    polling_url: str | None = None,
    interval_sec: int = 15,
    timeout_sec: int = 600,
) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"}
    url = polling_url or f"{BASE_URL}/videos/{job_id}"
    if url.startswith("/"):
        url = f"https://openrouter.ai{url}"

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        print(f"[{time.strftime('%X')}] Job {job_id} status: {status}")
        if status == "completed":
            return data
        if status in {"failed", "cancelled", "expired"}:
            raise RuntimeError(f"Video job ended with '{status}': {data}")
        time.sleep(interval_sec)

    raise TimeoutError(f"Timed out waiting for job {job_id}")


def download_video(api_key: str, job_data: dict, out_path: Path) -> Path:
    headers = {"Authorization": f"Bearer {api_key}"}
    urls = job_data.get("urls") or []
    unsigned_urls = job_data.get("unsigned_urls") or []

    if urls:
        resp = requests.get(urls[0], timeout=120)
    elif unsigned_urls:
        raw_url = unsigned_urls[0]
        dl_url = f"https://openrouter.ai{raw_url}" if raw_url.startswith("/") else raw_url
        resp = requests.get(dl_url, headers=headers, timeout=120)
    else:
        dl_url = f"{BASE_URL}/videos/{job_data['id']}/content?index=0"
        resp = requests.get(dl_url, headers=headers, timeout=120)

    resp.raise_for_status()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(resp.content)
    return out_path


def stitch_clips(clip_paths: list[Path], final_out: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or len(clip_paths) < 2:
        return False

    cmd = [ffmpeg, "-y"]
    for p in clip_paths:
        cmd.extend(["-i", str(p)])

    inputs_tag = "".join(f"[{i}:v][{i}:a]" for i in range(len(clip_paths)))
    filter_expr = f"{inputs_tag}concat=n={len(clip_paths)}:v=1:a=1[v][a]"
    cmd.extend([
        "-filter_complex",
        filter_expr,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(final_out),
    ])
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"ffmpeg error:\n{res.stderr}", file=sys.stderr)
    return res.returncode == 0 and final_out.exists()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Universal Higgsfield-style ad creative pipeline")
    parser.add_argument("--template", type=Path, required=True, help="Path to creative template JSON")
    parser.add_argument("--brief", type=Path, required=True, help="Path to product campaign brief JSON")
    parser.add_argument("--anchor-only", action="store_true", help="Only generate & save the anchor image")
    parser.add_argument("--shots", default="", help="Optional comma-separated shot IDs, e.g. '1' or '1,2'")
    parser.add_argument("--no-stitch", action="store_true", help="Skip stitching clips into final ad")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = get_api_key()

    tmpl = load_json(args.template)
    brief = load_json(args.brief)

    product = brief.get("product") or "default_product"
    campaign = brief.get("campaign") or args.brief.stem
    image_model = brief.get("image_model") or tmpl.get("image_model") or "bytedance-seed/seedream-5-5"
    video_model = brief.get("video_model") or tmpl.get("video_model") or "bytedance/seedance-2.0"
    aspect_ratio = brief.get("aspect_ratio") or tmpl.get("aspect_ratio") or "9:16"
    resolution = brief.get("resolution") or tmpl.get("resolution") or "720p"

    vars_dict = brief.get("vars") or {}
    shots = tmpl.get("shots") or []
    if not shots:
        raise ValueError(f"Template {args.template} defines no shots.")

    # Validate all shot prompts up front before spending credits
    rendered_shots: dict[int, tuple[dict, str]] = {}
    for s in shots:
        sid = int(s["id"])
        prompt_text = render_prompt(s["prompt_template"], vars_dict, sid)
        rendered_shots[sid] = (s, prompt_text)

    anchor_path = Path(brief.get("anchor_path") or f"anchors/products/{product}/{campaign}_anchor.png")
    anchor_prompt = brief.get("anchor_prompt", "").strip()
    total_cost = 0.0

    if not anchor_path.exists():
        if not anchor_prompt:
            raise ValueError(f"Anchor {anchor_path} missing and brief provides no anchor_prompt.")
        print(f"Generating anchor via {image_model} -> {anchor_path}...")
        anchor_path, img_cost = generate_anchor_image(
            api_key=api_key,
            model=image_model,
            prompt=anchor_prompt,
            aspect_ratio=aspect_ratio,
            out_path=anchor_path,
        )
        total_cost += img_cost
        print(f"Saved anchor -> {anchor_path.resolve()} (cost: ${img_cost:.4f})")
    else:
        print(f"Reusing existing anchor -> {anchor_path.resolve()}")

    if args.anchor_only:
        print(f"Anchor-only run complete. Total cost: ${total_cost:.4f}")
        return

    anchor_data_url = to_data_url(anchor_path)
    if args.shots.strip():
        selected_ids = [int(x.strip()) for x in args.shots.split(",") if x.strip()]
    else:
        selected_ids = [int(s["id"]) for s in shots]

    out_dir = Path("out") / product
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_clips: list[Path] = []

    for sid in selected_ids:
        if sid not in rendered_shots:
            raise ValueError(f"Shot ID {sid} not found in template {args.template}")
        shot_cfg, prompt_text = rendered_shots[sid]
        duration = int(shot_cfg.get("duration", 8))
        use_anchor = bool(shot_cfg.get("use_anchor", False))
        gen_audio = bool(shot_cfg.get("generate_audio", True))
        shot_name = shot_cfg.get("role") or f"shot{sid}"

        print(f"\n=== Shot {sid} ({shot_name}, {duration}s, anchor={use_anchor}) on {video_model} ===")
        job = submit_video_job(
            api_key=api_key,
            model=video_model,
            prompt=prompt_text,
            duration=duration,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            first_frame_data_url=anchor_data_url if use_anchor else None,
            generate_audio=gen_audio,
        )
        job_id = job["id"]
        print(f"Submitted -> job_id={job_id}")
        done = poll_until_done(api_key, job_id, polling_url=job.get("polling_url"))
        shot_cost = float((done.get("usage") or {}).get("cost") or 0.0)
        total_cost += shot_cost
        print(f"Shot {sid} cost: ${shot_cost:.4f}")

        clip_path = out_dir / f"{campaign}_shot{sid}_{shot_name}.mp4"
        download_video(api_key, done, clip_path)
        generated_clips.append(clip_path)
        print(f"Saved -> {clip_path.resolve()}")

    if not args.no_stitch and len(generated_clips) >= 2:
        final_out = out_dir / f"{campaign}_final.mp4"
        if stitch_clips(generated_clips, final_out):
            print(f"\nStitched final ad -> {final_out.resolve()}")

    print(f"\nTotal reported pipeline cost: ${total_cost:.4f}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
