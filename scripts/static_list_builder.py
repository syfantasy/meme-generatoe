#!/usr/bin/env python3
"""Pre-render the meme list pages served to the Yunzai ``#meme列表`` plugin.

The plugin normally asks the running service to render the list image on demand
(``POST /memes/render_list``).  Rendering 200 memes takes several seconds and a
render/Fly style deployment also pays a cold start on the first request, so the
exact same pages are baked into the image at build time and served as static
files:

    GET /memes/static/list/manifest.json
    GET /memes/static/list/p1-<version>.png
    GET /memes/static/list/p2-<version>.png
    ...

The manifest is written last, is fully self describing and carries the meme key
of every page, so a client can verify that the baked pages still match the meme
set it sees in ``/memes/static/infos.json`` before using them.  Anything that
does not match is ignored and the on-demand render endpoint remains the
fallback.

The same module is imported by ``scripts/entrypoint.sh`` so the service can
rebuild the pages in the background (using the memes it already loaded) when the
startup repository sync pulled a different meme set than the image was built
with.

Usage:
    python3 static_list_builder.py --out /app/static/meme-list
    python3 static_list_builder.py --out /app/static/meme-list --check
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = 1
DEFAULT_PAGE_SIZE = 200
DEFAULT_TEXT_TEMPLATE = "{index}. {keywords}"
PAGE_FILE_RE = re.compile(r"^p(\d+)-[0-9a-f]{6,64}\.png$")

# Repositories bundled by the Dockerfile, mapped to the directory the runtime
# bootstrap loads them from.  Order is irrelevant for the meme set but kept
# stable for the repo head signature.
REPO_MEME_DIRS: Sequence[tuple] = (
    ("meme-generator-contrib", "memes"),
    ("meme_emoji", "emoji"),
    ("meme_emoji_nsfw", "emoji"),
    ("meme-generator-jj", "memes"),
    ("tudou-meme", "meme"),
    ("meme-generator-cute", "memes"),
)
BUILTIN_REPO = "meme-generator"
BUILTIN_SUBDIR = "meme_generator/memes"


# --------------------------------------------------------------------------- #
# environment helpers
# --------------------------------------------------------------------------- #
def app_root(root: Optional[os.PathLike] = None) -> Path:
    if root is not None:
        return Path(root)
    return Path(os.environ.get("MEME_APP_ROOT", "/app"))


def default_out_dir() -> Path:
    return Path(os.environ.get("MEME_STATIC_LIST_DIR", "/app/static/meme-list"))


def _env_page_size(default: int = DEFAULT_PAGE_SIZE) -> int:
    raw = os.environ.get("MEME_STATIC_LIST_PAGE_SIZE") or os.environ.get("MEME_LIST_PAGE_SIZE")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if 1 <= value <= 1000 else default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def prepare_config(config_home: Optional[str] = None) -> Path:
    """Write the minimal meme_generator config used for standalone rendering.

    Must run *before* ``meme_generator`` is imported.  Mirrors what
    ``scripts/entrypoint.sh`` writes at container start: builtin memes are
    disabled so that the meme set is decided purely by ``meme_dirs`` below.
    """
    home = config_home or os.environ.get("XDG_CONFIG_HOME") or "/tmp/config"
    os.environ["XDG_CONFIG_HOME"] = home
    config_dir = Path(home) / "meme_generator"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.toml"
    config_file.write_text(
        "[meme]\nload_builtin_memes = false\nmeme_dirs = []\n",
        encoding="utf-8",
    )
    return config_file


# --------------------------------------------------------------------------- #
# meme loading / ordering
# --------------------------------------------------------------------------- #
def load_all_memes(root: Optional[os.PathLike] = None) -> List[Path]:
    """Load the same meme packs, from the same paths, as the runtime bootstrap."""
    from meme_generator import load_meme, load_memes  # noqa: F401  (import for side effects)

    base = app_root(root)
    loaded: List[Path] = []

    builtin_src = base / BUILTIN_REPO / BUILTIN_SUBDIR
    if builtin_src.exists():
        load_memes(str(builtin_src))
        loaded.append(builtin_src)
    else:
        package_dir = Path(importlib.import_module("meme_generator").__file__).parent / "memes"
        if package_dir.exists():
            load_memes(str(package_dir))
            loaded.append(package_dir)

    for repo, subdir in REPO_MEME_DIRS:
        target = base / repo / subdir
        if not target.exists():
            continue
        if repo == "tudou-meme":
            # tudou-meme is loaded as a python package by the runtime bootstrap.
            (target / "__init__.py").touch(exist_ok=True)
        load_memes(str(target))
        loaded.append(target)
    return loaded


@dataclass
class Entry:
    key: str
    keywords: List[str] = field(default_factory=list)
    date_modified: str = ""


_PINYIN = None  # lazy: callable or False when unavailable


def _pinyin_syllables(text: str) -> Optional[str]:
    global _PINYIN
    if _PINYIN is None:
        try:
            from pypinyin import lazy_pinyin  # type: ignore

            _PINYIN = lazy_pinyin
        except Exception:
            _PINYIN = False
    if not _PINYIN:
        return None
    try:
        return "".join(str(part) for part in _PINYIN(text, errors=lambda item: list(item))).casefold()
    except Exception:
        return None


def _sort_key(entry: Entry):
    name = entry.keywords[0] if entry.keywords else entry.key
    pinyin = _pinyin_syllables(name)
    return (pinyin if pinyin is not None else name.casefold(), name, entry.key)


def ordered_entries(root: Optional[os.PathLike] = None) -> List[Entry]:
    """Return the memes shown by ``#meme列表`` in display order.

    The plugin only shows memes exposing ``params_type``; ordering follows the
    first keyword (pinyin collation when pypinyin is installed, matching the
    zh-CN collation the plugin used to apply locally).
    """
    from meme_generator.manager import get_memes

    entries: List[Entry] = []
    for meme in get_memes():
        if getattr(meme, "params_type", None) is None:
            continue
        entries.append(
            Entry(
                key=str(meme.key),
                keywords=[str(item) for item in (getattr(meme, "keywords", None) or [])],
                date_modified=str(getattr(meme, "date_modified", "") or ""),
            )
        )
    entries.sort(key=_sort_key)
    return entries


def repo_heads(root: Optional[os.PathLike] = None) -> Dict[str, str]:
    base = app_root(root)
    heads: Dict[str, str] = {}
    for repo in (BUILTIN_REPO,) + tuple(item[0] for item in REPO_MEME_DIRS):
        directory = base / repo
        try:
            completed = subprocess.run(
                ["git", "-C", str(directory), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            heads[repo] = completed.stdout.strip() if completed.returncode == 0 else "unknown"
        except Exception:
            heads[repo] = "unknown"
    return heads


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def compute_signature(
    entries: Sequence[Entry],
    page_size: int,
    template: str,
    category_icon: bool,
    heads: Optional[Dict[str, str]] = None,
) -> str:
    payload = {
        "schema": MANIFEST_SCHEMA,
        "pageSize": int(page_size),
        "template": template,
        "categoryIcon": bool(category_icon),
        "repos": heads or {},
        "memes": [[entry.key, entry.keywords, entry.date_modified] for entry in entries],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return digest[:16]


def manifest_path(out_dir: Optional[os.PathLike] = None) -> Path:
    return Path(out_dir or default_out_dir()) / MANIFEST_NAME


def load_manifest(out_dir: Optional[os.PathLike] = None) -> Optional[dict]:
    path = manifest_path(out_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list) or not data["pages"]:
        return None
    return data


def manifest_is_current(
    manifest: Optional[dict],
    entries: Sequence[Entry],
    page_size: int,
    template: str,
    category_icon: bool,
    heads: Optional[Dict[str, str]] = None,
    out_dir: Optional[os.PathLike] = None,
) -> bool:
    if not manifest:
        return False
    if manifest.get("schema") != MANIFEST_SCHEMA:
        return False
    if int(manifest.get("pageSize") or 0) != int(page_size):
        return False
    if manifest.get("template") != template:
        return False
    if bool(manifest.get("categoryIcon")) != bool(category_icon):
        return False
    if int(manifest.get("total") or -1) != len(entries):
        return False
    expected = compute_signature(entries, page_size, template, category_icon, heads)
    if manifest.get("signature") != expected:
        return False
    directory = Path(out_dir or default_out_dir())
    for page in manifest["pages"]:
        name = os.path.basename(str(page.get("file") or ""))
        if not name or not (directory / name).is_file():
            return False
    return True


def _write_json_atomic(path: Path, payload: dict) -> None:
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def render_page(keys: Sequence[str], template: str, category_icon: bool) -> bytes:
    from meme_generator.manager import get_meme
    from meme_generator.utils import MemeProperties, render_meme_list

    meme_list = [(get_meme(key), MemeProperties()) for key in keys]
    result = render_meme_list(
        meme_list,
        text_template=template,
        add_category_icon=bool(category_icon),
    )
    data = result.getvalue() if hasattr(result, "getvalue") else result
    data = bytes(data)
    if len(data) < 512 or not data.startswith(b"\x89PNG"):
        raise RuntimeError(f"render_meme_list returned no usable PNG ({len(data)} bytes)")
    return data


def build(
    out_dir: Optional[os.PathLike] = None,
    page_size: Optional[int] = None,
    template: Optional[str] = None,
    category_icon: Optional[bool] = None,
    entries: Optional[Sequence[Entry]] = None,
    heads: Optional[Dict[str, str]] = None,
    root: Optional[os.PathLike] = None,
    log=print,
) -> dict:
    """Render every page into ``out_dir`` and publish a new manifest last.

    Page files are versioned (``p1-<signature>.png``) so a rebuild never serves
    a page that does not belong to the published manifest, and any failure
    aborts before the manifest is touched.
    """
    directory = Path(out_dir or default_out_dir())
    page_size = int(page_size or _env_page_size())
    template = template or os.environ.get("MEME_STATIC_LIST_TEXT_TEMPLATE") or DEFAULT_TEXT_TEMPLATE
    if category_icon is None:
        category_icon = _env_flag("MEME_STATIC_LIST_CATEGORY_ICON", True)

    if entries is None:
        entries = ordered_entries(root)
    entries = list(entries)
    if not entries:
        raise RuntimeError("no memes loaded; refusing to build an empty static list")

    heads = heads if heads is not None else repo_heads(root)
    signature = compute_signature(entries, page_size, template, category_icon, heads)

    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".static-list-staging-", dir=str(directory.parent)))

    pages: List[dict] = []
    try:
        for index in range(0, len(entries), page_size):
            page_number = index // page_size + 1
            chunk = entries[index:index + page_size]
            file_name = f"p{page_number}-{signature}.png"
            started = time.time()
            data = render_page([entry.key for entry in chunk], template, bool(category_icon))
            (staging / file_name).write_bytes(data)
            log(
                f"[static-list] rendered page {page_number} "
                f"({chunk[0].key}..{chunk[-1].key}, {len(data)} bytes, {time.time() - started:.1f}s)"
            )
            pages.append(
                {
                    "page": page_number,
                    "file": file_name,
                    "count": len(chunk),
                    "firstKey": chunk[0].key,
                    "lastKey": chunk[-1].key,
                    "keys": [entry.key for entry in chunk],
                }
            )

        manifest = {
            "schema": MANIFEST_SCHEMA,
            "version": signature,
            "signature": signature,
            "pageSize": page_size,
            "template": template,
            "categoryIcon": bool(category_icon),
            "total": len(entries),
            "pageCount": len(pages),
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "repos": heads,
            "pages": pages,
        }

        directory.mkdir(parents=True, exist_ok=True)
        for page in pages:
            shutil.move(str(staging / page["file"]), str(directory / page["file"]))
        _write_json_atomic(directory / MANIFEST_NAME, manifest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # Prune superseded page files, but keep recent ones around so a client that
    # is still holding the previous manifest is not left with a dangling URL.
    keep = {page["file"] for page in pages} | {MANIFEST_NAME}
    cutoff = time.time() - 15 * 60
    for stale in directory.iterdir():
        if stale.name in keep or not PAGE_FILE_RE.match(stale.name):
            continue
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass

    log(
        f"[static-list] published {len(pages)} page(s), {len(entries)} memes, "
        f"version {signature} -> {directory}"
    )
    return manifest


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pre-render the meme list pages served to the Yunzai #meme列表 plugin."
    )
    parser.add_argument("--out", default=str(default_out_dir()), help="output directory")
    parser.add_argument("--page-size", type=int, default=_env_page_size())
    parser.add_argument("--template", default=os.environ.get("MEME_STATIC_LIST_TEXT_TEMPLATE") or DEFAULT_TEXT_TEMPLATE)
    parser.add_argument("--no-category-icon", dest="category_icon", action="store_false", default=_env_flag("MEME_STATIC_LIST_CATEGORY_ICON", True))
    parser.add_argument("--source-root", default=None, help="image app root (default: MEME_APP_ROOT or /app)")
    parser.add_argument("--check", action="store_true", help="only report whether the manifest is up to date")
    parser.add_argument("--force", action="store_true", help="rebuild even when the manifest is up to date")
    args = parser.parse_args(argv)

    if args.check:
        prepare_config()
        load_all_memes(args.source_root)
        entries = ordered_entries(args.source_root)
        current = manifest_is_current(
            load_manifest(args.out),
            entries,
            args.page_size,
            args.template,
            args.category_icon,
            repo_heads(args.source_root),
            args.out,
        )
        print(f"[static-list] {'up-to-date' if current else 'stale'} ({len(entries)} memes)")
        return 0 if current else 3

    prepare_config()
    loaded = load_all_memes(args.source_root)
    print("[static-list] loaded meme dirs: " + ", ".join(str(item) for item in loaded))
    entries = ordered_entries(args.source_root)
    heads = repo_heads(args.source_root)
    if not args.force and manifest_is_current(
        load_manifest(args.out), entries, args.page_size, args.template, args.category_icon, heads, args.out
    ):
        print("[static-list] manifest already up to date; nothing to do")
        return 0
    build(
        out_dir=args.out,
        page_size=args.page_size,
        template=args.template,
        category_icon=args.category_icon,
        entries=entries,
        heads=heads,
        root=args.source_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
