#!/usr/bin/env python3
"""Generate local-worker metadata from the installed Codex bundled catalog."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess


def build_catalog(bundled, model, context_window):
    models = bundled.get("models", [])
    if not models:
        raise ValueError("The installed Codex did not return a bundled model catalog.")
    # Reuse the installed harness's own instructions at install time. No vendor
    # prompts or personal catalogs need to be committed to this repository.
    template = next((x for x in models if x.get("tool_mode") == "code_mode_only"), models[0])
    worker = copy.deepcopy(template)
    worker.update(slug=model, display_name=f"{model} (local worker)",
                  description="Local tool-capable model served through a custom Responses provider.",
                  context_window=context_window, max_context_window=context_window,
                  priority=1, visibility="list", supported_in_api=True,
                  default_reasoning_level="medium",
                  supported_reasoning_levels=[{"effort": "medium", "description": "Backend default reasoning"}],
                  additional_speed_tiers=[], service_tiers=[], upgrade=None, availability_nux=None,
                  include_skills_usage_instructions=False, include_plugin_usage_instructions=False,
                  include_apps_usage_instructions=False, input_modalities=["text"],
                  supports_search_tool=False, supports_image_detail_original=False,
                  tool_mode="code_mode_only", node_repl_disabled=False,
                  multi_agent_version="v2")
    worker.pop("model_messages", None)
    return {"models": [worker]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Exact model alias served by the local backend")
    parser.add_argument("--context-window", type=int, required=True, help="Must not exceed the backend's actual maximum")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex", default=shutil.which("codex") or "codex")
    parser.add_argument("--profile-output", type=Path, help="Also generate a ready-to-use Codex worker profile")
    parser.add_argument("--base-url", default="http://127.0.0.1:18991/v1", help="Responses endpoint used in the generated profile")
    args = parser.parse_args()
    if args.context_window < 8192:
        parser.error("A coding worker needs a context window of at least 8192 tokens.")
    try:
        result = subprocess.run([args.codex, "debug", "models", "--bundled"], check=True,
                                capture_output=True, text=True, timeout=30)
        catalog = build_catalog(json.loads(result.stdout), args.model, args.context_window)
        destination = args.output.expanduser().resolve()
        profile_path = args.profile_output.expanduser().resolve() if args.profile_output else None
        for path in (destination, profile_path):
            if path is not None and path.exists():
                parser.error(f"Refusing to overwrite {path}; select a new file or move the existing one first.")
        if profile_path == destination:
            parser.error("Catalog and profile must use different output paths.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(catalog, indent=2) + "\n")
        if profile_path:
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(
                f"model = {json.dumps(args.model)}\nmodel_provider = \"local_worker\"\n"
                f"model_catalog_json = {json.dumps(str(destination))}\n"
                f"model_context_window = {args.context_window}\n"
                f"model_auto_compact_token_limit = {int(args.context_window * .75)}\n"
                'model_reasoning_effort = "medium"\nweb_search = "disabled"\n\n'
                '[model_providers.local_worker]\nname = "Local implementation worker"\n'
                f"base_url = {json.dumps(args.base_url)}\n"
                'wire_api = "responses"\nrequires_openai_auth = false\n'
                'supports_websockets = false\nstream_idle_timeout_ms = 900000\n'
            )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Catalog generation failed: {type(exc).__name__}\n")
    print(f"Generated {destination}; set model_catalog_json to that absolute path in your worker profile.")
    if profile_path:
        print(f"Generated {profile_path}; next: overseer configure --profile {profile_path}")


if __name__ == "__main__":
    main()
