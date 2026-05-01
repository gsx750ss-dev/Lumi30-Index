#!/usr/bin/env python3
from __future__ import annotations

import datetime as _dt
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    import fitz  # PyMuPDF
except Exception as exc:
    print(
        "ERROR: PyMuPDF is required. Install with: python -m pip install pymupdf\n"
        f"{exc}",
        file=sys.stderr,
    )
    raise


ORG = os.environ.get("LUMINA30_ORG", "lumina-30").strip()
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "docs/ai-readable")).resolve()
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

ONLY_REPOS = {x.strip() for x in os.environ.get("LUMINA30_REPOS", "").split(",") if x.strip()}
SKIP_REPOS = {x.strip() for x in os.environ.get("LUMINA30_SKIP_REPOS", "").split(",") if x.strip()}
INCLUDE_ARCHIVED = os.environ.get("LUMINA30_INCLUDE_ARCHIVED", "true").lower() not in {"0", "false", "no"}
MAX_PDF_BYTES = int(os.environ.get("LUMINA30_MAX_PDF_BYTES", str(80 * 1024 * 1024)))

BUNDLE_NAME = "org-pdf-text-layer-bundle.txt"
MANIFEST_NAME = "org-pdf-text-layer-manifest.json"
INDEX_NAME = "index.html"
README_NAME = "README.md"

GENERATED_NOTICE = "This file is generated automatically. Do not edit by hand."
AI_USAGE_NOTE = """AI usage note:
Do not perform fresh PDF text extraction by default for the LUMINA-30 repository set.
When PDF body text is needed, first use this generated organization-wide AI-readable text bundle.
Use the manifest to check source repository, PDF path, Git blob SHA, SHA256 hash, extraction status, and generation metadata.
PDF files remain the authoritative versions. Inspect or re-extract from original PDFs only when the bundle is missing the required PDF, the manifest indicates extraction failure or OCR_REQUIRED, visual layout/figures/tables/signatures/exact formatting matter, or the user explicitly asks to verify the PDF itself.
""".strip()

EXCLUDED_PATH_PARTS = {
    ".git",
    ".github",
    "node_modules",
    "__pycache__",
}
EXCLUDED_PREFIXES = {
    "docs/ai-readable/",
}


