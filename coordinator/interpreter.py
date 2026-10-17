"""Turn a free-text reply into a structured task update.

Enhanced AI Task Understanding:
- Supports English, Hindi, Hinglish, informal messages, slang, typos, and rough language.
- Distinguishes actual completion from promises, future commitments, and partial progress.
- Extracts delay reasons, blockers, and proposed deadlines.
- Identifies responsible entities (persons, resources, approvals).
- Safety net: blocker cues always prevent completion.
- Clear fallback indicators and confidence scores.
"""
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from typing import Optional

from . import config
from .db import BLOCKED, COMPLETE, IN_PROGRESS, PENDING


@dataclass
class Interpretation:
    intent: str                      # complete | progress | blocked | unblocked | question | unclear
    status: Optional[str] = None     # new status, or None = no change
    blocker: Optional[str] = None
    note: Optional[str] = None
    task_id: Optional[int] = None
    confidence: float = 0.5
    source: str = "rules"            # rules | llm | llm+rules
    clarifying_question: Optional[str] = None
    proposed_deadline: Optional[str] = None
    delay_reason: Optional[str] = None
    progress_pct: Optional[int] = None
    responsible_entity: Optional[str] = None

    def to_dict(self):
        return asdict(self)


# ------------------------------------------------------------------ rule patterns
BLOCK_CUES = re.compile(
    r"\b(waiting\s+(?:for|on)|blocked|stuck|can'?t|cannot|unable\s+to|need(?:s|ed)?\s+(?:access|credentials|approval|help|permission|someone)"
    r"|don'?t\s+have|do\s+not\s+have|haven'?t\s+(?:received|got|heard)|not\s+(?:received|available|working)"
    r"|depends?\s+on|pending\s+(?:from|on)|no\s+access|(?:issue|problem|trouble)s?\s+with|getting\s+(?:an?\s+)?errors?"
    r"|throws?\s+(?:an?\s+)?errors?|errors?\s+(?:when|while|on|in)|failing|fails|broken|yet\s+to\s+(?:receive|get)"
    r"|can\s+(?:someone|anyone)\s+help|held\s+up|delayed\s+by|dependency|ruka\s+hua|ruki\s+hui|atka|atki|atak\s+gaya"
    r"|phas\s+gaya|phas\s+gayi|(?:nahi|nhi)\s+mil(?:a|i|e)|intezaar|intezar|wait\s+(?:kar|kr)|dikkat|samajh\s+(?:nahi|nhi)"
    r"|error\s+aa|(?:nahi|nhi)\s+chal\s+raha|(?:nahi|nhi)\s+ho\s+(?:pa\s+)?raha|access\s+(?:nahi|nhi)|credentials?\s+(?:nahi|nhi)"
    r"|stck|fild|eror|prblm)\b", re.I)

NO_BLOCKER = re.compile(r"\b(no|any|nothing|without)\s+(blockers?|blocking|issues?|problems?)\b", re.I)

UNBLOCK_CUES = re.compile(
    r"\b(unblocked|resolved|sorted\s+out|got\s+the|received\s+the|now\s+have|arrived|fixed|"
    r"no\s+longer\s+blocked|access\s+(?:is\s+)?(?:granted|approved|given)|approved|mil\s+(?:gaya|gayi|gaye)|theek\s+ho\s+gaya|solve\s+ho\s+gaya)\b", re.I)

COMPLETE_CUES = re.compile(
    r"\b(done|completed?|finished|finalized|merged|deployed|shipped|submitted|pushed|ready|"
    r"connected|implemented|wrapped\s+up|all\s+set|ho\s*gaya|ho\s*gya|hogaya|ho\s*gayi|hogayi|khatam|poora\s+ho|pura\s+ho|"
    r"kar\s+diya|kardiya|kar\s+dia|bana\s+diya|bhej\s+diya|complete\s+ho|sb\s+ho\s+gaya|sb\s+hogya|sb\s+khatam|dne|cmplt)\b", re.I)

