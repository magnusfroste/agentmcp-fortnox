"""Index Fortnox documents under ROOT into manifest.jsonl, one line per file.

  python3 index.py [root] [--refresh]       default root /data

Two sources share the folder layout <source>/<year>/<month>/<category>/<file>:
  fortnox-api/      written by fortnox_api.py, each PDF with a <file>.json of Fortnox's own fields
  fortnox-export/   optional: an export from Fortnox put there by hand (empty or absent is fine)

Re-runnable: a file already indexed with the same sha256 is skipped. --refresh rebuilds every
line (to pick up new fields) without extracting text again. Text goes to .text/<sha256>.txt.

Fields per file: file, source, category (leverantorsfaktura / kundfaktura / verifikation), month,
date, fortnox_nr, filename, sha256, bytes, has_text, text, vat_numbers, counterpart_vat,
counterpart_name, org_numbers, words, and from the API sidecar (when there is one) amount, currency,
fortnox. Optional, from the environment:
  INDEX_REFERENCES   prefixes of order numbers to pick out, e.g. "SO,PO,WO": each becomes a field
                     (so, po, wo) with the numbers found ("SO12345")
  INDEX_WORDS        words to note when a document contains them, e.g. "lager,pall,frakt"
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata

VERSION = 2  # bump when describe() gains fields, and the index is rebuilt on start
CATEGORIES = {"leverantörsfakturor": "leverantorsfaktura", "kundfakturor": "kundfaktura",
              "verifikationer": "verifikation"}
WORDS = [w.strip().lower() for w in os.environ.get("INDEX_WORDS", "").split(",") if w.strip()]
REFERENCES = [p.strip().upper() for p in os.environ.get("INDEX_REFERENCES", "").split(",")
              if re.fullmatch(r"[A-Za-z]{1,6}", p.strip())]
DATE_LABEL = re.compile(r"(fakturadatum|fakturadat\.?|invoice date|datum)\s*:?\s*", re.I)
DATE = re.compile(r"\b(20\d\d)[-./](0[1-9]|1[0-2])[-./](0[1-9]|[12]\d|3[01])\b")


def nfc(s):
    return unicodedata.normalize("NFC", s)


def text_of(root, path, digest):
    folder = os.path.join(root, ".text")
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, digest + ".txt")
    if not os.path.exists(out):
        try:
            r = subprocess.run(["pdftotext", "-layout", path, out + ".part"], capture_output=True)
        except FileNotFoundError:  # poppler-utils missing: the document is listed, without text
            r = None
        if r is None or r.returncode != 0:
            open(out + ".part", "w").close()
        os.replace(out + ".part", out)
    with open(out, errors="replace") as f:
        return f.read()


SUFFIX = r"(AB|Aktiebolag|HB|KB|UAB|GmbH|A/S|AS|Oy|Ltd|BV|S\.?L\.?U?|S\.?A\.?|sp\. z o\.o\.)"
COMPANY = re.compile(rf"^[A-ZÅÄÖ0-9][\w&.,'’ /-]{{0,55}}?\b{SUFFIX}\.?(\s*\(publ\))?$")
NOT_A_NAME = re.compile(r"(account|konto|bankgiro|plusgiro|iban|swift|bic|moms|vat|org\.?\s*nr|tel|e-?post|www\.|@|"
                        r"godkänd|f-skatt|\bbank\b|swedbank|nordea|handelsbanken|seb\b|luminor|citadele)", re.I)
INLINE = re.compile(rf"([A-ZÅÄÖ][\w&.'’-]*(?: [\w&.'’-]+){{0,5}},? {SUFFIX})\b")


def _ours(name, own_name):
    """Whether a name is our own company's: its first word, as invoices write it ("Exempel Design AB")."""
    first = (own_name or "").split()[0].lower() if own_name else ""
    return len(first) >= 3 and first in name.lower()


def _company_columns(line, own_name=None):
    for col in re.split(r"\s{3,}", line.strip()):
        col = col.strip(" :;,")
        if (COMPANY.match(col) and not NOT_A_NAME.search(col) and not _ours(col, own_name)
                and sum(ch.isdigit() for ch in col) <= 4):
            yield col[:60]


def counterpart_name(text, vat, own_name=None):
    """A column that is a company name (ends in AB, UAB, GmbH ...): first on the line holding the
    counterpart's VAT number, then near the top of the page, then anywhere."""
    lines = text.splitlines()
    if vat:
        for i, line in enumerate(lines):
            if vat in line.replace(" ", ""):
                for near in [line] + lines[max(0, i - 3):i] + lines[i + 1:i + 2]:
                    col = next(_company_columns(near, own_name), None)
                    if col:
                        return col
    for line in lines:
        col = next(_company_columns(line, own_name), None)
        if col:
            return col
    for m in INLINE.finditer(text):  # last resort: a company name inside running text ("Ellevio AB (publ):s ...")
        if not NOT_A_NAME.search(m.group(1)) and not _ours(m.group(1), own_name):
            return m.group(1)[:60]
    return None