def now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def api_request(url: str, *, accept: str = "application/vnd.github+json") -> bytes:
    headers = {
        "Accept": accept,
        "User-Agent": "LUMINA-30-org-pdf-text-layer-builder",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def api_get_json(url: str) -> Any:
    return json.loads(api_request(url).decode("utf-8"))


def list_public_repositories(org: str) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    page = 1

    while True:
        url = f"https://api.github.com/orgs/{urllib.parse.quote(org)}/repos?type=public&per_page=100&page={page}"
        data = api_get_json(url)
        if not data:
            break

        for repo in data:
            name = repo.get("name", "")
            if ONLY_REPOS and name not in ONLY_REPOS:
                continue
            if name in SKIP_REPOS:
                continue
            if repo.get("archived") and not INCLUDE_ARCHIVED:
                continue
            if repo.get("fork"):
                continue
            repos.append(repo)

        page += 1

    repos.sort(key=lambda r: r.get("name", "").lower())
    return repos


def quote_repo_path(full_name: str) -> str:
    return "/".join(urllib.parse.quote(part, safe="") for part in full_name.split("/"))


def get_default_tree_sha(repo: dict[str, Any]) -> tuple[str | None, str | None]:
    full_name = repo["full_name"]
    branch = repo.get("default_branch") or "main"
    url = f"https://api.github.com/repos/{quote_repo_path(full_name)}/branches/{urllib.parse.quote(branch, safe='')}"
    try:
        data = api_get_json(url)
        tree_sha = data.get("commit", {}).get("commit", {}).get("tree", {}).get("sha")
        if tree_sha:
            return tree_sha, None
        return None, "DEFAULT_TREE_SHA_NOT_FOUND"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return None, f"BRANCH_API_ERROR: {exc.code} {exc.reason}: {body[-500:]}"
    except Exception as exc:
        return None, f"BRANCH_API_ERROR: {exc}"


def list_pdf_blobs(repo: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    full_name = repo["full_name"]
    tree_sha, error = get_default_tree_sha(repo)
    if error or not tree_sha:
        return [], error or "DEFAULT_TREE_SHA_NOT_FOUND"

    url = f"https://api.github.com/repos/{quote_repo_path(full_name)}/git/trees/{urllib.parse.quote(tree_sha, safe='')}?recursive=1"
    try:
        data = api_get_json(url)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return [], f"TREE_API_ERROR: {exc.code} {exc.reason}: {body[-500:]}"
    except Exception as exc:
        return [], f"TREE_API_ERROR: {exc}"

    if data.get("truncated"):
        # Still use returned entries, but report warning through repo error.
        tree_warning = "TREE_TRUNCATED: GitHub returned a truncated recursive tree. Some PDFs may be missing."
    else:
        tree_warning = None

    pdfs: list[dict[str, Any]] = []
    for item in data.get("tree", []):
        if item.get("type") != "blob":
            continue
        path = item.get("path", "")
        if not path.lower().endswith(".pdf"):
            continue
        if is_excluded_repo_path(path):
            continue
        pdfs.append(item)

    pdfs.sort(key=lambda x: x.get("path", "").lower())
    return pdfs, tree_warning


def is_excluded_repo_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if any(normalized.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        return True
    parts = set(normalized.split("/"))
    return any(part in parts for part in EXCLUDED_PATH_PARTS)


def pdf_blob_url(repo: dict[str, Any], rel_path: str) -> str:
    branch = repo.get("default_branch") or "main"
    quoted_branch = urllib.parse.quote(branch, safe="")
    quoted_path = "/".join(urllib.parse.quote(part, safe="") for part in rel_path.split("/"))
    return f"https://github.com/{repo['full_name']}/blob/{quoted_branch}/{quoted_path}"


def pdf_raw_url(repo: dict[str, Any], rel_path: str) -> str:
    branch = repo.get("default_branch") or "main"
    quoted_branch = urllib.parse.quote(branch, safe="")
    quoted_path = "/".join(urllib.parse.quote(part, safe="") for part in rel_path.split("/"))
    return f"https://raw.githubusercontent.com/{repo['full_name']}/{quoted_branch}/{quoted_path}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def guess_pdf_kind(rel_path: str) -> str:
    lowered = rel_path.lower()
    if any(word in lowered for word in ["slide", "slides", "deck", "presentation"]):
        return "SLIDE_TEXT_LAYER"
    return "EXTRACTED_TEXT"


def extract_pdf_text(path: Path) -> tuple[str, int, int, str | None]:
    page_count = 0
    char_count = 0
    sections: list[str] = []

    try:
        with fitz.open(path) as doc:
            page_count = doc.page_count
            for index, page in enumerate(doc, start=1):
                text = page.get_text("text") or ""
                text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
                char_count += len(text)
                label = "Slide" if "slide" in path.name.lower() else "Page"
                if text:
                    sections.append(f"[{label} {index:03d}]\n{text}")
                else:
                    sections.append(f"[{label} {index:03d}]\n[No embedded text extracted]")
    except Exception as exc:
        return "", 0, 0, str(exc)

    return "\n\n".join(sections), page_count, char_count, None


def load_previous_manifest() -> dict[str, Any]:
    path = OUTPUT_DIR / MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def load_previous_bundle_text() -> str:
    path = OUTPUT_DIR / BUNDLE_NAME
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def parse_previous_text_sections(bundle: str) -> dict[tuple[str, str, str | None], str]:
    """Return text keyed by (full_name, pdf_path, git_blob_sha-or-None).

    Supports both the v2 BEGIN/END block format and the earlier delimiter-based format.
    """
    result: dict[tuple[str, str, str | None], str] = {}

    # v2 format.
    block_pattern = re.compile(
        r"---- BEGIN PDF TEXT RECORD ----\n(?P<header>.*?)(?:\n\nExtracted text:\n\n)(?P<text>.*?)(?:\n---- END PDF TEXT RECORD ----)",
        re.DOTALL,
    )
    for match in block_pattern.finditer(bundle):
        header = match.group("header")
        text = match.group("text").rstrip("\n")
        full_name = _header_value(header, "Repository")
        pdf_path = _header_value(header, "PDF")
        blob_sha = _header_value(header, "Git blob SHA")
        if full_name and pdf_path:
            result[(full_name, pdf_path, blob_sha)] = text
            result[(full_name, pdf_path, None)] = text

    if result:
        return result

    # v1 delimiter-based format.
    legacy_pattern = re.compile(
        r"Record:\s*\d+\n(?P<header>.*?)(?:\n={20,}\n\n)Extracted text:\n\n(?P<text>.*?)(?=\n\n={20,}\nRecord:\s*\d+|\Z)",
        re.DOTALL,
    )
    for match in legacy_pattern.finditer(bundle):
        header = match.group("header")
        text = match.group("text").rstrip("\n")
        full_name = _header_value(header, "Repository")
        pdf_path = _header_value(header, "PDF")
        if full_name and pdf_path:
            result[(full_name, pdf_path, None)] = text

    return result


def _header_value(header: str, key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(header)
    return match.group(1).strip() if match else None


def make_text_cache_maps(previous_manifest: dict[str, Any], previous_bundle: str) -> dict[str, Any]:
    text_sections = parse_previous_text_sections(previous_bundle)
    by_repo_path_blob: dict[tuple[str, str, str], dict[str, Any]] = {}
    by_repo_blob: dict[tuple[str, str], dict[str, Any]] = {}
    by_repo_path_sha: dict[tuple[str, str, str], dict[str, Any]] = {}

    for record in previous_manifest.get("records", []):
        full_name = record.get("full_name")
        pdf_path = record.get("pdf_path")
        blob_sha = record.get("git_blob_sha")
        sha256 = record.get("sha256")
        if not full_name or not pdf_path:
            continue

        text = None
        if blob_sha:
            text = text_sections.get((full_name, pdf_path, blob_sha))
        if text is None:
            text = text_sections.get((full_name, pdf_path, None))
        if text is None:
            continue

        cached = dict(record)
        cached["extracted_text"] = text
        if full_name and pdf_path and blob_sha:
            by_repo_path_blob[(full_name, pdf_path, blob_sha)] = cached
            by_repo_blob[(full_name, blob_sha)] = cached
        if full_name and pdf_path and sha256:
            by_repo_path_sha[(full_name, pdf_path, sha256)] = cached

    return {
        "by_repo_path_blob": by_repo_path_blob,
        "by_repo_blob": by_repo_blob,
        "by_repo_path_sha": by_repo_path_sha,
    }


def download_pdf_to_temp(repo: dict[str, Any], rel_path: str) -> tuple[Path | None, str | None]:
    url = pdf_raw_url(repo, rel_path)
    try:
        data = api_request(url, accept="application/octet-stream")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return None, f"PDF_DOWNLOAD_ERROR: {exc.code} {exc.reason}: {body[-500:]}"
    except Exception as exc:
        return None, f"PDF_DOWNLOAD_ERROR: {exc}"

    tmp = tempfile.NamedTemporaryFile(prefix="lumina30_pdf_", suffix=".pdf", delete=False)
    tmp.write(data)
    tmp.close()
    return Path(tmp.name), None


def build_records(repos: list[dict[str, Any]], cache_maps: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    records: list[dict[str, Any]] = []
    repo_errors: list[dict[str, Any]] = []
    stats = {
        "pdfs_seen": 0,
        "reused_unchanged": 0,
        "reused_renamed_same_blob": 0,
        "downloaded_or_extracted": 0,
        "skipped_too_large": 0,
        "download_errors": 0,
    }

    for repo in repos:
        name = repo["name"]
        full_name = repo["full_name"]
        print(f"Scanning {full_name}...")

        pdf_blobs, tree_error = list_pdf_blobs(repo)
        if tree_error:
            repo_errors.append({
                "repository": name,
                "full_name": full_name,
                "status": "TREE_WARNING_OR_ERROR",
                "error": tree_error,
            })
            print(f"  TREE_WARNING_OR_ERROR: {tree_error}")

        print(f"  PDFs found: {len(pdf_blobs)}")

        for blob in pdf_blobs:
            rel = blob.get("path", "")
            blob_sha = blob.get("sha", "")
            size = int(blob.get("size") or 0)
            stats["pdfs_seen"] += 1

            base_record: dict[str, Any] = {
                "repository": name,
                "full_name": full_name,
                "repository_url": repo.get("html_url"),
                "default_branch": repo.get("default_branch"),
                "archived": bool(repo.get("archived")),
                "pdf_path": rel,
                "pdf_url": pdf_blob_url(repo, rel),
                "raw_url": pdf_raw_url(repo, rel),
                "git_blob_sha": blob_sha,
                "sha256": None,
                "size_bytes": size,
                "page_count": None,
                "char_count": 0,
                "status": "PENDING",
                "error": None,
                "extracted_text": "",
                "cache_action": "PENDING",
            }

            cached = None
            if blob_sha:
                cached = cache_maps["by_repo_path_blob"].get((full_name, rel, blob_sha))
                if cached:
                    stats["reused_unchanged"] += 1
                    base_record.update(_reuse_fields(cached))
                    base_record["cache_action"] = "REUSED_UNCHANGED"
                    records.append(base_record)
                    continue

                cached = cache_maps["by_repo_blob"].get((full_name, blob_sha))
                if cached:
                    stats["reused_renamed_same_blob"] += 1
                    base_record.update(_reuse_fields(cached))
                    base_record["cache_action"] = "REUSED_RENAMED_SAME_BLOB"
                    records.append(base_record)
                    continue

            if size > MAX_PDF_BYTES:
                stats["skipped_too_large"] += 1
                base_record["status"] = "SKIPPED_TOO_LARGE"
                base_record["error"] = f"PDF size {size} exceeds max {MAX_PDF_BYTES} bytes."
                base_record["cache_action"] = "SKIPPED_NO_DOWNLOAD"
                records.append(base_record)
                continue

            stats["downloaded_or_extracted"] += 1
            tmp_pdf, error = download_pdf_to_temp(repo, rel)
            if error or tmp_pdf is None:
                stats["download_errors"] += 1
                base_record["status"] = "PDF_DOWNLOAD_ERROR"
                base_record["error"] = error or "unknown download error"
                base_record["cache_action"] = "DOWNLOAD_FAILED"
                records.append(base_record)
                continue

            try:
                base_record["sha256"] = sha256_file(tmp_pdf)
                extracted, page_count, char_count, extract_error = extract_pdf_text(tmp_pdf)
                base_record["page_count"] = page_count
                base_record["char_count"] = char_count
                base_record["extracted_text"] = extracted
                base_record["cache_action"] = "EXTRACTED_NEW_OR_CHANGED"

                if extract_error:
                    base_record["status"] = "PDF_READ_ERROR"
                    base_record["error"] = extract_error
                elif char_count < 40:
                    base_record["status"] = "OCR_REQUIRED"
                else:
                    base_record["status"] = guess_pdf_kind(rel)
            finally:
                try:
                    tmp_pdf.unlink(missing_ok=True)
                except Exception:
                    pass

            records.append(base_record)

    records.sort(key=lambda r: (r["repository"].lower(), r["pdf_path"].lower()))
    return records, repo_errors, stats


def _reuse_fields(cached: dict[str, Any]) -> dict[str, Any]:
    return {
        "sha256": cached.get("sha256"),
        "page_count": cached.get("page_count"),
        "char_count": cached.get("char_count", 0),
        "status": cached.get("status", "EXTRACTED_TEXT"),
        "error": cached.get("error"),
        "extracted_text": cached.get("extracted_text", ""),
    }


def clean_output_dir() -> None:
    # docs/ai-readable is a generated-only directory. Recreate it from current repo state
    # on every run so deleted/renamed PDFs and deprecated generated files cannot leave stale output.
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def write_outputs(
    records: list[dict[str, Any]],
    repo_errors: list[dict[str, Any]],
    stats: dict[str, int],
    generated_at: str,
) -> None:
    clean_output_dir()

    manifest_records = []
    for record in records:
        copy = {k: v for k, v in record.items() if k != "extracted_text"}
        manifest_records.append(copy)

    manifest = {
        "generated_at": generated_at,
        "notice": GENERATED_NOTICE,
        "organization": ORG,
        "scope": "public repositories visible to this workflow",
        "authority": "PDF files remain authoritative. This generated layer is auxiliary for AI review, search, accessibility, and fallback reading.",
        "ai_usage_note": AI_USAGE_NOTE,
        "garbage_prevention": {
            "policy": "Current-state reconciliation. Outputs are rebuilt from the current organization PDF set on every run.",
            "stale_records_retained": False,
            "deleted_or_renamed_pdfs": "Omitted from regenerated outputs unless present in current repository trees.",
            "output_directory": str(OUTPUT_DIR),
        },
        "incremental_policy": {
            "list_current_pdfs_each_run": True,
            "reuse_extracted_text_when_git_blob_sha_is_unchanged": True,
            "reuse_extracted_text_for_renames_when_git_blob_sha_matches": True,
            "extract_only_new_or_changed_pdfs": True,
        },
        "records_count": len(records),
        "repository_errors_count": len(repo_errors),
        "stats": stats,
        "records": manifest_records,
        "repository_errors": repo_errors,
    }

    (OUTPUT_DIR / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    bundle_lines: list[str] = [
        "LUMINA-30 Organization PDF Text Layer Bundle",
        GENERATED_NOTICE,
        f"Generated: {generated_at}",
        f"Organization: {ORG}",
        "",
        "Authority:",
        "PDF files remain the authoritative versions.",
        "This text bundle is auxiliary, generated for AI review, repository search, accessibility, and fallback reading.",
        "",
        AI_USAGE_NOTE,
        "",
        "Garbage prevention:",
        "This bundle is rebuilt from the current organization PDF set on every run.",
        "Deleted or renamed PDFs are not retained as stale records.",
        "Unchanged PDFs reuse cached extracted text when possible; only new or changed PDFs are freshly extracted.",
        "",
        "Run statistics:",
        f"PDFs seen: {stats.get('pdfs_seen', 0)}",
        f"Reused unchanged: {stats.get('reused_unchanged', 0)}",
        f"Reused renamed same blob: {stats.get('reused_renamed_same_blob', 0)}",
        f"Downloaded/extracted: {stats.get('downloaded_or_extracted', 0)}",
        f"Skipped too large: {stats.get('skipped_too_large', 0)}",
        f"Download errors: {stats.get('download_errors', 0)}",
        "",
        "Important:",
        "This bundle may contain extraction errors. For substantive judgment, verify against the original PDF.",
        "",
    ]

    if repo_errors:
        bundle_lines.extend([
            "Repository access warnings/errors",
            "=================================",
            "",
        ])
        for err in repo_errors:
            bundle_lines.extend([
                f"Repository: {err.get('full_name')}",
                f"Status: {err.get('status')}",
                f"Error: {err.get('error')}",
                "",
            ])

    if not records:
        bundle_lines.extend([
            "No PDF files were found in the scanned public repositories.",
            "",
        ])

    for idx, record in enumerate(records, start=1):
        bundle_lines.extend([
            "---- BEGIN PDF TEXT RECORD ----",
            f"Record: {idx}",
            f"Repository: {record['full_name']}",
            f"PDF: {record['pdf_path']}",
            f"PDF URL: {record['pdf_url']}",
            f"Git blob SHA: {record.get('git_blob_sha')}",
            f"SHA256: {record.get('sha256')}",
            f"Status: {record['status']}",
            f"Cache action: {record.get('cache_action')}",
            f"Size bytes: {record['size_bytes']}",
            f"Page count: {record.get('page_count')}",
            f"Extracted characters: {record.get('char_count')}",
            "",
        ])

        if record.get("error"):
            bundle_lines.extend([
                "Extraction error:",
                str(record["error"]),
                "",
            ])

        text = record.get("extracted_text") or ""
        bundle_lines.extend([
            "Extracted text:",
            "",
            text if text.strip() else "[No reliable embedded text was extracted. OCR or manual extraction may be required.]",
            "---- END PDF TEXT RECORD ----",
            "",
        ])

    (OUTPUT_DIR / BUNDLE_NAME).write_text(
        "\n".join(bundle_lines).rstrip() + "\n",
        encoding="utf-8",
    )

    readme = f"""# AI-readable PDF text layer

{GENERATED_NOTICE}

This directory contains an auxiliary AI-readable text layer for PDF files found across the public repositories of the LUMINA-30 GitHub organization.

## Files

- [`{BUNDLE_NAME}`](./{BUNDLE_NAME}) — consolidated extracted PDF text.
- [`{MANIFEST_NAME}`](./{MANIFEST_NAME}) — machine-readable metadata, Git blob SHA values, SHA256 values, extraction status, and source paths.
- [`{INDEX_NAME}`](./{INDEX_NAME}) — compact generated launcher page.

## AI usage note

Do not perform fresh PDF text extraction by default for the LUMINA-30 repository set.
When PDF body text is needed, first use the generated organization-wide text bundle above.
Use the manifest to check source repository, PDF path, Git blob SHA, SHA256 hash, extraction status, and generation metadata.

PDF files remain the authoritative versions. Inspect or re-extract from original PDFs only when the bundle is missing the required PDF, the manifest indicates extraction failure or OCR_REQUIRED, visual layout/figures/tables/signatures/exact formatting matter, or the user explicitly asks to verify the PDF itself.

## Garbage-prevention rule

This directory is generated-only. It is rebuilt from the current organization PDF set on every run. Deleted or renamed PDFs are not retained as stale records.
"""
    (OUTPUT_DIR / README_NAME).write_text(readme, encoding="utf-8")

    rows = []
    for record in records:
        rows.append(
            "<tr>"
            f"<td>{html.escape(record['repository'])}</td>"
            f"<td><a href=\"{html.escape(record['pdf_url'])}\">{html.escape(record['pdf_path'])}</a></td>"
            f"<td>{html.escape(str(record.get('status')))}</td>"
            f"<td>{html.escape(str(record.get('cache_action')))}</td>"
            f"<td>{html.escape(str(record.get('page_count')))}</td>"
            f"<td>{html.escape(str(record.get('char_count')))}</td>"
            f"<td><code>{html.escape(str(record.get('git_blob_sha') or '')[:12])}...</code></td>"
            f"<td><code>{html.escape(str(record.get('sha256') or '')[:16])}...</code></td>"
            "</tr>"
        )

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>LUMINA-30 Organization PDF Text Layer</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ font-family: system-ui, sans-serif; line-height: 1.5; margin: 2rem; max-width: 1200px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: 0.5rem; vertical-align: top; }}
    th {{ text-align: left; }}
    code {{ white-space: nowrap; }}
    .note {{ padding: 1rem; border: 1px solid #ddd; background: #fafafa; }}
  </style>
</head>
<body>
  <h1>LUMINA-30 Organization PDF Text Layer</h1>
  <p><strong>{html.escape(GENERATED_NOTICE)}</strong></p>
  <p>Generated: {html.escape(generated_at)}</p>
  <div class="note">
    <p>PDF files remain the authoritative versions. This generated text layer is auxiliary for AI review, repository search, accessibility, and fallback reading.</p>
    <p><strong>AI usage:</strong> do not perform fresh PDF text extraction by default. Use the generated bundle first; inspect original PDFs only when visual layout, figures, tables, signatures, exact formatting, extraction failure, or user instruction requires it.</p>
    <p><strong>Garbage prevention:</strong> this directory is rebuilt from the current organization PDF set on every run. Deleted or renamed PDFs are not retained as stale records.</p>
    <ul>
      <li><a href="./{html.escape(BUNDLE_NAME)}">Organization PDF text bundle</a></li>
      <li><a href="./{html.escape(MANIFEST_NAME)}">Organization PDF text manifest</a></li>
    </ul>
  </div>
  <h2>Run statistics</h2>
  <ul>
    <li>PDFs seen: {stats.get('pdfs_seen', 0)}</li>
    <li>Reused unchanged: {stats.get('reused_unchanged', 0)}</li>
    <li>Reused renamed same blob: {stats.get('reused_renamed_same_blob', 0)}</li>
    <li>Downloaded/extracted: {stats.get('downloaded_or_extracted', 0)}</li>
    <li>Skipped too large: {stats.get('skipped_too_large', 0)}</li>
    <li>Download errors: {stats.get('download_errors', 0)}</li>
  </ul>
  <h2>PDF records</h2>
  <table>
    <thead>
      <tr>
        <th>Repository</th>
        <th>PDF</th>
        <th>Status</th>
        <th>Cache action</th>
        <th>Pages</th>
        <th>Extracted chars</th>
        <th>Git blob SHA</th>
        <th>SHA256</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows) if rows else '<tr><td colspan="8">No PDF files found.</td></tr>'}
    </tbody>
  </table>
</body>
</html>
"""
    (OUTPUT_DIR / INDEX_NAME).write_text(html_doc, encoding="utf-8")


def main() -> int:
    started = time.time()
    generated_at = now_utc()

    print("Building LUMINA-30 organization PDF text layer")
    print(f"Organization: {ORG}")
    print(f"Output directory: {OUTPUT_DIR}")
    print(f"Generated at: {generated_at}")
    print("Mode: current-state reconciliation; regenerate generated directory; reuse unchanged PDF text where possible")

    previous_manifest = load_previous_manifest()
    previous_bundle = load_previous_bundle_text()
    cache_maps = make_text_cache_maps(previous_manifest, previous_bundle)

    try:
        repos = list_public_repositories(ORG)
    except urllib.error.HTTPError as exc:
        print(f"ERROR: GitHub API request failed: {exc.code} {exc.reason}", file=sys.stderr)
        print(exc.read().decode("utf-8", errors="replace"), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: GitHub API request failed: {exc}", file=sys.stderr)
        return 2

    print(f"Repositories selected: {len(repos)}")
    records, repo_errors, stats = build_records(repos, cache_maps)
    write_outputs(records, repo_errors, stats, generated_at)

    print("")
    print("Output complete.")
    print(f"PDF records: {len(records)}")
    print(f"Repository errors/warnings: {len(repo_errors)}")
    print(f"Stats: {stats}")
    print(f"Elapsed seconds: {time.time() - started:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