PARTIAL_CUES = re.compile(
    r"\b(still\s+(?:need|have)\s+to|yet\s+to|remaining|left\s+to\s+do|"
    r"(?:half|partly|partially|almost|nearly|mostly)\s+(?:done|finished|complete|completed)|"
    r"(?:not|isn'?t|aren'?t|wasn'?t)\s+(?:fully\s+|yet\s+)?(?:done|finished|complete|completed|ready)|"
    r"(?:haven'?t|hasn'?t|have\s+not)\s+(?:yet\s+)?(?:finished|completed)|(?:nahi|nhi)\s+(?:hua|hui|ho\s*paya|ho\s*payi|ho\s*gaya|ho\s*gya|hoga|complete|khatam|pura|poora)|"
    r"abhi\s+(?:nahi|nhi)|pura\s+nahi|poora\s+nahi|adhura|baaki\s+hai|bacha\s+hai|aadha|adha|thora\s+bacha)\b", re.I)

NOT_STARTED = re.compile(
    r"\b(?:haven'?t|have\s+not|didn'?t|not|yet\s+to)\s+(?:even\s+|yet\s+)?(?:started|begun)|not\s+started|yet\s+to\s+start|shuru\s+(?:nahi|nhi)|start\s+(?:nahi|nhi)|abhi\s+tak\s+(?:nahi|nhi)\s+kiya\b", re.I)

# Promises and Future Commitments (Must NOT be interpreted as Complete!)
PROMISE_COMMITMENT = re.compile(
    r"\b(?:i'?ll|i\s+will|will|going\s+to|plan\s+to|planning\s+to|kal\s*(?:tak\s*)?(?:karunga|karenge|ho\s*jayega|kar\s*dunga|de\s*dunga|bhejun|bhej\s*dunga)|"
    r"kal\s*tak|shaam\s*tak|subah\s*tak|today\s+evening|by\s+tomorrow|by\s+tonight|finish\s+by|complete\s+by|kar\s*lunga|kr\s*lunga|thodi\s*der\s*me)\b", re.I)

DELAY_CUES = re.compile(
    r"\b(?:delay|delayed|late|tabiyat\s+kharab|sick|power\s+cut|light\s+chali\s+gayi|electricity\s+cut|system\s+issue|"
    r"laptop\s+kharab|slow|lag|traveling|safar\s+me)\b", re.I)

PROPOSED_DEADLINE_PATTERNS = [
    r"\b(?:by|till|before|tak)\s+((?:tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d+\s*(?:days?|hours?|hrs?)))\b",
    r"\b((?:tomorrow|tonight|shaam\s+tak|kal\s+tak))\b",
    r"\bneed\s+(\d+\s*(?:more\s+)?(?:days?|hours?|hrs?))\b"
]

PROGRESS_CUES = re.compile(
    r"\b(working\s+on|started|starting|in\s+progress|halfway|half\s+way|making\s+progress|progress|testing|"
    r"writing|drafting|implementing|coding|reviewing|continuing|continue|on\s+it|going\s+well|underway|"
    r"on\s+track|going\s+fine|all\s+good|chal\s+raha|chal\s+rahi|kar\s+raha|kar\s+rahi|ho\s+raha|ho\s+rahi|shuru\s+kar|start\s+kar|kr\s+rha)\b", re.I)

PERCENT = re.compile(r"(\d{1,3})\s*%")
QUESTION_START = re.compile(r"^\s*(what|when|where|who|why|how|kab|kya|kaise|kyun|kahan|kaun|can\s+you|could\s+you|is\s+there|do\s+we|should\s+we)\b", re.I)
SPLIT_BEFORE = re.compile(r"[,;.!?\n]|\bbut\b|\bhowever\b|\bthough\b|\balthough\b|\band\b|\bso\b|\bpar\b|\blekin\b|\bmagar\b|\baur\b|\bbas\b", re.I)
FILLER = re.compile(r"^(?:i\s*am|i'?m|we\s*are|we'?re|currently|just|still|now)\s+", re.I)


