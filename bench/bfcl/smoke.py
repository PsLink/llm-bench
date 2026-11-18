"""Run the pinned non-streaming BFCL integration smoke in a fresh directory."""

import argparse
import json
import os
import sys
from collections import Counter
from importlib.metadata import distribution
from pathlib import Path

from utils.api import join_endpoint, to_base_url

REGISTRY_KEY = "llmbench-bfcl-smoke"
MANIFEST = Path(__file__).with_name("smoke-manifest.json")


def category_output(root, category, kind):
    paths = list(root.rglob(f"BFCL_v4_{category}_{kind}.json"))
    if len(paths) != 1:
        raise ValueError(f"Expected one BFCL {kind} file for {category}, found {len(paths)}")
    return paths[0]


def check_generated_cases(root, cases):
    """Reject partial generation before BFCL's partial evaluator can score it."""
    for category, expected in cases.items():
        path = category_output(root, category, "result")
        with path.open(encoding="utf-8") as source:
            rows = [json.loads(line) for line in source if line.strip()]
        if Counter(row["id"] for row in rows) != Counter(expected):
            raise ValueError(f"Unexpected case coverage for {category}")
        if any("traceback" in row or "error" in row for row in rows):
            raise ValueError(f"BFCL inference failed for {category}")


def installed_revision():
    installed = json.loads(distribution("bfcl_eval").read_text("direct_url.json") or "{}")
    revision = installed.get("vcs_info", {}).get("commit_id")
    if revision:
        return revision
    # A build from a verified local checkout has no VCS metadata in PEP 610.
    stamp = Path(sys.prefix) / "llmbench-bfcl-revision"
    return stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if installed_revision() != manifest["revision"]:
        parser.error("Install BFCL from the Git revision in the smoke manifest")
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "manifest.json").write_bytes(MANIFEST.read_bytes())
    (root / "test_case_ids_to_generate.json").write_text(
        json.dumps(manifest["cases"]), encoding="utf-8"
    )
    # BFCL resolves this environment variable when importing its configuration.
    os.environ["BFCL_PROJECT_ROOT"] = str(root)

    from bfcl_eval.__main__ import cli
    from bfcl_eval.constants.model_config import MODEL_CONFIG_MAPPING, ModelConfig
    from bfcl_eval.model_handler.api_inference.openai_completion import (
        OpenAICompletionsHandler,
    )
    import httpx

    base_url = to_base_url(join_endpoint(args.endpoint, "chat/completions"))
    api_key = os.environ.get("BFCL_ENDPOINT_API_KEY")

    class SmokeHandler(OpenAICompletionsHandler):
        def _build_client_kwargs(self):
            options = {"base_url": base_url, "max_retries": 0, "timeout": 120}
            if api_key:
                return {**options, "api_key": api_key}

            # The SDK requires a nonempty key. Remove its placeholder header so
            # unauthenticated endpoints receive the same request as in LLMBench.
            def remove_auth(request):
                request.headers.pop("Authorization", None)

            return {
                **options,
                "api_key": "unused",
                "http_client": httpx.Client(event_hooks={"request": [remove_auth]}),
            }

    MODEL_CONFIG_MAPPING[REGISTRY_KEY] = ModelConfig(
        model_name=args.model,
        display_name=REGISTRY_KEY,
        url="",
        org="",
        license="",
        model_handler=SmokeHandler,
        is_fc_model=True,
    )
    cli(
        [
            "generate", "--model", REGISTRY_KEY, "--run-ids",
            "--num-threads", str(manifest["concurrency"]),
            "--temperature", str(manifest["temperature"]),
        ],
        standalone_mode=False,
    )
    check_generated_cases(root / "result" / REGISTRY_KEY, manifest["cases"])
    cli(
        [
            "evaluate", "--model", REGISTRY_KEY, "--partial-eval",
            "--test-category", ",".join(manifest["cases"]),
        ],
        standalone_mode=False,
    )
    for category, expected in manifest["cases"].items():
        path = category_output(root / "score" / REGISTRY_KEY, category, "score")
        with path.open(encoding="utf-8") as source:
            header = json.loads(source.readline())
        if header["total_count"] != len(expected):
            raise ValueError(f"Unexpected scoring coverage for {category}")


if __name__ == "__main__":
    main()