def invoice_date(text):
    """The date after a 'Fakturadatum' style label, else the first ISO-like date in the text."""
    for m in DATE_LABEL.finditer(text):
        d = DATE.search(text[m.end():m.end() + 120])
        if d:
            return "-".join(d.groups())
    d = DATE.search(text)
    return "-".join(d.groups()) if d else None


def describe(root, path, data, digest, own=None):
    """own: the company whose books these are ({"own_vat", "company"}), so it is never the counterpart."""
    own = own or {}
    rel = nfc(os.path.relpath(path, root))
    parts = rel.split("/")
    name = parts[-1]
    category = next((CATEGORIES[p.lower()] for p in parts if p.lower() in CATEGORIES), None)
    month = next((f"{parts[i]}-{parts[i + 1]}" for i in range(len(parts) - 1)
                  if re.fullmatch(r"20\d\d", parts[i]) and re.fullmatch(r"\d\d", parts[i + 1])), None)
    m = re.match(r"(\d+)_", name)
    entry = {"v": VERSION, "file": rel, "source": parts[0] if len(parts) > 1 else None, "category": category, "month": month,
             "fortnox_nr": m.group(1) if m else None, "filename": name, "sha256": digest, "bytes": len(data)}
    if name.lower().endswith(".pdf"):
        text = text_of(root, path, digest)
        flat = text.replace(" ", "")
        vats = list(dict.fromkeys(re.findall(r"SE\d{10}01", flat)))
        cvat = next((v for v in vats if v != own.get("own_vat")), None)
        low = text.lower()
        entry.update({
            "has_text": len("".join(text.split())) > 50,
            "text": f".text/{digest}.txt",
            "vat_numbers": vats,
            "counterpart_vat": cvat,
            "counterpart_name": counterpart_name(text, cvat, own.get("company")),
            "date": invoice_date(text),
            "org_numbers": list(dict.fromkeys(re.findall(r"\b\d{6}-\d{4}\b", text))),
            "words": [w for w in WORDS if w in low],
        })
        for prefix in REFERENCES:
            entry[prefix.lower()] = sorted(set(re.findall(rf"\b{prefix}\d{{4,6}}\b", text)))
    else:
        entry["has_text"] = False
    side = path + ".json"
    if os.path.exists(side):  # Fortnox's own fields beat anything read out of the PDF
        with open(side) as f:
            meta = json.load(f)
        entry.update({k: v for k, v in {
            "date": meta.get("date"), "counterpart_name": meta.get("counterpart_name"),
            "counterpart_vat": meta.get("counterpart_vat") or entry.get("counterpart_vat"),
            "amount": meta.get("amount"), "currency": meta.get("currency"),
            "fortnox_nr": meta.get("fortnox_nr") or entry.get("fortnox_nr"), "fortnox": meta.get("fortnox"),
            "verifikation": meta.get("verifikation")}.items()
            if v is not None})
    return entry


def build(root="/data", refresh=False, changed=(), own=None):
    """Index what is new. changed: files (relative to root) whose PDF or .json was rewritten in
    place -- their old entries go and they are described again. own: see describe()."""
    manifest = os.path.join(root, "manifest.jsonl")
    done, kept = set(), []
    changed = {nfc(c) for c in changed}
    if os.path.exists(manifest) and not refresh:
        with open(manifest) as f:
            for l in f:
                if l.strip() and json.loads(l).get("file") not in changed:
                    kept.append(l)
                    done.add(json.loads(l)["sha256"])
    rewrite = refresh or bool(changed)
    lines, added = [], 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith(("manifest.", ".")) or name.endswith((".json", ".part")):
                continue
            path = os.path.join(dirpath, name)
            with open(path, "rb") as f:
                data = f.read()
            digest = hashlib.sha256(data).hexdigest()
            if digest in done:
                continue
            lines.append(json.dumps(describe(root, path, data, digest, own), ensure_ascii=False) + "\n")
            done.add(digest)
            added += 1
    if rewrite:
        tmp = manifest + ".part"
        with open(tmp, "w") as f:
            f.writelines(([] if refresh else kept) + lines)
        os.replace(tmp, manifest)
    elif lines:
        with open(manifest, "a") as f:
            f.writelines(lines)
    return added, len(done)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    added, total = build(args[0] if args else "/data", refresh="--refresh" in sys.argv)
    print(f"{added} file(s) {'indexed' if '--refresh' in sys.argv else 'added'} ({total} in total)")