def extract_blocker(text):
    """Return (blocker_phrase, text_before_it) for the first blocker cue, or (None, text)."""
    m = BLOCK_CUES.search(text)
    if not m:
        return None, text
    start = 0
    for b in SPLIT_BEFORE.finditer(text, 0, m.start()):
        start = b.end()
    end = len(text)
    for e in re.finditer(r"[.;!?\n]", text[m.start():]):
        end = m.start() + e.start()
        break
    phrase = text[start:end].strip(" ,-:")
    while FILLER.match(phrase):
        phrase = FILLER.sub("", phrase, count=1)
    phrase = phrase[:140].strip()
    before = text[:start].strip(" ,;-:")
    before = re.sub(r"\b(but|however|though|although|and|so)$", "", before, flags=re.I).strip(" ,;-:")
    return phrase or None, before


def extract_proposed_deadline(text: str) -> Optional[str]:
    """Extract proposed future completion estimate if present."""
    for pat in PROPOSED_DEADLINE_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).strip()
    return None


def extract_delay_reason(text: str) -> Optional[str]:
    """Extract delay explanation if present."""
    m = DELAY_CUES.search(text)
    if m:
        return text[m.start():m.start() + 80].strip()
    return None


def _match_task(text, tasks, active_task_id):
    """Pick which of the participant's tasks the reply is about."""
    if not tasks:
        return None
    # 1. Match explicit ID in text: #12 or task 12
    id_match = re.search(r"(?:#|\btask\s*(?:no\.?|number|id)?\s*)(\d{1,6})\b", text, re.I)
    if id_match:
        t_id = int(id_match.group(1))
        for t in tasks:
            if t["id"] == t_id:
                return t_id

    words = {w for w in re.findall(r"[a-z]{4,}", text.lower())}
    best, best_score = None, 0
    for t in tasks:
        tw = {w for w in re.findall(r"[a-z]{4,}", t["title"].lower())}
        score = len(words & tw)
        if score > best_score:
            best, best_score = t, score
    if best and best_score >= 1:
        return best["id"]
    ids = [t["id"] for t in tasks]
    return active_task_id if active_task_id in ids else ids[0]


