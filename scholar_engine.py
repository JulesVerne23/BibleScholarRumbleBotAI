# =============================================================================
# scholar_engine.py
# BibleScholar — core "abilities" engine
#
# Contains: NET Bible, Strong's, Heiser knowledge base, Zotero, book
# normalization, verse parsing, text sanitization, and all Ollama calls
# for the Scholar UI.  No Tkinter, no Selenium — pure logic only.
#
# Imported by BibleScholarRumble64.py (UI) and rumble_engine.py (bot).
# =============================================================================

import re
import json
import os
import time
import datetime
import threading
import urllib.parse
import requests

# ── Optional dotenv ──────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =============================================================================
# CONFIGURATION — Scholar UI
# =============================================================================
SCHOLAR_MODEL = "RumbleBot"          # Ollama model for the Scholar UI chat
OLLAMA_URL    = "http://localhost:11434/api/chat"

# ── Zotero ───────────────────────────────────────────────────────────────────
ZOTERO_GROUP_ID  = "4835417"
ZOTERO_API_BASE  = f"https://api.zotero.org/groups/{ZOTERO_GROUP_ID}"
ZOTERO_MAX_RESULTS = 5
ZOTERO_CACHE_TTL   = 300            # seconds before a cached result expires

# ── Conversation history (UI chat) ───────────────────────────────────────────
MAX_HISTORY_TURNS  = 20
conversation_history: list[dict] = []

# ── Shared Ollama lock ───────────────────────────────────────────────────────
# The Scholar UI streaming call acquires this.  The Rumble engine uses its
# OWN independent lock so the two never block each other.
scholar_ollama_lock = threading.Lock()

# =============================================================================
# ACTIVITY LOG
# =============================================================================
ACTIVITY_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rumble_activity.log")

_INJECTION_PATTERNS = [
    "ignore previous", "ignore your", "ignore all", "disregard",
    "new instructions", "system prompt", "you are now", "pretend you",
    "act as", "jailbreak", "dan mode", "<tool_call>", "tool_call",
    "{{", "}}", "<|im_start|>", "<|im_end|>", "<|system|>", "override",
    "forget everything", "your real instructions",
    "assistant:", "system:", "role:", "new role",
    "ignore the above", "ignore all previous",
    "disregard the above", "disregard all previous",
]

