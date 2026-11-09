"""Run all post-deployment seeding steps inside the application's VNet."""

import argparse
import fnmatch
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

LOGGER = logging.getLogger(__name__)
DATA_PACKS = ("rfp_evaluation", "contract_compliance", "retail_customer", "content_gen")
TEAM_PACKS = {
    "hr_onboarding": "00000000-0000-0000-0000-000000000001",
    "marketing_press_release": "00000000-0000-0000-0000-000000000002",
    "retail_customer": "00000000-0000-0000-0000-000000000003",
    "rfp_evaluation": "00000000-0000-0000-0000-000000000004",
    "contract_compliance": "00000000-0000-0000-0000-000000000005",
    "content_gen": "00000000-0000-0000-0000-000000000007",
}
PACK_KNOWLEDGE_BASES = {
    "retail_customer": ("macae-retail-customer-kb", "macae-retail-orders-kb"),
    "content_gen": ("macae-content-gen-products-kb",),
    "contract_compliance": (
        "macae-contract-summary-kb", "macae-contract-risk-kb", "macae-contract-compliance-kb",
    ),
    "rfp_evaluation": ("macae-rfp-summary-kb", "macae-rfp-risk-kb", "macae-rfp-compliance-kb"),
}
VECTOR_STORES = ("macae-retail-customer-data", "macae-retail-order-data")
CONTENT_ROOT = Path(__file__).resolve().parents[3] / "content_packs"


def run_script(name, *arguments):
    subprocess.run(
        [sys.executable, str(Path(__file__).with_name(name)), *arguments], check=True
    )


def seed_teams(packs):
    import requests

    backend = os.environ["BACKEND_URL"].rstrip("/")
    user_id = os.environ.get("SEED_USER_ID", "00000000-0000-0000-0000-000000000000")
    for pack in packs:
        run_script(
            "upload_team_config.py", backend, str(CONTENT_ROOT / pack / "agent_teams"),
            user_id, TEAM_PACKS[pack],
        )
    response = requests.get(
        f"{backend}/api/v4/team_configs",
        headers={"x-ms-client-principal-id": user_id}, timeout=120,
    )
    response.raise_for_status()
    actual = {team["team_id"] for team in response.json()}
    expected = {TEAM_PACKS[pack] for pack in packs}
    if not expected.issubset(actual):
        raise RuntimeError(f"Team verification failed; missing {sorted(expected - actual)}")
    return sorted(expected)


def seed_foundry(packs, credential):
    import httpx
    from azure.ai.projects import AIProjectClient

    stores = VECTOR_STORES if "retail_customer" in packs else ()
    kbs = [kb for pack in packs for kb in PACK_KNOWLEDGE_BASES.get(pack, ())]
    if stores:
        run_script("seed_vector_stores.py", "--only", ",".join(stores))
    if kbs:
        run_script("seed_knowledge_bases.py", "--only", ",".join(kbs))
        run_script("seed_kb_connections.py", "--only", ",".join(kbs))

    search = os.environ["AZURE_AI_SEARCH_ENDPOINT"].rstrip("/")
    project_id = os.environ["AZURE_AI_PROJECT_RESOURCE_ID"]
    search_headers = {
        "Authorization": f"Bearer {credential.get_token('https://search.azure.com/.default').token}"
    }
    arm_headers = {
        "Authorization": f"Bearer {credential.get_token('https://management.azure.com/.default').token}"
    }
    for kb in kbs:
        url = f"{search}/knowledgebases/{kb}?api-version=2025-11-01-preview"
        response = httpx.get(url, headers=search_headers, timeout=30)
        response.raise_for_status()
        if not response.json().get("knowledgeSources"):
            raise RuntimeError(f"Knowledge base has no sources: {kb}")
        target = f"{search}/knowledgebases/{kb}/mcp?api-version=2025-11-01-preview"
        url = (
            f"https://management.azure.com{project_id}/connections/{kb}-mcp"
            "?api-version=2025-04-01-preview"
        )
        response = httpx.get(url, headers=arm_headers, timeout=30)
        response.raise_for_status()
        properties = response.json()["properties"]
        if properties["target"] != target or properties["authType"] != "ProjectManagedIdentity":
            raise RuntimeError(f"Project connection verification failed: {kb}-mcp")

    verified_stores = {}
    if stores:
        with AIProjectClient(
            endpoint=os.environ["AZURE_AI_PROJECT_ENDPOINT"], credential=credential
        ) as project:
            with project.get_openai_client() as client:
                available = {store.name: store for store in client.vector_stores.list()}
                for name in stores:
                    store = available[name]
                    if store.status != "completed" or store.file_counts.completed == 0:
                        raise RuntimeError(f"Vector store is not ready: {name}")
                    verified_stores[name] = store.file_counts.completed
    return {"vector_stores": verified_stores, "knowledge_bases": kbs, "connections": len(kbs)}


