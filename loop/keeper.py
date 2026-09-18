"""The keeper: deterministic supervisor decisions plus a local-model scribe.

Measured on qwen2.5:7b before this was written: asked to *choose* the next
action from a raw observation it was right 9 times in 18 (it switched healthy
supervisors and reset sessions for context limits that had not been reached),
and asked to judge a failing log it called it "passed". Asked to *extract* a
blocker, a fix command and progress bullets from the same log it was right
every time. So decisions here are plain rules, and the local model only writes
the short result summaries that make a context reset or provider switch cheap.
Every model output has a deterministic fallback; the loop never depends on it.
"""
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

DEFAULT_KEEPER_MODEL = "qwen2.5:7b"
OLLAMA_URL = "http://localhost:11434"
REMINDER = ("Keeper reminder: you are the supervisor. Do not edit files, run builds or "
            "write the implementation into dispatch prompts. Describe the change and "
            "dispatch a worker to make it.")
LARGE_CODE_LINES = 40


def code_lines(text):
    """Lines inside fenced code blocks: a supervisor pasting an implementation."""
    total, inside = 0, False
    for line in str(text or "").splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            total += 1
    return total


def skeleton_keys(text, head=6, tail=6):
    """Opening and closing word runs of a line, with per-item decoration removed.

    Templates evade exact-duplicate checks by appending ids, quoting the item, or
    slotting a name into a fixed sentence. The fixed parts that remain (how the
    line starts and ends) are what repeat.
    """
    value = re.sub(r"\b[0-9a-f]{6,}\b", " ", str(text).lower())
    value = re.sub(r"[\"“”'‘’][^\"“”'‘’]{3,}[\"“”'‘’]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    words = re.findall(r"[a-z]+", value)
    if len(words) < max(head, tail) + 2:
        return []
    return ["starts: " + " ".join(words[:head]), "ends: " + " ".join(words[-tail:])]


def template_report(added_lines, minimum=10):
    """How much of a change is one sentence pattern repeated.

    Returns None below ``minimum`` prose lines, else the most repeated opening or
    ending, its count, and its share of prose lines. Tables and code rows are
    ignored: repetition there is normal.
    """
    prose = []
    for line in added_lines:
        stripped = line.strip()
        # Judge the prose inside a JSON value or string literal, not the code around it.
        literals = re.findall(r'"((?:[^"\\]|\\.){30,})"|\'((?:[^\'\\]|\\.){30,})\'', stripped)
        value = max((a or b for a, b in literals), key=len) if literals else stripped
        if value.startswith(("|", "#", "-", "{", "}", "[", "]")) or value.count(";") >= 2:
            continue
        # Paths, ids and urls repeat by nature in reports and manifests; repetition
        # there says nothing about whether the prose was written per item.
        if re.match(r"^[\w./-]+$", value) or value.count("/") >= 2 or "://" in value:
            continue
        if skeleton_keys(value):
            prose.append(value)
    if len(prose) < minimum:
        return None
    counts = {}
    for value in prose:
        for key in skeleton_keys(value):
            counts[key] = counts.get(key, 0) + 1
    key, count = max(counts.items(), key=lambda item: item[1])
    return {"prose_lines": len(prose), "pattern": key, "count": count,
            "share": round(count / len(prose), 3)}


def decide(obs):
    """The next supervisor action from one turn's observation. No model call.

    ``obs`` keys: supervisor_unavailable, available (best first, current provider
    excluded), earliest_reset_seconds, edited_files, large_code, violations,
    context_tokens, reset_at, stagnant_turns, stagnant_limit.
    """
    available = list(obs.get("available") or [])
    if obs.get("supervisor_unavailable"):
        if available:
            return _decision("switch_supervisor", "1: supervisor unavailable", provider=available[0])
        wait = obs.get("earliest_reset_seconds") or 1800
        return _decision("wait", "1: no supervisor available", wait_seconds=max(60, int(wait)))
    if obs.get("edited_files") or obs.get("large_code"):
        if obs.get("violations", 0) >= 2:
            return _decision("reset_context", "2: repeated supervisor violation", reminder=REMINDER)
        return _decision("remind", "2: supervisor did work itself", reminder=REMINDER)
    if obs.get("reset_at") and obs.get("context_tokens", 0) >= obs["reset_at"]:
        return _decision("reset_context", "3: context over threshold")
    if obs.get("stagnant_turns", 0) >= obs.get("stagnant_limit", 4):
        if available:
            return _decision("switch_supervisor", "4: stagnant, rotating supervisor",
                             provider=available[0])
        return _decision("reset_context", "4: stagnant, fresh session")
    return _decision("continue", "5: healthy")


def _decision(action, rule, provider=None, wait_seconds=0, reminder=None):
    return {"action": action, "rule": rule, "provider": provider,
            "wait_seconds": wait_seconds, "reminder": reminder}


class Ollama:
    """Minimal stdlib client for a local Ollama server."""

    def __init__(self, model=DEFAULT_KEEPER_MODEL, url=OLLAMA_URL, timeout=120):
        self.model, self.url, self.timeout = model, url.rstrip("/"), timeout
        self.spawned_at = 0.0

    def _post(self, path, body, timeout):
        request = urllib.request.Request(self.url + path, json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())

    def alive(self):
        try:
            with urllib.request.urlopen(self.url + "/api/version", timeout=3) as response:
                return response.status == 200
        except (OSError, ValueError):
            return False

    def ensure(self):
        """Start the server if it is down and confirm the model exists."""
        if not self.alive():
            if not shutil.which("ollama") or time.time() - self.spawned_at < 600:
                return False
            self.spawned_at = time.time()
            subprocess.Popen(["ollama", "serve"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            for _ in range(30):
                time.sleep(1)
                if self.alive():
                    break
            else:
                return False
        try:
            self._post("/api/show", {"model": self.model}, 10)
            return True
        except (OSError, ValueError, urllib.error.HTTPError):
            return False

    def chat(self, system, user, schema):
        data = self._post("/api/chat", {
            # Summaries arrive minutes apart; holding ~5 GB of model in memory
            # between them starved the machine. Reloading costs seconds.
            "model": self.model, "stream": False, "format": schema, "keep_alive": "2m",
            "options": {"temperature": 0, "num_ctx": 8192},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}, self.timeout)
        return json.loads(data["message"]["content"])


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "blocker": {"type": ["string", "null"]},
        "suggested_fix_command": {"type": ["string", "null"]},
        "progress": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "handoff": {"type": "string"},
    },
    "required": ["blocker", "suggested_fix_command", "progress", "handoff"],
}
SUMMARY_SYSTEM = ("You summarize a coding worker's log for the next supervisor session. "
                  "Be factual and quote only what is in the log. 'blocker' is what still "
                  "prevents the task from passing, or null. 'handoff' is at most 3 sentences.")
HINT_PATTERN = re.compile(
    r"(?i)(error|exception|traceback|failed|not found|doesn't exist|does not exist|missing|"
    r"cannot|npx |pip install|npm install|playwright install)")

SPECIFIC_PATTERN = re.compile(
    r"(?i)(not found|doesn't exist|does not exist|cannot find|no module|enoent|command not found)")
COMMAND_PATTERN = re.compile(r"(npx [\w@./-]+(?: [\w@./-]+)*|pip install [\w@.=<>-]+|npm install[\w@./ -]*)")


def fallback_summary(log_tail):
    lines = [line.strip() for line in str(log_tail or "").splitlines() if line.strip()]
    hints = [line[:240] for line in lines if HINT_PATTERN.search(line)][-6:]
    specific = [line for line in hints if SPECIFIC_PATTERN.search(line)]
    commands = [match.group(0).strip("`║ ") for line in hints
                for match in [COMMAND_PATTERN.search(line)] if match]
    return {"blocker": (specific or hints or [None])[-1],
            "suggested_fix_command": commands[-1] if commands else None,
            "progress": [], "handoff": " | ".join(hints) or " | ".join(lines[-3:])[:600]}


class Scribe:
    """Compresses worker results with the local model, or deterministically."""

    def __init__(self, client=None):
        self.client = client

    def summarise(self, task_id, outcome, log_tail):
        """Returns (summary, source). ``outcome`` is decided by the caller, never here."""
        tail = str(log_tail or "")[-6000:]
        if self.client is not None:
            try:
                if self.client.ensure():
                    value = self.client.chat(
                        SUMMARY_SYSTEM,
                        "TASK: " + task_id + "\nOUTCOME (from exit codes): " + outcome
                        + "\nLOG:\n" + tail, SUMMARY_SCHEMA)
                    if (isinstance(value, dict) and isinstance(value.get("handoff"), str)
                            and isinstance(value.get("progress"), list)):
                        value["progress"] = [str(item)[:240] for item in value["progress"][:4]]
                        value["handoff"] = value["handoff"][:800]
                        return value, "local-model"
            except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
                pass
        return fallback_summary(tail), "fallback"