def write_activity_log(event_type: str, author: str, content: str, response: str = "") -> bool:
    """
    Append a timestamped entry to the activity log.
    Returns True if the message looks like a prompt-injection attempt.
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lower = content.lower()
    suspicious = any(p in lower for p in _INJECTION_PATTERNS)
    if suspicious and event_type == "MENTION":
        event_type = "SUSPICIOUS"

    line = (
        f"[{timestamp}] [{event_type}]\n"
        f"  FROM    : {author}\n"
        f"  MESSAGE : {content}\n"
    )
    if response:
        line += f"  RESPONSE: {response}\n"
    if suspicious:
        line += "  *** POSSIBLE PROMPT INJECTION DETECTED ***\n"
    line += "-" * 60 + "\n"

    try:
        with open(ACTIVITY_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"[Warning] Could not write activity log: {e}", flush=True)

    return suspicious


# =============================================================================
# NET BIBLE JSON
# =============================================================================
NET_LOOKUP: dict = {}
_net_loaded  = threading.Event()   # UI can wait on this before verse lookups

def load_net_json(path="net_structured.json"):
    global NET_LOOKUP
    base_dir  = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(base_dir, path)
    if not os.path.exists(json_path):
        print(f"[Warning] net_structured.json not found at {json_path}.")
        _net_loaded.set()
        return
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            NET_LOOKUP = json.load(f)
        print(f"[Info] NET Bible loaded: {len(NET_LOOKUP)} books.")
    except Exception as e:
        print(f"[Warning] Could not load net_structured.json: {e}")
    finally:
        _net_loaded.set()


# =============================================================================
# STRONG'S JSON
# =============================================================================
STRONGS_LOOKUP: dict = {}
_strongs_loaded = threading.Event()

def normalize_strongs_key(raw_key: str) -> str:
    raw_key = str(raw_key).strip().upper()
    if raw_key.startswith(("H", "G")):
        prefix, number = raw_key[0], raw_key[1:]
    else:
        prefix, number = "H", raw_key
    number = number.lstrip("0") or "0"
    return f"{prefix}{number}"

def load_strongs_json(path="strongs.json"):
    global STRONGS_LOOKUP
    base_dir  = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(base_dir, path)
    if not os.path.exists(json_path):
        print(f"[Warning] strongs.json not found at {json_path}")
        _strongs_loaded.set()
        return
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            STRONGS_LOOKUP = json.load(f)
        print(f"[Info] Strong's loaded: {len(STRONGS_LOOKUP)} entries.")
    except Exception as e:
        print(f"[Warning] Could not load strongs.json: {e}")
    finally:
        _strongs_loaded.set()

def lookup_strongs(key_or_number):
    return STRONGS_LOOKUP.get(normalize_strongs_key(key_or_number))


# =============================================================================
# HEISER KNOWLEDGE BASE
# =============================================================================
HEISER_KNOWLEDGE: list = []
_heiser_loaded = threading.Event()

def load_heiser_knowledge(paths=None):
    global HEISER_KNOWLEDGE
    if paths is None:
        paths = [
            "BibleScholar_Knowledge_Demons_Unclean_Spirits.json",
            "Jesus_and_the_Gates_of_Hell.json",
        ]
    base_dir   = os.path.dirname(os.path.abspath(__file__))
    all_entries: list = []
    for path in (paths if isinstance(paths, (list, tuple)) else [paths]):
        json_path = os.path.join(base_dir, path)
        if not os.path.exists(json_path):
            print(f"[Warning] Heiser file not found: {path}")
            continue
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data if isinstance(data, list) else [data]
            all_entries.extend(entries)
            print(f"[Info] Heiser: loaded {len(entries)} entries from {path}")
        except Exception as e:
            print(f"[Error] Failed to load {path}: {e}")
    HEISER_KNOWLEDGE = all_entries
    print(f"[Info] Heiser total: {len(HEISER_KNOWLEDGE)} entries.")
    _heiser_loaded.set()

def search_heiser_knowledge(query: str, max_results: int = 3) -> str:
    if not HEISER_KNOWLEDGE:
        return ""
    query_words = set(re.findall(r'\b\w{4,}\b', query.lower()))
    scored = []
    for entry in HEISER_KNOWLEDGE:
        haystack = " ".join([
            entry.get("title", ""), entry.get("summary", ""),
            entry.get("embedding_text", ""),
            " ".join(entry.get("tags", [])),
        ]).lower()
        score = sum(1 for w in query_words if w in haystack)
        score += sum(2 for w in query_words if w in [t.lower() for t in entry.get("tags", [])])
        if score > 0:
            scored.append((score, entry))
    if not scored:
        return ""
    scored.sort(key=lambda x: x[0], reverse=True)
    lines = ["[HEISER KNOWLEDGE BASE — relevant entries found]"]
    for _, entry in scored[:max_results]:
        title   = entry.get("title", "Untitled")
        chapter = entry.get("chapter", "")
        detail  = (entry.get("embedding_text") or entry.get("summary", "")).strip()
        if len(detail) > 500:
            detail = detail[:500] + "…"
        source = entry.get("source", "")
        header = f"• {title}" + (f" (ch. {chapter})" if chapter else "")
        lines.append(header)
        if detail:
            lines.append(f"  {detail}")
        if source:
            lines.append(f"  Source: {source}")
    lines.append("[End of Heiser knowledge]")
    return "\n".join(lines)


# =============================================================================
# LAZY LOADER  — call once at startup; resources load in background threads
# =============================================================================
def start_background_loading(status_callback=None):
    """
    Load NET Bible, Strong's, and Heiser knowledge in parallel background
    threads.  status_callback(msg) is called on each milestone so the UI
    can update a status bar.  Safe to call from the main thread before
    root.mainloop().
    """
    def _load(fn, label):
        fn()
        if status_callback:
            status_callback(f"✓ {label} ready")

    threading.Thread(target=_load, args=(load_net_json,          "NET Bible"),   daemon=True).start()
    threading.Thread(target=_load, args=(load_strongs_json,      "Strong's"),    daemon=True).start()
    threading.Thread(target=_load, args=(load_heiser_knowledge,  "Heiser KB"),   daemon=True).start()


# =============================================================================
# BOOK NORMALIZATION
# =============================================================================
BOOK_NORMALIZATION = {
    "Genesis": "Genesis", "Gen": "Genesis", "Ge": "Genesis", "Gn": "Genesis",
    "Exodus": "Exodus", "Ex": "Exodus", "Exo": "Exodus", "Exod": "Exodus",
    "Leviticus": "Leviticus", "Lev": "Leviticus", "Le": "Leviticus", "Lv": "Leviticus",
    "Numbers": "Numbers", "Num": "Numbers", "Nu": "Numbers", "Nm": "Numbers", "Numb": "Numbers",
    "Deuteronomy": "Deuteronomy", "Deut": "Deuteronomy", "Dt": "Deuteronomy", "Deu": "Deuteronomy",
    "Joshua": "Joshua", "Josh": "Joshua", "Jos": "Joshua",
    "Judges": "Judges", "Judg": "Judges", "Jdg": "Judges", "Jg": "Judges",
    "Ruth": "Ruth", "Rth": "Ruth",
    "1 Samuel": "1 Samuel", "1Sam": "1 Samuel", "1 Sam": "1 Samuel",
    "I Samuel": "1 Samuel", "1Samuel": "1 Samuel",
    "2 Samuel": "2 Samuel", "2Sam": "2 Samuel", "2 Sam": "2 Samuel",
    "II Samuel": "2 Samuel", "2Samuel": "2 Samuel",
    "1 Kings": "1 Kings", "1Kgs": "1 Kings", "1 Kgs": "1 Kings",
    "I Kings": "1 Kings", "1Kings": "1 Kings", "1Kin": "1 Kings",
    "2 Kings": "2 Kings", "2Kgs": "2 Kings", "2 Kgs": "2 Kings",
    "II Kings": "2 Kings", "2Kings": "2 Kings", "2Kin": "2 Kings",
    "1 Chronicles": "1 Chronicles", "1Chr": "1 Chronicles", "1 Chr": "1 Chronicles",
    "I Chronicles": "1 Chronicles", "1Chron": "1 Chronicles", "1 Chron": "1 Chronicles",
    "2 Chronicles": "2 Chronicles", "2Chr": "2 Chronicles", "2 Chr": "2 Chronicles",
    "II Chronicles": "2 Chronicles", "2Chron": "2 Chronicles", "2 Chron": "2 Chronicles",
    "Ezra": "Ezra",
    "Nehemiah": "Nehemiah", "Neh": "Nehemiah", "Ne": "Nehemiah",
    "Esther": "Esther", "Esth": "Esther", "Est": "Esther",
    "Job": "Job",
    "Psalms": "Psalms", "Psalm": "Psalms", "Ps": "Psalms", "Psa": "Psalms", "Pss": "Psalms",
    "Proverbs": "Proverbs", "Prov": "Proverbs", "Pro": "Proverbs", "Prv": "Proverbs",
    "Ecclesiastes": "Ecclesiastes", "Eccl": "Ecclesiastes", "Ecc": "Ecclesiastes", "Ec": "Ecclesiastes",
    "Song of Solomon": "Song of Solomon", "Song": "Song of Solomon",
    "Song of Songs": "Song of Solomon", "SOS": "Song of Solomon",
    "Sos": "Song of Solomon", "SS": "Song of Solomon", "Solomon's Song": "Song of Solomon",
    "Isaiah": "Isaiah", "Isa": "Isaiah", "Is": "Isaiah",
    "Jeremiah": "Jeremiah", "Jer": "Jeremiah", "Je": "Jeremiah",
    "Lamentations": "Lamentations", "Lam": "Lamentations", "La": "Lamentations",
    "Ezekiel": "Ezekiel", "Ezek": "Ezekiel", "Eze": "Ezekiel", "Ezk": "Ezekiel",
    "Daniel": "Daniel", "Dan": "Daniel", "Da": "Daniel", "Dn": "Daniel",
    "Hosea": "Hosea", "Hos": "Hosea", "Ho": "Hosea",
    "Joel": "Joel", "Joe": "Joel", "Jl": "Joel",
    "Amos": "Amos", "Am": "Amos",
    "Obadiah": "Obadiah", "Obad": "Obadiah", "Ob": "Obadiah",
    "Jonah": "Jonah", "Jon": "Jonah", "Jnh": "Jonah",
    "Micah": "Micah", "Mic": "Micah", "Mc": "Micah",
    "Nahum": "Nahum", "Nah": "Nahum", "Na": "Nahum",
    "Habakkuk": "Habakkuk", "Hab": "Habakkuk", "Hb": "Habakkuk",
    "Zephaniah": "Zephaniah", "Zeph": "Zephaniah", "Zep": "Zephaniah", "Zp": "Zephaniah",
    "Haggai": "Haggai", "Hag": "Haggai", "Hg": "Haggai",
    "Zechariah": "Zechariah", "Zech": "Zechariah", "Zec": "Zechariah", "Zc": "Zechariah",
    "Malachi": "Malachi", "Mal": "Malachi", "Ml": "Malachi",
    "Matthew": "Matthew", "Matt": "Matthew", "Mat": "Matthew", "Mt": "Matthew",
    "Mathew": "Matthew", "Matth": "Matthew",
    "Mark": "Mark", "Mrk": "Mark", "Mk": "Mark",
    "Luke": "Luke", "Luk": "Luke", "Lk": "Luke",
    "John": "John", "Jhn": "John", "Jn": "John",
    "Acts": "Acts", "Act": "Acts", "Ac": "Acts",
    "Romans": "Romans", "Rom": "Romans", "Ro": "Romans", "Rm": "Romans",
    "1 Corinthians": "1 Corinthians", "1Cor": "1 Corinthians", "1 Cor": "1 Corinthians",
    "I Corinthians": "1 Corinthians", "1Corinthians": "1 Corinthians",
    "2 Corinthians": "2 Corinthians", "2Cor": "2 Corinthians", "2 Cor": "2 Corinthians",
    "II Corinthians": "2 Corinthians", "2Corinthians": "2 Corinthians",
    "Galatians": "Galatians", "Gal": "Galatians", "Ga": "Galatians",
    "Ephesians": "Ephesians", "Eph": "Ephesians", "Ep": "Ephesians",
    "Philippians": "Philippians", "Phil": "Philippians", "Php": "Philippians", "Pp": "Philippians",
    "Colossians": "Colossians", "Col": "Colossians",
    "1 Thessalonians": "1 Thessalonians", "1Thess": "1 Thessalonians", "1 Thess": "1 Thessalonians",
    "I Thessalonians": "1 Thessalonians", "1Th": "1 Thessalonians", "1 Th": "1 Thessalonians",
    "2 Thessalonians": "2 Thessalonians", "2Thess": "2 Thessalonians", "2 Thess": "2 Thessalonians",
    "II Thessalonians": "2 Thessalonians", "2Th": "2 Thessalonians", "2 Th": "2 Thessalonians",
    "1 Timothy": "1 Timothy", "1Tim": "1 Timothy", "1 Tim": "1 Timothy",
    "I Timothy": "1 Timothy", "1Ti": "1 Timothy",
    "2 Timothy": "2 Timothy", "2Tim": "2 Timothy", "2 Tim": "2 Timothy",
    "II Timothy": "2 Timothy", "2Ti": "2 Timothy",
    "Titus": "Titus", "Tit": "Titus", "Ti": "Titus",
    "Philemon": "Philemon", "Philem": "Philemon", "Phm": "Philemon", "Pm": "Philemon",
    "Hebrews": "Hebrews", "Heb": "Hebrews", "He": "Hebrews",
    "James": "James", "Jas": "James", "Jm": "James",
    "1 Peter": "1 Peter", "1Pet": "1 Peter", "1 Pet": "1 Peter",
    "I Peter": "1 Peter", "1Pe": "1 Peter", "1Pt": "1 Peter",
    "2 Peter": "2 Peter", "2Pet": "2 Peter", "2 Pet": "2 Peter",
    "II Peter": "2 Peter", "2Pe": "2 Peter", "2Pt": "2 Peter",
    "1 John": "1 John", "1Jn": "1 John", "1 Jn": "1 John",
    "I John": "1 John", "1Jo": "1 John", "1Jhn": "1 John",
    "2 John": "2 John", "2Jn": "2 John", "2 Jn": "2 John",
    "II John": "2 John", "2Jo": "2 John",
    "3 John": "3 John", "3Jn": "3 John", "3 Jn": "3 John",
    "III John": "3 John", "3Jo": "3 John",
    "Jude": "Jude", "Jud": "Jude",
    "Revelation": "Revelation", "Rev": "Revelation", "Re": "Revelation",
    "Revelations": "Revelation", "Rv": "Revelation",
}


# =============================================================================
# VERSE PARSING & NET BIBLE LOOKUP
# =============================================================================
VERSE_PATTERN   = re.compile(r'\b([1-3]?\s?[A-Za-z]+)\s+\d+:\d+(?:-\d+)?\b')
STRONGS_PATTERN = re.compile(
    r'(?:\[STRONGS:\s*([HG]\d+[a-z]?)\]'
    r'|\(([HG]\d+[a-z]?)\)'
    r'|\b([HG]\d{3,5}[a-z]?)\b)'
)
_VERSE_REF_RE = re.compile(
    r'^\s*(?P<book>[1-3]?\s?[A-Za-z]+(?:\s+[A-Za-z]+)?)\s+'
    r'(?P<chapter>\d+):(?P<verse_start>\d+)(?:-(?P<verse_end>\d+))?\s*[.!?]?\s*$',
    re.IGNORECASE,
)

def _normalize_book(raw: str) -> str:
    raw = raw.strip()
    for candidate in (raw, raw.title(), raw.capitalize()):
        if candidate in BOOK_NORMALIZATION:
            return BOOK_NORMALIZATION[candidate]
    collapsed = re.sub(r'\s+', ' ', raw).strip()
    for candidate in (collapsed, collapsed.title()):
        if candidate in BOOK_NORMALIZATION:
            return BOOK_NORMALIZATION[candidate]
    return raw

def resolve_verse_from_net(question: str):
    """Look up a verse reference in NET_LOOKUP. Returns formatted text or None."""
    m = _VERSE_REF_RE.match(question)
    if not m:
        return None
    raw_book    = m.group("book")
    chapter     = int(m.group("chapter"))
    verse_start = int(m.group("verse_start"))
    verse_end   = int(m.group("verse_end")) if m.group("verse_end") else verse_start
    book      = _normalize_book(raw_book)
    book_data = NET_LOOKUP.get(book)
    if not book_data:
        return None
    ch_data = book_data.get(str(chapter))
    if not ch_data:
        return None
    collected = [ch_data[str(v)].strip() for v in range(verse_start, verse_end + 1) if str(v) in ch_data]
    if not collected:
        return None
    ref_label = (
        f"{book} {chapter}:{verse_start}" if verse_start == verse_end
        else f"{book} {chapter}:{verse_start}-{verse_end}"
    )
    return f"{ref_label} — {' '.join(collected)} (NET)"

def parse_reference(ref: str):
    ref = ref.strip()
    if " " not in ref or ":" not in ref:
        return None
    book_part, cv = ref.rsplit(" ", 1)
    try:
        if "-" in cv:
            chapter_str, verse_range = cv.split(":", 1)
            start_str, end_str = verse_range.split("-", 1)
            return {"book": book_part.strip(), "chapter": int(chapter_str),
                    "verses": list(range(int(start_str), int(end_str) + 1))}
        chapter_str, verse_str = cv.split(":", 1)
        return {"book": book_part.strip(), "chapter": int(chapter_str), "verses": [int(verse_str)]}
    except ValueError:
        return None

def extract_references(ai_text: str) -> list[str]:
    return [m.group(0) for m in VERSE_PATTERN.finditer(ai_text)]

def extract_strongs_numbers(ai_text: str) -> list[str]:
    seen, ordered = set(), []
    for m in STRONGS_PATTERN.finditer(ai_text):
        raw = m.group(1) or m.group(2) or m.group(3)
        if not raw:
            continue
        num = normalize_strongs_key(raw)
        if num not in seen:
            seen.add(num)
            ordered.append(num)
    return ordered


# =============================================================================
# CJK / GARBAGE STRIPPING
# =============================================================================
_CJK_RE = re.compile(
    r'[\u2E80-\u2EFF\u2F00-\u2FDF\u3000-\u303F\u3040-\u309F\u30A0-\u30FF'
    r'\u3100-\u312F\u3130-\u318F\u3190-\u319F\u31A0-\u31BF\u31F0-\u31FF'
    r'\u3200-\u32FF\u3300-\u33FF\u3400-\u4DBF\u4E00-\u9FFF\uA000-\uA48F'
    r'\uA490-\uA4CF\uAC00-\uD7AF\uF900-\uFAFF\uFE10-\uFE1F\uFE30-\uFE4F'
    r'\uFF00-\uFFEF\U00020000-\U0002A6DF\U0002A700-\U0002B73F\U0002B740-\U0002B81F]+',
    re.UNICODE
)

def strip_non_latin(text: str) -> str:
    cleaned = _CJK_RE.sub('', text)
    return re.sub(r'  +', ' ', cleaned).strip()


# =============================================================================
# STRONG'S INJECTION
# =============================================================================
_STRONGS_Q_RE = re.compile(
    r'\b([HGhg]\d{1,5}[a-z]?)\b'
    r'|(?:strong\'?s?\s*#?\s*)(\d{3,5})\b'
    r'|\bstrong\'?s?\s+number\s+(\d{3,5})\b',
    re.IGNORECASE
)

def inject_strongs_facts(user_message: str) -> str:
    injections, seen = [], set()
    for m in _STRONGS_Q_RE.finditer(user_message):
        raw = m.group(1) or m.group(2) or m.group(3)
        if not raw:
            continue
        key = normalize_strongs_key(raw)
        if key in seen:
            continue
        seen.add(key)
        entry = STRONGS_LOOKUP.get(key)
        lang  = "Hebrew" if key.startswith("H") else "Greek"
        if entry:
            if isinstance(entry, dict):
                lemma = entry.get("lemma", "")
                xlit  = entry.get("xlit") or entry.get("translit", "")
                pron  = entry.get("pron", "")
                sdef  = entry.get("strongs_def", "").strip()
                kjv   = entry.get("kjv_def", "").strip()
                line  = (f"[VERIFIED] {key} ({lang}): lemma={lemma} "
                         f"xlit={xlit} pron={pron} — {sdef} [KJV gloss: {kjv}]")
            else:
                line = f"[VERIFIED] {key} ({lang}): {entry}"
        else:
            line = f"[NOT FOUND] {key} ({lang}): not in Strong's database."
        injections.append(line)
    if not injections:
        return user_message
    block = "\n".join(injections)
    return f"[VERIFIED STRONG'S DATA — use these facts exactly]\n{block}\n\n{user_message}"


# =============================================================================
# ZOTERO
# =============================================================================
_zotero_cache: dict[str, tuple[float, str]] = {}

def search_zotero(query: str, max_results: int = ZOTERO_MAX_RESULTS) -> str:
    try:
        encoded = urllib.parse.quote(query)
        url = (
            f"{ZOTERO_API_BASE}/items?q={encoded}&limit={max_results}"
            f"&format=json&itemType=-attachment&sort=relevance"
        )
        resp = requests.get(url, headers={"Zotero-API-Version": "3",
                                          "User-Agent": "BibleScholarBot/1.0"}, timeout=10)
        resp.raise_for_status()
        items = resp.json()
        if not items:
            return ""
        lines = ["[PROJECT ELEAZAR LIBRARY — relevant sources found]"]
        for item in items:
            data     = item.get("data", {})
            title    = data.get("title", "Untitled")
            itype    = data.get("itemType", "")
            date     = data.get("date", "")
            creators = data.get("creators", [])
            abstract = data.get("abstractNote", "").strip()
            url_f    = data.get("url", "")
            author_parts = []
            for c in creators[:2]:
                name = f"{c.get('lastName','')}, {c.get('firstName','')}".strip(", ")
                if name:
                    author_parts.append(name)
            authors = "; ".join(author_parts) if author_parts else "Unknown author"
            snippet = (abstract[:200] + "…") if len(abstract) > 200 else abstract
            entry   = f"• {title} ({itype}, {date}) — {authors}"
            if snippet:
                entry += f"\n  Abstract: {snippet}"
            if url_f:
                entry += f"\n  URL: {url_f}"
            lines.append(entry)
        lines.append("[End of library results — cite these if relevant]")
        return "\n".join(lines)
    except requests.exceptions.Timeout:
        return ""
    except Exception:
        return ""

def zotero_context_for(user_message: str) -> str:
    if re.match(r'^[\w\s]+\d+:\d+', user_message.strip()):
        return ""
    if len(user_message.strip()) < 10:
        return ""
    query = re.sub(r'@\w+', '', user_message).strip(" ,?!")
    if not query:
        return ""
    now    = time.time()
    cached = _zotero_cache.get(query)
    if cached and (now - cached[0]) < ZOTERO_CACHE_TTL:
        return cached[1]
    result = search_zotero(query)
    _zotero_cache[query] = (now, result)
    return result


# =============================================================================
# OLLAMA — SCHOLAR UI  (streaming)
# =============================================================================
def check_ollama_health() -> bool:
    try:
        r = requests.get("http://localhost:11434", timeout=3)
        return r.status_code < 500
    except Exception:
        return False

def stream_from_ollama(user_message: str):
    """
    Streaming Ollama call for the Scholar UI chat.
    Yields text chunks as they arrive.  Acquires scholar_ollama_lock with a
    30-second timeout so a busy Rumble request doesn't permanently block the UI.
    """
    if len(conversation_history) > MAX_HISTORY_TURNS * 2:
        conversation_history[:] = conversation_history[-(MAX_HISTORY_TURNS * 2):]

    zotero_block     = zotero_context_for(user_message)
    enriched_message = f"{zotero_block}\n\n{user_message}" if zotero_block else user_message
    conversation_history.append({"role": "user", "content": user_message})
    messages_for_request = conversation_history[:-1] + [{"role": "user", "content": enriched_message}]

    payload = {"model": SCHOLAR_MODEL, "messages": messages_for_request, "stream": True}

    ai_chunks: list[str] = []
    acquired = scholar_ollama_lock.acquire(timeout=30)
    if not acquired:
        yield "[Ollama is busy — please try again in a moment.]"
        return
    try:
        with requests.post(OLLAMA_URL, json=payload, stream=True) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                data  = json.loads(line.decode("utf-8"))
                chunk = data.get("message", {}).get("content", "")
                if chunk:
                    ai_chunks.append(chunk)
                    yield chunk
                if data.get("done"):
                    conversation_history.append({"role": "assistant", "content": "".join(ai_chunks)})
                    break
    except Exception as e:
        yield f"[Error communicating with model: {e}]"
    finally:
        scholar_ollama_lock.release()


# =============================================================================
# PERSISTENCE — conversation save/load
# =============================================================================
SAVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversations.json")

def load_saved_conversations() -> list[tuple[str, str]]:
    if not os.path.exists(SAVE_FILE):
        return []
    try:
        with open(SAVE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [(e["title"], e["content"]) for e in data if "title" in e and "content" in e]
    except Exception as e:
        print(f"[Warning] Could not load conversations.json: {e}")
        return []

def persist_conversations(convos: list[tuple[str, str]]):
    try:
        data = [{"title": t, "content": c} for t, c in convos]
        with open(SAVE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Warning] Could not save conversations.json: {e}")
