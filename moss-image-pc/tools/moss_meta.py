"""Read and write picture metadata (title, comment, authors, tags...) for MOSS images.

Written so File Explorer's Properties > Details shows it, and so other photo apps can read it:
  PNG : XMP packet (what Windows reads for PNG) + EXIF (eXIf chunk) + PNG text chunks.
  JPEG: EXIF, including the Windows XP* fields (full Unicode), + XMP. Editing a JPEG's
        metadata swaps only the metadata segments, so the picture is never recompressed.

Fields (all strings except tags, a list of strings):
  title, subject, comment (the prompt by default), authors, copyright, tags,
  software, date_taken ("YYYY:MM:DD HH:MM:SS"), settings (one line of generation settings)
"""
import re
import struct
import unicodedata
from html import escape, unescape
from pathlib import Path

from PIL import Image, PngImagePlugin

FIELDS = ("title", "subject", "comment", "authors", "copyright", "tags",
          "software", "date_taken", "settings")

# EXIF tag numbers
DESCRIPTION, ARTIST, COPYRIGHT, SOFTWARE, DATETIME = 0x010E, 0x013B, 0x8298, 0x0131, 0x0132
XP_TITLE, XP_COMMENT, XP_AUTHOR, XP_KEYWORDS, XP_SUBJECT = 0x9C9B, 0x9C9C, 0x9C9D, 0x9C9E, 0x9C9F
EXIF_IFD, DATETIME_ORIGINAL, USER_COMMENT = 0x8769, 0x9003, 0x9286

XMP_KEY = "XML:com.adobe.xmp"
XMP_JPEG_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
PNG_TEXT_KEYS = {"title": "Title", "authors": "Author", "comment": "Comment",
                 "copyright": "Copyright", "software": "Software", "subject": "Subject",
                 "settings": "MOSS-Settings"}


def empty():
    return {f: ([] if f == "tags" else "") for f in FIELDS}


def to_ascii(text):
    """Plain EXIF text fields only hold ASCII: turn é into e, — into -, etc."""
    text = (text or "").replace("—", "-").replace("–", "-").replace("’", "'") \
        .replace("‘", "'").replace("“", '"').replace("”", '"').replace("…", "...") \
        .replace("©", "(c)").replace("®", "(R)").replace("™", "(TM)")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text


def _xp(text):
    return (text or "").encode("utf-16-le") + b"\x00\x00"


def _from_xp(value):
    if isinstance(value, tuple):
        value = bytes(value)
    if isinstance(value, bytes):
        return value.decode("utf-16-le", "ignore").rstrip("\x00")
    return str(value or "")


def _from_user_comment(value):
    if not isinstance(value, bytes) or len(value) < 8:
        return str(value or "")
    head, body = value[:8], value[8:]
    if head.startswith(b"UNICODE"):
        return body.decode("utf-16-le", "ignore").rstrip("\x00")
    return body.decode("utf-8", "ignore").rstrip("\x00")


def split_tags(text):
    return [t.strip() for t in re.split(r"[;,]", text or "") if t.strip()]


# ---------- building ----------

def build_exif(meta, base=None):
    ex = base if base is not None else Image.Exif()
    title = meta.get("title", "")
    ex[DESCRIPTION] = to_ascii(title)      # Windows shows this as Title
    ex[XP_TITLE] = _xp(title)
    ex[XP_COMMENT] = _xp(meta.get("comment", ""))
    ex[XP_SUBJECT] = _xp(meta.get("subject", ""))
    ex[XP_KEYWORDS] = _xp("; ".join(meta.get("tags") or []))
    ex[XP_AUTHOR] = _xp(meta.get("authors", ""))
    ex[ARTIST] = to_ascii(meta.get("authors", ""))
    ex[COPYRIGHT] = to_ascii(meta.get("copyright", ""))
    ex[SOFTWARE] = to_ascii(meta.get("software", ""))
    if meta.get("date_taken"):
        ex[DATETIME] = meta["date_taken"]
    sub = ex.get_ifd(EXIF_IFD)
    sub[USER_COMMENT] = b"UNICODE\x00" + (meta.get("comment") or "").encode("utf-16-le")
    if meta.get("date_taken"):
        sub[DATETIME_ORIGINAL] = meta["date_taken"]
    return ex


