"""
mcap_client.py

Python client for calling the MCAP perception-analyzer API directly
(POST {mcap.url} -> vehicle Make/Model/Color analysis of one or more images).

Mirrors the EXACT request the Node proxy (server/middlerware/mcap.js, class Mcap.fetchData)
builds against the real backend:
  - multipart/form-data
  - field "options"  -> JSON string of the analytics/context payload
  - field "images"   -> one part per uploaded file (repeatable field name)
  - header "Authorization: Bearer <token>"
  - any extra query-string params are forwarded as-is to the real endpoint

Config (url/token per env) comes from a default.json-style file, same shape the
Node `config` package reads (dev/stage -> mcap.url, mcap.token).

Usage:
    from mcap_client import MCAPClient

    client = MCAPClient(config_path="default.json", env="dev", token="YOUR_TOKEN")
    result = client.analyze_images(["car.jpg"])          # matches the real API shape
    result = client.analyze_image("car.jpg")              # convenience, single image

Or as a one-off:
    from mcap_client import call_mcap_api
    result = call_mcap_api("car.jpg", env="dev", token="YOUR_TOKEN")

Or from the command line:
    python mcap_client.py car.jpg [more.jpg ...] --env dev --token YOUR_TOKEN --out result.json
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import requests


class MCAPRequestError(Exception):
    """Raised when the MCAP API returns a non-2xx response."""

    def __init__(self, status_code: int, response_body: Any):
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(f"MCAP request failed ({status_code}): {response_body}")


class MCAPConfig:
    """Loads the mcap.url / mcap.token pair for a given environment from a config json file."""

    def __init__(self, config_path: str = "default.json", env: str = "dev"):
        self.config_path = Path(config_path)
        self.env = env
        raw = self._load()

        if env not in raw:
            raise KeyError(f"Environment '{env}' not found in {self.config_path}")
        if "mcap" not in raw[env]:
            raise KeyError(f"'mcap' section missing under '{env}' in {self.config_path}")

        self.mcap_url: str = raw[env]["mcap"]["url"]
        self.mcap_token: str = raw[env]["mcap"].get("token", "") or ""

    def _load(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)


class MCAPClient:
    """
    Client for the MCAP perception analyzer /frames endpoint.

    Auth token resolution order (first non-empty wins):
      1. `token` passed explicitly to __init__
      2. MCAP_TOKEN environment variable
      3. `mcap.token` value inside the config file for the chosen env
    """

    # Same default payload shape as McapAnalysis.jsx's jsonInput default.
    DEFAULT_PAYLOAD: Dict[str, Any] = {
        "analytics": {
            "vehicleAnalysis": {
                "embeddings": {"version": "auriga-v1"},
                "attributes": {},
                "details": {},
                "plates": {},
                "rankings": {},
            }
        },
        "context": {
            "projectExternalId": "Default Project",
            "siteExternalId": "HQ",
            "viewExternalId": "Alleyway 5",
            "daysToRetainData": 30,
        },
    }

    _CONTENT_TYPES = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }

    def __init__(
        self,
        config_path: str = "default.json",
        env: str = "dev",
        token: Optional[str] = None,
        url: Optional[str] = None,
        timeout: int = 30,
    ):
        """
        :param config_path: path to a default.json-style config file.
        :param env: "dev" or "stage" (whichever section your config defines).
        :param token: explicit bearer token; overrides config/env-var lookup.
        :param url: explicit MCAP endpoint URL; overrides the config file's mcap.url.
        :param timeout: request timeout in seconds.
        """
        config = MCAPConfig(config_path=config_path, env=env)
        self.url = url or config.mcap_url
        self.token = token or os.environ.get("MCAP_TOKEN") or config.mcap_token
        self.timeout = timeout

        if not self.token:
            raise ValueError(
                "No MCAP token found. Pass token=..., set the MCAP_TOKEN env var, "
                "or fill in mcap.token in the config file."
            )

    def _headers(self) -> Dict[str, str]:
        # Matches Mcap.fetchData's baseHeaders exactly.
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }

    def _guess_content_type(self, path: Path) -> str:
        return self._CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")

    def analyze_images(
        self,
        image_paths: list,
        payload: Optional[Dict[str, Any]] = None,
        query_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Send one or more images plus the analytics/context payload to the MCAP API in a
        single multipart request, exactly as Mcap.fetchData does:
          - JSON payload goes in a text field called "options"
          - each image goes in a repeated field called "images"
          - any query_params are forwarded as the request's query string

        :param image_paths: list of local image file paths (at least one).
        :param payload: optional override of the analytics/context payload; defaults
                         to DEFAULT_PAYLOAD (same shape as the jsx component's default,
                         with "configEnv" stripped the way the proxy does).
        :param query_params: optional dict forwarded as URL query params (mirrors req.query
                              being spread into `searchParams` in the proxy).
        :return: parsed JSON response body as a dict.
        :raises FileNotFoundError: if any image_path does not exist.
        :raises MCAPRequestError: if the API responds with a non-2xx status code.
        """
        if not image_paths:
            raise ValueError("analyze_images requires at least one image path.")

        body = dict(payload if payload is not None else self.DEFAULT_PAYLOAD)
        body.pop("configEnv", None)  # proxy deletes this before forwarding

        file_handles = []
        try:
            files = []
            for image_path in image_paths:
                image_file = Path(image_path)
                if not image_file.exists():
                    raise FileNotFoundError(f"Image not found: {image_path}")
                fh = open(image_file, "rb")
                file_handles.append(fh)
                files.append(
                    ("images", (image_file.name, fh, self._guess_content_type(image_file)))
                )
            # "options" is a plain text field carrying the JSON string, same as the proxy.
            files.append(("options", (None, json.dumps(body))))

            response = requests.post(
                self.url,
                headers=self._headers(),
                files=files,
                params=query_params or {},
                timeout=self.timeout,
            )
        finally:
            for fh in file_handles:
                fh.close()

        try:
            data = response.json()
        except ValueError:
            data = {"raw_text": response.text}

        if not response.ok:
            raise MCAPRequestError(response.status_code, data)

        return data

    def analyze_image(
        self,
        image_path: str,
        payload: Optional[Dict[str, Any]] = None,
        query_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Convenience wrapper around analyze_images() for the single-image case."""
        return self.analyze_images([image_path], payload=payload, query_params=query_params)


def parse_vehicles(result: Dict[str, Any]) -> list:
    """
    Flatten a raw MCAP response (as returned by MCAPClient.analyze_image/analyze_images)
    into a simple list of per-vehicle dicts, dropping the large embedding vectors.

    Each item looks like:
        {
            "make": "Hyundai",
            "model": "Sonata",
            "makeModel": "Hyundai, Sonata",
            "makeModelConfidence": 0.93,
            "vehicleType": "sedan",
            "color": ["white"],
            "boundingBox": {"x1": ..., "y1": ..., "x2": ..., "y2": ...},
            "damage": [{"position": "tail", "confidence": 0.66}, ...],
            "plateText": "DWD1915",
            "plateState": "NY",
            "plateConfidence": 0.86,
        }

    Vehicles/plates are matched via the plate's "vehicleId" field, matching its index
    in the "vehicles" list for that frame.
    """
    out = []
    try:
        vehicle_analyses = result["data"]["attributes"]["analytics"]["vehicleAnalysis"]
    except (KeyError, TypeError):
        return out

    for frame in vehicle_analyses or []:
        plates_by_vehicle_id = {p.get("vehicleId"): p for p in frame.get("plates", []) or []}

        for idx, vehicle in enumerate(frame.get("vehicles", []) or []):
            attrs = vehicle.get("attributes", {}) or {}
            rankings = attrs.get("rankings", {}) or {}

            plate = plates_by_vehicle_id.get(idx)
            plate_attrs = (plate.get("attributes", {}) or {}) if plate else {}

            damage = [
                {"position": d.get("position"), "confidence": (d.get("rankings") or {}).get("labelAndPosition")}
                for d in (vehicle.get("details") or [])
                if d.get("label") == "damage"
            ]

            out.append({
                "make": attrs.get("make"),
                "model": attrs.get("model"),
                "makeModel": attrs.get("makeModel"),
                "makeModelConfidence": rankings.get("makeModel"),
                "vehicleType": attrs.get("vehicleType"),
                "color": attrs.get("color"),
                "boundingBox": vehicle.get("boundingBox"),
                "damage": damage,
                "plateText": plate_attrs.get("text"),
                "plateState": plate_attrs.get("state"),
                "plateConfidence": plate_attrs.get("ranking"),
            })

    return out


def call_mcap_api(
    image_paths,
    config_path: str = "default.json",
    env: str = "dev",
    token: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    query_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    One-off convenience function: builds a client and analyzes one or more images
    in a single MCAP request.

    :param image_paths: a single path (str) or a list of paths.
    """
    if isinstance(image_paths, str):
        image_paths = [image_paths]
    client = MCAPClient(config_path=config_path, env=env, token=token)
    return client.analyze_images(image_paths, payload=payload, query_params=query_params)


def _main():
    parser = argparse.ArgumentParser(description="Call the MCAP perception analyzer API.")
    parser.add_argument("images", nargs="+", help="Path(s) to the image file(s) to analyze")
    parser.add_argument("--config", default="default.json", help="Path to config json (default.json)")
    parser.add_argument("--env", default="dev", choices=["dev", "stage"], help="Environment")
    parser.add_argument("--token", default=None, help="Bearer token (else uses MCAP_TOKEN env var or config)")
    parser.add_argument("--out", default=None, help="Optional path to save the JSON result")
    parser.add_argument(
        "--simplify",
        action="store_true",
        help="Print/save the flattened per-vehicle list (via parse_vehicles) instead of the raw response",
    )
    args = parser.parse_args()

    result = call_mcap_api(args.images, config_path=args.config, env=args.env, token=args.token)
    output = parse_vehicles(result) if args.simplify else result
    print(json.dumps(output, indent=2))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)
        print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    _main()