def rule_interpret(text, tasks=None, active_task_id=None):
    tasks = tasks or []
    task_id = _match_task(text, tasks, active_task_id)
    clean = NO_BLOCKER.sub("", text)
    block_m, unblock_m = BLOCK_CUES.search(clean), UNBLOCK_CUES.search(clean)
    pct = PERCENT.search(text)
    snippet = text.strip()[:160]
    proposed_dl = extract_proposed_deadline(text)
    delay_why = extract_delay_reason(text)

    # 1. Blocker cue takes absolute precedence (unless resolved later in phrase)
    if block_m and not (unblock_m and unblock_m.start() > block_m.start()):
        phrase, before = extract_blocker(clean)
        return Interpretation(
            intent="blocked",
            status=BLOCKED,
            blocker=phrase,
            note=before or None,
            task_id=task_id,
            confidence=0.85,
            source="rules",
            delay_reason=delay_why
        )

    # 2. Blocker resolved
    if unblock_m:
        if COMPLETE_CUES.search(clean) and not PARTIAL_CUES.search(clean) and not PROMISE_COMMITMENT.search(clean):
            return Interpretation(
                intent="complete",
                status=COMPLETE,
                blocker=None,
                note=snippet,
                task_id=task_id,
                confidence=0.75,
                source="rules"
            )
        return Interpretation(
            intent="unblocked",
            status=IN_PROGRESS,
            blocker=None,
            note=snippet,
            task_id=task_id,
            confidence=0.8,
            source="rules"
        )

    # 3. Explicit percentage
    if pct:
        p = int(pct.group(1))
        if p >= 100:
            return Interpretation(
                intent="complete",
                status=COMPLETE,
                blocker=None,
                note=snippet,
                task_id=task_id,
                confidence=0.85,
                source="rules",
                progress_pct=100
            )
        return Interpretation(
            intent="progress",
            status=IN_PROGRESS,
            blocker=None,
            note=f"{p}% done",
            task_id=task_id,
            confidence=0.85,
            source="rules",
            progress_pct=p,
            proposed_deadline=proposed_dl
        )

    # 4. Promises / Future Commitments (Must NOT be marked Complete!)
    # e.g. "I'll do it tomorrow", "kal tak ho jayega"
    if PROMISE_COMMITMENT.search(clean):
        # If it also says "not done yet" or "working on it", it's in progress
        new_st = IN_PROGRESS if PARTIAL_CUES.search(clean) or PROGRESS_CUES.search(clean) else None
        return Interpretation(
            intent="progress",
            status=new_st,
            blocker=None,
            note=snippet,
            task_id=task_id,
            confidence=0.7,
            source="rules",
            proposed_deadline=proposed_dl,
            delay_reason=delay_why
        )

    # 5. Not started / partially done
    if NOT_STARTED.search(clean):
        return Interpretation(
            intent="progress",
            status=PENDING,
            blocker=None,
            note=snippet,
            task_id=task_id,
            confidence=0.75,
            source="rules",
            proposed_deadline=proposed_dl
        )
    if PARTIAL_CUES.search(clean):
        return Interpretation(
            intent="progress",
            status=IN_PROGRESS,
            blocker=None,
            note=snippet,
            task_id=task_id,
            confidence=0.75,
            source="rules",
            proposed_deadline=proposed_dl,
            delay_reason=delay_why
        )

    # 6. Actual Completion
    if COMPLETE_CUES.search(clean):
        return Interpretation(
            intent="complete",
            status=COMPLETE,
            blocker=None,
            note=snippet,
            task_id=task_id,
            confidence=0.8,
            source="rules",
            progress_pct=100
        )

    # 7. Progress cues
    if PROGRESS_CUES.search(clean):
        return Interpretation(
            intent="progress",
            status=IN_PROGRESS,
            blocker=None,
            note=snippet,
            task_id=task_id,
            confidence=0.7,
            source="rules",
            proposed_deadline=proposed_dl,
            delay_reason=delay_why
        )

    # 8. Questions
    if "?" in text or QUESTION_START.match(text):
        return Interpretation(
            intent="question",
            status=None,
            blocker=None,
            note=snippet,
            task_id=task_id,
            confidence=0.65,
            source="rules"
        )

    # 9. Unclear / Ambiguous
    return Interpretation(
        intent="unclear",
        status=None,
        blocker=None,
        note=None,
        task_id=task_id,
        confidence=0.3,
        source="rules",
        clarifying_question=f"Could you please clarify your update: is this task complete, in progress, or are you facing any blockers?"
    )


# ------------------------------------------------------------------ LLM layer
SYSTEM_PROMPT = """You maintain a project tracker from chat replies sent by team members.
The team speaks English, Hindi, and Hinglish with colloquial expressions, slang, and typos.
Given a member's reply and their open tasks, decide what the reply means for ONE task.

Key Classification Rules:
- "complete": the task is fully finished right now. NEVER classify future promises ("kal kar dunga", "will finish by 5pm") as complete!
- "blocked": something external stops progress (waiting for credentials, access, approval, a teammate, a bug they can't fix). If the reply mixes progress with a blocker (e.g. "connected, but waiting for test credentials") the task is BLOCKED, never complete.
- "progress": work is under way or partly done. If they mention a promise/future deadline ("will finish tomorrow"), note the proposed_deadline.
- "unblocked": a previously recorded blocker is now resolved; work continues.
- "question": they asked something instead of reporting.
- "unclear": cannot tell. Then write one short clarifying_question.

Extract any delay_reason, proposed_deadline, progress_pct (0-100), and responsible_entity (person/resource).
Respond with ONLY a JSON object:
{"task_id": int, "intent": "complete|blocked|progress|unblocked|question|unclear",
 "status": "Pending|In Progress|Blocked|Complete|null", "blocker": "short phrase or null",
 "note": "short summary or null", "confidence": 0.0-1.0,
 "clarifying_question": "string or null", "proposed_deadline": "string or null",
 "delay_reason": "string or null", "progress_pct": int or null, "responsible_entity": "string or null"}"""