def build_xmp(meta):
    def alt(tag, value):
        return f"<{tag}><rdf:Alt><rdf:li xml:lang=\"x-default\">{escape(value)}</rdf:li></rdf:Alt></{tag}>" if value else ""
    tags = "".join(f"<rdf:li>{escape(t)}</rdf:li>" for t in meta.get("tags") or [])
    date = meta.get("date_taken", "")
    iso = re.sub(r"^(\d{4}):(\d{2}):(\d{2}) ", r"\1-\2-\3T", date) if date else ""
    parts = [
        alt("dc:title", meta.get("title", "")),
        alt("dc:description", meta.get("subject", "")),
        alt("exif:UserComment", meta.get("comment", "")),
        f"<dc:creator><rdf:Seq><rdf:li>{escape(meta['authors'])}</rdf:li></rdf:Seq></dc:creator>" if meta.get("authors") else "",
        alt("dc:rights", meta.get("copyright", "")),
        f"<dc:subject><rdf:Bag>{tags}</rdf:Bag></dc:subject>" if tags else "",
        f"<xmp:CreatorTool>{escape(meta['software'])}</xmp:CreatorTool>" if meta.get("software") else "",
        f"<xmp:CreateDate>{iso}</xmp:CreateDate><exif:DateTimeOriginal>{iso}</exif:DateTimeOriginal>" if iso else "",
        f"<moss:Settings>{escape(meta['settings'])}</moss:Settings>" if meta.get("settings") else "",
    ]
    return ("<?xpacket begin=\"﻿\" id=\"W5M0MpCehiHzreSzNTczkc9d\"?>"
            "<x:xmpmeta xmlns:x=\"adobe:ns:meta/\"><rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\">"
            "<rdf:Description rdf:about=\"\" xmlns:dc=\"http://purl.org/dc/elements/1.1/\""
            " xmlns:xmp=\"http://ns.adobe.com/xap/1.0/\" xmlns:exif=\"http://ns.adobe.com/exif/1.0/\""
            " xmlns:moss=\"https://github.com/OpenMOSS/MOSS-Image/ns/1.0/\">"
            + "".join(parts) +
            "</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end=\"w\"?>")


# ---------- reading ----------

def _xmp_value(xmp, tag):
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", xmp, re.S)
    if not m:
        return ""
    inner = m.group(1)
    items = re.findall(r"<rdf:li\b[^>]*>(.*?)</rdf:li>", inner, re.S)
    return unescape(items[0] if items else inner).strip()


def _xmp_list(xmp, tag):
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", xmp, re.S)
    return [unescape(x).strip() for x in re.findall(r"<rdf:li\b[^>]*>(.*?)</rdf:li>", m.group(1), re.S)] if m else []


def read(path):
    meta = empty()
    with Image.open(path) as im:
        info = dict(im.info)
        ex = im.getexif()
    sub = ex.get_ifd(EXIF_IFD)
    xmp = info.get("xmp") or info.get(XMP_KEY) or ""
    if isinstance(xmp, bytes):
        xmp = xmp.decode("utf-8", "ignore")

    # Lowest priority first, so later sources win: PNG text < plain EXIF < XMP < Windows XP fields.
    for field, key in PNG_TEXT_KEYS.items():
        if isinstance(info.get(key), str):
            meta[field] = info[key]
    if ex.get(DESCRIPTION): meta["title"] = str(ex[DESCRIPTION])
    if ex.get(ARTIST): meta["authors"] = str(ex[ARTIST])
    if ex.get(COPYRIGHT): meta["copyright"] = str(ex[COPYRIGHT])
    if ex.get(SOFTWARE): meta["software"] = str(ex[SOFTWARE])
    if sub.get(USER_COMMENT): meta["comment"] = _from_user_comment(sub[USER_COMMENT])
    meta["date_taken"] = str(sub.get(DATETIME_ORIGINAL) or ex.get(DATETIME) or "")
    if xmp:
        for field, tag in (("title", "dc:title"), ("subject", "dc:description"), ("comment", "exif:UserComment"),
                           ("copyright", "dc:rights"), ("software", "xmp:CreatorTool"), ("settings", "moss:Settings")):
            value = _xmp_value(xmp, tag)
            if value:
                meta[field] = value
        creators = _xmp_list(xmp, "dc:creator")
        if creators: meta["authors"] = "; ".join(creators)
        tags = _xmp_list(xmp, "dc:subject")
        if tags: meta["tags"] = tags
    for field, tag in (("title", XP_TITLE), ("comment", XP_COMMENT), ("subject", XP_SUBJECT), ("authors", XP_AUTHOR)):
        if ex.get(tag):
            meta[field] = _from_xp(ex[tag])
    if ex.get(XP_KEYWORDS):
        meta["tags"] = split_tags(_from_xp(ex[XP_KEYWORDS]))
    return meta