def dataset_files(pack_dir, item):
    source = (pack_dir / item["source"]).resolve()
    if not source.is_relative_to(pack_dir.resolve()) or not source.is_dir():
        raise ValueError(f"Invalid dataset source: {source}")
    files = [
        path for path in sorted(source.rglob("*"))
        if path.is_file()
        and fnmatch.fnmatch(path.relative_to(source).as_posix(), item.get("pattern") or "*")
    ]
    if not files:
        raise ValueError(f"No dataset files matched in {source}")
    for path in files:
        if not path.resolve().is_relative_to(source):
            raise ValueError(f"Dataset file escapes its source directory: {path}")
    return [(path, path.relative_to(source).as_posix()) for path in files]


def seed_pack(pack_dir, storage, account, search_endpoint, credential):
    from azure.core.exceptions import ResourceExistsError
    from azure.search.documents import SearchClient

    manifest = json.loads((pack_dir / "pack.json").read_text())
    uploads = [*manifest.get("blob_indexes", []), *manifest.get("blob_uploads", [])]
    if not uploads:
        raise ValueError(f"No dataset uploads defined for {pack_dir.name}")
    containers = {}
    indexes = {}
    for item in uploads:
        container_name = item["container"]
        container = storage.get_container_client(container_name)
        try:
            container.create_container()
        except ResourceExistsError:
            LOGGER.info("Using existing container %s", container_name)
        for path, blob_name in dataset_files(pack_dir, item):
            data = path.read_bytes()
            blob = container.get_blob_client(blob_name)
            blob.upload_blob(data, overwrite=True)
            actual = blob.download_blob().readall()
            if hashlib.sha256(actual).digest() != hashlib.sha256(data).digest():
                raise RuntimeError(f"Blob verification failed: {container_name}/{blob_name}")
            containers.setdefault(container_name, set()).add(blob_name)
            LOGGER.info("Uploaded and verified %s/%s", container_name, blob_name)
        if item.get("index_name"):
            indexes[item["index_name"]] = container_name

    for item in manifest.get("search_indexes", []):
        indexes[item["index_name"]] = uploads[0]["container"]

    for index_name, container_name in indexes.items():
        subprocess.run(
            [
                sys.executable, str(Path(__file__).with_name("index_datasets.py")),
                account, container_name, search_endpoint, index_name,
            ],
            check=True,
        )
        with SearchClient(search_endpoint, index_name, credential) as client:
            expected = len(containers[container_name])
            for attempt in range(16):
                count = client.get_document_count()
                if count >= expected:
                    break
                if attempt == 15:
                    raise RuntimeError(
                        f"Index {index_name} contains {count} documents; expected at least {expected}"
                    )
                time.sleep(2)

    return {
        "pack": pack_dir.name,
        "blobs": {name: len(files) for name, files in containers.items()},
        "indexes": list(indexes),
    }


def parse_args(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packs", nargs="+", choices=tuple(TEAM_PACKS), default=tuple(TEAM_PACKS))
    parser.add_argument("--datasets-only", action="store_true", help="Skip team and Foundry setup.")
    return parser.parse_args(arguments)


def main():
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import BlobServiceClient

    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    account = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
    search_endpoint = os.environ["AZURE_AI_SEARCH_ENDPOINT"]
    with ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"]) as credential:
        teams = seed_teams(args.packs) if not args.datasets_only else []
        with BlobServiceClient(
            f"https://{account}.blob.core.windows.net", credential=credential
        ) as storage:
            results = [
                seed_pack(CONTENT_ROOT / pack, storage, account, search_endpoint, credential)
                for pack in args.packs if pack in DATA_PACKS
            ]
        foundry = seed_foundry(args.packs, credential) if not args.datasets_only else {}
    print("CONTENT_SEEDING_VERIFIED " + json.dumps(results), flush=True)
    if not args.datasets_only:
        print("FULL_SEEDING_VERIFIED " + json.dumps({"teams": teams, **foundry}), flush=True)


if __name__ == "__main__":
    main()
