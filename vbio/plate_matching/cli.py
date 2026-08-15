import argparse
import json
import sys
from pathlib import Path

import cv2

from .matcher import match
from .presentation import print_report, visualise


def run_one(image_path, json_path, plate_path, target_text, out_path, force_phase=None, show=False, json_out=None, quiet=False, allow_freeform=False):
    main_img, ir_crop = cv2.imread(str(image_path)), cv2.imread(str(plate_path))
    if main_img is None or ir_crop is None:
        sys.exit(f"Could not read {'image' if main_img is None else 'plate crop'}: {image_path if main_img is None else plate_path}")
    with open(json_path, encoding="utf-8") as handle:
        result = match(main_img, ir_crop, json.load(handle), target_text, force_phase, allow_freeform)
    if not quiet:
        print_report(result, target_text)
    image = visualise(main_img, ir_crop, result, target_text)
    cv2.imwrite(str(out_path), image)
    if json_out:
        with open(json_out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, default=float)
    if show:
        cv2.imshow("plate -> vehicle", image)
        cv2.waitKey(0)
        cv2.destroyAllWindows()
    return result


def main():
    parser = argparse.ArgumentParser(description="Match an LPR plate crop to its vehicle in a frame.")
    parser.add_argument("-i", "--image"); parser.add_argument("-j", "--json"); parser.add_argument("-p", "--plate")
    parser.add_argument("-t", "--text", default=""); parser.add_argument("-o", "--out"); parser.add_argument("--json-out")
    parser.add_argument("--force-phase", choices=["ocr", "visual", "geometric"]); parser.add_argument("--allow-freeform", action="store_true")
    parser.add_argument("--show", action="store_true"); parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--batch"); parser.add_argument("--out-dir", default="./results")
    args = parser.parse_args()
    if args.batch:
        source, destination = Path(args.batch), Path(args.out_dir)
        destination.mkdir(parents=True, exist_ok=True)
        processed = 0
        for json_path in sorted(source.glob("*.json")):
            stem = json_path.stem
            image_path = next((source / f"{stem}{suffix}" for suffix in (".jpg", ".jpeg", ".png") if (source / f"{stem}{suffix}").exists()), None)
            plate_path = next((source / f"{stem}_plate{suffix}" for suffix in (".jpg", ".jpeg", ".png") if (source / f"{stem}_plate{suffix}").exists()), None)
            if image_path is None or plate_path is None:
                print(f"skip {stem}: missing image or _plate crop")
                continue
            run_one(image_path, json_path, plate_path, args.text, destination / f"{stem}_match.png",
                    args.force_phase, False, None, True, args.allow_freeform)
            processed += 1
        print(f"Processed {processed} frame(s) -> {destination}")
        return
    if not (args.image and args.json and args.plate):
        parser.error("--image, --json and --plate are all required (or use --batch)")
    run_one(args.image, args.json, args.plate, args.text, args.out or f"{Path(args.image).stem}_match.png", args.force_phase, args.show, args.json_out, args.quiet, args.allow_freeform)