# ---------- writing ----------

def _jpeg_segments(data):
    """Split a JPEG into (marker, payload) segments up to the image data."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG file")
    pos, segs = 2, []
    while pos < len(data):
        if data[pos] != 0xFF:
            raise ValueError("unexpected JPEG layout")
        marker = data[pos + 1]
        if marker == 0xDA:  # start of scan: the rest is image data
            return segs, data[pos:]
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        segs.append((marker, data[pos + 4:pos + 2 + length]))
        pos += 2 + length
    raise ValueError("no image data found")


def _write_jpeg_metadata(path, exif_bytes, xmp_text):
    data = Path(path).read_bytes()
    segs, rest = _jpeg_segments(data)
    keep = [(m, p) for m, p in segs
            if not (m == 0xE1 and (p.startswith(b"Exif\x00\x00") or p.startswith(XMP_JPEG_HEADER)))]
    # Put the new EXIF and XMP right after JFIF (APP0), where readers expect them.
    insert_at = 1 if keep and keep[0][0] == 0xE0 else 0
    new = [(0xE1, exif_bytes), (0xE1, XMP_JPEG_HEADER + xmp_text.encode("utf-8"))]
    out = bytearray(b"\xff\xd8")
    for marker, payload in keep[:insert_at] + new + keep[insert_at:]:
        if len(payload) + 2 > 0xFFFF:
            raise ValueError("metadata too large for a JPEG segment")
        out += bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload
    out += rest
    tmp = Path(str(path) + ".tmp")
    tmp.write_bytes(out)
    tmp.replace(path)


def write(path, meta, source=None, jpeg_quality=95):
    """Write metadata to path. If source is given, the picture is taken from source
    (e.g. sd-cli's PNG) and saved as path's format; otherwise path is edited in place."""
    path = Path(path)
    fmt = "JPEG" if path.suffix.lower() in (".jpg", ".jpeg") else "PNG"
    full = empty()
    full.update({k: v for k, v in meta.items() if k in FIELDS})
    xmp = build_xmp(full)

    if fmt == "JPEG" and source is None:
        with Image.open(path) as im:
            exif = build_exif(full, im.getexif())
        _write_jpeg_metadata(path, exif.tobytes(), xmp)
        return

    with Image.open(source or path) as im:
        im.load()
        info = dict(im.info)
        exif = build_exif(full, im.getexif())
        pixels = im.copy()
    tmp = path.with_name(path.name + ".tmp")
    if fmt == "JPEG":
        pixels.convert("RGB").save(tmp, "JPEG", quality=jpeg_quality, exif=exif.tobytes())
        tmp.replace(path)
        _write_jpeg_metadata(path, exif.tobytes(), xmp)
        return
    png = PngImagePlugin.PngInfo()
    ours = set(PNG_TEXT_KEYS.values()) | {XMP_KEY, "Description", "Keywords"}
    for key, value in info.items():  # keep other text, e.g. sd-cli's own "parameters"
        if isinstance(value, str) and key not in ours:
            png.add_itxt(key, value)
    for field, key in PNG_TEXT_KEYS.items():
        if full.get(field):
            png.add_itxt(key, full[field])
    if full.get("tags"):
        png.add_itxt("Keywords", "; ".join(full["tags"]))
    png.add_itxt(XMP_KEY, xmp)
    pixels.save(tmp, "PNG", pnginfo=png, exif=exif.tobytes())
    tmp.replace(path)
