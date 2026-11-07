#!/usr/bin/env python3
"""Rebuild the standalone NebulaSurfer index.html from the latest game data.

The workflow runs this script on a schedule. It reads the committed index.html,
extracts its embedded catalog, merges in the newest HerrErde/subway-source
release data, downloads only the images for newly added entries, and rewrites
index.html in place. Nothing else in the bundle is touched.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
DATA_DIR = Path(os.environ.get("LOCAL_DATA_DIR", "")) if os.environ.get("LOCAL_DATA_DIR") else None
API = "https://api.github.com/repos/HerrErde/subway-source/releases/latest"
FANDOM_API = "https://subwaysurf.fandom.com/api.php"
UA = "NebulaSurfer-AutoUpdater/1.0 (GitHub Actions)"

NEEDED = [
    "characters_data.json",
    "boards_data.json",
    "characters_links.json",
    "boards_links.json",
    "characters_outfit.json",
    "calendar_data.json",
    "citytours_data.json",
]


def log(*args):
    print(*args, flush=True)


def http_json(url, token=None):
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(url, headers=headers, timeout=45)
    r.raise_for_status()
    return r.json()


def http_bytes(url, attempts=3):
    headers = {"User-Agent": UA}
    last = None
    for attempt in range(attempts):
        try:
            r = requests.get(url, headers=headers, timeout=45)
            r.raise_for_status()
            if r.content:
                return r.content
        except Exception as exc:  # noqa: BLE001 - retry transient network failures
            last = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"download failed: {url} ({last})")


def load_release():
    """Return (version, season, assets dict)."""
    token = os.environ.get("GITHUB_TOKEN")
    if DATA_DIR:
        meta = json.loads((DATA_DIR / "release.json").read_text())
        assets = {name: json.loads((DATA_DIR / name).read_text()) for name in NEEDED}
    else:
        meta = http_json(API, token)
        urls = {a["name"]: a["browser_download_url"] for a in meta.get("assets", [])}
        missing = [name for name in NEEDED if name not in urls]
        if missing:
            raise RuntimeError(f"release is missing assets: {', '.join(missing)}")
        assets = {}
        for name in NEEDED:
            assets[name] = json.loads(http_bytes(urls[name]).decode("utf-8"))
    tag = meta.get("tag_name") or meta.get("name") or ""
    version = tag.replace("-", ".") if tag else ""
    if not version:
        raise RuntimeError("could not determine release version")

    season = None
    cal = assets.get("calendar_data.json", {}).get("calendarId", "")
    m = re.search(r"s(\d+)_", cal)
    if m:
        season = int(m.group(1))
    if season is None:
        gauntlets = assets.get("citytours_data.json", {}).get("gauntlets", [])
        for g in gauntlets:
            m = re.search(r"s(\d+)_", str(g.get("id", "")))
            if m:
                season = int(m.group(1))
                break
    if season is None:
        raise RuntimeError("could not determine season from release data")
    return version, season, assets


def fandom_character_images():
    """Best-effort map of wiki character name -> original image URL."""
    cache = Path(os.environ.get("FANDOM_CACHE", ""))
    if cache and cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    params = {
        "action": "parse",
        "page": "Characters",
        "prop": "text",
        "format": "json",
        "formatversion": "2",
    }
    try:
        r = requests.get(FANDOM_API, params=params, headers={"User-Agent": UA}, timeout=45)
        r.raise_for_status()
        text = r.json()["parse"]["text"]
    except Exception as exc:  # noqa: BLE001
        log(f"warning: Fandom character table unavailable: {exc}")
        return {}
    mapping = {}
    for row in re.findall(r"<tr>(.*?)</tr>", text, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 3:
            continue
        num = re.sub(r"<[^>]+>", "", tds[0]).strip()
        if not num.isdigit():
            continue
        imgcell = tds[1]
        m = re.search(
            r'href="(https://static\.wikia\.nocookie\.net/[^"]+?\.(?:png|jpg|jpeg|webp)[^"]*)"',
            imgcell,
            re.I,
        ) or re.search(
            r'data-src="(https://static\.wikia\.nocookie\.net/[^"]+?\.(?:png|jpg|jpeg|webp)[^"]*)"',
            imgcell,
            re.I,
        )
        if not m:
            continue
        url = m.group(1).replace("&amp;", "&").split("/revision/")[0]
        namecell = tds[2]
        n = re.search(r"<a[^>]*>(.*?)</a>", namecell, re.S)
        if n:
            name = re.sub(r"<[^>]+>", "", n.group(1))
        else:
            t = re.search(r'title="([^"]+)"', namecell)
            name = t.group(1) if t else ""
        name = (
            name.replace("&amp;", "&")
            .replace("&#39;", "'")
            .replace("&quot;", '"')
            .strip()
        )
        if name:
            mapping[name] = url
    if cache:
        try:
            cache.write_text(json.dumps(mapping, ensure_ascii=False))
        except Exception:
            pass
    return mapping


def norm(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def prefix_index(value, length):
    count = 0
    for i, ch in enumerate(value):
        if ch.isalnum():
            count += 1
            if count == length:
                return i + 1
    return len(value)


def camel_words(value):
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)


def derive_outfit_record(rid, outfit_by_name, existing):
    """Work out display name and source image for a standalone outfit entry."""
    if rid in existing:
        old = dict(existing[rid])
        old["kind"] = "outfit"
        return old
    stem = rid[:-6] if rid.endswith("Outfit") else rid
    ns = norm(stem)
    candidates = [name for name in outfit_by_name if ns.startswith(norm(name))]
    if not candidates:
        raise RuntimeError(f"cannot map outfit record {rid} to a base character")
    base = max(candidates, key=lambda name: len(norm(name)))
    idx = prefix_index(stem, len(norm(base)))
    remainder = stem[idx:]
    last = norm(base.split()[-1])
    nr = norm(remainder)
    if nr.startswith(last):
        remainder = remainder[prefix_index(remainder, len(last)):]
    elif len(base.split()) > 1 and nr.endswith(last):
        remainder = remainder[: prefix_index(remainder, len(nr) - len(last))]
    label = camel_words(remainder).strip()
    words = []
    for word in label.split():
        if word.lower() in {"and", "in", "of", "the"}:
            words.append(word.lower())
        else:
            words.append(word[:1].upper() + word[1:])
    label = " ".join(words)

    outfits = outfit_by_name[base]["outfits"]
    label_norm = norm(label)
    chosen = None
    for outfit in outfits:
        name = (outfit.get("name") or "").strip()
        if not name:
            continue
        clean = re.sub(r"\s*Outfit$", "", name, flags=re.I).strip()
        cn = norm(clean)
        if cn == label_norm or cn.startswith(label_norm) or label_norm.startswith(cn):
            label = clean
            chosen = outfit
            break
    if chosen is None:
        empties = [o for o in outfits if not (o.get("name") or "").strip()]
        chosen = empties[0] if empties else outfits[-1]
    source = chosen["url"].split("/revision/")[0]
    return {
        "id": rid,
        "name": f"{base} · {label}",
        "kind": "outfit",
        "base": base,
        "sourceImage": source,
        "image": "",
        "outfits": ["default"],
    }


def image_data_uri(url):
    variants = [
        url + "/revision/latest/scale-to-width-down/320",
        url,
        url.replace("https://static.wikia.nocookie.net", "https://vignette.wikia.nocookie.net"),
    ]
    last = None
    for variant in variants:
        try:
            raw = http_bytes(variant, attempts=2)
            im = Image.open(BytesIO(raw)).convert("RGBA")
            im.thumbnail((320, 320), Image.Resampling.LANCZOS)
            buf = BytesIO()
            im.save(buf, "WEBP", quality=82, method=6)
            return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            last = exc
    log(f"warning: image unavailable, using placeholder: {url} ({last})")
    im = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    buf = BytesIO()
    im.save(buf, "WEBP", quality=80)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def js_single_quoted(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def js_template_literal(value):
    return value.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def parse_bundle(html):
    prefix = 'const Ip="'
    start = html.find(prefix)
    if start < 0:
        raise RuntimeError("index.html is not a NebulaSurfer standalone bundle")
    version_end = html.find('"', start + len(prefix))
    version = html[start + len(prefix):version_end]
    metadata_start = version_end + 1
    season_match = re.match(r",Cp=(\d+),Np=JSON\.parse\('", html[metadata_start:])
    if not season_match:
        raise RuntimeError("could not locate catalog metadata in index.html")
    season = int(season_match.group(1))
    char_start = metadata_start + season_match.end()
    char_marker = "'),Op=JSON.parse(`"
    char_end = html.find(char_marker, char_start)
    if char_end < 0:
        raise RuntimeError("could not locate end of character catalog")
    char_js = html[char_start:char_end]
    board_start = char_end + len(char_marker)
    board_marker = "`),Te={version:Ip"
    board_end = html.find(board_marker, board_start)
    if board_end < 0:
        raise RuntimeError("could not locate end of board catalog")
    board_js = html[board_start:board_end]
    characters = json.loads(char_js.replace("\\'", "'").replace("\\\\", "\\"))
    boards = json.loads(board_js.replace("\\`", "`").replace("\\${", "${").replace("\\\\", "\\"))
    return {
        "version": version,
        "season": season,
        "characters": characters,
        "boards": boards,
        "spans": (start, version_end, season_match, char_start, char_end, board_start, board_end, board_marker),
    }


def build_catalog(assets, old, fandom_map):
    char_data = assets["characters_data.json"]
    board_data = assets["boards_data.json"]
    char_links = assets["characters_links.json"]
    board_links = assets["boards_links.json"]
    outfit_data = assets["characters_outfit.json"]
    outfit_by_name = {x["name"]: x for x in outfit_data}

    old_chars = {x["id"]: x for x in old["characters"]}
    old_boards = {x["id"]: x for x in old["boards"]}
    char_link_by_name = {x["name"]: x for x in char_links}
    board_link_by_name = {x["name"].replace(" (Hoverboard)", ""): x for x in board_links}
    used_char_links = set()
    used_board_links = set()

    def first_unused(mapping, used):
        return next((link for name, link in mapping.items() if name not in used), None)

    main_records = [x for x in char_data if not x["id"].endswith("Outfit")]
    outfit_records = [x for x in char_data if x["id"].endswith("Outfit")]
    if len(main_records) != len(char_links):
        log(f"warning: {len(main_records)} main characters vs {len(char_links)} wiki links")

    characters = []
    for rec in main_records:
        rid = rec["id"]
        existing = old_chars.get(rid, {})
        name = existing.get("name")
        link = char_link_by_name.get(name) if name else None
        if link:
            used_char_links.add(name)
        else:
            link = first_unused(char_link_by_name, used_char_links)
            if link:
                used_char_links.add(link["name"])
        if link:
            name = link["name"]
            fallback_url = link.get("img_url", "")
        else:
            name = name or re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", rid).title()
            fallback_url = ""
        source = existing.get("sourceImage") or fandom_map.get(name) or fallback_url
        characters.append({
            "id": rid,
            "name": name,
            "number": rec["number"],
            "kind": "surfer",
            "image": existing.get("image", ""),
            "sourceImage": source,
            "outfits": [o["id"] for o in (rec.get("outfits") or [{"id": "default"}])],
        })
    for rec in outfit_records:
        entry = derive_outfit_record(rec["id"], outfit_by_name, old_chars)
        entry["number"] = rec["number"]
        entry["outfits"] = [o["id"] for o in (rec.get("outfits") or [{"id": "default"}])]
        characters.append(entry)
    characters.sort(key=lambda x: x["number"])

    if len(board_links) != len(board_data):
        log(f"warning: {len(board_links)} board links vs {len(board_data)} board records")
    boards = []
    for rec in board_data:
        rid = rec["id"]
        existing = old_boards.get(rid, {})
        name = existing.get("name")
        link = board_link_by_name.get(name) if name else None
        if not link and name:
            target = norm(name)
            link = next((l for n, l in board_link_by_name.items() if norm(n) == target and n not in used_board_links), None)
        if link:
            used_board_links.add(link["name"].replace(" (Hoverboard)", ""))
        else:
            link = first_unused(board_link_by_name, used_board_links)
            if link:
                used_board_links.add(link["name"].replace(" (Hoverboard)", ""))
        if link:
            name = link["name"].replace(" (Hoverboard)", "")
            fallback_url = link.get("img_url", "")
        else:
            name = name or re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", rid).title()
            fallback_url = ""
        source = existing.get("sourceImage") or fallback_url
        boards.append({
            "id": rid,
            "name": name,
            "number": rec["number"],
            "image": existing.get("image", ""),
            "sourceImage": source,
            "upgrades": [o["id"] for o in (rec.get("upgrades") or [])],
        })
    boards.sort(key=lambda x: x["number"])

    # Fetch artwork only for entries that are new to this bundle.
    for entry in characters + boards:
        if not entry.get("image"):
            entry["image"] = image_data_uri(entry.get("sourceImage") or "https://example.invalid/blank.png")
    return characters, boards


def rebuild_html(html, parsed, version, season, characters, boards):
    start, version_end, season_match, char_start, char_end, board_start, board_end, board_marker = parsed["spans"]
    char_json = json.dumps(characters, separators=(",", ":"), ensure_ascii=False)
    board_json = json.dumps(boards, separators=(",", ":"), ensure_ascii=False)
    char_js = js_single_quoted(char_json)
    board_js = js_template_literal(board_json)
    head = f'const Ip="{version}",Cp={season},Np=JSON.parse(\''
    middle = "'),Op=JSON.parse(`"
    tail = board_marker
    return html[:start] + head + char_js + middle + board_js + tail + html[board_end + len(board_marker):]


def set_output(version, changed):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"version={version}\n")
        f.write(f"changed={'true' if changed else 'false'}\n")


def main():
    html = INDEX.read_text(encoding="utf-8")
    parsed = parse_bundle(html)
    version, season, assets = load_release()
    fandom_map = fandom_character_images()
    characters, boards = build_catalog(assets, parsed, fandom_map)

    old_cmp = {
        "version": parsed["version"],
        "season": parsed["season"],
        "characters": parsed["characters"],
        "boards": parsed["boards"],
    }
    new_cmp = {
        "version": version,
        "season": season,
        "characters": [{k: v for k, v in x.items() if k != "image"} for x in characters],
        "boards": [{k: v for k, v in x.items() if k != "image"} for x in boards],
    }
    # Compare metadata only; image data URIs are allowed to stay byte-identical.
    old_meta = {
        "version": old_cmp["version"],
        "season": old_cmp["season"],
        "characters": [{k: v for k, v in x.items() if k != "image"} for x in old_cmp["characters"]],
        "boards": [{k: v for k, v in x.items() if k != "image"} for x in old_cmp["boards"]],
    }
    if old_meta == new_cmp:
        log(f"No catalog changes. Current version {version}, {len(characters)} characters, {len(boards)} boards.")
        set_output(version, False)
        return 0

    rebuilt = rebuild_html(html, parsed, version, season, characters, boards)
    if rebuilt == html:
        log("Catalog metadata changed but rebuilt HTML is identical; nothing to commit.")
        set_output(version, False)
        return 0
    INDEX.write_text(rebuilt, encoding="utf-8")
    log(f"Updated index.html to {version} (season {season}): {len(characters)} characters, {len(boards)} boards.")
    set_output(version, True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: {exc}")
        sys.exit(1)
