"""Scrub paths, identities, hosts, ids and secrets out of telemetry text (IDE-151).

Every pattern runs over at most `MAX_IN` characters and none can backtrack more
than linearly: starts are anchored by single-character lookbehinds and every
repetition is one character class or has a small fixed bound. A Python regex
cannot be interrupted, so the bound is the defence.

Scrubbing removes identifiers and secrets, not code: a script that prints file
content sends it scrubbed. That boundary is stated in docs/telemetry.md.
"""

import os
import re

MAX_IN = 16 * 1024
FIND_MAX = 64 * 1024

A = re.ASCII
AI = re.ASCII | re.IGNORECASE

SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b", A)),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b", A)),
    ("Linear API key", re.compile(r"\blin_api_[A-Za-z0-9]{32,}\b", A)),
    ("PAT assignment", re.compile(r"\b(PAT|TOKEN|SECRET|PASSWORD)\s*[=:]\s*['\"]?[A-Za-z0-9+/_-]{32,}", A)),
    ("basic auth header", re.compile(r"Authorization:\s*Basic\s+[A-Za-z0-9+/=]{40,}", A)),
    ("JWT", re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]*", A)),
]


def find_secrets(text):
    """Every secret-looking hit in `text`: [{"name", "index"}]."""
    text = "" if text is None else str(text)[:FIND_MAX]
    hits = []
    for name, pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            hits.append({"name": name, "index": match.start()})
    return hits


def _home_and_user():
    home = user = ""
    try:
        home = os.path.expanduser("~")
        if home == "~":
            home = ""
    except Exception:
        pass
    try:
        import getpass
        user = getpass.getuser() or ""
    except Exception:
        pass
    return home, user


# A platform script, relative to the platform root: a skill's own file or a shared script.
UNIT = r"[a-z0-9][a-z0-9-]{0,63}"
REL_SRC = (r"(skills[\\/]" + UNIT + r"[\\/][\w.-]{1,80}\.(?:py|js|sh)"
           r"|scripts[\\/](?:[\w.-]{1,64}[\\/]){0,3}[\w.-]{1,80}\.py)")
PLATFORM_SCRIPT = re.compile(r"(?<![\w.-])" + REL_SRC, A)

# An absolute path whose tail is a platform script inside an installed platform root: the
# plugin caches (…/ai-sdlc-platform/<version>/) and the npx payload (…/.idp/platform/<version>/).
PLATFORM_PATH = re.compile(
    r"(?<![\w>.<~])(?:[A-Za-z]:|~)?[\\/](?:[^\s'\"<>|;),\\/]+[\\/]){0,24}?"
    r"(?:ai-sdlc-platform|\.idp[\\/]platform)[\\/][^\s'\"<>|;),\\/]+[\\/]" + REL_SRC, A)


def platform_script(text):
    """'skills/<unit>/<file>' or 'scripts/<path>' for the first platform script in `text`, else None."""
    match = PLATFORM_SCRIPT.search("" if text is None else str(text)[:MAX_IN])
    return match.group(1).replace("\\", "/") if match else None


SECRET_KEY = re.compile(
    r"(?:^|[_.-])pat(?:$|[_.-])|[a-z]Pat$|token|secret|passw(?:or)?d|pwd|api[_-]?key|apikey"
    r"|access[_-]?key|credential|bearer|private[_-]?key|cookie|auth", AI)
KEY_VALUE = re.compile(
    r"(?<![\w.-])([\"']?)([\w.-]{1,64})\1([ \t]*[:=][ \t]*)"
    r"(\"(?:[^\"\\\r\n]|\\.)*\"?|'[^'\r\n]*'?|[^\s,;&}\]]+)", A)

# File extensions that look like a TLD: a name ending in one is a file, not a host.
FILE_EXT = set("js mjs cjs json jsonl ts tsx jsx sh ps1 py md txt yml yaml xml html css log lock exe cmd bat "
               "dll so png jpg zip tar gz rb go rs java cs toml ini cfg conf env tmp bak map csv sql pdf pyc".split())
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME = re.compile(r"(?<![\w.@-])" + LABEL + r"(?:\." + LABEL + r")+(?![\w-])", AI)

SECRET_REPLACERS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), "<secret>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b", A), "<secret>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b", A), "<secret>"),
    (re.compile(r"\blin_api_[A-Za-z0-9]{32,}\b", A), "<secret>"),
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]*", A), "<secret>"),
    # The whole header line, quoted or not: schemes such as Digest carry several fields.
    (re.compile(r"(?<![\w-])([\"']?)((?:proxy-)?authorization)\1[ \t]*[:=][^\r\n]*", AI), "Authorization: <secret>"),
    (re.compile(r"\bBearer[ \t]+[\w.~+/=-]{8,}", AI), "Bearer <secret>"),
    (re.compile(r"(?<![\w-])[a-z2-7]{52}(?![\w-])", A), "<secret>"),
]


def _redact_key_values(text):
    def replace(match):
        quote, key, sep, value = match.groups()
        if not SECRET_KEY.search(key) or value == "<secret>":
            return match.group(0)
        masked = '"<secret>"' if value.startswith('"') else "'<secret>'" if value.startswith("'") else "<secret>"
        return f"{quote}{key}{quote}{sep}{masked}"
    return KEY_VALUE.sub(replace, text)