_VALID_INTENTS = {"complete", "blocked", "progress", "unblocked", "question", "unclear"}
_VALID_STATUS = {PENDING, IN_PROGRESS, BLOCKED, COMPLETE, "Not Started", "Completed", "In Review"}


def _call_anthropic(text, tasks, active_task_id):
    task_lines = [{"task_id": t["id"], "title": t["title"], "status": t["status"],
                   "recorded_blocker": t.get("blocker")} for t in tasks]
    user_msg = (f"Active task_id (the one we just asked about): {active_task_id}\n"
                f"Open tasks: {json.dumps(task_lines)}\n"
                f"Reply from member: {json.dumps(text)}")
    body = json.dumps({"model": config.ANTHROPIC_MODEL, "max_tokens": 450, "system": SYSTEM_PROMPT,
                       "messages": [{"role": "user", "content": user_msg}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
        "content-type": "application/json", "x-api-key": config.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    raw = "".join(b.get("text", "") for b in data.get("content", []))
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("no JSON in LLM output")
    return json.loads(m.group(0))


def _from_llm(obj, tasks, active_task_id):
    intent = obj.get("intent")
    if intent not in _VALID_INTENTS:
        raise ValueError("bad intent")
    status = obj.get("status")
    status = status if status in _VALID_STATUS else None
    ids = [t["id"] for t in tasks]
    tid = obj.get("task_id")
    if tid not in ids:
        tid = active_task_id if active_task_id in ids else (ids[0] if ids else None)
    defaults = {"complete": COMPLETE, "blocked": BLOCKED, "progress": IN_PROGRESS, "unblocked": IN_PROGRESS}
    if intent in defaults and status is None and intent != "progress":
        status = defaults[intent]
    if intent in ("question", "unclear"):
        status = None
    return Interpretation(
        intent=intent,
        status=status,
        blocker=obj.get("blocker") or None,
        note=obj.get("note") or None,
        task_id=tid,
        confidence=float(obj.get("confidence") or 0.8),
        source="llm",
        clarifying_question=obj.get("clarifying_question") or None,
        proposed_deadline=obj.get("proposed_deadline") or None,
        delay_reason=obj.get("delay_reason") or None,
        progress_pct=obj.get("progress_pct"),
        responsible_entity=obj.get("responsible_entity") or None
    )


def interpret(text, tasks, active_task_id=None):
    """Main entry point. Always returns an Interpretation, never raises."""
    rules = rule_interpret(text, tasks, active_task_id)
    if not config.ANTHROPIC_API_KEY or not tasks:
        return rules
    try:
        result = _from_llm(_call_anthropic(text, tasks, active_task_id), tasks, active_task_id)
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError, OSError) as e:
        print(f"[interpreter] LLM fallback ({e}); using rules")
        return rules
    # Safety net: never record "complete" when the text has a blocker cue or future promise
    if result.status == COMPLETE and rules.intent == "blocked":
        result.intent, result.status = "blocked", BLOCKED
        result.blocker = result.blocker or rules.blocker
        result.source = "llm+rules"
    if result.status == COMPLETE and rules.intent == "progress" and rules.proposed_deadline:
        result.intent, result.status = "progress", IN_PROGRESS
        result.source = "llm+rules"
    if result.status == BLOCKED and not result.blocker:
        result.blocker = rules.blocker
    return result