ORG_FLAG = re.compile(r"(?<![\w-])(--?org(?:ani[sz]ation)?)[ =]+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s\"']+)", AI)
ORG_KEY = re.compile(r"(?<![\w-])(org(?:ani[sz]ation)?|tenant|account)\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&\"']+)", AI)
SCP_REMOTE = re.compile(r"(?<![\w.+-])[\w.-]{1,64}@[\w.-]{1,255}:(?!//)[^\s'\"<>]*", A)
URL = re.compile(r"(?<![\w+.-])[a-z][a-z0-9+.-]{0,20}://[^\s'\"<>)]*", AI)
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+", A)
HOME_ROOT = re.compile(r"(?<![\w>.<~:])(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)|/Users|/home|/root|~)"
                       r"(?:[\\/][^'\"<>|;)\r\n]*)?", A)
UNC = re.compile(r"(?<![\w\\])\\{2,4}[^\s'\"<>|,;)]+", A)
DRIVE = re.compile(r"(?<![\w>.<~])([A-Za-z]):/", A)
WIN_PATH = re.compile(r"(?<![\w>.<~])[A-Za-z]:[\\/](?:[^\s'\"<>|;)\\/]|[\\/]|[ ](?=[^\s'\"<>|;)\\/]+[\\/]))*", A)
# A segment may hold a space when a later word of the run carries a slash ("Acme Secret/repo").
POSIX_PATH = re.compile(r"(?<![\w>.<~:/\\-])/(?:[\w.@+~%-]|/|[ ](?=[\w.@+~%-]+/))*", A)
HOME_TAIL = re.compile(r"<home>(?:[\\/][^\s'\"<>|,;)]*)?")
IPV6_FULL = re.compile(r"(?<![\w:.])(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}(?![\w:])", AI)
IPV6_SHORT = re.compile(r"(?<![\w:.])(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?(?![\w:])", AI)
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", A)
# A single-label host only where the position says host: user@host, ssh [opts] [user@]host, Host host.
AT_HOST = re.compile(r"(?<=\w@)[a-z0-9][a-z0-9-]{0,62}(?![\w.@-])", AI)
SSH_HOST = re.compile(r"(?<![\w-])(ssh|scp|sftp)((?:[ \t]+-[A-Za-z]+(?:[ \t]+[^\s-][^\s]*)?)*[ \t]+(?:[\w.-]{1,64}@)?)"
                      r"(?=[a-z0-9-]*[a-z])[a-z0-9][a-z0-9-]{0,62}(?![\w.@/:-])", AI)
HOST_KEY = re.compile(r"(?<![\w-])(Host(?:Name)?[ \t]+)[a-z0-9][a-z0-9-]{0,62}(?![\w.@-])", A)
GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", AI)
HEX = re.compile(r"\b[0-9a-f]{16,}\b", AI)
NUMBER = re.compile(r"(?<![\w<])\d+(?:\.\d+)*(?![\w>])", A)


def _host(match):
    name = match.group(0)
    tld = name[name.rfind(".") + 1:].lower()
    return "<host>" if re.fullmatch(r"[a-z]{2,24}", tld) and tld not in FILE_EXT else name


def scrub(text, for_signature=False):
    """Scrub a string; input beyond 16 KB is dropped.

    `for_signature` also replaces standalone numbers and normalises separators and
    drive letters, so one failure signs the same across counts, users and operating systems.
    """
    if text is None:
        return ""
    s = str(text)[:MAX_IN]
    for pattern, replacement in SECRET_REPLACERS:
        s = pattern.sub(replacement, s)
    s = _redact_key_values(s)
    s = ORG_FLAG.sub(lambda m: f"{m.group(1)} <org>", s)
    s = ORG_KEY.sub(lambda m: f"{m.group(1)}=<org>", s)

    # scp-style remotes (git@host:org/repo) before the email rule would take the user@host part.
    s = SCP_REMOTE.sub("<url>", s)
    s = URL.sub("<url>", s)
    s = EMAIL.sub("<email>", s)

    s = PLATFORM_PATH.sub(lambda m: "<path>/" + m.group(1).replace("\\", "/"), s)
    home, user = _home_and_user()
    if len(home) > 1:
        for variant in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
            s = re.sub(re.escape(variant), "<home>", s, flags=re.IGNORECASE)
    # From a home-like root to the end of the line: a folder name may hold spaces and an organization.
    s = HOME_ROOT.sub("<path>", s)
    s = UNC.sub("<path>", s)
    if len(user) >= 3:
        s = re.sub(r"(?<![\w<])" + re.escape(user) + r"(?![\w>])", "<user>", s)

    if for_signature:
        s = s.replace("\\", "/")
        s = DRIVE.sub(lambda m: f"{m.group(1).lower()}:/", s)
    s = WIN_PATH.sub("<path>", s)
    s = POSIX_PATH.sub(lambda m: m.group(0) if m.group(0) == "/" else "<path>", s)

    if for_signature:
        s = HOME_TAIL.sub("<path>", s)
    s = IPV6_FULL.sub("<host>", s)
    s = IPV6_SHORT.sub(lambda m: "<host>" if re.search(r"[0-9a-f]", m.group(0), re.I) else m.group(0), s)
    s = IPV4.sub("<host>", s)
    s = AT_HOST.sub("<host>", s)
    s = SSH_HOST.sub(lambda m: f"{m.group(1)}{m.group(2)}<host>", s)
    s = HOST_KEY.sub(lambda m: f"{m.group(1)}<host>", s)
    s = HOSTNAME.sub(_host, s)
    s = GUID.sub("<id>", s)
    s = HEX.sub("<id>", s)
    if for_signature:
        s = NUMBER.sub("<n>", s)
    return s
